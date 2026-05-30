# Qlib Bridge

This document describes how Alpha Ledger data can be exported to Microsoft Qlib format for cross-validation and model experimentation.

## Quick Start

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

Uses Alpha Ledger's adjusted prices directly (adj_open, adj_close, etc.) without normalization. Suitable for Qlib pipeline smoke testing.

### qlib_normalized (future)

Normalizes prices to first-day-close = 1.0. Matches Qlib's official data format.

## CSV Fields

| Field | Source | Notes |
|-------|--------|-------|
| `date` | `price_bars.date` | YYYY-MM-DD |
| `open` | `adj_open` | In raw_adjusted mode |
| `close` | `adj_close` | In raw_adjusted mode |
| `high` | `adj_high` | In raw_adjusted mode |
| `low` | `adj_low` | In raw_adjusted mode |
| `volume` | `volume` | Original volume |
| `vwap` | `amount/volume` or `(high+low+close)/3` | See VWAP note below |
| `money` | `amount` | Total traded value (may be empty) |
| `factor` | `adj_factor` | Adjustment factor |
| `change` | `change_pct / 100` | close/prev_close - 1 |

## VWAP Note

Sina CN daily endpoint (`scale=240`) returns only day/open/high/low/close/volume — no `amount` field. Sina intraday has `amount` but only recent coverage, so it is not suitable for full historical daily amount backfill.

Two paths now provide `amount` for `price_bars`:

1. **BaoStock QFQ path** (`backfill-qfq`): The `query_history_k_data_plus` call with `adjustflag=2` also requests `amount` and `turn` alongside adjusted OHLC. These raw market metrics are written to `price_bars.amount` and `price_bars.turnover_pct` with no-overwrite semantics (existing positive amount and non-null turnover_pct are preserved).

2. **BaoStock enrichment path** (`enrich-daily-bars`): A separate pass using `adjustflag=3` specifically for `amount` and `turn` when they were missed by the QFQ path.

When `amount` is still NULL after both passes:
- `vwap` is computed as `(high + low + close) / 3` (typical price approximation)
- `money` is left empty

This is a common proxy in quantitative research. For actual VWAP from intraday data, use Alpha Ledger's `intraday_bars` table.

**Important:** The `amount` and `turnover_pct` from the BaoStock QFQ path are raw market metrics carried alongside forward-adjusted OHLC. They should pass VWAP sanity checks (`vwap_sanity_check()` in `qfq_backfill`) before full trust.

## Priority 1: Daily Amount/Turnover Enrichment (2026-05-30)

The `backfill-qfq` command now also captures `amount` and `turnover_pct` from BaoStock in the same API call that fetches adjusted OHLC (no extra pass needed for tickers that go through QFQ backfill).

For rows that were already ADJUSTED before the QFQ path gained amount/turnover support, the `enrich-daily-bars` command provides a separate backfill using `adjustflag=3`.

After either path, `export-qlib-csv` produces real `vwap = amount / volume` and `money = amount` instead of the typical-price fallback.

```bash
# Dry-run: see which tickers need enrichment
python -m alpha_ledger enrich-daily-bars \
    --start 2024-01-01 --end 2026-05-29 --dry-run

# Live run
python -m alpha_ledger enrich-daily-bars \
    --start 2024-01-01 --end 2026-05-29 --throttle 0.3
```

**Design choices:**
- QFQ path (`backfill-qfq`): BaoStock `adjustflag=2` with fields `date,open,high,low,close,volume,amount,turn,tradestatus,isST`. The `amount` and `turn` (→ `turnover_pct`) are raw market metrics carried alongside adjusted OHLC in the same API call.
- Enrichment path (`enrich-daily-bars`): BaoStock `adjustflag=3` (no adjustment) as a catch-up for rows that were already ADJUSTED before QFQ gained amount/turnover support.
- Both paths: only `amount` and `turn` (→ `turnover_pct`) are stored from the optional fields.
- Resume-safe: only rows where `amount IS NULL OR amount <= 0 OR turnover_pct IS NULL` are touched.
- Benchmarks/indexes are skipped (BaoStock does not support them).
- Reports written to `reports/daily_enrichment_*.md` and `.json`.

**Important:** Qlib Alpha158/Alpha360 training should wait for both full QFQ backfill (`backfill-qfq`) and daily enrichment (`enrich-daily-bars`) to complete before formal training runs. The enrichment path exists today, but the production DB is still being backfilled.

## Alpha158 vs Alpha360

| Handler | Fields Needed | Status |
|---------|--------------|--------|
| Alpha360 | OHLCV | Working |
| Alpha158 | OHLCV + VWAP | Working (vwap via typical price fallback) |

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
