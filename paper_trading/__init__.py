"""KHunter 盘中实时模拟盘模块。

基于 KHunter 的选股策略（买点候选）与顺势宝择时（买卖/加仓/止盈止损），
在虚拟账户上自动撮合，支持盘中实时监控与历史回放验证。

核心组件：
  - PaperAccount / Position / Lot    虚拟账户与持仓（T+1 用 Lot 批次）
  - PaperBroker                       撮合引擎（费用/涨跌停/停牌/整手）
  - SignalEngine                      候选→择时→Signal 编排
  - CandidatePoolBuilder             盘初全市场选股出候选池
  - MarketDataPoller                 实时行情 + 拼未完成K
  - IntradayScheduler                盘中轮询主循环
  - PaperTradingEngine               Facade，聚合全部组件
"""

__all__ = []
