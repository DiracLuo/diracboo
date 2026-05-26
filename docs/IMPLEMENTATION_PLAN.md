# 当前实现概览

本文件记录当前项目形态，不再保留早期 v0.1/v0.2 待办式计划。

## 目标

Alpha Ledger 的目标是持续找出更可能赚钱的股票候选，而不是维护一个好看的系统。

当前正式研究主线是 A 股。美股/港股保留为可扩展实验能力，但在数据源、事件源和执行规则达到同等可信度前，不进入正式每日买入建议、组合收益和策略权重结论。

当前实现遵循三段式闭环：

1. 事前：策略筛选器在历史某日只使用当时可见数据生成候选。
2. 事中：候选进入确认、取消、观察或可操作状态。
3. 事后：用后续真实行情验证收益、回撤、止损、止盈和评分有效性。

## 主账本

当前主账本是候选账本：

- `candidates`
- `candidate_evaluations`
- `candidate_horizon_evaluations`

`signals` 表保留为兼容手工研究笔记的结构，但默认种子不再写入手工信号，项目报告和审计也不再依赖它。

`strategies.version` 记录当前策略参数版本；报告会显示 `strategy_id@version`，避免调参后混淆历史样本。

## 核心命令

```bash
python -m alpha_ledger fetch-prices --start 2026-04-01 --end 2026-05-25 --markets CN_A --include-benchmarks --adjust qfq
python -m alpha_ledger screen --as-of 2026-05-15
python -m alpha_ledger daily-plan --as-of 2026-05-15
python -m alpha_ledger replay --start 2026-05-15 --end 2026-05-15 --through 2026-05-25 --benchmark 000300.SS --fetch-cn-a-intraday
python -m alpha_ledger score-calibration --start 2026-04-01 --end 2026-05-15 --through 2026-05-25
python -m alpha_ledger portfolio-backtest --start 2026-04-01 --end 2026-05-15 --through 2026-05-25 --benchmark 000300.SS
python -m alpha_ledger walk-forward --start 2026-04-01 --end 2026-05-25
```

## 回放逻辑

- 候选生成只读取候选日及以前数据。
- 财务指标必须满足披露日约束。
- 候选日不假设能以收盘价成交。
- 有分时数据时，A 股按次一交易日开盘前 5 根 K 线 VWAP 入场。
- A 股遵守 T+1，买入日不触发退出。
- 有分时数据时按分时顺序判断止损/止盈；无分时才回退日线。
- 固定周期验证只统计走满 T+5 / T+10 / T+20 / T+60 的样本。
- 组合回测默认只统计正式市场 `CN_A`，并跳过零成交/停牌样本和一字涨停无法买入样本。
- 所有正式收益指标优先使用前复权价格计算，并扣除交易成本。
- 成交价仍保留原始价格，便于检查买入/卖出是否合理。
- A 股默认以沪深 300 `000300.SS` 计算基准收益、超额收益和超额胜率。
- 缺复权或缺基准时允许生成报告，但不得视为正式 alpha 结论。

## 策略进化

策略按以下指标竞争：

- 平均净收益
- 平均基准收益
- 平均超额收益
- 超额胜率
- 净胜率
- 止损率
- 目标触达率
- 最大浮盈 MFE
- 最大回撤 MAE
- 分数校准结果
- 组合回测表现

样本不足时只能观察，不提高权重。连续表现差的策略应收紧、降权或移除。

## 已落实的正确性约束

- 交易成本只以 `metrics.trade_cost_pct()` 为统一来源，数据库回填、候选评估和组合回测保持一致。
- `portfolio-backtest --cost-bps` 只作为显式自定义覆盖；默认使用市场成本。
- 候选盈亏比要求 `stop_loss > 0` 且入场价高于止损价，避免 0 止损导致风报比失真。
- 策略表记录版本号，回放报告输出 `strategy_id@version`。
- 回放报告加入分数校准和数据质量审计，用于判断高分是否真的更赚钱、分时数据是否足够可信。
- `price_bars` 增加 `adj_open/adj_high/adj_low/adj_close/adj_factor/adjustment_status`，A 股日线优先写入前复权价格。
- `candidate_evaluations` 和固定周期评估增加 `benchmark_return_pct`、`excess_return_pct`、`benchmark_ticker`。
- 组合回测输出沪深 300 同期收益、主动收益、基准回撤和单笔超额表现。
- `walk-forward` 已有 readiness 框架；当前样本少于 120 个交易日、50 笔组合交易且复权覆盖不足时输出 `INSUFFICIENT_HISTORY`，不做自动调参。
