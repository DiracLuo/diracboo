# Alpha Ledger

Alpha Ledger 是一个以盈利验证为中心的股票策略账本。它的主线不是手工写故事，也不是自动交易机器人，而是：

1. 用策略筛选器生成候选股。
2. 把候选的当时数据、买点、止损、目标、理由和风险写入 `candidates`。
3. 用后续真实行情回放，记录执行价、退出路径、净收益、胜率和止损率。
4. 让策略按样本表现竞争，保留有效策略，收紧或淘汰无效策略。

默认账本不再种入手工信号。兴业科技只保留为案例数据和策略灵感，真正入账必须由筛选器在当日条件下挖出来。

## 当前主线

当前正式研究范围是 A 股（`CN_A`）。美股和港股的数据接入与策略映射仍保留为实验能力，但默认不进入每日买入清单、正式回放收益结论、策略权重建议和组合回测。

- `candidates`：策略筛选出的候选池，是当前项目的主账本。
- `candidate_evaluations`：截至某日的后验验证。
- `candidate_horizon_evaluations`：固定 T+5 / T+10 / T+20 / T+60 周期验证。
- `daily-plan`：当天可操作清单，只展示确认后更接近可交易的候选。
- `replay`：历史日期逐日筛选并用未来行情验证。
- `portfolio-backtest`：组合层面回测，控制仓位、持仓冷却和交易成本。
- `walk-forward`：检查当前样本是否足够做正式滚动验证；样本不足时只输出 `INSUFFICIENT_HISTORY`。

A 股回放优先使用 5 分钟线：

- 入场：候选日后第一个交易日开盘前 5 根 K 线 VWAP。
- 退出：有分时数据时按分时止损/止盈顺序；没有分时时回退日线。
- T+1：A 股买入日不允许同日退出。
- 成本：评估收益使用净收益，默认按市场统一成本扣除；A 股往返成本为 0.18%。
- 复权：收益统计优先使用前复权价格；成交价仍保留原始价格用于执行展示。
- 基准：A 股默认用沪深 300（`000300.SS`）计算基准收益和超额收益。
- 约束：组合回测会跳过零成交/停牌样本和一字涨停无法买入样本。

## 常用流程

初始化和种入策略、兴业科技参考数据：

```bash
python -m alpha_ledger init
python -m alpha_ledger seed
```

拉取行情和事件：

```bash
python -m alpha_ledger fetch-prices \
  --start 2026-04-01 \
  --end 2026-05-25 \
  --markets CN_A \
  --include-benchmarks \
  --adjust qfq
python -m alpha_ledger fetch-events --start 2026-04-01 --end 2026-05-15 --markets CN_A --skip-money-flow
```

生成某天候选和每日计划：

```bash
python -m alpha_ledger screen --as-of 2026-05-15
python -m alpha_ledger confirm-candidates --as-of 2026-05-18
python -m alpha_ledger daily-plan --as-of 2026-05-15
```

历史回放并为 A 股候选补充分时数据：

```bash
python -m alpha_ledger replay \
  --start 2026-05-15 \
  --end 2026-05-15 \
  --through 2026-05-25 \
  --benchmark 000300.SS \
  --fetch-cn-a-intraday
```

验证评分、审计策略、组合回测：

```bash
python -m alpha_ledger score-calibration --start 2026-04-01 --end 2026-05-15 --through 2026-05-25
python -m alpha_ledger audit --as-of 2026-05-25
python -m alpha_ledger portfolio-backtest --start 2026-04-01 --end 2026-05-15 --through 2026-05-25 --benchmark 000300.SS
python -m alpha_ledger walk-forward --start 2026-04-01 --end 2026-05-25
```

自检：

```bash
python -m alpha_ledger verify
python -m unittest discover -s tests -v
```

## 当前策略

只保留已经有筛选器或事件映射器的策略：

正式 A 股策略：

- `trend_breakout`：强趋势突破
- `abnormal_volume_small_midcap`：中小盘异常放量异动
- `a_share_hard_event_catalyst`：A 股硬事件催化
- `xingye_style_prepositioning`：兴业科技型重估埋伏启动

实验保留策略，不进入当前正式收益结论：

- `us_sec_event_momentum`：美股 SEC 重大披露后动量
- `us_news_event_momentum`：美股新闻评级事件动量
- `hk_buyback_recovery`：港股回购修复
- `hk_southbound_recovery`：港股南向资金修复
- `hk_news_recovery`：港股业绩新闻修复

未实现筛选器的旧设想不进入默认策略库，避免“看起来有策略，实际上没产出”的污染。

## 数据和输出

- `data/universe/default_universe.csv`：默认股票池。
- `data/reference/xingye_002674_20260401_20260525.csv`：兴业科技案例行情参考。
- `data/events/us_hk_events_template.csv`：美股/港股事件导入模板。
- `data/alpha_ledger.sqlite`：本地运行账本，属于可再生成/可更新数据。
- `reports/*.md`：报告输出物，不作为源文档维护。

## 原则

Alpha Ledger 不保证任何股票上涨。它的价值是把每次候选变成可复盘样本，并用净收益、回撤、止损率、目标命中率和评分校准结果逼迫策略进化。

名字不是 alpha。只有在回放和样本外表现里持续赚钱的筛选逻辑，才值得提高权重。

当前最重要的判断口径是组合层面的净收益、最大回撤、换手率、止损率和分数校准，而不是候选数量。
在复权覆盖率和基准覆盖率不足时，报告会生成，但不能视为正式 alpha 结论。
