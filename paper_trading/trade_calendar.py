"""交易日历与交易时段判断。

优先用 akshare 的 ``tool_trade_date_hist_sina`` 拉取交易日历并缓存到本地
（避免每次启动都联网）。拉取失败时退化为"周一至周五"近似（并给出告警），
仅影响实时模式的时段判断，不影响回放。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime
from typing import Optional

logger = logging.getLogger(__name__)


class TradeCalendar:
    def __init__(self, cache_path: str = "data/trade_dates.json"):
        self.cache_path = cache_path
        self._dates: Optional[set] = None
        self._fallback = False

    def _load(self) -> None:
        if self._dates is not None:
            return
        # 1) 本地缓存
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._dates = set(json.load(f))
                return
            except Exception:
                pass
        # 2) 联网拉取
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            dates = set()
            for d in df["trade_date"]:
                dates.add(str(d)[:10])
            self._dates = dates
            os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(sorted(dates), f)
            logger.info(f"交易日历已加载并缓存: {len(dates)} 个交易日")
        except Exception as e:
            logger.warning(f"交易日历拉取失败，退化为周一~周五近似: {e}")
            self._dates = set()
            self._fallback = True

    def is_trading_day(self, date) -> bool:
        self._load()
        ds = str(date)[:10]
        if self._dates:
            return ds in self._dates
        # 退化：周一~周五
        try:
            return datetime.strptime(ds, "%Y-%m-%d").weekday() < 5
        except Exception:
            return True

    def is_in_trading_session(self, now: Optional[datetime] = None) -> bool:
        """是否处于 A 股交易时段（9:30-11:30 / 13:00-15:00）。"""
        now = now or datetime.now()
        if not self.is_trading_day(now.date()):
            return False
        t = now.time()
        return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))

    def refresh(self) -> None:
        """强制重新拉取（每年初调用一次即可）。"""
        self._dates = None
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)
        self._load()
