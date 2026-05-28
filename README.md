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
- `daily-plan`：当天可操作清单，分为"今日新信号"（当天数据筛选）、"今日确认"（往日信号+当天确认）、"等确认"和"观察池"。
- `replay`：历史日期逐日筛选并用未来行情验证。
- `portfolio-backtest`：组合层面回测，控制仓位、持仓冷却和交易成本。确认类候选跳过 RRR 过滤。
- `validate`：统计验证，含 Monte Carlo 置换检验、Bootstrap Sharpe 置信区间、Walk-Forward 窗口分析。
- `walk-forward`：检查当前样本是否足够做正式滚动验证；样本不足时只输出 `INSUFFICIENT_HISTORY`。
- `data-update` / `data-audit`：维护本地 A 股增量数据仓库，并审计数据是否足以支持正式结论。
- `loss-review`：复盘亏损样本，把失败模式转成后续过滤器。

A 股回放优先使用 5 分钟线：

- 入场：候选日后第一个交易日开盘前 5 根 K 线 VWAP。
- 退出：有分时数据时按分时止损/止盈顺序；没有分时时回退日线。
- T+1：A 股买入日不允许同日退出。
- 成本：评估收益使用净收益，默认按市场统一成本扣除；A 股往返成本为 0.18%。
- 复权：收益统计优先使用前复权价格；成交价仍保留原始价格用于执行展示。
- 基准：A 股默认用沪深 300（`000300.SS`）计算基准收益和超额收益。
- 分层基准：候选级评估支持按板块映射沪深300、中证500、中证1000、创业板指、科创50、北证50。
- 约束：组合回测会跳过零成交/停牌样本和一字涨停无法买入样本；跌停无法退出时延期到下一可卖日。
- 风控：支持 ATR 动态止损、盈亏比过滤、按市值盯市回撤和 `portfolio-backtest --sizing-mode risk-parity` 风险平价仓位。

日报时间点：

- `daily-run --as-of` 表示数据截止日，收盘后运行。数据拉取、筛选、确认、报告均基于截止日数据。
- 日报候选分四类：”今日新信号”（当天筛选的 BUY_CANDIDATE）、”今日确认”（往日信号当天确认）、”等确认”（高分待确认）、”观察池”。
- 若 `as_of` 晚于 `price_bars` 最新行情日，日报标记 `STALE_DATA`，不生成可操作清单。
- 若数据审计不是 `HIGH_CONFIDENCE`，日报只展示观察/确认候选，不输出强买入结论。
- A 股审计优先用沪深300行情日作为交易日历，避免把春节、五一等休市日误判为缺口。

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

每日增量数据仓库和审计：

```bash
python -m alpha_ledger data-update --as-of 2026-05-27 --markets CN_A --adjust none
python -m alpha_ledger data-audit --start 2026-04-01 --end 2026-05-27 --markets CN_A
python -m alpha_ledger daily-run --as-of 2026-05-27
```

修复已有历史库里的覆盖缺口时，显式使用 `--repair-coverage`。默认 `--repair-scope benchmarks` 只补分层基准，避免误触发全市场复权重拉：

```bash
python -m alpha_ledger data-update \
  --as-of 2026-05-27 \
  --markets CN_A \
  --repair-coverage \
  --repair-scope benchmarks \
  --skip-events \
  --skip-intraday
```

复权接口是否可用先用小样本探测，不要直接假设“拿不到”：

```bash
python -m alpha_ledger data-audit \
  --start 2026-04-01 \
  --end 2026-05-26 \
  --markets CN_A \
  --probe-adjustment \
  --ignore-adjustment-for-short-term
```

短线 T+5/T+10 研究允许 `RAW_FALLBACK`，但只能作为未复权短线研究口径，置信度最高为 `MEDIUM_CONFIDENCE`，不能进入正式升权或长期 walk-forward 结论。

历史补库按批次运行，避免每次回测临时拉接口：

```bash
python -m alpha_ledger data-backfill --start 2025-12-01 --end 2026-05-26 --markets CN_A --batch-days 180 --adjust none --throttle 0.3
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

验证评分、审计策略、组合回测、统计验证：

```bash
python -m alpha_ledger score-calibration --start 2026-04-01 --end 2026-05-15 --through 2026-05-25
python -m alpha_ledger audit --as-of 2026-05-25
python -m alpha_ledger portfolio-backtest --start 2026-04-01 --end 2026-05-15 --through 2026-05-25 --benchmark 000300.SS
python -m alpha_ledger validate --start 2026-04-01 --end 2026-05-15 --through 2026-05-25
python -m alpha_ledger walk-forward --start 2026-04-01 --end 2026-05-25
python -m alpha_ledger loss-review --start 2026-04-01 --end 2026-05-15 --through 2026-05-25
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

未实现筛选器的旧设想不进入默认策略库，避免”看起来有策略，实际上没产出”的污染。

## 量价因子

`screener.py` 在筛选后调用 `alpha_factors.py` 对候选做跨截面因子排名，调整候选分数（+/-8 分）。当前 6 个因子：

- `breakout_20d`：close / max(close, 20)，越高越接近突破
- `volume_ratio_10d`：volume / avg(volume, 10)，越高越活跃
- `momentum_5d`：5 日收益率
- `momentum_20d`：20 日收益率
- `volatility_20d`：20 日收益标准差（越低越好）
- `price_to_ma20`：close / MA(20)，越高越强势

## 回调确认机制

`trend_breakout` 筛选出涨幅 >=8.5% 的强势股时，标记为 `WATCH_PULLBACK`（暂缓买入）。回调确认机制通过三日形态判断是否可以买入：

1. **回调日（Day T）**：价格回调 5-10%，成交量 < 突破日 50%
2. **企稳日（Day T+1）**：不破 T 日最低价，收阳线
3. **反转日（Day T+2）**：收盘 > T+1 日收盘

三日形态全部满足后，候选升级为 `BUY_CANDIDATE`，entry_price 更新为确认日收盘价。

## 统计验证

`validate` 命令对组合回测结果做三项独立检验：

- **Monte Carlo 置换检验**：打乱交易顺序 1000 次，检验 Sharpe 和最大回撤是否显著优于随机
- **Bootstrap Sharpe 置信区间**：重采样日收益 1000 次，估计 Sharpe 的 95% CI
- **Walk-Forward 窗口分析**：将权益曲线切分为 5 个窗口，计算一致性和跨窗口 Sharpe

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
