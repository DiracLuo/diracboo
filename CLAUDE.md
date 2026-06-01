# Alpha Ledger — 项目指南

## 项目简介

Alpha Ledger 是 A 股策略验证账本。核心流程：筛选 → 候选记录 → 真实行情回放 → 净收益统计 → 策略淘汰/升级。不做自动交易，不做手工叙事。

## 技术栈

- **语言**: Python 3.10+
- **数据库**: SQLite (`data/alpha_ledger.sqlite`)
- **数据源**: 新浪(Sina)日线/分钟线, BaoStock(前复权/换手率), AkShare(事件/财务/分钟线兜底), 腾讯(港股日线), Yahoo Finance HTTP API(备用), SEC EDGAR(美股公告)
- **ML**: Qlib（3 个模型：M1 2024起/T+5、M2 2025起/T+10、M3 2026起/T+10）
- **缠论**: `chan_analysis.py`（简化版：K线合并→分型→笔→中枢→位置判断）
- **测试**: pytest, unittest
- **入口**: `python -m alpha_ledger <command>`

## 目录结构

```
alpha_ledger/       # 核心模块（cli.py 为命令入口）
tests/              # 测试（test_alpha_ledger.py 为主测试）
scripts/            # 一次性脚本和调试工具
data/               # 数据仓库（SQLite、universe、events、qlib_export）
reports/            # 生成的报告（daily_plan, backtest 等）
docs/               # 文档和同步报告
workflow_config_*.yaml  # Qlib 工作流配置
```

## 全部 CLI 命令

```
init                    创建数据库 schema
seed                    种入策略和兴业科技案例
bootstrap               init + seed + screen + audit + daily-plan 一站式
fetch-prices            拉取 OHLCV 到 price_bars
fetch-events            拉取公告/研报/财报/资金流
fetch-intraday          拉取 A 股分钟线到 intraday_bars
import-events-csv       从 CSV 导入美股/港股/ A 股事件
data-update             增量更新本地 A 股数据仓库
data-audit              审计本地数据覆盖度和置信度
data-backfill           批量回补本地 A 股日线
backfill-qfq            回补前复权（QFQ）价格
enrich-daily-bars       用 BaoStock 补充成交额和换手率
audit-tickers           审计 ticker 归一化（.SH → .SS）干跑
repair-tickers          修复 .SH → .SS 归一化（默认干跑）
daily-run               本地每日数据 + 筛选 + 确认 + 报告全流程
daily-events            拉取事件后重新筛选并更新报告
screen                  运行本地筛选器生成候选
confirm-candidates      确认或取消 WATCH_CONFIRMATION 候选
evaluate-candidates     候选信号日后回验
candidates              列出某日候选
daily-plan              生成每日可操作清单
replay                  历史日期范围逐日筛选回放
score-calibration       验证评分预测力
audit                   审计策略健康度和失效风险
tune-weights            从回放结果建议或应用策略权重调整
portfolio-backtest      组合层面回测
walk-forward            滚动验证准备度检查
validate                统计验证（Monte Carlo / Bootstrap / Walk-Forward）
loss-review             复盘亏损样本，标记失败模式
export-qlib-csv         导出 price_bars 为 Qlib 格式 CSV
import-qlib-predictions 导入 Qlib pred.pkl 到 model_scores
verify                  校验手工信号哈希
```

常用命令示例：

```bash
# 每日流程
python -m alpha_ledger data-update --as-of YYYY-MM-DD --markets CN_A --adjust none
python -m alpha_ledger data-audit --start ... --end ... --markets CN_A
python -m alpha_ledger daily-run --as-of YYYY-MM-DD

# 筛选与确认
python -m alpha_ledger screen --as-of YYYY-MM-DD
python -m alpha_ledger confirm-candidates --as-of YYYY-MM-DD
python -m alpha_ledger daily-plan --as-of YYYY-MM-DD

# 回放与验证
python -m alpha_ledger replay --start ... --end ... --through ... --benchmark 000300.SS
python -m alpha_ledger validate --start ... --end ... --through ...
python -m alpha_ledger portfolio-backtest --start ... --end ... --through ... --benchmark 000300.SS

# 自检
python -m pytest tests/ -v
python -m alpha_ledger verify
```

## 编码规范

- 使用中文注释和文档（项目面向中文用户）
- 命令行参数风格：`--as-of`, `--markets`, `--through`, `--benchmark`
- 日期格式统一为 `YYYY-MM-DD`
- 收益率用百分比数值表示（3.8 = 3.8%，由 `pct_change` 函数 `*100` 得出）
- A 股往返成本默认 0.18%
- 复权优先使用前复权（QFQ），短线研究允许 RAW_FALLBACK 但置信度不超 MEDIUM

## 关键约束

- **T+1 规则**: A 股买入日不允许同日退出
- **涨跌停**: 一字涨停无法买入，跌停无法退出时延期
- **数据审计**: 非 HIGH_CONFIDENCE 时只展示观察/确认候选，不输出强买入结论
- **STALE_DATA**: `as_of` 晚于最新行情日时不生成可操作清单
- **基准**: A 股默认沪深 300（`000300.SS`），分层基准支持中证500/1000/创业板指/科创50/北证50

## 策略库

正式 A 股策略（有筛选器实现）：
- `trend_breakout` — 强趋势突破（含回调确认机制 WATCH_PULLBACK）
- `abnormal_volume_small_midcap` — 中小盘异常放量
- `a_share_hard_event_catalyst` — 硬事件催化
- `xingye_style_prepositioning` — 兴业科技型重估埋伏
- `cn_a_pead_quality_surprise` — 财报超预期漂移（PEAD，要求 M2/M3 >= 60%）

实验策略（不进入正式结论）：美股 SEC/News、港股回购/南向/新闻

## 多模型评分

报告中每个候选显示 3 个模型的百分位评分（M1 | M2 | M3），配置在 `alpha_factors.MULTI_MODEL_CONFIGS`：
- M1: `qlib_alpha158` / `t5_full_20260530`（训练 2024-01-02 起，T+5 label）
- M2: `qlib_alpha158_20250101` / `t10_v2`（训练 2025-01-01 起，T+10 label）
- M3: `qlib_alpha158_20260101` / `t10_v2`（训练 2026-01-01 起，T+10 label）

模型 Top 3 选股段单独展示在"今日新信号"之后。PEAD 策略要求 M2 或 M3 >= 60% 才入选。

## 不要做的事

- 不要把手工信号种入候选账本（必须由筛选器产出）
- 不要假设复权接口不可用（先用 `--probe-adjustment` 探测）
- 不要把实验策略的收益混入正式结论
- 不要在数据不足时生成正式 alpha 结论
- 不要修改 `data/` 下的 SQLite 文件结构而不先确认迁移方案

## 项目文档参考

- `README.md` — 项目完整说明和命令示例
- `docs/STRATEGY_LIBRARY.md` — 策略详细说明
- `docs/DATA_SOURCES.md` — 数据源说明
- `docs/IMPLEMENTATION_PLAN.md` — 当前实现概览（非早期 v0.1/v0.2 计划）
- `docs/qlib_bridge.md` — Qlib 集成文档
- `docs/STRATEGY_DECAY_GUARD.md` — 策略失效与拥挤防护
- `docs/CASE_XINGYE_002674.md` — 兴业科技 002674.SZ 案例
- `docs/codex_mcp_setup_report.md` — Codex MCP 配置报告
- `docs/sync_codex_20260531.md` — Codex MCP 同步报告
