"""盘中实时调度器（高频层主循环）。

节奏：
  - 09:15  pre_open：解锁 T+1 可卖 + 构建当日候选池（全市场选股）
  - 盘中每 N 分钟 tick：对 候选池 ∪ 持仓 批量拉实时价 → 拼未完成K → 顺势宝择时 → 撮合
  - 15:05  eod_settle：收盘结算净值快照

非交易时段 tick 直接跳过。用 schedule 库驱动；由 engine 在后台线程启动。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import List, Optional

from .candidate_pool import CandidatePoolBuilder
from .market_data_poller import MarketDataPoller

logger = logging.getLogger(__name__)


class IntradayScheduler:
    def __init__(self, engine):
        self.engine = engine
        self.pool = CandidatePoolBuilder(engine)
        self.poller = MarketDataPoller(engine)
        self.candidate_date: Optional[str] = None
        self._stop = False

    def stop(self):
        self._stop = True

    # ---------------- 盘前 ----------------
    def pre_open(self, trade_date: str, max_stocks: Optional[int] = None) -> None:
        self.engine.account.mark_available_for_new_day()
        if self.candidate_date != trade_date:
            try:
                self.pool.build(trade_date, max_stocks=max_stocks)
                self.candidate_date = trade_date
            except Exception:
                logger.exception("候选池构建失败")

    # ---------------- 盘中 tick ----------------
    def tick(self) -> int:
        """返回本轮处理的信号数。"""
        if not self.engine.calendar.is_in_trading_session():
            return 0
        today = datetime.now().strftime("%Y-%m-%d")
        self.pre_open(today)

        cand = self.engine.dao.get_candidates(today)
        codes = {c["code"] for c in cand}
        codes |= set(self.engine.account.position_codes())
        if not codes:
            return 0

        quotes = self.poller.fetch_quotes(list(codes))
        acted = 0
        for code in codes:
            q = quotes.get(code)
            if q is None or q.is_suspended or q.price <= 0:
                continue
            df = self.poller.build_intraday_df(code, q)
            if df is None or len(df) < 60:
                continue
            pos = self.engine.account.get_position(code)
            sig = self.engine.signal_engine.evaluate(
                code, q.name, df, q, today,
                "position" if pos else "candidate")
            try:
                self.engine.dao.insert_signal({
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "trade_date": today, "code": code, "side": sig.side,
                    "quantity": sig.quantity, "strength": round(sig.strength, 3),
                    "message": sig.message[:200],
                    "source": sig.source, "acted": 0})
            except Exception:
                pass
            if sig.side != "hold":
                fill = self.engine.signal_engine.act(sig, q, today)
                if fill and fill.status == "filled":
                    acted += 1
        return acted

    # ---------------- 收盘结算 ----------------
    def eod_settle(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        prev = self.engine.dao.get_nav_series(1)
        prev_total = prev[0]["total_value"] if prev else None
        snap = self.engine.settle(today, prev_total)
        logger.info(f"收盘结算 {today}: 净值 {snap['total_value']:.2f} nav={snap['nav']:.4f}")

    # ---------------- 主循环 ----------------
    def run(self, max_stocks: Optional[int] = None) -> None:
        import schedule
        today = lambda: datetime.now().strftime("%Y-%m-%d")  # noqa: E731
        schedule.every().day.at("09:15").do(lambda: self.pre_open(today(), max_stocks))
        schedule.every(self.engine.config.poll_interval_minutes).minutes.do(self.tick)
        schedule.every().day.at("15:05").do(self.eod_settle)
        logger.info(f"盘中调度已启动: 每 {self.engine.config.poll_interval_minutes} 分钟轮询, "
                    f"09:15 建池 / 15:05 结算")
        # 启动时若已在交易时段，立即跑一次盘前 + tick
        if self.engine.calendar.is_in_trading_session():
            self.pre_open(today(), max_stocks)
            self.tick()
        while not self._stop:
            schedule.run_pending()
            time.sleep(20)
        logger.info("盘中调度已停止")
