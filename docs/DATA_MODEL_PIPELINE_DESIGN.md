# 数据与模型 Pipeline 指导文档

> 目标：统一 Alpha Ledger 的数据层、Qlib 数据流、模型策略层和生产日报门禁。后续 Agent 做数据更新、模型训练、预测、日报前置流程时，以本文档为准。

## 1. 基本原则

Alpha Ledger 的生产链路：

```text
核心数据可信 -> 复权因子维护 -> Qlib 数据更新 -> 模型预测成功 -> 信号生成与准入 -> 正式日报
```

唯一推荐生产命令：

```bash
python -m alpha_ledger production-run --as-of YYYY-MM-DD
```

规则：

- `production-run` 是唯一推荐生产入口。
- `production-daily` 是只读日报生成器，不默认更新数据、不刷新 Qlib、不跑模型。
- `daily-plan` / `daily-run` 是旧/研究入口，不用于正式生产。
- 数据源降级是“换备用源”，不是填 0、估算值或旧数据冒充正式数据。
- 模型预测失败时，不生成正式日报，必须输出失败提示。
- 快数据先保证日报，慢数据异步补，不阻塞核心流程。
- 字段不完整的股票不进入最终正式买入清单，可进入异常说明。

## 2. 生产流程与失败处理

```text
确认数据截止日
-> 拉取核心行情
-> 检测除权断点并快速修复复权因子
-> 检查数据覆盖率
-> 更新 Qlib 数据
-> 跑生产模型预测
-> 检查预测成功
-> 生成候选信号
-> 检查信号可操作性
-> 生成日报
-> 异步补慢数据
```

对应命令由 `production-run` 编排，底层步骤为：

```text
data-update --core-only --adjust none
-> detect-adjustment-breaks
-> qfq-repair-breaks
-> data-audit
-> qlib-refresh --mode incremental
-> model-predict --models production
-> screen-all + data-update (1m intraday) + refine-candidates-with-intraday
-> production-daily
```

生产流程中分时数据仅作为执行上下文：

- `production-run` 信号分时复核步骤拉取 1 分钟 K 线（`intraday_period='1'`），用于 VWAP 支撑、尾盘强度、高点回撤和弱势收盘等简易日内结论。
- `production-async` 不再拉取分时数据，仅补充事件、财务和资金流。
- 分时数据不改变选股逻辑，不用于模型训练或预测。

| 阶段 | 处理 |
|---|---|
| 数据接口失败 | 尝试备用源；仍失败则记录缺失和覆盖率 |
| 核心数据不达标 | 不生成正式买入计划 |
| Qlib 更新失败 | 本次生产日报失败 |
| 模型预测失败 | 不生成正式日报，输出失败提示 |
| 慢任务失败 | 记录失败，不阻塞主流程 |

## 3. 数据层

### 3.1 快路径与慢路径

| 类型 | 内容 | 处理 |
|---|---|---|
| 快路径 | 交易日历、当日快照 OHLCV、pre_close、amount、分层基准、除权断点检测、前复权断点回补、Qlib 数据、生产模型预测、日报 | 每天收盘后同步完成 |
| 慢路径 | 事件、财报、资金流、成交额/换手历史修复、北向/融资融券、复权补漏校验 | 异步补充，不阻塞日报 |

### 3.2 数据源优先级

| 场景 | 主源 | 备用/修复源 |
|---|---|---|
| 当日快路径 OHLCV + amount | AkShare `stock_zh_a_spot()` / 新浪快照 | 同花顺 JS 解析 |
| 历史日线 OHLCV | 现有新浪历史接口 | BaoStock / 其他可用源 |
| 历史 amount / turnover 修复 | BaoStock | 同花顺 JS 解析 |
| 分层基准 | 当前指数行情源 | 后续按可用性补充 |

当日快路径优先使用 AkShare 的新浪全市场快照接口。BaoStock 不作为每日快路径主源，只用于历史补库、校验和缺口股票逐只修复。同花顺 JS 解析是明确备用源，用于主源失败、限流或 IP 风险时切换。

