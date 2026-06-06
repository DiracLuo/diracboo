# Qlib Bridge

This document describes how Alpha Ledger data can be exported to Microsoft Qlib format for cross-validation and model experimentation.

Production note: the recommended production path is `python -m alpha_ledger production-run --as-of YYYY-MM-DD`. The manual CSV and `dump_all` steps below are research/maintenance procedures, not the daily production path.

## Production Boundary

Daily production should use the unified pipeline:

```bash
python -m alpha_ledger production-run --as-of YYYY-MM-DD
```

That command performs core data refresh, qfq factor repair, Qlib incremental refresh, production model prediction, 1-minute intraday refinement, and formal daily report generation. The manual export and `dump_all` commands below are for research, initialization, or maintenance only.

## Research / Maintenance Quick Start

### 1. Export to Qlib CSV

```bash
python -m alpha_ledger export-qlib-csv \
    --start 2025-12-01 \
    --end 2026-05-28 \
    --output data/qlib_export \
    --markets CN_A
```

Output: one CSV per stock in `data/qlib_export/`, plus `quality_report.md` and `quality_report.json`.

### 2. Convert to Qlib Binary

```bash
cd ~/code/external/qlib

python scripts/dump_bin.py dump_all \
    --data_path "/path/to/Stock Analysis/data/qlib_export" \
    --qlib_dir ~/.qlib/qlib_data/alpha_ledger \
    --include_fields open,close,high,low,volume,vwap,money,factor,change \
    --date_field_name date \
    --file_suffix .csv
```

### 3. Run Qlib Smoke Test

```bash
cd ~/code/external/qlib
python qlib/cli/run.py /path/to/Stock\ Analysis/workflow_config_alpha_ledger_smoke.yaml
```

## Ticker Mapping

| Alpha Ledger | Qlib Filename |
|-------------|---------------|
| `600519.SS` | `SH600519.csv` |
| `002674.SZ` | `SZ002674.csv` |
| `430047.BJ` | `BJ430047.csv` |

## Export Modes

### raw_adjusted (default)

Uses Alpha Ledger's unified qfq price reader. Exported OHLC are computed as `raw OHLC * adj_factor`; `adj_*` columns are kept only as compatibility fields, not the source of truth.

### qlib_normalized (future)

Normalizes prices to first-day-close = 1.0. Matches Qlib's official data format.

## CSV Fields

| Field | Source | Notes |
|-------|--------|-------|
| `date` | `price_bars.date` | YYYY-MM-DD |
| `open` | `open * adj_factor` | In raw_adjusted mode |
| `close` | `close * adj_factor` | In raw_adjusted mode |
| `high` | `high * adj_factor` | In raw_adjusted mode |
| `low` | `low * adj_factor` | In raw_adjusted mode |
| `volume` | `volume` | Original volume |
| `vwap` | `amount/volume` or `(high+low+close)/3` | See VWAP note below |
| `money` | `amount` | Total traded value (may be empty) |
| `factor` | `adj_factor` | Adjustment factor |
| `change` | `change_pct / 100` | close/prev_close - 1 |

## VWAP / Amount Note

Sina CN daily endpoint (`scale=240`) returns only day/open/high/low/close/volume — no `amount` field. Sina intraday has `amount` but only recent coverage, so it is not suitable for full historical daily amount backfill.

Current production and maintenance paths for `amount`:

1. **Daily fast path**: `data-update --core-only --adjust none` uses the fast A-share snapshot path for same-day OHLCV, `pre_close`, `amount`, and layered benchmarks. This is the production path.

2. **BaoStock enrichment path** (`enrich-daily-bars`): A separate maintenance pass using `adjustflag=3` specifically for `amount` and `turn` when fast-path or historical rows are missing these fields.

When `amount` is still NULL after enrichment:
- `vwap` is computed as `(high + low + close) / 3` (typical price approximation)
- `money` is left empty

This is a common proxy in quantitative research. For actual VWAP from intraday data, use Alpha Ledger's `intraday_bars` table.

**Important:** `amount` and `turnover_pct` are raw market metrics. They are not multiplied by qfq factors. They should pass VWAP sanity checks before full trust.

