"""阶段1 历史回放验证：单股逐日推进，验证 buy→add→sell 闭环。

用历史 K 线模拟"每日收盘"，逐日驱动 SignalEngine，验证账户/撮合/择时的完整闭环、
df 顺序、T+1、涨跌停、费用、资产守恒。不依赖实时行情。

用法:
    .venv/bin/python -m paper_trading.replay_test            # 回放第一只股票
    .venv/bin/python -m paper_trading.replay_test 600101     # 指定股票
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.timing_strategies import TimingStrategyFactory
from utils.global_db import get_global_db

from paper_trading.account import PaperAccount
from paper_trading.broker import PaperBroker, Quote
from paper_trading.config_schema import PaperConfig
from paper_trading.dao import PaperTradingDAO
from paper_trading.df_orient import to_ascending
from paper_trading.signal_engine import SignalEngine


def run(code: str | None = None, initial: float = 100000.0,
        db_path: str = "data/_replay.db", verbose: bool = True):
    db = get_global_db()
    codes = db.list_all_stocks()
    if not codes:
        print("✗ 行情库无数据，请先: python main.py init --max-stocks 50")
        return None
    code = code or codes[0]
    df = db.read_stock(code)
    if df is None or len(df) < 80:
        print(f"✗ {code} 数据不足 ({len(df) if df is not None else 0} 行)")
        return None

    asc = to_ascending(df)  # 正序，iloc[-1] 最新
    names = db.get_all_stock_names() if hasattr(db, "get_all_stock_names") else {}
    name = names.get(code, "")

    cfg = PaperConfig(initial_capital=initial)
    if os.path.exists(db_path):
        os.remove(db_path)
    dao = PaperTradingDAO(db_path)
    acct = PaperAccount(initial, dao)
    br = PaperBroker(cfg.fee, dao)
    timing = TimingStrategyFactory.create_strategy("macd_bollinger", {})
    engine = SignalEngine(timing, br, acct, dao, cfg)

    start, n = 70, len(asc)
    summary: dict = {}
    trades = []
    for i in range(start, n):
        sub_asc = asc.iloc[:i]                       # 前 i 天，iloc[-1]=第i天
        today, prev = sub_asc.iloc[-1], sub_asc.iloc[-2]
        sub_desc = sub_asc.iloc[::-1].reset_index(drop=True)  # 转倒序给 engine
        td = str(today["date"])[:10]
        q = Quote(code=code, name=name, price=float(today["close"]),
                  prev_close=float(prev["close"]), open=float(today["open"]),
                  high=float(today["high"]), low=float(today["low"]))
        acct.mark_available_for_new_day()
        acct.update_price(code, float(today["close"]))
        sig = engine.evaluate(code, name, sub_desc, q, td,
                              "position" if acct.get_position(code) else "candidate")
        summary[sig.side] = summary.get(sig.side, 0) + 1
        if sig.side != "hold":
            fill = engine.act(sig, q, td)
            trades.append({
                "date": td, "side": sig.side, "qty": sig.quantity,
                "status": fill.status if fill else "-",
                "reason": (fill.reject_reason[:18] if fill and fill.status == "rejected" else ""),
                "total_value": round(acct.total_value(), 2),
            })

    final = acct.total_value()
    pos = acct.get_position(code)
    if verbose:
        print(f"\n=== 回放 {code} {name} | {n} 根K线，回放 {n-start} 天 ===")
        print(f"信号统计: {summary}")
        for t in trades[:20]:
            print(f"  {t['date']} {t['side']:7s} qty={t['qty']:5d} {t['status']:8s} "
                  f"{t['reason']:18s} 净值={t['total_value']}")
        tag = "盈利" if final > initial else "亏损"
        print(f"最终: 净值={final:.2f} ({tag} {final-initial:+.2f}) "
              f"持仓={pos.quantity if pos else 0} 股")
    return {"summary": summary, "trades": trades, "final": final,
            "holding": pos.quantity if pos else 0}


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
