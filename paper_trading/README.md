# KHunter 盘中实时模拟盘（paper_trading）

基于 KHunter 选股策略 + 顺势宝择时的**桌面端实时模拟盘**：虚拟资金、跟实时行情、自动买卖，不花真钱。

## 快速开始

```bash
cd /Users/kindnessmaybe/Desktop/量化/KHunter
.venv/bin/python main.py paper
```

打开后是一个桌面窗口（PySide6），顶部是账户概览卡片，下方四个标签页，工具栏两个按钮：

- **▶ 历史回放演示**：选一只股票，用其 3 年历史 K 线逐日回放完整买卖流程（立即可用，无需等待盘中）
- **🔴 启动盘中实时**：启动盘中实时模式——每个交易日 09:15 全市场选股建候选池，盘中每 2 分钟对「候选池 ∪ 持仓」轮询实时行情 → 顺势宝择时 → 虚拟撮合，15:05 收盘结算

## 四个标签页

| 页 | 内容 |
|---|---|
| 当前持仓 | 代码/名称/持仓/可卖(T+1)/成本/现价/市值/浮盈% |
| 交易记录 | 每笔买卖（含拒单原因：涨停封板/停牌/现金不足/T+1）|
| 净值曲线 | 每日单位净值折线（红涨绿跌，A股配色）|
| 信号日志 | 盘中每轮择时信号（含未成交）|

## 核心规则（已通过单元验证 16/16）

- **T+1**：买入当日不可卖（`available` 次日开盘前解锁）
- **费用**：佣金万2.5最低5元 / 印花税卖出千1 / 过户费沪市万0.1
- **涨跌停**：主板±10%、创业板/科创板±20%、ST±5%、北交所±30%；触及涨跌停即拒单
- **整手**：买入100股整数倍，卖出可清仓尾差
- **仓位控制**：单票≤20%、总仓位≤80%

## 配置

`config/config.yaml` 的 `paper_trading` 段可调：轮询间隔、仓位上限、费率、择时策略等。
初始资金沿用 `trading.initial_capital`（默认 30 万）。

## 模块结构

| 文件 | 职责 |
|---|---|
| `engine.py` | Facade，整合所有组件 + 数据查询接口 |
| `account.py` | 虚拟账户/持仓/Lot（T+1）|
| `broker.py` | 撮合引擎（费用/涨跌停/停牌/整手）|
| `signal_engine.py` | 候选→择时→Signal→撮合编排（df 顺序唯一转换点）|
| `candidate_pool.py` | 盘初全市场选股（复用 KHunter 13 策略）|
| `market_data_poller.py` | 实时行情 + 拼未完成K |
| `scheduler.py` | 盘中轮询主循环 |
| `dao.py` | SQLite 持久化（7 张表）|
| `gui.py` | PySide6 桌面界面 |
| `replay_test.py` | 历史回放验证脚本 |

## 复用 KHunter

- 数据：`AKShareFetcher` / `stock_kline` 表
- 选股：`StrategyRegistry.run_all`
- 择时：`ShunShiBaoStrategy.get_timing_result`
- 行情：`ak.stock_zh_a_spot_em`

## 已知限制 / 后续

- **免费数据源限流**：盘中高频轮询可能触发反爬，已内置降级（spot_em→batch→暂停）
- **市值过滤失效**：tushare 无 token 时候选池的市值过滤跳过（不影响核心）
- **熔断/钉钉推送**：阶段4 增强（可选）
