"""候选池构建器（低频层）。

每个交易日盘初调用一次：复用 KHunter 的 13 个选股策略全市场扫描，
命中任一策略即入候选池，落 pt_candidate_pool。盘中高频轮询只盯候选池 ∪ 持仓。

选股层要求 df 倒序（db_manager.read_stock 默认倒序），直接传入，不翻转。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class CandidatePoolBuilder:
    def __init__(self, engine, max_stocks: Optional[int] = None):
        self.engine = engine
        self.max_stocks = max_stocks
        self._registry = None

    @property
    def registry(self):
        if self._registry is None:
            from strategy.strategy_registry import get_registry
            self._registry = get_registry("config/strategy_params.yaml")
            self._registry.auto_register_from_directory("strategy")
            logger.info(f"候选池：已加载 {len(self._registry.list_strategies())} 个选股策略")
        return self._registry

    def build(self, trade_date: str, max_stocks: Optional[int] = None,
              verbose: bool = False) -> List[Dict[str, Any]]:
        dbm = self.engine.db_manager
        codes = dbm.list_all_stocks()
        if not codes:
            logger.warning("行情库无股票，请先 init")
            return []
        limit = max_stocks or self.max_stocks or len(codes)
        codes = codes[:limit]
        names = (dbm.get_all_stock_names()
                 if hasattr(dbm, "get_all_stock_names") else {})

        candidates: Dict[str, Dict[str, Any]] = {}
        for idx, code in enumerate(codes):
            name = names.get(code, "")
            if any(k in name for k in ("退", "未知")) or name.startswith(("ST", "*ST")):
                continue
            df = dbm.read_stock(code)
            if df is None or len(df) < 60:
                continue
            hits = []
            for sname, strat in self.registry.strategies.items():
                try:
                    df_i = strat.calculate_indicators(df)
                    sigs = strat.select_stocks(df_i, name)
                    if sigs:
                        hits.append(sname)
                except Exception:
                    continue
            if hits:
                close = float(df.iloc[0]["close"]) if len(df) else 0.0
                candidates[code] = {
                    "code": code, "name": name,
                    "hit_strategies": ",".join(hits),
                    "close_at_select": close,
                    "support": None, "resistance": None,
                }
            if verbose and (idx + 1) % 200 == 0:
                logger.info(f"候选池构建进度 {idx+1}/{len(codes)}, 已命中 {len(candidates)}")

        cl = list(candidates.values())[: self.engine.config.max_candidates]
        self.engine.dao.replace_candidates(trade_date, cl)
        logger.info(f"候选池[{trade_date}]构建完成: {len(cl)} 只 (扫描 {len(codes)})")
        return cl