`amount` 是核心字段，影响流动性筛选、Qlib `money/vwap`、交易执行估算和模型特征质量，不能当成可选字段。

禁止：`amount` 缺失填 0、旧数据冒充当天数据、估算字段冒充正式字段。

允许：备用源成功后写入正式数据并记录 `source=backup`；估算字段只能标记 `ESTIMATED`，不作为硬门禁字段。

### 3.3 Readiness Gate

| 项 | 门槛 |
|---|---:|
| 交易日历 | 100% 正确 |
| 分层基准 | 100% 可用 |
| 全市场 amount 覆盖率 | >= 95% |
| 当日 OHLCV 覆盖率 | 建议 >= 98% |
| 生产模型预测 | 当天必须成功 |
| 最终入选信号关键字段 | 100% 完整 |

### 3.4 前复权处理

每日快路径保存原始 OHLCV、`pre_close`、`change_pct` 和 `amount`。原始价格永远不改；前复权以 `adj_factor` 为主数据，使用时统一计算 `qfq_price = raw_price * adj_factor`。

前复权用于历史收益计算、长期回测、模型训练数据连续性、跨除权窗口评估和策略升权验证。

复权处理路径：

```text
每日快路径：
拉 AKShare/Sina spot 快照行情。有效字段直接写入 price_bars，包括 open/high/low/close/volume/amount/pre_close/change_pct/change_amount/bid_price/ask_price/quote_time。当天 spot 字段是最新交易日的权威字段，重复运行应 upsert 覆盖当日数据。

每日断点检测：
detect-adjustment-breaks 对比 pre_close 与上一交易日原始 close，发现除权断点后写入 adjustment_maintenance_queue。

每日断点回补：
qfq-repair-breaks 只读取当天 CONFIRMED_BY_PRECLOSE 队列股票，逐只用 BaoStock qfq 拉取 2024-01-01 至 as_of 的前复权价格，只刷新 adj_* / adj_factor / adjustment_status 等复权字段，不修改原始 open/high/low/close/volume/amount/pre_close/change_pct。

定期审计：
qfq-maintenance --mode scan-and-repair 扫描近期断点；BaoStock 只作为疑似样本、失败样本和历史补漏的校验源。

训练/长回测前检查：
检查复权覆盖率和 adjustment_status，不达标则先跑 qfq repair。
```

复权状态：

| 状态 | 含义 |
|---|---|
| ADJUSTED | 已维护有效 adj_factor，可通过 raw × adj_factor 得到前复权价格 |
| RAW_FALLBACK | 暂无前复权，短线研究可用但要标注 |
| UNKNOWN | 状态不明，不应进入正式长期结论 |

短线日报不因复权缺失阻断，但必须在数据质量摘要中展示复权状态。长期回测、策略升权、长周期模型验证和跨除权窗口评估，应优先使用复权数据。

### 3.5 Qlib 数据版本

`qlib_dataset_version` 是 Qlib 可读数据环境的版本标识，不是预测结果版本。

至少记录：

```text
provider_uri、覆盖日期、字段集合、股票池、来源数据库、生成时间、生成方式
```

第一阶段继续使用：

```text
SQLite price_bars -> staging CSV/Parquet -> qlib dump_update -> Qlib bin
```

优先做增量化、版本记录和运行记录，不优先重写 SQLite 直写 Qlib bin。

## 4. 模型策略层

### 4.1 Qlib 定位

Qlib 是模型工厂，不是 alpha 本身。Alpha158 / Alpha360 主要使用 `open / high / low / close / vwap / volume`。公开量价数据可做 baseline，但真正的可交易 alpha 还依赖数据质量、A 股特色数据、标签设计、样本外验证、执行和组合约束。

### 4.2 第一版模型竞技池

第一版保留 18 个模型：`3 个起训时间 × 2 组特征 × 3 个 label`。

| 维度 | 取值 |
|---|---|
| 起训时间 | 2024 起、2025 起、2026 起 |
| 特征 | Alpha158、Alpha360 |
| Label horizon | T+2、T+5、T+10 |

全量历史模型和近周期模型都保留，通过测试和跟踪竞争。

