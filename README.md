# Alpha Ledger

Alpha Ledger 是一个以盈利验证为中心的 A 股量化研究账本。

它不是自动交易机器人，也不是泛资讯股票平台。项目主线是：从本地数据仓库中产生候选信号，经过统一准入、决策输出、信号跟踪和回测评估，持续验证哪些策略和模型真的能在 A 股中创造 alpha。

产品架构以 [PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md) 为准。

## 当前定位

当前正式赚钱主线只做 A 股 `CN_A`。

美股、港股和 Kronos 等外部研究方向保留为实验分支，但不进入正式收益结论、每日买入清单和策略权重建议，除非后续证明有稳定样本外增量 alpha。

## 产品模块

Alpha Ledger 当前按七个产品模块理解和拆分工作：

1. 数据资产模块
2. 信号生产模块
3. 信号准入模块
4. 决策输出模块
5. 信号跟踪模块
6. 回测评估模块
7. 策略模型治理模块

产品模块架构图见 [docs/assets/alpha-ledger-product-architecture.png](docs/assets/alpha-ledger-product-architecture.png)。

## 核心账本

- `candidates`：主候选账本，只记录策略或模型在当时可见数据下产生的候选。
- `model_scores`：Qlib 等模型预测分数，作为独立证据，不直接等同买入信号。
- `candidate_evaluations`：候选后验评估。
- `candidate_horizon_evaluations`：T+5 / T+10 / T+20 / T+60 固定周期评估。

主观灵感、朋友提示和新闻印象不能直接写入主账本，必须先转化为可回放规则或研究配置。

## 当前使用流程

日常使用优先看每日交易计划，而不是在多个中间报告里手工挑股票。

唯一推荐生产入口：

```bash
python -m alpha_ledger production-run --as-of YYYY-MM-DD
```

`production-run` 会按固定顺序完成核心数据快路径、除权断点检测、前复权因子快修、数据审计、Qlib 增量刷新、Production 模型预测、1 分钟分时复核和正式日报生成。不要用 `daily-plan`、`daily-run` 或旧脚本替代生产入口。

核心数据快路径使用 AKShare/Sina spot 快照，重复运行会覆盖当天记录，并写入接口返回的有效字段：原始 OHLCV、成交额、昨收、涨跌幅、涨跌额、买入/卖出价和时间戳。

每日决策优先阅读：

1. `reports/production/daily/daily_plan_<date>.md`
2. 信号跟踪摘要（后续待落地）
3. 组合回测报告
4. 策略审计与 score calibration
5. loss review

时间点规则：

- `--as-of` 表示数据截止日，不表示自然日今天。
- `data_as_of_date` 是最后一个完整入库交易日。
- `trade_plan_date` 是数据截止日之后的下一个 A 股交易日。
- 如果 `as_of` 晚于本地最新行情日，报告必须标记 `STALE_DATA`，不得输出可买清单。

## 研究流程

研究主要发生在信号生产模块，核心入口是策略模型库。

新策略、新模型或组合规则应先作为研究配置运行：

1. 产生实验信号。
2. 复用生产系统的信号准入规则。
3. 复用统一回测评估和分层基准。
4. 做分数校准、亏损归因和样本外验证。
5. 达标后进入策略模型治理流程。
6. 通过治理后再进入生产配置。

生产 Pipeline 是每天赚钱用的；研究 Pipeline 是发现未来赚钱逻辑用的。两者账本隔离，但准入、回测和评估口径统一。

## 常用命令

```bash
# 生产主流程：唯一推荐入口
python -m alpha_ledger production-run --as-of YYYY-MM-DD

# 生产底层命令：仅用于排障或手工分步执行，日常不要替代 production-run
python -m alpha_ledger data-update --as-of YYYY-MM-DD --markets CN_A --core-only --adjust none
python -m alpha_ledger detect-adjustment-breaks --as-of YYYY-MM-DD
python -m alpha_ledger qfq-repair-daily --as-of YYYY-MM-DD
python -m alpha_ledger data-audit --start YYYY-MM-DD --end YYYY-MM-DD --markets CN_A --ignore-adjustment-for-short-term
python -m alpha_ledger qlib-refresh --as-of YYYY-MM-DD --mode incremental
python -m alpha_ledger model-predict --as-of YYYY-MM-DD --models production
python -m alpha_ledger production-daily --as-of YYYY-MM-DD

# 生产慢任务：日报后异步补充，不阻塞正式日报
python -m alpha_ledger production-async --as-of YYYY-MM-DD

# 定期维护：每周或半月补漏前复权因子
python -m alpha_ledger qfq-maintenance --as-of YYYY-MM-DD --mode scan-and-repair --lookback-days 60 --force

# 研究/旧入口：不是正式生产入口，不要给生产日报使用
python -m alpha_ledger screen --as-of YYYY-MM-DD
python -m alpha_ledger confirm-candidates --as-of YYYY-MM-DD
python -m alpha_ledger daily-plan --as-of YYYY-MM-DD

# 回测、验证与治理
python -m alpha_ledger replay --start <START> --end <END> --through <THROUGH> --benchmark auto
python -m alpha_ledger portfolio-backtest --start <START> --end <END> --through <THROUGH> --benchmark auto
python -m alpha_ledger score-calibration --start <START> --end <END> --through <THROUGH>
python -m alpha_ledger loss-review --start <START> --end <END> --through <THROUGH>
python -m alpha_ledger model-governance review --as-of YYYY-MM-DD
```

## 文档索引

- [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md)：产品架构主文档，后续产品和研发取舍以它为准。
- [docs/DATA_MODEL_PIPELINE_DESIGN.md](docs/DATA_MODEL_PIPELINE_DESIGN.md)：数据层、Qlib 数据流、模型策略和生产门禁设计共识。
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)：当前实现边界、已落实能力和剩余代办。
- [docs/STRATEGY_LIBRARY.md](docs/STRATEGY_LIBRARY.md)：策略说明。
- [docs/qlib_bridge.md](docs/qlib_bridge.md)：Qlib 数据导出、训练和预测导入说明。
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)：数据源说明。
- [docs/CASE_XINGYE_002674.md](docs/CASE_XINGYE_002674.md)：兴业科技案例。

## 原则

Alpha Ledger 不保证任何股票上涨。它的价值是把每次候选变成可复盘样本，并用净收益、超额收益、最大回撤、止损率、分数校准和组合表现逼迫策略进化。

名字不是 alpha。只有在回测、跟踪和样本外表现里持续赚钱的筛选逻辑，才值得进入生产配置。
