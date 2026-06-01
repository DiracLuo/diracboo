# Alpha Ledger 进展同步文档

> 时间：2026-05-30 ~ 2026-05-31
> 参与方：Codex（架构设计）、Claude（开发实现）

---

## 一、代码改动总览

5 个 commit，22 个文件，+6,370 行：

| Commit | 内容 |
|--------|------|
| `7909e06` | ticker 规范化、QFQ 回填、日线充实、Qlib 导出/导入、6 个新 CLI 命令 |
| `33ba9a1` | change_pct 口径修复（除权日用 adj_close 计算） |
| `1e7d693` | 模型分数接入候选评分（独立维度，不叠加到策略分） |
| `afb9ee0` | 日报表格精简（去掉冗余列，拆分股票名/代码） |
| `62fb542` | 排除指数/ETF 进入筛选（287 只） |

---

## 二、新增模块（6 个）

| 模块 | 行数 | 功能 |
|------|------|------|
| `tickers.py` | 107 | CN_A ticker 规范化（.SH→.SS），Qlib instrument 双向转换 |
| `ticker_repair.py` | 559 | .SH 历史数据审计 + 修复（10 张表扫描，2 张表自动修复） |
| `qfq_backfill.py` | 555 | 前复权回填（BaoStock/AkShare，resume-safe，批量提交） |
| `daily_enrichment.py` | 438 | amount + turnover_pct 充实（BaoStock） |
| `qlib_export.py` | 529 | CSV 导出（Qlib 格式，含质量报告） |
| `qlib_import.py` | 216 | pred.pkl 导入 model_scores 表 |

---

## 三、数据库变更

### 新增表
- `model_scores`：model_name, model_version, market, ticker, score_date, score, rank, percentile

### 新增列
- `candidates`：model_score, model_percentile
- `data_coverage_daily`：intraday_tradable_target_count, intraday_tradable_symbol_count, intraday_tradable_missing_count, intraday_no_trade_symbol_count

### 数据迁移
- `_recompute_change_pct_from_adj_close`：所有 ADJUSTED 行的 change_pct 从 adj_close 重算
- 325 只退市标的 active 标记为 0

---

## 四、数据资产现状

| 数据 | 状态 | 详情 |
|------|------|------|
| price_bars | 3,230,573 行 | 2024-01-02 ~ 2026-05-29，5,762 只 CN_A |
| ADJUSTED 覆盖 | 97.3% | 3,143,822 行，剩余 2.7% 为 BJ（BaoStock 不支持） |
| amount/turnover | 97.2% | 同上，BJ 缺失 |
| intraday_bars | 5,412,978 行 | 2026-04-28 ~ 2026-05-29 |
| model_scores | 440,972 行 | T+5 和 T+10 各 220,486 行 |
| .SH 残留 | 0 | 全部归一为 .SS |
| active instruments | 5,762 | 与 price_bars 一致 |

---

## 五、Qlib 集成

### 完成的步骤
1. ✅ CSV 导出（`data/qlib_export_full/`，5,762 文件，3.23M bars）
2. ✅ dump_bin（`~/.qlib/qlib_data/alpha_ledger_full/`）
3. ✅ Alpha158 模型训练（全市场 5,762 只）
4. ✅ 预测导入 model_scores

### 模型结果

| 模型 | 周期 | 标的 | IC | 状态 |
|------|------|------|-----|------|
| Alpha158 | T+5 | 5,762 | **0.073** | ✅ 主力 |
| Alpha158 | T+10 | 5,762 | 0.034 | ✅ 对照 |
| Alpha158 | T+2 | 300 (CSI300) | 0.033 | 已弃用 |
| Alpha360 | T+2 | 300 | 0.028 | 已弃用 |

### Workflow config
- `workflow_config_alpha360.yaml` — 最终参数（LGB 默认 + 全市场）
- `workflow_config_alpha158.yaml` — 最终参数

---

## 六、change_pct 口径修复

**问题**：原 change_pct 用 raw close 计算，除权日产生假信号（如 600036.SS 除权日 -5.39%，实际 -1.30%）

**修复**：改用 adj_close 计算，与 BaoStock/Tushare/交易所官方口径一致

**影响范围**：market_data.py（所有 fetch 函数）、qfq_backfill.py、db.py（迁移）

---

## 七、日报改进

### 表格精简
去掉：市场、EV、建议仓位、入场参考、禁追价、最晚退出、失效条件
新增：模型分（独立列，显示 percentile）
拆分：股票名/代码为两列，策略去掉版本号

### 模型分数集成
- `alpha_factors.py`：新增 `attach_model_scores()` 函数
- `screener.py`：调用 attach_model_scores，持久化到 candidates 表
- `reporting.py`：所有区域显示双分数（策略分 | 模型分）

---

## 八、筛选改进

- 排除指数/ETF（关键词过滤：指数、ETF、LOF、基金，287 只）
- 移除因子加分层（adjust_candidate_scores 不再调用，Alpha158 已覆盖）

---

## 九、外部工具集成

- Longbridge CLI：已安装、已认证
- Longbridge Skills：126 个 skill 已安装
- Longbridge MCP：已添加、已认证、可正常调用

---

## 十、待办

| 优先级 | 事项 | 说明 |
|--------|------|------|
| 高 | 策略 alpha 优化 | 当前回测 -20.34%，41 笔，胜率 39% |
| 高 | 模型分与策略分交叉验证 | 观察两者分歧案例，校准权重 |
| 中 | BJ 数据源补充 | BaoStock 不支持 BJ，需找替代源 |
| 中 | Qlib 模型迭代 | 换模型、自定义特征、多周期集成 |
| 低 | 事件溯源重构 | candidates 表架构改造 |
| 低 | Walk-forward 验证 | 交易数不足，暂未就绪 |

---

## 十一、关键文件索引

| 文件 | 作用 |
|------|------|
| `alpha_ledger/tickers.py` | ticker 规范化核心 |
| `alpha_ledger/qfq_backfill.py` | 前复权回填 |
| `alpha_ledger/qlib_export.py` | Qlib CSV 导出 |
| `alpha_ledger/qlib_import.py` | pred.pkl 导入 |
| `alpha_ledger/alpha_factors.py` | 因子 + 模型分附加 |
| `alpha_ledger/screener.py` | 筛选主逻辑 |
| `alpha_ledger/reporting.py` | 日报渲染 |
| `alpha_ledger/db.py` | schema + 迁移 |
| `workflow_config_alpha158.yaml` | Qlib 训练配置 |