## Amount / Turnover Enrichment

After daily fast-path update or enrichment, `export-qlib-csv` produces real `vwap = amount / volume` and `money = amount` instead of the typical-price fallback where possible.

```bash
# Dry-run: see which tickers need enrichment
python -m alpha_ledger enrich-daily-bars \
    --start 2024-01-01 --end 2026-05-29 --dry-run

# Live run
python -m alpha_ledger enrich-daily-bars \
    --start 2024-01-01 --end 2026-05-29 --throttle 0.3
```

**Design choices:**
- Daily production path: same-day fast snapshot first; missing fields are repaired by fallback sources rather than filled with 0.
- Enrichment path (`enrich-daily-bars`): BaoStock `adjustflag=3` (no adjustment) as catch-up for historical `amount` and `turn`.
- Only `amount` and `turn` (→ `turnover_pct`) are stored from the optional enrichment fields.
- Resume-safe: only rows where `amount IS NULL OR amount <= 0 OR turnover_pct IS NULL` are touched.
- Benchmarks/indexes are skipped (BaoStock does not support them).
- Reports written to `reports/daily_enrichment_*.md` and `.json`.

## QFQ Factor Maintenance Before Formal Training

Formal Qlib Alpha158/Alpha360 training should wait for qfq factor repair and daily enrichment:

```bash
python -m alpha_ledger detect-adjustment-breaks --as-of YYYY-MM-DD
python -m alpha_ledger qfq-repair-breaks --as-of YYYY-MM-DD --start 2024-01-01 --source baostock
python -m alpha_ledger qfq-maintenance --as-of YYYY-MM-DD --mode scan-and-repair --lookback-days 60 --force
```

The current qfq source of truth is `raw OHLC * adj_factor`. `adj_*` columns are compatibility fields only. `backfill-qfq`, if still present in the codebase, is a legacy slow maintenance fallback and must not be used as the daily production qfq path.

## Ticker Normalization

A 股 ticker 后缀需从 `.SH`/`.SZ` 归一化为 `.SS`/`.SZ`（新浪格式）。相关命令：

```bash
# 干跑审计，查看哪些 ticker 需要修复
python -m alpha_ledger audit-tickers

# 执行修复（默认干跑，加 --apply 真正写入）
python -m alpha_ledger repair-tickers --apply
```

## Qlib Predictions 导入

Qlib 模型训练产出的 `pred.pkl` 可导入为模型分数：

```bash
python -m alpha_ledger import-qlib-predictions \
    --pred-path ~/code/external/qlib/output/pred.pkl \
    --model-name alpha158_v1
```

导入后写入 `model_scores` 表，候选的 `model_score` 和 `model_percentile` 列会自动更新。

## Alpha158 vs Alpha360

| Handler | Fields Needed | Status |
|---------|--------------|--------|
| Alpha360 | OHLCV | Working |
| Alpha158 | OHLCV + VWAP | Working（有 amount 的 ticker 用真实 VWAP，否则回退 `(H+L+C)/3`） |

## Data Quality

The export generates a quality report classifying each bar:

- `ok` — Normal trading day with adjusted prices
- `possible_suspended` — Volume=0, all prices equal
- `missing_price` — Some adjusted price fields are NULL
- `zero_volume_with_price` — Volume=0 but prices differ
- `bad_adjustment` — adjustment_status != 'ADJUSTED'

## Environment Notes

Qlib requires specific numpy/cvxpy versions:
- numpy < 2.0 (tested with 1.26.4)
- cvxpy < 1.5 (tested with 1.4.4)

If you encounter import errors after installing Qlib, run:
```bash
pip install "numpy<2" "cvxpy<1.5"
```

## References

- Current state (2026-05-30): `reports/qlib_current_state_20260530.md`
- Integration plan: `reports/qlib_integration_plan_v2.md`
- Phase 0-3 summary: `reports/qlib_phase03_summary.md`
- Phase 4 summary: `reports/qlib_phase04_summary.md`
- Qlib source: `~/code/external/qlib` (read-only)