### 4.3 Label 设计

第一版主竞技 label：`next_open_to_close_T2 / T5 / T10`。

含义：`T 日生成信号，T+1 开盘买入，T+N 收盘卖出`。

可保留少量 close-to-close baseline 做对照。

Qlib 简单 label 只看终点收益，不看中间路径。真实交易收益由 Alpha Ledger 路径回测验证。第二版再研究 MFE、止盈止损路径收益等 path-aware label。

### 4.4 验证路径与 test 口径

```text
Train / Valid / Test -> Walk-forward -> Paper trading / 信号追踪
```

当前上线前硬门槛：

```text
固定 Train / Valid / Test
Lite Walk-forward
```

Paper trading 不作为当前上线前硬门槛，但信号追踪模块会持续反馈真实表现。

Test 只评估 label 已成熟日期：

```text
test_eval_end = latest_price_date 往前推 horizon 个交易日
```

最新但 label 未成熟的预测，只进入生产预测或信号追踪，不计入 test 绩效。

Lite Walk-forward 使用季度滚动窗口，覆盖 Alpha158 / Alpha360、2024 / 2025 / 2026 起训、T+2 / T+5 / T+10。

### 4.5 模型状态

| 状态 | 用途 |
|---|---|
| RESEARCH | 只用于研究报告 |
| CANDIDATE | 可在日报展示参考，不参与正式排序和入选 |
| PRODUCTION | 可参与正式买入信号排序 |
| PAUSED | 暂停使用，等待人工复核 |
| RETIRED | 淘汰，不再使用 |

多个 Production 模型暂不强制系统融合。日报逐个列出模型分数，用户重点关注多模型共识，但系统不强制要求共识才能入选。

生产路径只读取 `model_registry.status = 'PRODUCTION'`。Retired/legacy 模型不得作为 PEAD、日报、模型选股或生产排序 fallback。

命令边界：

| 命令 | 定位 |
|---|---|
| `production-run` | 唯一推荐生产入口 |
| `production-daily` | 只读日报生成器 |
| `detect-adjustment-breaks` | 生产/维护共用的除权断点检测 |
| `qfq-repair-breaks` | 每日生产前复权断点回补，只处理 CONFIRMED_BY_PRECLOSE 队列股票 |
| `qfq-maintenance --mode scan-and-repair` | 周期复权补漏维护 |
| `model-predict` | Production 模型推理，不训练 |
| `model-arena` | 研究线模型竞技，不写正式生产分数 |
| `model-governance review` | 衰退预警和人工复核提示，不自动下线 |

### 4.6 模型指标

核心指标：样本数、Coverage、IC、Rank IC、ICIR / Rank ICIR、Top 分组收益、Top 分组胜率、平均超额收益、超额胜率、Top-Bottom Spread、最大回撤、Turnover、稳定性。

Candidate 阶段第一屏重点：样本数、胜率、平均收益、平均超额收益、超额胜率、是否跑赢当前线上模型。

Production 替换原则：

```text
新模型必须证明比当前线上模型更赚钱、更稳定，或提供互补信号。
```

衰退预警：

```text
最近两周或最近 20 笔信号触发人工复核，不自动降权或下线。
```

## 5. 后续落地路线

```text
Phase 1：生产数据快路径
- AkShare 新浪快照补当日 OHLCV + amount。
- BaoStock 只做历史修复和缺口补齐。
- 建立覆盖率审计和失败提示。

Phase 2：Qlib 与模型运行
- Qlib 增量更新。
- qlib_dataset_version。
- model registry / prediction_run。
- 生产模型失败时生成异常报告。

Phase 3：模型竞技与验证
- 18 模型竞技池。
- next_open_to_close_T2/T5/T10 label。
- 固定 test 成熟标签口径。
- 每个模型必须补齐常规 Train/Valid/Test 验证。
- 每个候选模型必须补齐历史 Walk-forward 验证；它属于研究线，不等同于生产分数追踪。

Phase 4：生产治理
- Candidate / Production 状态隔离。
- 生产日报只使用有效 prediction_run。
- 模型衰退预警和人工复核。
```
