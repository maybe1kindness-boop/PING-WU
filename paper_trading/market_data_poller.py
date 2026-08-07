"""实时行情轮询（高频层）。

优先用 ``ak.stock_zh_a_spot_em()`` 单次请求拿全市场 OHLC+昨收+涨跌幅，
缓存后按需切片；失败时降级到 ``AKShareFetcher.get_stock_prices_batch``。
提供 ``build_intraday_df`` 把当日实时价拼为最后一根未完成K，供择时使用。

输出 df 保持 KHunter 惯例的**倒序**（最新在前）；df 顺序转换由 SignalEngine 负责。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Event, Lock, Thread
from typing import Dict, List, Optional

import pandas as pd
import requests

from .broker import Quote

logger = logging.getLogger(__name__)


class MarketDataPoller:
    def __init__(self, engine):
        self.engine = engine
        self.cfg = engine.config
        self._cache: Dict[str, Quote] = {}
        self._cache_ts: float = 0.0
        self._fail_count = 0
        self._degraded = False
        self._refresh_lock = Lock()
        self._refresh_done = Event()
        self._refresh_done.set()
        self._refreshing = False
        self._background_stop = Event()
        self._background_thread: Optional[Thread] = None

    def prefetch_async(self, fast: bool = True) -> None:
        """Warm a quote snapshot without blocking the desktop UI."""
        self._refresh_async(fast=fast)

    def start_background_refresh(self, interval_sec: float = 20.0, fast: bool = True) -> None:
        """Keep the local quote snapshot warm for instant local screening."""
        with self._refresh_lock:
            if self._background_thread and self._background_thread.is_alive():
                return

        self._background_stop.clear()

        def run() -> None:
            while not self._background_stop.is_set():
                age = time.time() - self._cache_ts if self._cache_ts else None
                if not self._cache or age is None or age >= interval_sec:
                    self._refresh_async(fast=fast)
                self._background_stop.wait(1.0)

        thread = Thread(target=run, name="screen-quote-refresh-loop", daemon=True)
        with self._refresh_lock:
            self._background_thread = thread
        thread.start()

    def _refresh_async(self, fast: bool = True) -> None:
        with self._refresh_lock:
            if self._refreshing:
                return
            self._refreshing = True
            self._refresh_done.clear()

        def run() -> None:
            try:
                self._fetch_uncached(fast=fast)
            finally:
                with self._refresh_lock:
                    self._refreshing = False
                    self._refresh_done.set()

        Thread(target=run, name="screen-quote-refresh", daemon=True).start()

    # ---------------- 全市场行情 ----------------
    def fetch_all(
        self,
        force: bool = False,
        max_age: Optional[float] = None,
        fast: bool = False,
    ) -> Dict[str, Quote]:
        age = time.time() - self._cache_ts
        cache_ttl = self.cfg.spot_min_interval_sec if max_age is None else max_age
        if not force and self._cache and age < cache_ttl:
            return self._cache
        if not force and self._cache:
            # Stale-while-revalidate: local screening stays instant while the
            # background thread replaces this snapshot.
            self._refresh_async(fast=fast)
            return self._cache
        if self._refreshing:
            self._refresh_done.wait(timeout=30)
            if self._cache:
                return self._cache
        self._refresh_async(fast=fast)
        self._refresh_done.wait(timeout=30)
        return self._cache

    def _fetch_uncached(self, fast: bool = False) -> Dict[str, Quote]:
        if fast:
            self._cache = self._fetch_batch_fallback()
            self._cache_ts = time.time()
            self._degraded = True
            return self._cache
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            self._cache = self._parse_spot(df)
            self._cache_ts = time.time()
            self._fail_count = 0
            self._degraded = False
            if len(self._cache) < 1000:
                raise RuntimeError(f"spot_em 返回异常，仅 {len(self._cache)} 行")
        except Exception as e:
            self._fail_count += 1
            logger.warning(f"spot_em 失败({self._fail_count}): {e}; 降级 batch 接口")
            self._cache = self._fetch_batch_fallback()
            self._cache_ts = time.time()
            self._degraded = self._fail_count >= 3
        return self._cache

    def _parse_spot(self, df: pd.DataFrame) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        # spot_em 列名：代码/名称/最新价/涨跌幅/涨跌额/买入/卖出/昨收/开盘/最高/最低/成交量/成交额/换手率...
        col_map = {
            "code": "代码", "name": "名称", "price": "最新价",
            "pct": "涨跌幅", "open": "今开", "high": "最高",
            "low": "最低", "prev_close": "昨收", "volume": "成交量",
        }
        def pick(*cands):
            for c in cands:
                if c in df.columns:
                    return c
            return None
        c_code = pick("代码", "code")
        c_name = pick("名称", "name")
        c_price = pick("最新价", "price")
        for _, row in df.iterrows():
            try:
                code = str(row[c_code]).zfill(6)
                name = str(row.get(c_name, "")) if c_name else ""
                price = row.get(c_price, 0)
                if price is None or float(price) <= 0:
                    continue
                def num(*cands):
                    c = pick(*cands)
                    try:
                        return float(row[c]) if c and row[c] not in (None, "") else 0.0
                    except Exception:
                        return 0.0
                out[code] = Quote(
                    code=code, name=name, price=float(price),
                    open=num("今开", "open"), high=num("最高", "high"),
                    low=num("最低", "low"), prev_close=num("昨收", "prev_close"),
                    volume=num("成交量", "volume"), pct=num("涨跌幅", "pct"),
                    amount=num("成交额", "amount"),
                    market_cap=num("总市值", "market_cap"),
                    is_st=name.startswith(("ST", "*ST")),
                )
            except Exception:
                continue
        return out

    def _fetch_batch_fallback(self) -> Dict[str, Quote]:
        """Use Tencent's batch quote endpoint when Eastmoney is unavailable.

        The old fallback only asked for ``self._cache.keys()``. On the first
        failed refresh that cache is empty, so it silently returned no quotes
        and every amount/market-cap rule evaluated to false.
        """
        try:
            codes = list(self.engine.db_manager.list_all_stocks())
        except Exception:
            codes = []
        if not codes:
            codes = list(self._cache.keys())
        if not codes:
            return {}
        return self._fetch_tencent_quotes(codes)

    def _fetch_tencent_quotes(self, codes: List[str]) -> Dict[str, Quote]:
        """Fetch full-market quotes from Tencent as a data-source fallback."""
        batch_size = 200
        headers = {"User-Agent": "Mozilla/5.0"}

        def number(parts, index):
            try:
                value = parts[index] if index < len(parts) else ""
                return float(value.replace(",", "")) if value else 0.0
            except (AttributeError, TypeError, ValueError):
                return 0.0

        def fetch_batch(batch_number: int, batch: List[str]) -> Dict[str, Quote]:
            quotes: Dict[str, Quote] = {}
            batch = [str(code).zfill(6) for code in batch]
            query = ",".join(
                ("sh" if code.startswith(("6", "8", "9")) else "sz") + code
                for code in batch
            )
            try:
                response = requests.get(
                    f"https://qt.gtimg.cn/q={query}",
                    timeout=8,
                    headers=headers,
                )
                response.encoding = "gbk"
                if response.status_code != 200:
                    return quotes
                for line in response.text.split(";"):
                    if "=" not in line or "~" not in line:
                        continue
                    key, raw = line.split("=", 1)
                    code = key.split("v_", 1)[-1][2:]
                    parts = raw.strip().strip('"').split("~")
                    if len(parts) < 46 or not code:
                        continue
                    price = number(parts, 3)
                    if price <= 0:
                        continue
                    # Tencent reports amount in 万元 and market cap in 亿元.
                    quotes[code] = Quote(
                        code=code,
                        name=parts[1] if len(parts) > 1 else "",
                        price=price,
                        open=number(parts, 5),
                        high=number(parts, 33),
                        low=number(parts, 34),
                        prev_close=number(parts, 4),
                        volume=number(parts, 6),
                        pct=number(parts, 32),
                        amount=number(parts, 37) * 1e4,
                        market_cap=number(parts, 44) * 1e8,
                        is_st=(parts[1].startswith(("ST", "*ST")) if len(parts) > 1 else False),
                    )
            except requests.RequestException as exc:
                logger.debug("腾讯行情降级批次失败(%s): %s", batch_number, exc)
            except Exception as exc:
                logger.debug("腾讯行情降级解析失败(%s): %s", batch_number, exc)
            return quotes

        batches = [
            codes[start:start + batch_size]
            for start in range(0, len(codes), batch_size)
        ]
        quotes: Dict[str, Quote] = {}
        worker_count = min(16, max(1, len(batches)))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(fetch_batch, index, batch): index
                for index, batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                quotes.update(future.result())
        logger.info("腾讯行情降级完成: 成功 %s/%s 只", len(quotes), len(codes))
        return quotes

    # ---------------- 切片 ----------------
    def fetch_quotes(self, codes: List[str]) -> Dict[str, Quote]:
        if not self._cache or time.time() - self._cache_ts > self.cfg.spot_min_interval_sec:
            self.fetch_all()
        return {c: self._cache[c] for c in codes if c in self._cache}

    def health(self) -> Dict[str, object]:
        return {
            "cached": len(self._cache),
            "age_sec": round(time.time() - self._cache_ts, 1) if self._cache_ts else None,
            "fail_count": self._fail_count,
            "degraded": self._degraded,
            "refreshing": self._refreshing,
        }

    # ---------------- 拼未完成K ----------------
    def build_intraday_df(self, code: str, quote: Quote,
                          tail_bars: int = 120) -> Optional[pd.DataFrame]:
        """历史倒序df + 当日实时价拼为未完成K（若当日已在历史中则不拼）。"""
        df = self.engine.db_manager.read_stock(code)
        if df is None or len(df) < 60:
            return None
        df = df.iloc[:tail_bars].copy()  # 倒序，截尾
        today = datetime.now().strftime("%Y-%m-%d")
        latest = str(df.iloc[0]["date"])[:10]
        if latest < today and quote.price > 0:
            # 拼一根当日未完成K到最前（倒序）
            new_row = pd.DataFrame([{
                "date": pd.Timestamp(today),
                "open": quote.open or quote.price,
                "high": quote.high or quote.price,
                "low": quote.low or quote.price,
                "close": quote.price,
                "volume": quote.volume,
            }])
            for c in df.columns:
                if c not in new_row.columns:
                    new_row[c] = None
            df = pd.concat([new_row[df.columns], df], ignore_index=True)
        elif latest == today and quote.price > 0:
            # 数据库已有当天K线时，用本次实时快照覆盖，避免使用盘初旧值。
            first = df.index[0]
            if "open" in df.columns and quote.open > 0:
                df.at[first, "open"] = quote.open
            if "high" in df.columns and quote.high > 0:
                df.at[first, "high"] = quote.high
            if "low" in df.columns and quote.low > 0:
                df.at[first, "low"] = quote.low
            if "close" in df.columns:
                df.at[first, "close"] = quote.price
            if "volume" in df.columns and quote.volume > 0:
                df.at[first, "volume"] = quote.volume
        return df
