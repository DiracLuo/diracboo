# 数据源接入

## 当前已接入

当前正式收益验证优先使用 A 股数据。美股/港股数据源和事件源已经保留，但因事件完整性、分时执行和交易约束还未达到 A 股同等质量，默认只作为实验数据，不进入正式买入建议和组合收益结论。

Alpha Ledger 已经接入三类公开日线行情源，并统一写入 `price_bars`：

| 市场 | 当前源 | 用途 | 备注 |
|---|---|---|---|
| 美股 | 新浪美股日K接口 | 日线 OHLCV | 实验保留，暂不进入正式收益结论 |
| 港股 | 腾讯港股日K接口 | 日线 OHLCV | 实验保留，暂不进入正式收益结论 |
| A股 | 新浪 A 股日K接口 + AkShare 前复权日线 | 原始 OHLCV + 复权收益价 | 原始价用于成交展示，`adj_*` 用于收益统计 |
| A股基准 | 新浪 A 股日K接口 | 沪深 300 `000300.SS` | 默认 A 股 benchmark，用于 alpha 统计 |

A 股回放还支持 5 分钟线，统一写入 `intraday_bars`：

| 市场 | 当前源 | 用途 | 备注 |
|---|---|---|---|
| A股 | 新浪 A 股分钟 K 线 | 5 分钟入场、止损/止盈路径 | 当前优先源 |
| A股 | AkShare `stock_zh_a_hist_min_em` | 备用分钟线 | 网络可用时作为兜底 |

另已通过 AkShare 接入 A 股事件数据：

| 数据 | 当前源 | 用途 | 备注 |
|---|---|---|---|
| 公告 | `stock_notice_report` / `stock_individual_notice_report` | 事件候选池、催化识别 | 可按日期扩展候选池 |
| 调研/研报 | `stock_research_report_em` | 机构调研、重估线索 | 当前作为公告事件写入 |
| 财务指标 | `stock_financial_analysis_indicator` | 收入、利润、毛利率、负债率过滤 | 用于事件打分加分 |
| 资金流 | `stock_fund_flow_individual` | 当前日资金确认 | 只能可靠用于当前日，不用于历史回放 |

美股和港股事件侧新增了通用 CSV 导入入口。它不是为了替代未来的自动接口，而是先把财报日历、SEC/新闻、分析师评级、港股公告、回购、南向资金等事件统一写入 `corporate_events`，让这些事件可以进入同一套候选筛选、事后回放和策略竞争机制。

`fetch-events` 也已经支持真实美股/港股来源：

| 市场 | 数据 | 当前源 | 写入表 |
|---|---|---|---|
| 美股 | 10-K / 10-Q / 8-K / S-1 披露 | SEC EDGAR `data.sec.gov` | `corporate_events` |
| 美股 | 个股新闻 | AkShare `stock_news_em` | `corporate_events` |
| 港股 | 个股新闻 | AkShare `stock_news_em` | `corporate_events` |
| 港股 | 分红派息 | AkShare `stock_hk_dividend_payout_em` | `corporate_events` |
| 港股 | 南向持股统计 | AkShare `stock_hsgt_stock_statistics_em` | `corporate_events` |
| 港股 | 财务指标 | AkShare `stock_financial_hk_analysis_indicator_em` | `financial_metrics` |

拉取美股/港股事件和财务（实验用途）：

```bash
python -m alpha_ledger fetch-events \
  --start 2026-04-01 \
  --end 2026-05-15 \
  --markets US,HK \
  --skip-money-flow
```

CSV 字段：

```csv
market,ticker,name,event_date,event_type,title,source,source_url,importance_score,summary
```

模板文件：

```bash
data/events/us_hk_events_template.csv
```

导入美股/港股事件：

```bash
python -m alpha_ledger import-events-csv \
  --path data/events/us_hk_events_template.csv
```

`market` 支持 `US`、`HK`、`CN_A`；港股 `ticker` 可以写 `700`、`0700` 或 `0700.HK`，导入时会规范为 `0700.HK` 并自动补入可拉取行情的 instrument。

默认股票池在：

```bash
data/universe/default_universe.csv
```

拉取正式 A 股行情：

```bash
python -m alpha_ledger fetch-prices \
  --start 2026-04-01 \
  --end 2026-05-25 \
  --markets CN_A \
  --include-benchmarks \
  --adjust qfq
```

如果只想补沪深 300 基准：

```bash
python -m alpha_ledger fetch-prices \
  --start 2026-04-01 \
  --end 2026-05-25 \
  --markets CN_A \
  --symbols 000300.SS \
  --include-benchmarks \
  --adjust qfq
```

筛选候选：

```bash
python -m alpha_ledger screen --as-of 2026-05-25
python -m alpha_ledger candidates --as-of 2026-05-25
```

拉取公告、调研、财务和当前资金流：

```bash
python -m alpha_ledger fetch-events \
  --start 2026-04-01 \
  --end 2026-05-15 \
  --markets CN_A \
  --skip-money-flow
```

历史回放时建议跳过资金流，因为当前可用资金流接口是即时数据，不能倒灌到历史日期。

回放报告现在额外输出：

