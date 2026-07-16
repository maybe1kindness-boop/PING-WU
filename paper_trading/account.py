"""虚拟账户与持仓状态机。

T+1 实现要点：
  - 买入/加仓时新增 Lot，但 ``available``（可卖数量）**不增加**；
  - 每个交易日开盘前由调度器调用 ``mark_available_for_new_day``，
    把昨日及之前持有的数量全部置为可卖（即 ``available = quantity``）。

账户状态通过 PaperTradingDAO 持久化；关键操作（买/卖/结算）后自动落库。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .dao import PaperTradingDAO


@dataclass
class Lot:
    """单批建仓记录（支持 FIFO 卖出与 T+1）。"""

    lot_id: Optional[int]
    code: str
    buy_date: str            # YYYY-MM-DD，决定 T+1 可用性
    quantity: int            # 该批原始数量
    remaining: int           # 剩余未卖
    price: float             # 成交价
    fee: float               # 该批买入费用


@dataclass
class Position:
    code: str
    name: str = ""
    quantity: int = 0          # 总持仓（股）
    available: int = 0         # 可卖（T+1：昨日及之前持有的部分）
    avg_cost: float = 0.0      # 加权平均成本（含费用）
    first_buy_date: str = ""
    lots: List[Lot] = field(default_factory=list)
    latest_price: float = 0.0  # 最近刷新价（用于浮动市值）

    def market_value(self) -> float:
        return self.quantity * self.latest_price

    def float_pnl_pct(self) -> float:
        if self.avg_cost <= 0 or self.quantity <= 0:
            return 0.0
        return (self.latest_price - self.avg_cost) / self.avg_cost


class PaperAccount:
    """虚拟账户：现金 + 多头持仓，状态机式变更。"""

    def __init__(self, initial_capital: float, dao: PaperTradingDAO):
        self.initial_capital = initial_capital
        self.dao = dao
        self.cash: float = initial_capital
        self.positions: Dict[str, Position] = {}

    # ---------------- 加载/持久化 ----------------
    def load(self) -> None:
        """从 DB 恢复账户状态（首次启动时若库为空则用初始资金）。"""
        saved_cap = self.dao.get_meta("initial_capital")
        if saved_cap is not None:
            self.initial_capital = float(saved_cap)
        saved_cash = self.dao.get_meta("cash")
        self.cash = float(saved_cash) if saved_cash is not None else self.initial_capital

        self.positions.clear()
        for p in self.dao.get_positions():
            pos = Position(
                code=p["code"], name=p["name"], quantity=p["quantity"],
                available=p["available"], avg_cost=p["avg_cost"],
                first_buy_date=p["first_buy_date"] or "",
            )
            for lot in self.dao.get_open_lots(p["code"]):
                pos.lots.append(Lot(
                    lot_id=lot["lot_id"], code=lot["code"], buy_date=lot["buy_date"],
                    quantity=lot["quantity"], remaining=lot["remaining"],
                    price=lot["price"], fee=lot["fee"],
                ))
            self.positions[pos.code] = pos

    def persist_meta(self) -> None:
        self.dao.set_meta("initial_capital", self.initial_capital)
        self.dao.set_meta("cash", self.cash)

    # ---------------- 持仓查询 ----------------
    def get_position(self, code: str) -> Optional[Position]:
        return self.positions.get(code)

    def position_codes(self) -> List[str]:
        return [c for c, p in self.positions.items() if p.quantity > 0]

    def update_price(self, code: str, price: float) -> None:
        pos = self.positions.get(code)
        if pos and price and price > 0:
            pos.latest_price = price

    def positions_value(self) -> float:
        return sum(p.market_value() for p in self.positions.values())

    def total_value(self) -> float:
        return self.cash + self.positions_value()

    # ---------------- T+1 ----------------
    def mark_available_for_new_day(self) -> None:
        """交易日开盘前调用：把所有持仓 available 置为 quantity（解锁昨日持仓）。"""
        for pos in self.positions.values():
            if pos.quantity > 0 and pos.available < pos.quantity:
                pos.available = pos.quantity
                self.dao.upsert_position(self._pos_row(pos))

    # ---------------- 买入/加仓 ----------------
    def buy(self, code: str, name: str, trade_date: str,
            quantity: int, price: float, fee: float) -> None:
        """买入/加仓：扣现金、加 Lot、更新持仓（available 不变，T+1）。"""
        amount = quantity * price
        total_cost = amount + fee
        if total_cost > self.cash + 1e-6:
            raise ValueError(f"现金不足: 需 {total_cost:.2f}, 有 {self.cash:.2f}")
        self.cash -= total_cost
        lot_id = self.dao.add_lot(code, trade_date, quantity, price, fee)
        pos = self.positions.get(code)
        if pos is None:
            pos = Position(code=code, name=name, first_buy_date=trade_date)
            self.positions[code] = pos
        else:
            pos.name = name or pos.name
        # 加权平均成本（含费用）
        old_total = pos.quantity * pos.avg_cost
        pos.quantity += quantity
        pos.avg_cost = (old_total + amount + fee) / pos.quantity if pos.quantity else 0.0
        pos.lots.append(Lot(lot_id=lot_id, code=code, buy_date=trade_date,
                            quantity=quantity, remaining=quantity, price=price, fee=fee))
        self.dao.upsert_position(self._pos_row(pos))
        self.persist_meta()

    # ---------------- 卖出/减仓 ----------------
    def sell(self, code: str, trade_date: str, quantity: int) -> List[Dict]:
        """FIFO 卖出 quantity 股，返回卖出批次明细。available 不足抛 ValueError。"""
        pos = self.positions.get(code)
        avail = pos.available if pos else 0
        if avail < quantity:
            raise ValueError(f"{code} 可卖不足: 需 {quantity}, 有 {avail}")
        sold_lots = self.dao.reduce_lots(code, quantity)
        # 同步内存 lots
        for s in sold_lots:
            for lot in pos.lots:
                if lot.lot_id == s["lot_id"]:
                    lot.remaining -= s["sell_qty"]
                    break
        pos.quantity -= quantity
        pos.available -= quantity
        pos.lots = [l for l in pos.lots if l.remaining > 0]
        if pos.quantity <= 0:
            pos.avg_cost = 0.0
        self.dao.upsert_position(self._pos_row(pos))
        return sold_lots

    def add_cash(self, amount: float) -> None:
        """卖出净款入账。"""
        self.cash += amount
        self.persist_meta()

    @staticmethod
    def _pos_row(pos: Position) -> Dict:
        return {
            "code": pos.code, "name": pos.name, "quantity": pos.quantity,
            "available": pos.available, "avg_cost": pos.avg_cost,
            "first_buy_date": pos.first_buy_date,
        }

    # ---------------- 估值快照 ----------------
    def snapshot(self, trade_date: str, prev_total: Optional[float] = None) -> Dict:
        total = self.total_value()
        nav = total / self.initial_capital if self.initial_capital else 0.0
        daily_pnl = (total - prev_total) if prev_total is not None else 0.0
        return {
            "trade_date": trade_date,
            "cash": self.cash,
            "positions_value": self.positions_value(),
            "total_value": total,
            "nav": nav,
            "drawdown": 0.0,  # 由 EODSettler 用历史峰值计算
            "position_count": len([p for p in self.positions.values() if p.quantity > 0]),
            "daily_pnl": daily_pnl,
        }
