"""PaperTradingEngine —— 聚合所有组件的 Facade。

职责：
  - 初始化并持有 account / broker / signal_engine / dao / calendar
  - 提供数据查询接口（账户概览/持仓/交易/净值/信号/候选池）供 GUI 调用
  - 提供回放驱动（无实时行情时用历史数据演示完整买卖流程）
  - 预留实时模式入口（start_realtime，由 scheduler/poller 接入）

实时模块（candidate_pool/market_data_poller/scheduler）后续接入；本类已为其预留接口。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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
        self.khunter_config: Dict[str, Any] = {}
        try:
            with open(config_file, "r", encoding="utf-8") as f:
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
        self._peak_value = max(self.account.total_value(), self.account.initial_capital)
        self._realtime_started = False

    # ---------------- 内部 ----------------
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
