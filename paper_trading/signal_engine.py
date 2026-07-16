"""信号引擎：候选/持仓 → 顺势宝择时 → Signal → 撮合 编排。

本模块是 paper_trading 内 df 顺序的【唯一转换点】：接收 KHunter 惯例的倒序 df，
调择时前用 ``to_ascending`` 转为正序（顺势宝要求最新在 iloc[-1]）。

仓位控制：
  - 单票最大仓位 ``max_position_pct``
  - 总持仓上限 ``max_total_position_pct``（隐含最低现金保留）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .account import PaperAccount
from .broker import Fill, PaperBroker, Quote
from .config_schema import PaperConfig
from .dao import PaperTradingDAO
from .df_orient import to_ascending


@dataclass
class Signal:
    code: str
    name: str
    side: str            # buy/add/sell/reduce/hold
    quantity: int = 0
    strength: float = 0.0
    message: str = ""
    support: float = 0.0
    resistance: float = 0.0
    source: str = "candidate"   # candidate / position
    trade_type: str = ""


class SignalEngine:
    def __init__(self, timing, broker: PaperBroker, account: PaperAccount,
                 dao: PaperTradingDAO, config: PaperConfig):
        self.timing = timing
        self.broker = broker
        self.account = account
        self.dao = dao
        self.config = config

    def _position_dict(self, pos) -> Optional[dict]:
        if pos is None or pos.quantity <= 0:
            return None
        # 顺势宝只读 quantity；传总持仓，broker 撮合时按 available 截断（T+1）
        return {"quantity": pos.quantity}

    def evaluate(self, code: str, name: str, df_desc: pd.DataFrame,
                 quote: Quote, trade_date: str, source: str = "candidate") -> Signal:
        """对单只股票生成 Signal。df_desc 为 KHunter 倒序 df。"""
        if df_desc is None or len(df_desc) < 60:
            return Signal(code=code, name=name, side="hold", message="数据不足")
        df_asc = to_ascending(df_desc)
        pos = self.account.get_position(code)
        if pos:
            self.account.update_price(code, quote.price)
        try:
            tr = self.timing.get_timing_result(
                df_asc, self._position_dict(pos), self.account.cash,
                use_prev_day_signal=False,
            )
        except Exception as e:
            return Signal(code=code, name=name, side="hold", message=f"择时异常:{e}")

        # 映射 TimingResult → Signal
        if pos is None or pos.quantity <= 0:
            if tr.is_buy:
                qty = self._calc_buy_qty(code, quote.price)
                return Signal(code, name, "buy", qty, tr.signal_strength, tr.message,
                              tr.support_level, tr.resistance_level, source,
                              tr.trade_type or "buy")
        else:
            if tr.is_sell:
                side = "reduce" if tr.trade_type == "reduce" else "sell"
                return Signal(code, name, side, tr.sell_quantity, tr.signal_strength,
                              tr.message, tr.support_level, tr.resistance_level,
                              "position", tr.trade_type or side)
            if tr.is_buy and tr.trade_type == "add":
                add_qty = min(tr.buy_quantity, self._calc_buy_qty(code, quote.price))
                if add_qty >= 100:
                    return Signal(code, name, "add", add_qty, tr.signal_strength,
                                  tr.message, tr.support_level, tr.resistance_level,
                                  "position", "add")
        return Signal(code, name, "hold", 0, tr.signal_strength, tr.message,
                      tr.support_level, tr.resistance_level, source, "hold")

    def _calc_buy_qty(self, code: str, price: float) -> int:
        """按仓位上限（单票+总仓位+现金）计算可买整手。"""
        if price <= 0:
            return 0
        total = self.account.total_value()
        pos = self.account.get_position(code)
        single_room = total * self.config.max_position_pct - (pos.market_value() if pos else 0)
        total_room = total * self.config.max_total_position_pct - self.account.positions_value()
        room = max(0.0, min(single_room, total_room, self.account.cash))
        return self.broker.calc_buy_quantity(room, price, code)

    def act(self, signal: Signal, quote: Quote, trade_date: str) -> Optional[Fill]:
        """根据 Signal 撮合，返回 Fill（hold 返回 None）。"""
        if signal.side == "buy":
            return self.broker.execute_buy(self.account, quote, signal.quantity,
                                           trade_date, signal.message, "buy")
        if signal.side == "add":
            return self.broker.execute_add(self.account, quote, signal.quantity,
                                           trade_date, signal.message)
        if signal.side in ("sell", "reduce"):
            return self.broker.execute_sell(self.account, quote, signal.quantity,
                                            trade_date, signal.message, signal.side)
        return None
