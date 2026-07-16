"""df 顺序统一工具（paper_trading 内唯一的顺序转换点）。

KHunter 各层对 df 顺序的约定不一致：
  - 选股层（strategy_registry.run_all / BaseStrategy）：要求**倒序**，最新在 iloc[0]
  - 顺势宝择时层（get_timing_result）：要求**正序**，最新在 iloc[-1]
  - stock_kline 表 / db_manager.read_stock：默认**倒序**

为避免污染 KHunter 原策略源文件，本模块集中提供转换函数。
调用约定：CandidatePoolBuilder 与 MarketDataPoller 全程保持倒序；
仅在 SignalEngine 调择时前调用一次 ``to_ascending``。
"""
from __future__ import annotations

import pandas as pd


def _is_descending(df: pd.DataFrame) -> bool:
    """判断 df 是否倒序（最新在前）。"""
    if df is None or len(df) < 2 or "date" not in df.columns:
        return False
    try:
        return df["date"].iloc[0] > df["date"].iloc[1]
    except Exception:
        return False


def to_ascending(df: pd.DataFrame) -> pd.DataFrame:
    """倒序(最新在前) → 正序(最新在末尾)。

    仅在检测到确为倒序时翻转，避免对已是正序的 df 误操作。
    返回 reset_index 后的新 df，不修改原对象。
    """
    if _is_descending(df):
        return df.iloc[::-1].reset_index(drop=True)
    return df


def to_descending(df: pd.DataFrame) -> pd.DataFrame:
    """正序 → 倒序（最新在前）。"""
    if df is None or len(df) < 2 or "date" not in df.columns:
        return df
    try:
        if df["date"].iloc[0] < df["date"].iloc[1]:
            return df.iloc[::-1].reset_index(drop=True)
    except Exception:
        pass
    return df
