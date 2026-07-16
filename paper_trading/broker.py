"""虚拟撮合引擎（PaperBroker）。

把择时信号翻译成可成交/拒单订单，处理 A 股硬约束：
  - 实时价撮合（盘中触发即用当前价成交）
  - T+1（当日买入不可卖，由 PaperAccount.available 控制）
  - 费用：佣金万2.5最低5元、印花税卖出千1、过户费沪市万0.1
  - 涨跌停封板拒单（MVP 保守：价格触及涨跌停即视为封板，宁可漏单不可错成交）
  - 停牌跳过
  - 整手买入（100 股整数倍），卖出可清仓尾差

费用口径参考 ``strategy_runner.calculate_trading_cost``。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Tuple

from .account import PaperAccount
from .config_schema import FeeConfig
from .dao import PaperTradingDAO


@dataclass
class Quote:
    code: str
    name: str = ""
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0
    pct: float = 0.0          # 涨跌幅 %
    is_st: bool = False
    is_suspended: bool = False


@dataclass
class Fill:
    code: str
    side: str = ""            # buy/add/sell/reduce
    name: str = ""
    executed_qty: int = 0
    price: float = 0.0
    amount: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    total: float = 0.0        # 买入=总花费含费；卖出=净入账
    status: str = "filled"    # filled / rejected
    reject_reason: str = ""
    fill_time: str = ""
    signal_msg: str = ""


def _exchange(code: str) -> str:
    """判定交易所：SSE(沪) / SZSE(深) / BSE(北)。"""
    if code.startswith(("60", "68", "9")):
        return "SSE"
    if code.startswith(("00", "30", "20")):
        return "SZSE"
    return "BSE"


def _limit_ratio(code: str, is_st: bool) -> float:
    """涨跌停幅度比例。"""
    if is_st:
        return 0.05
    if code.startswith(("300", "301", "688")):  # 创业板 / 科创板
        return 0.20
    if code.startswith(("8", "4")):             # 北交所
        return 0.30
    return 0.10


class PaperBroker:
    def __init__(self, fee: FeeConfig, dao: PaperTradingDAO):
        self.fee = fee
        self.dao = dao

    # ---------------- 费用 ----------------
    def estimate_fee(self, amount: float, side: str, code: str) -> Dict[str, float]:
        commission = max(amount * self.fee.commission_rate, self.fee.commission_min) if amount > 0 else 0.0
        stamp_tax = amount * self.fee.stamp_tax_rate if side in ("sell", "reduce") else 0.0
        transfer_fee = amount * self.fee.transfer_fee_rate if _exchange(code) == "SSE" else 0.0
        return {"commission": commission, "stamp_tax": stamp_tax, "transfer_fee": transfer_fee}

    # ---------------- 涨跌停 ----------------
    def limit_prices(self, quote: Quote) -> Tuple[float, float]:
        """返回 (涨停价, 跌停价)；无昨收返回 (0,0)。"""
        if quote.prev_close <= 0:
            return (0.0, 0.0)
        r = _limit_ratio(quote.code, quote.is_st)
        return (round(quote.prev_close * (1 + r), 2),
                round(quote.prev_close * (1 - r), 2))

    def is_limit_up_locked(self, quote: Quote) -> bool:
        up, _ = self.limit_prices(quote)
        return up > 0 and quote.price >= up - 1e-4

    def is_limit_down_locked(self, quote: Quote) -> bool:
        _, down = self.limit_prices(quote)
        return down > 0 and quote.price <= down + 1e-4

    # ---------------- 整手 ----------------
    def calc_buy_quantity(self, cash: float, price: float, code: str) -> int:
        """计算在佣金下限边界下能买多少整百手。"""
        if price <= 0:
            return 0
        qty = int(cash / (price * (1 + self.fee.commission_rate))) // 100 * 100
        while qty >= 100:
            amount = qty * price
            f = self.estimate_fee(amount, "buy", code)
            if amount + f["commission"] + f["transfer_fee"] <= cash + 1e-6:
                return int(qty)
            qty -= 100
        return 0

    # ---------------- 撮合 ----------------
    def execute_buy(self, account: PaperAccount, quote: Quote, qty: int,
                    trade_date: str, signal_msg: str = "", side: str = "buy") -> Fill:
        now = datetime.now().strftime("%H:%M:%S")

        def reject(reason: str) -> Fill:
            f = Fill(code=quote.code, side=side, name=quote.name, price=quote.price,
                     status="rejected", reject_reason=reason, fill_time=now, signal_msg=signal_msg)
            self.dao.insert_trade(self._trade_row(f, trade_date))
            return f

        if quote.is_suspended:
            return reject("停牌")
        if self.is_limit_up_locked(quote):
            return reject("涨停封板")
        if qty < 100:
            return reject("不足1手")
        amount = qty * quote.price
        fd = self.estimate_fee(amount, side, quote.code)
        cost = amount + fd["commission"] + fd["transfer_fee"]
        if cost > account.cash + 1e-6:
            return reject("现金不足")
        account.buy(quote.code, quote.name, trade_date, qty, quote.price,
                    fd["commission"] + fd["transfer_fee"])
        fill = Fill(code=quote.code, side=side, name=quote.name, executed_qty=qty,
                    price=quote.price, amount=amount, commission=fd["commission"],
                    stamp_tax=0.0, transfer_fee=fd["transfer_fee"], total=cost,
                    fill_time=now, signal_msg=signal_msg)
        self.dao.insert_trade(self._trade_row(fill, trade_date))
        return fill

    def execute_sell(self, account: PaperAccount, quote: Quote, qty: int,
                     trade_date: str, signal_msg: str = "", side: str = "sell") -> Fill:
        now = datetime.now().strftime("%H:%M:%S")

        def reject(reason: str) -> Fill:
            f = Fill(code=quote.code, side=side, name=quote.name, price=quote.price,
                     status="rejected", reject_reason=reason, fill_time=now, signal_msg=signal_msg)
            self.dao.insert_trade(self._trade_row(f, trade_date))
            return f

        pos = account.get_position(quote.code)
        if pos is None or pos.available <= 0:
            return reject("无持仓/可卖为0(T+1)")
        qty = min(qty, pos.available)
        if qty <= 0:
            return reject("可卖为0(T+1)")
        if quote.is_suspended:
            return reject("停牌")
        if self.is_limit_down_locked(quote):
            return reject("跌停封板")
        amount = qty * quote.price
        fd = self.estimate_fee(amount, side, quote.code)
        net = amount - fd["commission"] - fd["stamp_tax"] - fd["transfer_fee"]
        account.sell(quote.code, trade_date, qty)
        account.add_cash(net)
        fill = Fill(code=quote.code, side=side, name=quote.name, executed_qty=qty,
                    price=quote.price, amount=amount, commission=fd["commission"],
                    stamp_tax=fd["stamp_tax"], transfer_fee=fd["transfer_fee"], total=net,
                    fill_time=now, signal_msg=signal_msg)
        self.dao.insert_trade(self._trade_row(fill, trade_date))
        return fill

    def execute_add(self, account: PaperAccount, quote: Quote, qty: int,
                    trade_date: str, signal_msg: str = "") -> Fill:
        return self.execute_buy(account, quote, qty, trade_date, signal_msg, side="add")

    def _trade_row(self, fill: Fill, trade_date: str) -> Dict:
        return {
            "trade_date": trade_date, "trade_time": fill.fill_time,
            "code": fill.code, "name": fill.name, "side": fill.side,
            "quantity": fill.executed_qty, "price": fill.price, "amount": fill.amount,
            "commission": fill.commission, "stamp_tax": fill.stamp_tax,
            "transfer_fee": fill.transfer_fee, "total": fill.total,
            "status": fill.status, "reject_reason": fill.reject_reason,
            "signal_msg": fill.signal_msg,
        }