- 原始候选策略胜率与单股单日去重后的策略胜率。
- 分市场表现；默认正式范围只统计 A 股，美股/港股不进入正式结论。
- 买入当天触发与确认后触发的表现差异。
- MFE/MAE，即候选后的最大有利波动和最大不利波动。
- 基于去重结果的策略权重建议；如需真正写入权重，使用 `tune-weights --apply`。
- 分数校准和数据质量审计，检查高分候选是否真的更赚钱、分时覆盖是否足够可信。
- 沪深 300 同期基准收益、平均超额收益、超额胜率。
- 前复权覆盖率；覆盖不足时报告会标记“当前回测收益不可信”。

当前样本不足以做正式 walk-forward 调参，先使用 readiness 检查：

```bash
python -m alpha_ledger walk-forward \
  --start 2026-04-01 \
  --end 2026-05-25
```

## 增量数据仓库

正式研究优先维护本地 A 股数据池，不依赖每次回放临时拉接口。

每日收盘后运行：

```bash
python -m alpha_ledger data-update --as-of 2026-05-27 --markets CN_A --adjust none
python -m alpha_ledger data-audit --start 2026-04-01 --end 2026-05-27 --markets CN_A
python -m alpha_ledger daily-run --as-of 2026-05-27
```

已有历史行情不缺日期、但缺分层基准或复权覆盖时，使用覆盖修复模式。默认只修分层基准，不重拉全市场复权：

```bash
python -m alpha_ledger data-update \
  --as-of 2026-05-27 \
  --markets CN_A \
  --repair-coverage \
  --repair-scope benchmarks \
  --skip-events \
  --skip-intraday
```

该模式会重新检查已有日期范围内的覆盖缺口：

- 分层基准缺失时补沪深300、中证500、中证1000、创业板指、科创50、北证50。
- 仅当使用 `--repair-scope adjustments` 或 `--repair-scope all` 时，才尝试把已有 A 股 `RAW_FALLBACK` 日线重新拉取为前复权。
- 修复结果继续写入 `data_fetch_runs` 和 `data_fetch_errors`。

复权接口可用性先用小样本探测：

```bash
python -m alpha_ledger data-audit \
  --start 2026-04-01 \
  --end 2026-05-26 \
  --markets CN_A \
  --probe-adjustment \
  --ignore-adjustment-for-short-term
```

如果探测成功率较高，应补库修复复权；如果多源持续失败，短线 T+5/T+10 研究可临时使用 `RAW_FALLBACK`，但正式升权、长周期结论和 walk-forward 仍需复权覆盖达标。

短线忽略复权时，系统会标记“未复权短线研究口径”，并把置信度上限限制在 `MEDIUM_CONFIDENCE`；正式买入清单仍要求 `HIGH_CONFIDENCE`。

历史补库按批次运行，避免长时间连续请求：

```bash
python -m alpha_ledger data-backfill --start 2025-12-01 --end 2026-05-26 --markets CN_A --batch-days 180 --adjust none --throttle 0.3
```

当前补库主线使用 `--adjust none`，只维护原始价格短线研究库。需要修复复权时单独使用 `--adjust qfq` 或 `--repair-scope adjustments`，不要和日常补库混跑。

新增数据任务表：

- `data_fetch_runs`：每次数据任务的范围、状态、写入数量和错误数。
- `data_fetch_errors`：接口失败和错误原因。
- `data_coverage_daily`：日线、复权、基准、事件、财报、分时覆盖率。
- `data_source_health`：数据源成功/失败状态。

日报使用数据审计结果生成 `confidence_level`。未达到 `HIGH_CONFIDENCE` 时，日报不输出强买入结论。

A 股审计优先使用沪深300行情日作为交易日历；缺沪深300时退回已有市场行情日期，再缺才使用普通工作日并在审计备注中标记日历不可靠。这样不会把五一等休市日误判为行情缺口。

```bash
python -m alpha_ledger tune-weights \
  --start 2026-04-01 \
  --end 2026-05-15 \
  --through 2026-05-25

python -m alpha_ledger tune-weights \
  --start 2026-04-01 \
  --end 2026-05-15 \
  --through 2026-05-25 \
  --apply
```

## AkShare 与 Tushare

AkShare 已安装并用于事件、财务、当前资金流和 A 股分钟线兜底；但本环境中 AkShare 的东方财富链路偶尔会出现 `ProxyError / RemoteDisconnected`，所以 A 股分钟线当前优先使用新浪直连源。

Tushare 适合后续补充结构化 A股基础数据、财务指标、行情和公司事件；通常需要 token。

当前代码把行情数据源放在 `alpha_ledger/market_data.py`，事件数据源放在 `alpha_ledger/event_data.py`。后续加入 Tushare 或其他源时，只需要新增对应 fetch 函数，不需要改账本结构。

## 当前限制

- 公开免费接口可能限流、缺字段或延迟。
- A 股公告池已扩展到事件来源股票，但公告接口会混入部分非普通股票代码，行情拉取时会自动跳过失败标的。
- 候选股只是策略触发，不等于立即买入；涨停、暴涨和流动性不足必须进入二次人工复核。
- 历史资金流暂未可靠接入，避免产生未来函数。
- 美股和港股当前只有日线执行口径，A 股优先使用分时执行口径。
- 如果 `price_bars.adjustment_status` 仍为 `RAW_FALLBACK`，收益会使用原始价格回退，但不能作为高置信正式收益结论。
- 如果缺少分层基准行情，报告不能可靠判断 alpha，策略权重建议也不会基于该段数据升权。
