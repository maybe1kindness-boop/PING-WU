"""paper_trading 配置默认值与加载。

配置来源优先级：
  1. config/config.yaml 的 ``paper_trading`` 段
  2. 缺失项回退到 ``trading.initial_capital`` 等 KHunter 已有字段
  3. 再缺失则用本文件 dataclass 默认值
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict

from utils.paths import data_path


@dataclass
class FeeConfig:
    """A股交易费用（参考 strategy_runner.calculate_trading_cost 的费率）。"""

    commission_rate: float = 0.00025    # 佣金 万2.5（双向）
    commission_min: float = 5.0         # 佣金最低 5 元
    stamp_tax_rate: float = 0.001       # 印花税 卖出 千1
    transfer_fee_rate: float = 0.00001  # 过户费 沪市 万0.1（双向）
    buy_slippage: float = 0.0           # 买入滑点（模拟盘默认0；回测里用0.01）
    sell_slippage: float = 0.0          # 卖出滑点


@dataclass
class PaperConfig:
    """模拟盘运行参数。"""

    # 账户
    initial_capital: float = 300000.0
    # 盘中轮询
    poll_interval_minutes: int = 2       # 正常盘中轮询间隔
    degraded_interval_minutes: int = 5   # 限流降级后间隔
    # 仓位控制
    max_position_pct: float = 0.20       # 单票最大仓位（占总资产）
    max_total_position_pct: float = 0.80  # 总持仓上限（占总资产）
    # 策略
    timing_strategy: str = "macd_bollinger"  # 顺势宝（已验证门禁通过）
    timing_fallback: str = "bollinger"       # 门禁失败时降级（无门禁）
    max_candidates: int = 300               # 候选池上限
    # 熔断
    cb_max_drawdown: float = 0.10           # 累计回撤熔断阈值
    cb_consecutive_loss_days: int = 5       # 连续亏损交易日熔断
    # 行情
    use_spot_em: bool = True                # 优先 ak.stock_zh_a_spot_em 全市场
    spot_min_interval_sec: float = 3.0      # spot_em 最小请求间隔（反爬）
    # 存储
    db_path: str = field(default_factory=lambda: str(data_path("paper_trading.db")))
    # 费率
    fee: FeeConfig = field(default_factory=FeeConfig)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_paper_config(khunter_config: Dict[str, Any] | None) -> PaperConfig:
    """从 KHunter config.yaml 合并出 PaperConfig。"""
    khunter_config = khunter_config or {}
    pt: Dict[str, Any] = khunter_config.get("paper_trading", {}) or {}

    # 费率段
    fee = FeeConfig()
    for k, v in (pt.get("fee") or {}).items():
        if hasattr(fee, k):
            setattr(fee, k, v)

    cfg = PaperConfig(fee=fee)
    # 逐项覆盖 paper_trading 段
    for k, v in pt.items():
        if k != "fee" and hasattr(cfg, k):
            setattr(cfg, k, v)

    # initial_capital 回退到 KHunter trading 段
    if "initial_capital" not in pt:
        cfg.initial_capital = khunter_config.get("trading", {}).get(
            "initial_capital", cfg.initial_capital
        )
    return cfg
