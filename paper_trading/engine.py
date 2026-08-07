"""PaperTradingEngine —— 聚合所有组件的 Facade。

职责：
  - 初始化并持有 account / broker / signal_engine / dao / calendar
  - 提供数据查询接口（账户概览/持仓/交易/净值/信号/候选池）供 GUI 调用
  - 提供回放驱动（无实时行情时用历史数据演示完整买卖流程）
  - 预留实时模式入口（start_realtime，由 scheduler/poller 接入）

实时模块（candidate_pool/market_data_poller/scheduler）后续接入；本类已为其预留接口。
"""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timedelta

import pandas as pd
import yaml

from .account import PaperAccount
from .broker import PaperBroker, Quote
from .config_schema import PaperConfig, load_paper_config
from .dao import PaperTradingDAO
from .df_orient import to_ascending
from .signal_engine import SignalEngine
from .trade_calendar import TradeCalendar

logger = logging.getLogger(__name__)


class PaperTradingEngine:
    def __init__(self, config_file: str = "config/config.yaml",
                 db_path: Optional[str] = None, timing_name: Optional[str] = None):
        # ---- 加载配置 ----
        self.config_file = Path(config_file)
        self.khunter_config: Dict[str, Any] = {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                self.khunter_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning(f"配置文件不存在: {config_file}，使用默认值")
        self.config: PaperConfig = load_paper_config(self.khunter_config)
        if db_path:
            self.config.db_path = db_path

        # ---- 持久化 + 账户 ----
        self.dao = PaperTradingDAO(self.config.db_path)
        self.account = PaperAccount(self.config.initial_capital, self.dao)
        self.account.load()

        # ---- 撮合 + 择时 + 信号引擎 ----
        self.broker = PaperBroker(self.config.fee, self.dao)
        self.timing = self._create_timing(timing_name)
        self.signal_engine = SignalEngine(
            self.timing, self.broker, self.account, self.dao, self.config)

        # ---- 辅助 ----
        self.calendar = TradeCalendar()
        self._db_manager = None
        from .market_data_poller import MarketDataPoller
        self._screen_poller = MarketDataPoller(self)
        self._screen_poller.start_background_refresh(interval_sec=20.0)
        self._peak_value = max(self.account.total_value(), self.account.initial_capital)
        self._realtime_started = False

    # ---------------- 内部 ----------------
    def update_strategy_config(
        self,
        timing_strategy: str,
        selection_strategies: List[str],
        max_position_pct: float,
        max_total_position_pct: float,
        max_hold_days: int,
        take_profit: float,
        stop_loss: float,
    ) -> None:
        """Apply desktop-edited strategy settings and persist them to YAML."""
        paper = self.khunter_config.setdefault("paper_trading", {})
        paper.update({
            "timing_strategy": timing_strategy,
            "selection_strategies": list(selection_strategies),
            "max_position_pct": float(max_position_pct),
            "max_total_position_pct": float(max_total_position_pct),
            "max_hold_days": int(max_hold_days),
            "take_profit": float(take_profit),
            "stop_loss": float(stop_loss),
        })
        self.config = load_paper_config(self.khunter_config)
        self.timing = self._create_timing(timing_strategy)
        self.signal_engine.timing = self.timing
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                self.khunter_config,
                handle,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )

    def screen_with_user_code(
        self, source: str, max_stocks: Optional[int] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Run a user-provided local matcher against the cached A-share data.

        The source must define ``match_stock(df, code, name)`` and return a
        boolean or a dict containing ``matched`` (or ``match``) and an optional
        ``reason``. This is deliberately a screening-only API; it does not
        create orders or mutate the paper account.
        """
        if not isinstance(source, str) or not source.strip():
            raise ValueError("筛选代码不能为空")
        if len(source) > 100_000:
            raise ValueError("筛选代码超过 100KB 限制")

        natural_rules = self._parse_natural_rules(source)
        if natural_rules is not None:
            return self._screen_natural_rules(
                natural_rules, max_stocks, progress_callback, force_refresh
            )

        namespace = {
            "pd": pd,
            "datetime": datetime,
            "__name__": "khunter_user_screen",
        }
        try:
            exec(compile(source, "<desktop-screen-code>", "exec"), namespace, namespace)
        except Exception as exc:
            raise ValueError(f"代码编译或加载失败: {exc}") from exc

        matcher = namespace.get("match_stock") or namespace.get("select_stock")
        if not callable(matcher):
            raise ValueError("代码必须定义 match_stock(df, code, name) 函数")

        codes = self.db_manager.list_all_stocks()
        if max_stocks and max_stocks > 0:
            codes = codes[:max_stocks]
        names = (
            self.db_manager.get_all_stock_names()
            if hasattr(self.db_manager, "get_all_stock_names") else {}
        )
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for code in codes:
            name = names.get(code, "")
            try:
                df = self.db_manager.read_stock(code)
                if df is None or df.empty:
                    continue
                raw = matcher(df.copy(), code, name)
                matched = False
                reason = "命中自定义条件"
                extra: Dict[str, Any] = {}
                if isinstance(raw, dict):
                    matched = bool(raw.get("matched", raw.get("match", raw.get("selected", False))))
                    reason = str(raw.get("reason", reason))
                    extra = {k: v for k, v in raw.items() if k not in {"matched", "match", "selected", "reason"}}
                else:
                    matched = bool(raw)
                if matched:
                    latest_price = float(df.iloc[0].get("close", 0) or 0)
                    row = {"code": code, "name": name, "price": latest_price, "reason": reason}
                    row.update(extra)
                    results.append(row)
            except Exception as exc:
                errors.append({"code": str(code), "error": str(exc)})

        return {
            "results": results,
            "processed": len(codes),
            "errors": errors,
        }

    def _parse_natural_rules(self, source: str) -> Optional[Dict[str, Any]]:
        """Parse the common Chinese rule syntax used by the desktop editor."""
        if "def " in source or "import " in source or "return " in source:
            return None
        text = source.strip().replace("，", ",").replace("；", ",").replace("、", ",")
        parts = [part.strip() for part in re.split(r"[,\n]+", text) if part.strip()]
        if not parts:
            return None
        spec: Dict[str, Any] = {
            "exclude_st": False,
            "exclude_bj": False,
            "exclude_star": False,
            "price_max": None,
            "amount_min": None,
            "market_cap_max": None,
            "limit_days": None,
            "limit_count": None,
        }
        unsupported = []
        for part in parts:
            compact = part.replace(" ", "")
            if compact.startswith("非ST") or "排除ST" in compact:
                spec["exclude_st"] = True
                continue
            if compact.startswith("非北交") or "排除北交" in compact:
                spec["exclude_bj"] = True
                continue
            if compact.startswith("非科创") or "排除科创" in compact:
                spec["exclude_star"] = True
                continue
            match = re.search(r"股价(?:<|小于|低于)([0-9]+(?:\.[0-9]+)?)", compact)
            if match:
                spec["price_max"] = float(match.group(1))
                continue
            match = re.search(r"市值(?:<|小于|低于)([0-9]+(?:\.[0-9]+)?)(万亿|亿|万)?", compact)
            if match:
                spec["market_cap_max"] = self._parse_amount_value(match.group(1), match.group(2))
                continue
            match = re.search(r"(?:今日|当天)?成交额(?:>|大于|超过)([0-9]+(?:\.[0-9]+)?)(万亿|亿|万)?", compact)
            if match:
                spec["amount_min"] = self._parse_amount_value(match.group(1), match.group(2))
                continue
            match = re.search(r"近([0-9]+)(月|个月|个交易日|日)(?:至少)?([0-9]+)次涨停", compact)
            if match:
                period = int(match.group(1))
                unit = match.group(2)
                spec["limit_days"] = period * 22 if "月" in unit else period
                spec["limit_count"] = int(match.group(3))
                continue
            unsupported.append(part)
        if unsupported:
            raise ValueError("无法识别的中文条件：" + "、".join(unsupported))
        return spec

    @staticmethod
    def _parse_amount_value(number: str, unit: Optional[str]) -> float:
        multiplier = {"万亿": 1e12, "亿": 1e8, "万": 1e4}.get(unit or "", 1.0)
        return float(number) * multiplier

    def _screen_natural_rules(
        self, spec: Dict[str, Any], max_stocks: Optional[int],
        progress_callback: Optional[Callable[[str], None]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        def report(message: str) -> None:
            if progress_callback:
                try:
                    progress_callback(message)
                except Exception:
                    logger.debug("筛选进度回调失败", exc_info=True)

        codes = self.db_manager.list_all_stocks()
        if max_stocks and max_stocks > 0:
            codes = codes[:max_stocks]
        names = (
            self.db_manager.get_all_stock_names()
            if hasattr(self.db_manager, "get_all_stock_names") else {}
        )
        # 盘中口径：价格、成交额和总市值必须来自同一次实时快照。
        poller = self._get_screen_poller()
        report("正在获取实时行情...")
        quotes = poller.fetch_all(
            force=force_refresh,
            max_age=float(getattr(self.config, "screen_quote_cache_sec", 30.0)),
            fast=True,
        )
        report(f"实时行情完成（{len(quotes)} 只），正在计算筛选条件...")

        # 涨停次数依赖历史K线。先按实时条件缩小范围，再批量刷新近期K线，
        # 避免把实时快照和过期本地历史数据混在一起。
        refresh_codes = codes
        if spec["limit_count"] is not None:
            refresh_codes = []
            for code in codes:
                name = names.get(code, "")
                if spec["exclude_st"] and (name.startswith(("ST", "*ST")) or "ST" in name.upper()):
                    continue
                if spec["exclude_bj"] and str(code).startswith(("4", "8")):
                    continue
                if spec["exclude_star"] and str(code).startswith(("688", "689")):
                    continue
                quote = quotes.get(code)
                if quote is None or quote.price <= 0:
                    continue
                if spec["price_max"] is not None and quote.price >= spec["price_max"]:
                    continue
                if spec["amount_min"] is not None and quote.amount <= spec["amount_min"]:
                    continue
                if spec["market_cap_max"] is not None and (
                    quote.market_cap <= 0 or quote.market_cap >= spec["market_cap_max"]
                ):
                    continue
                refresh_codes.append(code)
            report(f"需要校验历史涨停的股票 {len(refresh_codes)} 只，正在检查本地 K 线...")
            self._refresh_intraday_screen_history(
                refresh_codes, days=max(40, int(spec["limit_days"] or 22) + 10)
            )
            report("历史 K 线准备完成，正在计算筛选结果...")

        results = []
        errors = []
        needs_history = spec["limit_count"] is not None
        for code in codes:
            name = names.get(code, "")
            try:
                if spec["exclude_st"] and (name.startswith(("ST", "*ST")) or "ST" in name.upper()):
                    continue
                if spec["exclude_bj"] and str(code).startswith(("4", "8")):
                    continue
                if spec["exclude_star"] and str(code).startswith(("688", "689")):
                    continue
                quote = quotes.get(code)
                if quote is None or quote.price <= 0:
                    continue
                price = float(quote.price)
                checks = []
                if spec["price_max"] is not None:
                    checks.append(price < spec["price_max"])

                if spec["market_cap_max"] is not None:
                    # 同花顺盘中筛选按当前总市值判断，不回退到旧的本地市值。
                    market_cap = float(quote.market_cap or 0)
                    checks.append(market_cap > 0 and market_cap < spec["market_cap_max"])
                else:
                    market_cap = 0.0

                if spec["amount_min"] is not None:
                    # 同花顺盘中筛选按当前交易日成交额判断。
                    amount = float(quote.amount or 0)
                    checks.append(amount > spec["amount_min"])
                else:
                    amount = 0.0

                limit_count = 0
                if needs_history:
                    df = self.db_manager.read_stock(code)
                    if df is None or df.empty:
                        continue
                    latest_date = pd.Timestamp(df.iloc[0]["date"]).date()
                    if latest_date < datetime.now().date() - timedelta(days=7):
                        errors.append({
                            "code": str(code),
                            "error": f"历史K线未更新至近期交易日（最新 {latest_date}）",
                        })
                        continue
                    intraday_df = poller.build_intraday_df(
                        code, quote, tail_bars=int(spec["limit_days"] or 22) + 2
                    )
                    if intraday_df is not None and not intraday_df.empty:
                        df = intraday_df
                    closes = pd.to_numeric(df["close"], errors="coerce")
                    limit_flags = []
                    for idx in range(min(int(spec["limit_days"]), len(closes) - 1)):
                        current = closes.iloc[idx]
                        previous = closes.iloc[idx + 1]
                        limit_flags.append(
                            self._is_limit_up(code, name, current, previous)
                        )
                    limit_count = sum(limit_flags)
                    checks.append(limit_count >= spec["limit_count"])
                # Exclusion-only rules still select every stock that survived
                # the exclusions. Numeric/history rules append their checks.
                if not checks or all(checks):
                    results.append({
                        "code": code,
                        "name": name,
                        "price": round(price, 2),
                        "reason": f"命中中文规则；成交额={amount / 1e8:.2f}亿，市值={market_cap / 1e8:.2f}亿，近段涨停={limit_count}次",
                    })
            except Exception as exc:
                errors.append({"code": str(code), "error": str(exc)})
        return {"results": results, "processed": len(codes), "errors": errors}

    def _get_screen_poller(self):
        """Reuse one poller so repeated desktop screens can share its cache."""
        if self._screen_poller is None:
            from .market_data_poller import MarketDataPoller
            self._screen_poller = MarketDataPoller(self)
        return self._screen_poller

    def _refresh_intraday_screen_history(self, codes: List[str], days: int) -> None:
        """Refresh recent K-lines in batches before an intraday screen."""
        if not codes:
            return
        try:
            from utils.akshare_fetcher import AKShareFetcher

            latest_dates = self._get_latest_kline_dates(codes)
            cutoff = datetime.now().date() - timedelta(days=7)
            stale_codes = []
            for code in codes:
                latest = latest_dates.get(str(code))
                try:
                    latest_date = pd.Timestamp(latest).date() if latest else None
                except (TypeError, ValueError):
                    latest_date = None
                if latest_date is None or latest_date < cutoff:
                    stale_codes.append(code)
            if not stale_codes:
                logger.info("盘中筛选K线已足够新，跳过网络刷新: %s 只", len(codes))
                return

            fetcher = AKShareFetcher()
            batch_size = 100
            refreshed = 0
            for start in range(0, len(stale_codes), batch_size):
                batch = stale_codes[start:start + batch_size]
                data, _ = fetcher.kline_fetcher._fetch_kline_tickflow_batch(
                    batch, days=days
                )
                if data:
                    fetcher.kline_fetcher._batch_update_kline_to_db(data)
                refreshed += len(data)
            logger.info(
                "盘中筛选K线刷新完成: 检查=%s, 请求=%s, 成功=%s, 天数=%s",
                len(codes), len(stale_codes), refreshed, days,
            )
        except Exception as exc:
            logger.warning("盘中筛选K线刷新失败: %s", exc)

    def _get_latest_kline_dates(self, codes: List[str]) -> Dict[str, Any]:
        """Read latest dates in one query instead of opening one query per stock."""
        if not codes:
            return {}
        latest: Dict[str, Any] = {}
        for start in range(0, len(codes), 900):
            batch = [str(code) for code in codes[start:start + 900]]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db_manager.query(
                "SELECT code, MAX(date) AS latest_date "
                f"FROM stock_kline WHERE code IN ({placeholders}) GROUP BY code",
                tuple(batch),
            )
            latest.update({str(row["code"]): row.get("latest_date") for row in rows})
        return latest

    @staticmethod
    def _is_limit_up(code: str, name: str, current: Any, previous: Any) -> bool:
        """Use board-specific rounded limit prices for intraday limit-up counts."""
        try:
            current_price = float(current)
            previous_price = float(previous)
        except (TypeError, ValueError):
            return False
        if current_price <= 0 or previous_price <= 0:
            return False
        upper = str(name or "").upper()
        if upper.startswith(("ST", "*ST")) or "ST" in upper:
            rate = 0.05
        elif str(code).startswith(("300", "301", "688", "689")):
            rate = 0.20
        elif str(code).startswith(("4", "8")):
            rate = 0.30
        else:
            rate = 0.10
        limit_price = round(previous_price * (1 + rate) + 1e-8, 2)
        return current_price >= limit_price - 0.001

    def save_custom_screen_results(self, results: List[Dict[str, Any]]) -> str:
        """Persist the latest desktop code-screen results as a CSV file."""
        output_dir = self.config_file.parent.parent / "runtime" / "data" / "running"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"custom_screen_{datetime.now():%Y%m%d_%H%M%S}.csv"
        fields = ["code", "name", "price", "reason"]
        with open(output, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        return str(output)

    def list_watchlist_groups(self) -> List[Dict[str, Any]]:
        return self.dao.list_watchlist_groups()

    def add_to_watchlist(self, group_name: str,
                         items: List[Dict[str, Any]]) -> int:
        return self.dao.add_watchlist_items(group_name, items)

    def get_watchlist_items(self, group_name: str) -> List[Dict[str, Any]]:
        return self.dao.get_watchlist_items(group_name)

    def _user_strategy_store(self) -> Path:
        path = self.config_file.parent.parent / "runtime" / "data" / "user_screen_strategies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def list_user_screen_strategies(self) -> List[Dict[str, Any]]:
        path = self._user_strategy_store()
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, list):
                return []
            return [item for item in payload if isinstance(item, dict) and item.get("name")]
        except (OSError, ValueError, TypeError):
            return []

    def save_user_screen_strategy(
        self, name: str, source: str, max_stocks: Optional[int]
    ) -> Dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("策略名称不能为空")
        if len(name) > 80:
            raise ValueError("策略名称不能超过80个字符")
        if not str(source or "").strip():
            raise ValueError("策略内容不能为空")
        strategies = self.list_user_screen_strategies()
        record = {
            "name": name,
            "source": str(source),
            "max_stocks": int(max_stocks or 0),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        strategies = [item for item in strategies if item.get("name") != name]
        strategies.append(record)
        path = self._user_strategy_store()
        temp = path.with_suffix(path.suffix + ".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(strategies, handle, ensure_ascii=False, indent=2)
        temp.replace(path)
        return record

    def load_user_screen_strategy(self, name: str) -> Dict[str, Any]:
        for item in self.list_user_screen_strategies():
            if item.get("name") == name:
                return item
        raise KeyError(f"找不到保存的策略: {name}")

    def delete_user_screen_strategy(self, name: str) -> None:
        strategies = [
            item for item in self.list_user_screen_strategies()
            if item.get("name") != name
        ]
        path = self._user_strategy_store()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(strategies, handle, ensure_ascii=False, indent=2)

    @property
    def db_manager(self):
        if self._db_manager is None:
            from utils.global_db import get_global_db
            self._db_manager = get_global_db()
        return self._db_manager

    def _create_timing(self, name: Optional[str]):
        from trading.timing_strategies import TimingStrategyFactory
        name = name or self.config.timing_strategy
        try:
            return TimingStrategyFactory.create_strategy(name, self.khunter_config)
        except ValueError as e:
            logger.warning(f"择时 {name} 创建失败 ({e})，降级到 {self.config.timing_fallback}")
            return TimingStrategyFactory.create_strategy(
                self.config.timing_fallback, self.khunter_config)

    # ---------------- 数据查询（供 GUI）----------------
    def account_overview(self) -> Dict[str, Any]:
        acct = self.account
        total = acct.total_value()
        self._peak_value = max(self._peak_value, total)
        peak = self._peak_value
        return {
            "cash": round(acct.cash, 2),
            "positions_value": round(acct.positions_value(), 2),
            "total_value": round(total, 2),
            "initial_capital": acct.initial_capital,
            "pnl": round(total - acct.initial_capital, 2),
            "pnl_pct": round((total / acct.initial_capital - 1) * 100, 2)
                       if acct.initial_capital else 0.0,
            "drawdown_pct": round((peak - total) / peak * 100, 2) if peak > 0 else 0.0,
            "position_count": len([p for p in acct.positions.values() if p.quantity > 0]),
            "timing_strategy": type(self.timing).__name__,
            "realtime": self._realtime_started,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.account.positions.values():
            if p.quantity <= 0:
                continue
            out.append({
                "code": p.code, "name": p.name, "quantity": p.quantity,
                "available": p.available, "avg_cost": round(p.avg_cost, 3),
                "latest_price": round(p.latest_price, 3) if p.latest_price else 0.0,
                "market_value": round(p.market_value(), 2),
                "float_pnl_pct": round(p.float_pnl_pct() * 100, 2),
            })
        return out

    def get_trades(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.dao.get_trades(date)

    def get_nav_series(self, days: int = 90) -> List[Dict[str, Any]]:
        return self.dao.get_nav_series(days)

    def get_signals(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.dao.get_signals(date)

    def get_candidates(self, date: str) -> List[Dict[str, Any]]:
        return self.dao.get_candidates(date)

    # ---------------- 回放驱动 ----------------
    def replay_history(self, code: Optional[str] = None, reset: bool = True,
                       verbose: bool = False) -> Dict[str, Any]:
        """对单只（或第一只）股票做历史回放，每日驱动 engine 并落净值。"""
        if reset:
            self.reset()
        codes = self.db_manager.list_all_stocks()
        if not codes:
            raise RuntimeError("行情库无数据，请先 python main.py init --max-stocks 50")
        code = code or codes[0]
        df = self.db_manager.read_stock(code)
        if df is None or len(df) < 80:
            raise RuntimeError(f"{code} 数据不足")
        names = (self.db_manager.get_all_stock_names()
                 if hasattr(self.db_manager, "get_all_stock_names") else {})
        name = names.get(code, "")
        asc = to_ascending(df)
        start, n = 70, len(asc)
        summary: Dict[str, int] = {}
        prev_total = None
        for i in range(start, n):
            sub_asc = asc.iloc[:i]
            today, prev = sub_asc.iloc[-1], sub_asc.iloc[-2]
            sub_desc = sub_asc.iloc[::-1].reset_index(drop=True)
            td = str(today["date"])[:10]
            q = Quote(code=code, name=name, price=float(today["close"]),
                      prev_close=float(prev["close"]), open=float(today["open"]),
                      high=float(today["high"]), low=float(today["low"]))
            self.account.mark_available_for_new_day()
            self.account.update_price(code, float(today["close"]))
            sig = self.signal_engine.evaluate(
                code, name, sub_desc, q, td,
                "position" if self.account.get_position(code) else "candidate")
            summary[sig.side] = summary.get(sig.side, 0) + 1
            if sig.side != "hold":
                self.signal_engine.act(sig, q, td)
            self.settle(td, prev_total)
            prev_total = self.account.total_value()
        if verbose:
            print(f"回放 {code} {name}: {summary}, 最终净值 {self.account.total_value():.2f}")
        return {"code": code, "name": name, "summary": summary,
                "final_value": round(self.account.total_value(), 2),
                "days": n - start}

    # ---------------- 结算 ----------------
    def settle(self, trade_date: str, prev_total: Optional[float] = None) -> Dict[str, Any]:
        snap = self.account.snapshot(trade_date, prev_total)
        total = snap["total_value"]
        self._peak_value = max(self._peak_value, total)
        peak = self._peak_value
        snap["drawdown"] = round((peak - total) / peak * 100, 2) if peak > 0 else 0.0
        self.dao.upsert_nav(snap)
        return snap

    def mark_new_day(self) -> None:
        self.account.mark_available_for_new_day()

    # ---------------- 控制 ----------------
    def reset(self) -> None:
        """清空模拟盘所有数据，账户回到初始资金。"""
        with self.dao._conn() as c:
            for t in ("pt_position", "pt_position_lot", "pt_trade",
                      "pt_nav_snapshot", "pt_candidate_pool", "pt_signal_log"):
                c.execute(f"DELETE FROM {t}")
        self.account.cash = self.account.initial_capital
        self.account.positions.clear()
        self.account.persist_meta()
        self._peak_value = self.account.initial_capital

    def start_realtime(self, max_stocks: Optional[int] = None) -> str:
        """启动盘中实时模式（后台线程跑 scheduler）。返回提示信息。"""
        if self._realtime_started:
            return "实时模式已在运行"
        import threading
        from .scheduler import IntradayScheduler
        self.scheduler = IntradayScheduler(self)
        self._rt_thread = threading.Thread(
            target=self.scheduler.run, args=(max_stocks,), daemon=True)
        self._rt_thread.start()
        self._realtime_started = True
        msg = (f"盘中实时模式已启动：每 {self.config.poll_interval_minutes} 分钟轮询，"
               f"09:15 建候选池 / 15:05 收盘结算")
        logger.info(msg)
        return msg

    def stop(self) -> None:
        if hasattr(self, "scheduler"):
            self.scheduler.stop()
        self._realtime_started = False
