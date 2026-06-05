# Alpha Ledger 当前实现与后续待办

本文件记录当前实现边界和近期待办，不再作为产品架构来源。产品方向、模块边界和流程设计以 [PRODUCT_ARCHITECTURE.md](PRODUCT_ARCHITECTURE.md) 为准。

## 1. 当前实现边界

当前正式主线是 A 股 `CN_A`。

美股、港股和 Kronos 等外部研究能力保留为实验分支，不进入正式日报、正式组合收益和策略权重结论，除非后续通过样本外验证并进入生产配置。

## 2. 已落地能力

### 数据资产模块

- 本地 SQLite 数据仓库：`data/alpha_ledger.sqlite`。
- A 股日线、分时、分层基准、事件、财报和数据质量相关表。
- A 股 ticker 规范化：内部统一 `.SS` / `.SZ` / `.BJ`。
- 前复权因子化：原始 OHLCV 不改，`pre_close` 用于除权断点检测，`adj_factor` 是复权主数据，`adj_*` 仅作为兼容字段。
- 数据更新、审计、补库、除权断点检测、QFQ 因子维护、成交额和换手率补充命令。

### 信号生产模块

- 策略筛选器可生成候选并写入 `candidates`。
- Qlib 预测分可导入 `model_scores`，并作为候选独立证据。
- 正式 A 股策略和实验策略由策略库维护，未验证策略不得直接进入正式结论。

### 信号准入模块

- 已有数据质量、交易可行性、盈亏比、重复信号、市场范围等过滤逻辑。
- 当前状态命名仍在代码中逐步向产品文档统一，后续应收敛到：
  - `RAW_CANDIDATE`
  - `REJECTED_BY_GATE`
  - `WATCHLIST`
  - `ACTIONABLE_SIGNAL`
  - `PLAN_PRIMARY`
  - `PLAN_BACKUP`

### 决策输出模块

- 正式生产入口是 `production-run`，它编排数据快路径、复权因子快修、审计、Qlib 增量刷新、Production 模型预测、1 分钟分时复核和正式日报。
- `production-daily` 是只读日报生成器，只读取已经准备好的数据和预测。
- `daily-plan` / `daily-run` 仅作为旧/研究入口保留，不作为正式生产路径。
- `--as-of` 表示数据截止日，不表示自然日今天。
- `STALE_DATA` 时不得输出可买清单。
- 当前日报仍需按产品文档继续收敛为更稳定的第一屏结构。

### 回测评估模块

回测评估是一个模块，包含三个层次：

- 信号级：固定 T+5 / T+10 / T+20 / T+60。
- 路径级：按入场、止损、止盈和持有到期模拟交易路径。
- 组合级：资金曲线、最大回撤、仓位、换手和组合 alpha。

已落实的关键口径：

- 候选生成只使用候选日及以前可见数据。
- 财务指标必须满足披露日约束。
- 候选日不假设能以收盘价成交。
- A 股遵守 T+1，买入日不触发退出。
- 有分时数据时按分时路径判断止损/止盈。
- 无分时时回退日线，并应在报告中标记。
- 涨停买不进，跌停卖不出时延期退出。
- 收益扣除交易成本。
- 默认计算分层基准收益和超额收益。

### 策略模型治理模块

已具备以下验证和治理入口：

- `score-calibration`
- `loss-review`
- `audit`
- `validate`
- `walk-forward` readiness 检查

样本不足时只能观察，不得自动升权。连续窗口表现差的策略应收紧、降权或移除。

## 3. 当前主账本

- `candidates`：主候选账本。
- `candidate_evaluations`：候选后验验证。
- `candidate_horizon_evaluations`：固定周期验证。
- `model_scores`：模型预测分。

`signals` 表仅作为兼容结构保留，默认产品流程不依赖手工信号。

## 4. 常用验收命令

```bash
python -m unittest discover -s tests -v

python -m alpha_ledger data-audit \
  --start <START> \
  --end <END> \
  --markets CN_A

python -m alpha_ledger production-run --as-of <DATE>

python -m alpha_ledger production-async --as-of <DATE>

python -m alpha_ledger detect-adjustment-breaks --as-of <DATE>

python -m alpha_ledger qfq-repair-daily --as-of <DATE>

python -m alpha_ledger qfq-maintenance \
  --as-of <DATE> \
  --mode scan-and-repair \
  --lookback-days 60 \
  --force

python -m alpha_ledger model-governance review --as-of <DATE>

python -m alpha_ledger replay \
  --start <START> \
  --end <END> \
  --through <THROUGH> \
  --benchmark auto

python -m alpha_ledger portfolio-backtest \
  --start <START> \
  --end <END> \
  --through <THROUGH> \
  --benchmark auto
```

## 5. Phase 1 剩余待办

本轮产品收敛后的代码主线是 `production-run`。以下事项为后续继续增强项：

### 5.1 固化日报第一屏结构

目标：让用户每天先看到一个稳定、低噪音、可执行的交易计划。

待明确：

- 今日是否适合交易。
- 数据截止日和计划交易日。
- 主选和备选。
- 买入条件、止损、目标、失效条件。
- 信号跟踪摘要。

### 5.2 继续增强标准生产 Pipeline

目标：避免漏更新价格、基准、模型预测或准入状态。

产品文档定义的生产 Pipeline 为：

```bash
python -m alpha_ledger production-run --as-of YYYY-MM-DD
```

它负责快速数据更新、数据审计、Qlib 增量刷新、Production 模型预测和只读日报生成。

当前生产链路还包括：`pre_close` 除权断点检测、`qfq-repair-daily` 复权因子快修、信号池 1 分钟分时复核和分时结论生成。其中模型预测刷新必须进入日报前置流程。每天不一定重训模型，但必须确认当天 `model_scores` 是最新可用状态。`daily-plan` / `daily-run` 仅作为旧/研究入口，不再作为正式生产路径。

### 5.3 拆分快任务和慢任务

目标：收盘后先生成可靠日报，不被事件、公告、财报、资金流等慢接口阻塞。

建议拆分：

- 快任务：价格、`pre_close`、`amount`、基准、交易状态、复权因子快修、数据审计、模型预测、筛选、信号池 1 分钟分时复核、准入、日报。
- 慢任务：事件、公告、财报、资金流、周期复权维护、模型训练、深度复盘。

### 5.4 信号跟踪模块

目标：让用户看到系统过去选出的股票后来怎么走，同时避免观察池过载。

规则以产品文档为准：

- `ACTIONABLE_SIGNAL` 全部进入信号跟踪。
- `PLAN_PRIMARY` 重点跟踪。
- `PLAN_BACKUP` 低优先级跟踪。
- `WATCHLIST` 不默认进入核心跟踪，只进入观察队列或研究记录。
- `REJECTED_BY_GATE` 只记录拒绝原因。

## 6. 不再重复维护的内容

以下内容不在本文件展开，避免多处文档互相打架：

- 产品模块架构：见 `docs/PRODUCT_ARCHITECTURE.md`。
- 策略细节：见 `docs/STRATEGY_LIBRARY.md`。
- Qlib 导出、训练和预测导入：见 `docs/qlib_bridge.md`。
- 数据源细节：见 `docs/DATA_SOURCES.md`。
- 运行报告：见 `reports/*.md`，但报告不是长期产品设计依据。
