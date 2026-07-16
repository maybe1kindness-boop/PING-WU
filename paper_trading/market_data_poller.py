"""实时行情轮询（高频层）。

优先用 ``ak.stock_zh_a_spot_em()`` 单次请求拿全市场 OHLC+昨收+涨跌幅，
缓存后按需切片；失败时降级到 ``AKShareFetcher.get_stock_prices_batch``。
提供 ``build_intraday_df`` 把当日实时价拼为最后一根未完成K，供择时使用。

输出 df 保持 KHunter 惯例的**倒序**（最新在前）；df 顺序转换由 SignalEngine 负责。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

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

    # ---------------- 全市场行情 ----------------
    def fetch_all(self, force: bool = False) -> Dict[str, Quote]:
        age = time.time() - self._cache_ts
        if not force and self._cache and age < self.cfg.spot_min_interval_sec:
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
                    is_st=name.startswith(("ST", "*ST")),
                )
            except Exception:
                continue
        return out

    def _fetch_batch_fallback(self) -> Dict[str, Quote]:
        try:
            from utils.akshare_fetcher import AKShareFetcher
            fetcher = AKShareFetcher(self.cfg.db_path.rsplit("/", 1)[0] or "data")
            prices = fetcher.get_stock_prices_batch(list(self._cache.keys())[:200]
                                                    if self._cache else [])
            return {c: Quote(code=c, price=p) for c, p in prices.items() if p}
        except Exception as e:
            logger.error(f"batch 降级也失败: {e}")
            return {}

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
        return df
