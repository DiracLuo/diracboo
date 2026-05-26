# 数据源接入

## 当前已接入

Alpha Ledger v0.2 已经接入三类公开行情源，并统一写入 `price_bars`：

| 市场 | 当前源 | 用途 | 备注 |
|---|---|---|---|
| 美股 | 新浪美股日K接口 | 日线 OHLCV | 当前默认 universe 使用 `sina_us` |
| 港股 | 腾讯港股日K接口 | 日线 OHLCV | 当前默认 universe 使用 `tencent_hk` |
| A股 | 新浪 A 股日K接口 | 日线 OHLCV | 当前默认 universe 使用 `sina_cn` |

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

拉取美股/港股事件和财务：

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

拉取行情：

```bash
python -m alpha_ledger fetch-prices \
  --start 2026-04-01 \
  --end 2026-05-25 \
  --markets US,HK,CN_A
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
- 分市场表现，用于比较 A 股、美股、港股候选池质量。
- 买入当天触发与确认后触发的表现差异。
- MFE/MAE，即候选后的最大有利波动和最大不利波动。
- 基于去重结果的策略权重建议；如需真正写入权重，使用 `tune-weights --apply`。

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

AkShare 已安装并用于事件、财务和当前资金流；但本环境中 AkShare 的 A 股历史 K 线接口 `stock_zh_a_hist` 背后访问 `push2his.eastmoney.com`，实测会出现 `ProxyError / RemoteDisconnected`，所以价格历史暂时继续使用新浪直连源。

Tushare 适合后续补充结构化 A股基础数据、财务指标、行情和公司事件；通常需要 token。

当前代码把行情数据源放在 `alpha_ledger/market_data.py`，事件数据源放在 `alpha_ledger/event_data.py`。后续加入 Tushare 或其他源时，只需要新增对应 fetch 函数，不需要改账本结构。

## 当前限制

- 公开免费接口可能限流、缺字段或延迟。
- A 股公告池已扩展到事件来源股票，但公告接口会混入部分非普通股票代码，行情拉取时会自动跳过失败标的。
- 候选股只是策略触发，不等于立即买入；涨停、暴涨和流动性不足必须进入二次人工复核。
- 历史资金流暂未可靠接入，避免产生未来函数。
