# Alpha Ledger — Agent 工作指南

本文件给 Claude Code、Codex 或其他 agent 使用。产品方向、模块边界和演进路线以 [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md) 为准。

## 项目定位

Alpha Ledger 是 A 股量化研究账本，目标是持续验证哪些策略和模型能真正产生可交易 alpha。

不要把它理解成：

- 自动交易机器人。
- 泛股票资讯平台。
- 手工荐股记录本。
- 模型实验堆叠项目。

当前正式市场只做 `CN_A`。美股、港股、Kronos 和其他外部模型路线属于实验分支，不得混入正式日报、正式组合收益和策略权重结论。

## 产品模块

开发和任务拆解按七个模块归类：

1. 数据资产模块
2. 信号生产模块
3. 信号准入模块
4. 决策输出模块
5. 信号跟踪模块
6. 回测评估模块
7. 策略模型治理模块

新增需求先判断归属模块，再判断是否需要跨模块协作。不要为了一个研究想法同时改数据、准入、日报和回测口径，除非任务明确要求。

## 生产与研究

生产 Pipeline 用于每天生成可执行日报，只能使用已批准的策略、模型和参数版本。

唯一推荐生产入口：

```bash
python -m alpha_ledger production-run --as-of YYYY-MM-DD
```

生产 Pipeline 的详细步骤见 [docs/PRODUCT_ARCHITECTURE.md](docs/PRODUCT_ARCHITECTURE.md) 和 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)。

`daily-run`、`daily-plan`、手工 `screen` 不是正式生产入口，只能作为研究或排障命令。

研究 Pipeline 用于探索新策略、新模型或组合规则。研究应优先集中在信号生产模块和策略模型库，后段复用生产系统的准入、回测、分层基准和治理口径。

研究输出必须与生产账本隔离，除非通过样本外验证和策略模型治理。

## 信号状态

保持信号状态统一：

- `RAW_CANDIDATE`：原始候选。
- `REJECTED_BY_GATE`：被准入规则挡掉。
- `WATCHLIST`：观察信号，暂未进入正式可交易。
- `ACTIONABLE_SIGNAL`：通过准入，可交易。
- `PLAN_PRIMARY`：决策输出主选。
- `PLAN_BACKUP`：决策输出备选。

准入决定信号身份，决策输出只做排序、分组、限额和解释。不要在日报渲染阶段再用另一套逻辑把信号改判为无效。

## 技术栈

- Python 3.10+
- SQLite：`data/alpha_ledger.sqlite`
- 入口：`python -m alpha_ledger <command>`
- 数据源：Sina、BaoStock、AkShare、Longbridge、Yahoo/SEC 等按模块使用
- 机器学习：Qlib，预测分写入 `model_scores`
- 测试：`unittest` / `pytest`

## 项目结构

```
alpha_ledger/       Python 包，CLI 入口、核心逻辑
data/               SQLite 数据库、Qlib 导出、universe
docs/               产品架构、数据模型、策略库等文档
outputs/            Qlib 刷新、模型运行中间产物
reports/            生产日报、模型验证、回测报告
scripts/            独立脚本（训练、回补、模型更新）
tests/              测试用例
```

## 常用命令

```bash
# 正式生产主流程
python -m alpha_ledger production-run --as-of YYYY-MM-DD

# 生产底层命令：仅排障或手工分步执行，日常不要替代 production-run
python -m alpha_ledger data-update --as-of YYYY-MM-DD --markets CN_A --core-only --adjust none
python -m alpha_ledger detect-adjustment-breaks --as-of YYYY-MM-DD
python -m alpha_ledger qfq-repair-breaks --as-of YYYY-MM-DD
python -m alpha_ledger data-audit --start YYYY-MM-DD --end YYYY-MM-DD --markets CN_A --ignore-adjustment-for-short-term
python -m alpha_ledger qlib-refresh --as-of YYYY-MM-DD --mode incremental
python -m alpha_ledger model-predict --as-of YYYY-MM-DD --models production
python -m alpha_ledger production-daily --as-of YYYY-MM-DD

# 生产慢任务：日报后异步补充，不阻塞正式日报
python -m alpha_ledger production-async --as-of YYYY-MM-DD

# 定期复权维护：每周或半月补漏
python -m alpha_ledger qfq-maintenance --as-of YYYY-MM-DD --mode scan-and-repair --lookback-days 60 --force

# 回测与验证
python -m alpha_ledger replay --start <START> --end <END> --through <THROUGH> --benchmark auto
python -m alpha_ledger portfolio-backtest --start <START> --end <END> --through <THROUGH> --benchmark auto
python -m alpha_ledger score-calibration --start <START> --end <END> --through <THROUGH>
python -m alpha_ledger loss-review --start <START> --end <END> --through <THROUGH>

# Qlib 研究/维护
python -m alpha_ledger export-qlib-csv --start <START> --end <END> --output data/qlib_export_full --markets CN_A
python -m alpha_ledger import-qlib-predictions --pred-path <pred.pkl> --model-name <MODEL> --model-version <VERSION>
```

## 代码约束

- 不要把手工信号写入 `candidates` 主账本。
- 不要把实验策略收益混入正式结论。
- 不要在数据不足、基准缺失或准入口径不一致时生成正式 alpha 结论。
- 不要随意修改 `data/` 下 SQLite schema；涉及迁移必须先说明方案。
- A 股 ticker 内部统一使用 `.SS` / `.SZ` / `.BJ`，Qlib 边界再做 `SH600519` 这类转换。
- A 股原始 OHLCV 不改；前复权主数据是 `adj_factor`，由 `pre_close` 断点检测和 `qfq-repair-breaks` 快修维护。`adj_*` 只是兼容字段。
- BaoStock qfq 不作为每日生产主路径，只作为 `qfq-maintenance` 的补漏、疑似样本校验和历史维护源。
- A 股收益评估直接使用前复权价格（`adj_close` 等）；`RAW_FALLBACK` 状态的数据仅限短线研究，正式升权受限。
- 交易成本使用统一成本函数，不要在模块内重复硬编码费率。
- 回测评估模块包含信号级、路径级、组合级三层，不要把"回放评估"和"组合回测"拆成互相冲突的口径。

## 文档参考

- `docs/PRODUCT_ARCHITECTURE.md`：产品架构主参考。
- `docs/DATA_MODEL_PIPELINE_DESIGN.md`：数据层、Qlib 数据流、模型策略和生产门禁设计共识。
- `docs/IMPLEMENTATION_PLAN.md`：当前实现边界和剩余代办。
- `docs/STRATEGY_LIBRARY.md`：策略细节。
- `docs/qlib_bridge.md`：Qlib 集成说明。
- `docs/DATA_SOURCES.md`：数据源说明。
- `README.md`：面向用户的项目入口。
