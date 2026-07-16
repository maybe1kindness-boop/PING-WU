"""模拟盘专用 SQLite 持久化层。

独立库文件（默认 ``data/paper_trading.db``），不与 KHunter 行情库混用。
所有表带 trade_date 便于回溯；主键/唯一约束保证幂等。

表清单：
  pt_position        当前持仓汇总（单账户单票一行）
  pt_position_lot    分批建仓明细（支持 T+1 与 FIFO 卖出）
  pt_trade           交易记录（含成交与拒单）
  pt_nav_snapshot    每日净值快照
  pt_candidate_pool  当日候选池
  pt_signal_log      盘中信号日志（含未成交）
  pt_meta            元数据（初始资金/暂停标志/最后轮询时间等）
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_position (
    code TEXT PRIMARY KEY,
    name TEXT,
    quantity INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    first_buy_date TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pt_position_lot (
    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    buy_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    remaining INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lot_code_date ON pt_position_lot(code, buy_date, lot_id);
CREATE TABLE IF NOT EXISTS pt_trade (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    trade_time TEXT,
    code TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    reject_reason TEXT,
    signal_msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_date_code ON pt_trade(trade_date, code);
CREATE TABLE IF NOT EXISTS pt_nav_snapshot (
    trade_date TEXT PRIMARY KEY,
    cash REAL, positions_value REAL, total_value REAL,
    nav REAL, drawdown REAL, position_count INTEGER, daily_pnl REAL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pt_candidate_pool (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    hit_strategies TEXT,
    close_at_select REAL,
    support REAL,
    resistance REAL,
    PRIMARY KEY (trade_date, code)
);
CREATE TABLE IF NOT EXISTS pt_signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT,
    trade_date TEXT,
    code TEXT, side TEXT, quantity INTEGER,
    strength REAL, message TEXT, source TEXT, acted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signal_date ON pt_signal_log(trade_date, generated_at);
CREATE TABLE IF NOT EXISTS pt_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class PaperTradingDAO:
    """线程安全的 SQLite 访问层（每次操作借用独立连接）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(SCHEMA)

    # ---------------- 元数据 ----------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM pt_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pt_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ---------------- 持仓汇总 ----------------
    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM pt_position").fetchall()
            return [dict(r) for r in rows]

    def upsert_position(self, p: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pt_position(code,name,quantity,available,avg_cost,first_buy_date,updated_at) "
                "VALUES(:code,:name,:quantity,:available,:avg_cost,:first_buy_date,:updated_at) "
                "ON CONFLICT(code) DO UPDATE SET "
                "name=excluded.name,quantity=excluded.quantity,available=excluded.available,"
                "avg_cost=excluded.avg_cost,first_buy_date=excluded.first_buy_date,updated_at=excluded.updated_at",
                {**p, "updated_at": datetime.now().isoformat(timespec="seconds")},
            )

    def delete_position(self, code: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM pt_position WHERE code=?", (code,))

    # ---------------- 分批 Lot ----------------
    def add_lot(self, code: str, buy_date: str, quantity: int,
                price: float, fee: float) -> int:
        """新增一批建仓，返回 lot_id。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pt_position_lot(code,buy_date,quantity,price,fee,remaining) "
                "VALUES(?,?,?,?,?,?)",
                (code, buy_date, quantity, price, fee, quantity),
            )
            return cur.lastrowid

    def get_open_lots(self, code: str) -> List[Dict[str, Any]]:
        """返回某只股票所有 remaining>0 的批次，按 FIFO 排序。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_position_lot WHERE code=? AND remaining>0 "
                "ORDER BY buy_date ASC, lot_id ASC",
                (code,),
            ).fetchall()
            return [dict(r) for r in rows]

    def reduce_lots(self, code: str, qty: int) -> List[Dict[str, Any]]:
        """FIFO 卖出 qty 股，返回卖出的批次明细 [{lot_id, sell_qty, price, fee_share}]。

        在一个事务内完成扣减；若可卖不足则抛 ValueError。
        """
        if qty <= 0:
            return []
        with self._lock, self._conn() as c:
            lots = c.execute(
                "SELECT * FROM pt_position_lot WHERE code=? AND remaining>0 "
                "ORDER BY buy_date ASC, lot_id ASC",
                (code,),
            ).fetchall()
            total = sum(r["remaining"] for r in lots)
            if total < qty:
                raise ValueError(f"{code} 可卖不足: 需 {qty}, 有 {total}")
            sold, out = qty, []
            for r in lots:
                if sold <= 0:
                    break
                take = min(r["remaining"], sold)
                new_rem = r["remaining"] - take
                c.execute("UPDATE pt_position_lot SET remaining=? WHERE lot_id=?",
                          (new_rem, r["lot_id"]))
                # 按比例分摊该批费用
                fee_share = r["fee"] * take / r["quantity"] if r["quantity"] else 0
                out.append({"lot_id": r["lot_id"], "sell_qty": take,
                            "buy_price": r["price"], "fee_share": fee_share})
                sold -= take
            return out

    def mark_all_lots_available(self, code: Optional[str] = None) -> None:
        """T+1：把指定股票（或全部）的 lot 标记为可卖（仅更新 pt_position.available）。"""
        # available 的语义见 PaperAccount.mark_available_for_new_day
        pass

    # ---------------- 交易记录 ----------------
    def insert_trade(self, t: Dict[str, Any]) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pt_trade(trade_date,trade_time,code,name,side,quantity,price,amount,"
                "commission,stamp_tax,transfer_fee,total,status,reject_reason,signal_msg) "
                "VALUES(:trade_date,:trade_time,:code,:name,:side,:quantity,:price,:amount,"
                ":commission,:stamp_tax,:transfer_fee,:total,:status,:reject_reason,:signal_msg)",
                t,
            )
            return cur.lastrowid

    def get_trades(self, trade_date: Optional[str] = None,
                   limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            if trade_date:
                rows = c.execute(
                    "SELECT * FROM pt_trade WHERE trade_date=? ORDER BY trade_id DESC LIMIT ?",
                    (trade_date, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM pt_trade ORDER BY trade_id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    # ---------------- 净值快照 ----------------
    def upsert_nav(self, nav: Dict[str, Any]) -> None:
        nav = {**nav, "updated_at": datetime.now().isoformat(timespec="seconds")}
        cols = ["trade_date", "cash", "positions_value", "total_value", "nav",
                "drawdown", "position_count", "daily_pnl", "updated_at"]
        placeholders = ",".join(f":{c}" for c in cols)
        with self._lock, self._conn() as c:
            c.execute(
                f"INSERT INTO pt_nav_snapshot({','.join(cols)}) VALUES({placeholders}) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "cash=excluded.cash,positions_value=excluded.positions_value,"
                "total_value=excluded.total_value,nav=excluded.nav,drawdown=excluded.drawdown,"
                "position_count=excluded.position_count,daily_pnl=excluded.daily_pnl,updated_at=excluded.updated_at",
                {c: nav.get(c) for c in cols},
            )

    def get_nav_series(self, days: int = 90) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_nav_snapshot ORDER BY trade_date DESC LIMIT ?", (days,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ---------------- 候选池 ----------------
    def replace_candidates(self, trade_date: str,
                           candidates: List[Dict[str, Any]]) -> int:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM pt_candidate_pool WHERE trade_date=?", (trade_date,))
            for cand in candidates:
                c.execute(
                    "INSERT OR IGNORE INTO pt_candidate_pool"
                    "(trade_date,code,name,hit_strategies,close_at_select,support,resistance) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (trade_date, cand.get("code"), cand.get("name"),
                     cand.get("hit_strategies"), cand.get("close_at_select"),
                     cand.get("support"), cand.get("resistance")),
                )
            return len(candidates)

    def get_candidates(self, trade_date: str) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_candidate_pool WHERE trade_date=?", (trade_date,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------- 信号日志 ----------------
    def insert_signal(self, s: Dict[str, Any]) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pt_signal_log(generated_at,trade_date,code,side,quantity,"
                "strength,message,source,acted) VALUES(?,?,?,?,?,?,?,?,0)",
                (s.get("generated_at"), s.get("trade_date"), s.get("code"),
                 s.get("side"), s.get("quantity"), s.get("strength"),
                 s.get("message"), s.get("source")),
            )
            return cur.lastrowid

    def get_signals(self, trade_date: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            if trade_date:
                rows = c.execute(
                    "SELECT * FROM pt_signal_log WHERE trade_date=? "
                    "ORDER BY id DESC LIMIT ?", (trade_date, limit)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM pt_signal_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def latest_signal_id(self) -> Optional[int]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT MAX(id) AS m FROM pt_signal_log").fetchone()
            return row["m"] if row and row["m"] is not None else None
