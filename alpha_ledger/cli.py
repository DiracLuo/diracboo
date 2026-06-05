from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from .audit import audit_all, latest_audits
from .qlib_export import export_qlib_csv, write_quality_report, audit_ticker_normalization, write_ticker_audit_report
from .ticker_repair import audit_ticker_repair, repair_tickers, write_ticker_repair_report
from .qlib_import import import_qlib_predictions, write_import_report
from .daily_enrichment import enrich_daily_bars, write_enrichment_report
from .qfq_backfill import qfq_backfill, write_qfq_backfill_report
from .adjustments import (
    detect_adjustment_breaks,
    qfq_maintenance_scan_and_repair,
    qfq_repair_daily,
)
from .benchmarks import CN_A_BENCHMARKS
from .data_ops import REPAIR_SCOPES, audit_data_coverage, data_update, probe_adjustment_sources
from .db import DEFAULT_DB_PATH, connect, init_db, upsert_many
from .event_data import fetch_events_to_db, import_events_csv
from .ledger import now_utc, verify_signals
from .loss_review import write_loss_review
from .market_data import (
    DEFAULT_UNIVERSE_PATH,
    fetch_bars,
    fetch_intraday_bars,
    parse_date,
    read_db_instruments,
    read_universe,
)
from .metrics import (
    apply_strategy_weight_adjustments,
    evaluate_candidate_horizons_for_date,
    evaluate_candidates,
    score_calibration,
    suggest_strategy_weight_adjustments,
)
from .portfolio_backtest import run_portfolio_backtest, write_portfolio_report
from .pipeline_ops import (
    model_arena,
    model_evaluate,
    model_governance_review,
    model_predict,
    production_async,
    production_daily,
    production_run,
    qlib_refresh,
)
from .replay import replay_candidates
from .validation import write_validation_report
from .reporting import write_daily_plan, write_replay_report
from .screener import confirm_candidates, confirm_pullback_candidates, latest_candidates, screen_all
from .seed import seed_all
from .walk_forward import run_walk_forward, write_walk_forward_report


def _fmt_float(value: object, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alpha Ledger stock prediction ledger")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create database schema")
    subparsers.add_parser("seed", help="Seed strategies and Xingye case")

    bootstrap = subparsers.add_parser("bootstrap", help="Init, seed, screen, audit, and daily plan")
    bootstrap.add_argument("--as-of", required=True, help="Evaluation/report date, e.g. 2026-05-25")

    fetch_prices = subparsers.add_parser("fetch-prices", help="Fetch OHLCV bars into price_bars")
    fetch_prices.add_argument("--start", required=True, help="Start date, e.g. 2026-05-01")
    fetch_prices.add_argument("--end", required=True, help="End date, e.g. 2026-05-25")
    fetch_prices.add_argument(
        "--universe",
        default=str(DEFAULT_UNIVERSE_PATH),
        help="Universe CSV path",
    )
    fetch_prices.add_argument(
        "--source",
        choices=["universe", "db", "both"],
        default="universe",
        help="Read instruments from universe CSV, database instruments, or both",
    )
    fetch_prices.add_argument(
        "--markets",
        help="Comma-separated markets to fetch, e.g. US,HK,CN_A",
    )
    fetch_prices.add_argument(
        "--symbols",
        help="Comma-separated tickers/source symbols to fetch, e.g. NVDA,0700.HK,002674.SZ",
    )
    fetch_prices.add_argument(
        "--throttle",
        type=float,
        default=0.15,
        help="Seconds to sleep between remote requests",
    )
    fetch_prices.add_argument(
        "--include-benchmarks",
        action="store_true",
        help="Include built-in market benchmarks such as CN_A:000300.SS",
    )

    data_update_parser = subparsers.add_parser("data-update", help="Incrementally update local A-share data warehouse")
    data_update_parser.add_argument("--as-of", required=True)
    data_update_parser.add_argument("--markets", default="CN_A")
    data_update_parser.add_argument("--throttle", type=float, default=0.15)
    data_update_parser.add_argument(
        "--adjust",
        choices=("none", "qfq"),
        default="qfq",
        help="Daily adjustment mode for fetched A-share stock bars; default uses forward-adjusted (qfq) prices",
    )
    data_update_parser.add_argument("--skip-events", action="store_true")
    data_update_parser.add_argument("--skip-intraday", action="store_true")
    data_update_parser.add_argument(
        "--intraday-period",
        choices=("1", "5", "15", "30", "60"),
        default="5",
        help="Intraday bar period in minutes (default: 5)",
    )
    data_update_parser.add_argument(
        "--core-only",
        action="store_true",
        help="Production fast path: prices/benchmarks only; skip slow events, intraday, and qfq adjustment",
    )
    data_update_parser.add_argument(
        "--repair-coverage",
        action="store_true",
        help="Repair existing CN_A coverage gaps such as missing layered benchmarks and RAW_FALLBACK bars",
    )
    data_update_parser.add_argument(
        "--repair-scope",
        choices=REPAIR_SCOPES,
        default="benchmarks",
        help="Coverage repair target. benchmarks avoids full-market adjustment retries.",
    )

    data_audit = subparsers.add_parser("data-audit", help="Audit local data coverage and confidence")
    data_audit.add_argument("--start", required=True)
    data_audit.add_argument("--end", required=True)
    data_audit.add_argument("--markets", default="CN_A")
    data_audit.add_argument("--probe-adjustment", action="store_true", help="Probe A-share qfq adjustment source availability")
    data_audit.add_argument("--probe-sample-size", type=int, default=10)
    data_audit.add_argument(
        "--ignore-adjustment-for-short-term",
        action="store_true",
        help="Allow RAW_FALLBACK for short-term research while capping confidence below HIGH_CONFIDENCE",
    )
    data_audit.add_argument("--throttle", type=float, default=0.15)

    daily_run_parser = subparsers.add_parser(
        "daily-run",
        help="Legacy/research daily flow; use production-run for production",
    )
    daily_run_parser.add_argument("--as-of", required=True)
    daily_run_parser.add_argument("--throttle", type=float, default=0.15)
    daily_run_parser.add_argument("--fast", action="store_true", help="Fast mode: prices + screening + report only, skip slow event fetching")
    daily_run_parser.add_argument("--skip-model-update", action="store_true", help="Deprecated no-op; legacy model auto-update is disabled")

    daily_events_parser = subparsers.add_parser("daily-events", help="Fetch events, re-screen with fresh data, and update report")
    daily_events_parser.add_argument("--as-of", required=True)
    daily_events_parser.add_argument("--throttle", type=float, default=0.15)

    data_backfill = subparsers.add_parser("data-backfill", help="Backfill local A-share daily bars in batches")
    data_backfill.add_argument("--start", required=True)
    data_backfill.add_argument("--end", required=True)
    data_backfill.add_argument("--markets", default="CN_A")
    data_backfill.add_argument("--batch-days", type=int, default=20)
    data_backfill.add_argument("--throttle", type=float, default=0.15)
    data_backfill.add_argument(
        "--adjust",
        choices=("none", "qfq"),
        default="qfq",
        help="Daily adjustment mode for fetched A-share stock bars; default uses forward-adjusted (qfq) prices",
    )
    fetch_prices.add_argument(
        "--adjust",
        choices=("qfq", "none"),
        default="qfq",
        help="Daily adjustment mode for A-share return prices; raw OHLC is still stored for execution",
    )

    fetch_intraday = subparsers.add_parser("fetch-intraday", help="Fetch A-share minute bars into intraday_bars")
    fetch_intraday.add_argument("--start", required=True, help="Start date, e.g. 2026-05-18")
    fetch_intraday.add_argument("--end", required=True, help="End date, e.g. 2026-05-25")
    fetch_intraday.add_argument(
        "--source",
        choices=["universe", "db", "both"],
        default="db",
        help="Read instruments from universe CSV, database instruments, or both",
    )
    fetch_intraday.add_argument(
        "--universe",
        default=str(DEFAULT_UNIVERSE_PATH),
        help="Universe CSV path",
    )
    fetch_intraday.add_argument(
        "--symbols",
        help="Comma-separated A-share tickers/source symbols to fetch, e.g. 002975.SZ,sz002975",
    )
    fetch_intraday.add_argument("--period", choices=["1", "5", "15", "30", "60"], default="5")
    fetch_intraday.add_argument(
        "--throttle",
        type=float,
        default=0.15,
        help="Seconds to sleep between remote requests",
    )

    fetch_events = subparsers.add_parser("fetch-events", help="Fetch announcements/research/financials/flows")
    fetch_events.add_argument("--start", required=True, help="Start date")
    fetch_events.add_argument("--end", required=True, help="End date")
    fetch_events.add_argument(
        "--notice-limit-per-day",
        type=int,
        default=80,
        help="Max daily all-market notices to import",
    )
    fetch_events.add_argument(
        "--markets",
        default="CN_A",
        help="Comma-separated markets from DB/universe instruments for per-symbol event fetches",
    )
    fetch_events.add_argument(
        "--symbols",
        help="Comma-separated tickers/source symbols to fetch per-symbol event data",
    )
    fetch_events.add_argument(
        "--skip-money-flow",
        action="store_true",
        help="Skip current money flow import",
    )

    import_events = subparsers.add_parser("import-events-csv", help="Import US/HK/CN corporate events from CSV")
    import_events.add_argument("--path", required=True, help="CSV path with market,ticker,event_date,event_type,title")

    screen = subparsers.add_parser("screen", help="Run local screeners and save candidates")
    screen.add_argument("--as-of", required=True, help="Screening date")

    eval_candidates = subparsers.add_parser("evaluate-candidates", help="Evaluate candidates after their signal date")
    eval_candidates.add_argument("--candidate-date", required=True, help="Candidate date")
    eval_candidates.add_argument("--through", required=True, help="Evaluation through date")

    replay = subparsers.add_parser("replay", help="Replay candidate screening over a historical date range")
    replay.add_argument("--start", required=True, help="Replay start date")
    replay.add_argument("--end", required=True, help="Replay end date")
    replay.add_argument("--through", required=True, help="Evaluate every candidate through this date")
    replay.add_argument("--out", help="Output markdown path")
    replay.add_argument(
        "--fetch-cn-a-intraday",
        action="store_true",
        help="Fetch 5-minute A-share bars for replay candidates before evaluation",
    )
    replay.add_argument("--intraday-period", choices=["1", "5", "15", "30", "60"], default="5")
    replay.add_argument("--intraday-throttle", type=float, default=0.15)
    replay.add_argument("--require-adjusted", action="store_true", help="Exclude CN_A evaluations without adjusted prices")
    replay.add_argument("--benchmark", default="auto", help="Benchmark ticker or auto for CN_A layered benchmarks")

    tune_weights = subparsers.add_parser("tune-weights", help="Suggest or apply strategy weight changes from replay")
    tune_weights.add_argument("--start", required=True, help="Replay start date")
    tune_weights.add_argument("--end", required=True, help="Replay end date")
    tune_weights.add_argument("--through", required=True, help="Evaluation through date")
    tune_weights.add_argument("--min-samples", type=int, default=5, help="Minimum evaluated samples before changing weight")
    tune_weights.add_argument("--apply", action="store_true", help="Apply DOWN_WEIGHT recommendations")

    audit = subparsers.add_parser("audit", help="Audit strategy health and decay risk")
    audit.add_argument("--as-of", required=True, help="Audit date")

    daily_plan = subparsers.add_parser(
        "daily-plan",
        help="Legacy/research daily plan; production uses production-run -> production-daily",
    )
    daily_plan.add_argument("--as-of", required=True, help="Candidate date")
    daily_plan.add_argument("--out", help="Output markdown path")

    candidates = subparsers.add_parser("candidates", help="List candidates for a date")
    candidates.add_argument("--as-of", required=True, help="Candidate date")
    subparsers.add_parser("verify", help="Verify optional manual signal hashes")

    confirm = subparsers.add_parser("confirm-candidates", help="Confirm or cancel WATCH_CONFIRMATION candidates")
    confirm.add_argument("--as-of", required=True, help="Confirmation date")

    calibrate = subparsers.add_parser("score-calibration", help="Validate score predictive power")
    calibrate.add_argument("--start", required=True)
    calibrate.add_argument("--end", required=True)
    calibrate.add_argument("--through", required=True)
    calibrate.add_argument("--horizon", type=int, default=10)

    portfolio = subparsers.add_parser("portfolio-backtest", help="Portfolio-level backtest")
    portfolio.add_argument("--start", required=True)
    portfolio.add_argument("--end", required=True)
    portfolio.add_argument("--through", required=True)
    portfolio.add_argument("--max-positions", type=int, default=5)
    portfolio.add_argument("--capital", type=float, default=1_000_000)
    portfolio.add_argument(
        "--cost-bps",
        type=int,
        default=None,
        help="Optional custom round-trip trading cost in basis points; default uses market-specific costs",
    )
    portfolio.add_argument("--cooldown-days", type=int, default=10)
    portfolio.add_argument("--execution", choices=("intraday", "daily"), default="intraday")
    portfolio.add_argument("--benchmark", default="auto", help="Benchmark ticker or auto for CN_A layered benchmarks")
    portfolio.add_argument("--require-intraday", action="store_true", help="Exclude orders that lack intraday VWAP entry data")
    portfolio.add_argument("--drawdown-reduce-threshold", type=float, default=10.0)
    portfolio.add_argument("--drawdown-halt-threshold", type=float, default=20.0)
    portfolio.add_argument("--max-per-sector", type=int, default=2)
    portfolio.add_argument("--sizing-mode", choices=("equal", "risk-parity"), default="equal")
    portfolio.add_argument("--risk-budget-pct", type=float, default=2.0)
    portfolio.add_argument("--max-position-pct", type=float, default=25.0)
    portfolio.add_argument("--out", help="Output markdown path")

    walk_forward = subparsers.add_parser("walk-forward", help="Walk-forward sanity check and readiness report")
    walk_forward.add_argument("--start", required=True)
    walk_forward.add_argument("--end", required=True)
    walk_forward.add_argument("--out", help="Output markdown path")

    validate = subparsers.add_parser("validate", help="Statistical validation of portfolio backtest (Monte Carlo, Bootstrap, Walk-Forward)")
    validate.add_argument("--start", required=True)
    validate.add_argument("--end", required=True)
    validate.add_argument("--through", required=True)
    validate.add_argument("--max-positions", type=int, default=5)
    validate.add_argument("--capital", type=float, default=1_000_000)
    validate.add_argument("--cooldown-days", type=int, default=10)
    validate.add_argument("--execution", choices=["intraday", "daily"], default="intraday")
    validate.add_argument("--benchmark", default="auto")
    validate.add_argument("--mc-permutations", type=int, default=1000, help="Monte Carlo permutation count")
    validate.add_argument("--bootstrap-samples", type=int, default=1000, help="Bootstrap resample count")
    validate.add_argument("--wf-windows", type=int, default=5, help="Walk-forward window count")
    validate.add_argument("--out", help="Output markdown path")

    loss_review = subparsers.add_parser("loss-review", help="Review losing samples and tag recurring failure modes")
    loss_review.add_argument("--start", required=True)
    loss_review.add_argument("--end", required=True)
    loss_review.add_argument("--through", required=True)
    loss_review.add_argument("--out", help="Output markdown path")

    export_qlib = subparsers.add_parser("export-qlib-csv", help="Export price_bars to Qlib-compatible CSV")
    export_qlib.add_argument("--start", required=True, help="Start date, e.g. 2024-01-01")
    export_qlib.add_argument("--end", required=True, help="End date, e.g. 2026-05-29")
    export_qlib.add_argument("--output", default="data/qlib_export", help="Output directory")
    export_qlib.add_argument(
        "--mode",
        choices=["raw_adjusted"],
        default="raw_adjusted",
        help="Export mode: raw_adjusted uses adj_* prices directly",
    )
    export_qlib.add_argument("--markets", default="CN_A", help="Comma-separated markets, e.g. CN_A")

    import_pred = subparsers.add_parser("import-qlib-predictions", help="Import Qlib pred.pkl into model_scores")
    import_pred.add_argument("--artifact", required=True, help="Path to pred.pkl file")
    import_pred.add_argument("--model-name", required=True, help="Model name, e.g. qlib_alpha360_lgb")
    import_pred.add_argument("--model-version", required=True, help="Model version, e.g. smoke_v1")
    import_pred.add_argument("--market", default="CN_A", help="Market identifier")
    import_pred.add_argument("--out-dir", default="reports", help="Output directory for import report")

    qlib_refresh_cmd = subparsers.add_parser("qlib-refresh", help="Refresh Qlib bin data and record dataset version")
    qlib_refresh_cmd.add_argument("--as-of", required=True)
    qlib_refresh_cmd.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    qlib_refresh_cmd.add_argument("--max-workers", type=int, default=8)
    qlib_refresh_cmd.add_argument("--output-root", default=None)

    model_predict_cmd = subparsers.add_parser("model-predict", help="Run production model inference without training")
    model_predict_cmd.add_argument("--as-of", required=True)
    model_predict_cmd.add_argument("--models", choices=("production",), default="production")
    model_predict_cmd.add_argument("--output-dir", default=None)

    production_daily_cmd = subparsers.add_parser("production-daily", help="Run production daily report from prepared data")
    production_daily_cmd.add_argument("--as-of", required=True)
    production_daily_cmd.add_argument(
        "--skip-data-update",
        action="store_true",
        help="Deprecated no-op: production-daily is read-only by default",
    )
    production_daily_cmd.add_argument(
        "--allow-inline-data-update",
        action="store_true",
        help="Explicitly allow a core-only inline data update before report generation",
    )
    production_daily_cmd.add_argument("--output-dir", default=None)
    production_daily_cmd.add_argument("--no-overwrite", action="store_true")

    production_run_cmd = subparsers.add_parser("production-run", help="Run the single production data/model/report pipeline")
    production_run_cmd.add_argument("--as-of", required=True)
    production_run_cmd.add_argument("--skip-data-update", action="store_true")
    production_run_cmd.add_argument("--no-overwrite", action="store_true")
    production_run_cmd.add_argument("--output-root", default=None)

    production_async_cmd = subparsers.add_parser(
        "production-async",
        help="Run slow production-adjacent data tasks without blocking/overwriting the formal daily report",
    )
    production_async_cmd.add_argument("--as-of", required=True)
    production_async_cmd.add_argument("--output-dir", default=None)

    governance = subparsers.add_parser("model-governance", help="Model governance utilities")
    governance_sub = governance.add_subparsers(dest="governance_command", required=True)
    governance_review = governance_sub.add_parser("review", help="Review recent production model health")
    governance_review.add_argument("--as-of", required=True)
    governance_review.add_argument("--output-dir", default=None)

    model_arena_cmd = subparsers.add_parser("model-arena", help="Train and compare research model pool")
    model_arena_cmd.add_argument("--as-of", required=True)
    model_arena_cmd.add_argument("--pool", choices=("baseline18",), default="baseline18")
    model_arena_cmd.add_argument("--max-workers", type=int, default=1)
    model_arena_cmd.add_argument("--dry-run", action="store_true")
    model_arena_cmd.add_argument("--output-dir", default=None)
    model_arena_cmd.add_argument("--resume-run-id", default=None)

    model_evaluate_cmd = subparsers.add_parser("model-evaluate", help="Run fixed-test validation for research model artifacts")
    model_evaluate_cmd.add_argument("--pool", choices=("baseline18",), default="baseline18")
    model_evaluate_cmd.add_argument("--model-version", required=True)
    model_evaluate_cmd.add_argument("--as-of", required=True)
    model_evaluate_cmd.add_argument("--mode", choices=("fixed-test",), default="fixed-test")
    model_evaluate_cmd.add_argument("--output-dir", default=None)

    audit_tickers = subparsers.add_parser("audit-tickers", help="Dry-run audit of ticker normalization (.SH → .SS)")
    audit_tickers.add_argument("--out-dir", default="reports", help="Output directory for audit report")
    audit_tickers.add_argument("--limit", type=int, default=20, help="Max issues to print to stdout")

    repair_tickers_cmd = subparsers.add_parser("repair-tickers", help="Repair .SH → .SS normalization (dry-run by default)")
    repair_tickers_cmd.add_argument("--apply", action="store_true", help="Actually apply repairs (without this flag, runs as dry-run)")
    repair_tickers_cmd.add_argument("--out-dir", default="reports", help="Output directory for repair report")
    repair_tickers_cmd.add_argument("--limit", type=int, default=20, help="Max issues to print to stdout")

    backfill_qfq = subparsers.add_parser("backfill-qfq", help="Backfill forward-adjusted (qfq) CN_A prices")
    backfill_qfq.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    backfill_qfq.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    backfill_qfq.add_argument("--throttle", type=float, default=0.3, help="Seconds between API calls")
    backfill_qfq.add_argument("--limit", type=int, default=None, help="Max tickers to process (smoke runs)")
    backfill_qfq.add_argument("--tickers", default=None, help="Comma-separated ticker subset")
    backfill_qfq.add_argument("--commit-every", type=int, default=50, help="Commit batch size")
    backfill_qfq.add_argument(
        "--source",
        choices=["baostock", "auto"],
        default="baostock",
        help="Adjustment source: baostock (BaoStock only, no fallback) or auto (BaoStock first, AkShare fallback)",
    )
    backfill_qfq.add_argument("--out-dir", default="reports", help="Output directory for backfill report")
    backfill_qfq.add_argument("--dry-run", action="store_true", help="Report targets without network or DB writes")

    qfq_maintenance = subparsers.add_parser(
        "qfq-maintenance",
        help="Run periodic CN_A forward-adjustment maintenance when the interval has elapsed",
    )
    qfq_maintenance.add_argument("--as-of", required=True, help="Maintenance date YYYY-MM-DD")
    qfq_maintenance.add_argument("--interval-days", type=int, default=14, help="Minimum days between successful runs")
    qfq_maintenance.add_argument("--lookback-days", type=int, default=45, help="Recent window to repair")
    qfq_maintenance.add_argument(
        "--mode",
        choices=("backfill", "scan-and-repair"),
        default="backfill",
        help="backfill keeps the legacy BaoStock/AkShare path; scan-and-repair detects pre_close breaks first",
    )
    qfq_maintenance.add_argument("--throttle", type=float, default=0.3, help="Seconds between API calls")
    qfq_maintenance.add_argument("--limit", type=int, default=None, help="Max tickers to process")
    qfq_maintenance.add_argument("--commit-every", type=int, default=50, help="Commit batch size")
    qfq_maintenance.add_argument(
        "--source",
        choices=["baostock", "auto"],
        default="auto",
        help="Adjustment source: auto uses BaoStock first and AkShare fallback",
    )
    qfq_maintenance.add_argument("--out-dir", default="reports/qfq_maintenance", help="Output directory")
    qfq_maintenance.add_argument("--force", action="store_true", help="Run even if the maintenance interval has not elapsed")
    qfq_maintenance.add_argument("--dry-run", action="store_true", help="Report targets without network or DB writes")

    detect_adj = subparsers.add_parser(
        "detect-adjustment-breaks",
        help="Detect CN_A ex-right/ex-dividend adjustment breaks from pre_close",
    )
    detect_adj.add_argument("--as-of", required=True, help="Detection date YYYY-MM-DD")
    detect_adj.add_argument("--market", default="CN_A")

    repair_daily = subparsers.add_parser(
        "qfq-repair-daily",
        help="Repair queued CN_A qfq factors using explicit pre_close factor ratios",
    )
    repair_daily.add_argument("--as-of", required=True, help="Repair date YYYY-MM-DD")
    repair_daily.add_argument("--market", default="CN_A")

    enrich_daily = subparsers.add_parser(
        "enrich-daily-bars",
        help="Enrich CN_A price_bars with BaoStock amount and turnover_pct",
    )
    enrich_daily.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    enrich_daily.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    enrich_daily.add_argument("--throttle", type=float, default=0.3, help="Seconds between API calls")
    enrich_daily.add_argument("--limit", type=int, default=None, help="Max tickers to process (smoke runs)")
    enrich_daily.add_argument("--tickers", default=None, help="Comma-separated ticker subset")
    enrich_daily.add_argument("--commit-every", type=int, default=50, help="Commit batch size")
    enrich_daily.add_argument("--out-dir", default="reports", help="Output directory for enrichment report")
    enrich_daily.add_argument("--dry-run", action="store_true", help="Report targets without network or DB writes")

    fetch_nb = subparsers.add_parser("fetch-northbound", help="Fetch northbound (HSGT) daily flow data")
    fetch_nb.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: fetch all history)")

    fetch_mt = subparsers.add_parser("fetch-margin", help="Fetch margin trading daily detail")
    fetch_mt.add_argument("--as-of", required=True, help="Date YYYY-MM-DD")

    return parser


def command_init(db_path: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
    print(f"Initialized database: {db_path}")


def command_seed(db_path: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        counts = seed_all(conn)
    print(
        "Seeded "
        f"{counts['strategies']} strategies, "
        f"{counts['price_bars']} price bars, "
        f"{counts['research_events']} research events."
    )


def command_bootstrap(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        counts = seed_all(conn)
        candidate_count = screen_all(conn, as_of)
        audit_count = audit_all(conn, as_of)
        report_path = write_daily_plan(conn, as_of)
    print(
        "Bootstrap complete: "
        f"{counts['strategies']} strategies, "
        f"{counts['price_bars']} price bars, "
        f"{counts['research_events']} research events, "
        f"{candidate_count} candidates, "
        f"{audit_count} strategy audits, "
        f"daily_plan={report_path}"
    )


def _split_csv(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def command_fetch_prices(
    db_path: str,
    universe_path: str,
    start: str,
    end: str,
    source: str,
    markets: str | None,
    symbols: str | None,
    throttle: float,
    include_benchmarks: bool,
    adjust: str,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        market_filter = _split_csv(markets)
        symbol_filter = _split_csv(symbols)
        instruments = []
        if source in {"universe", "both"}:
            instruments.extend(read_universe(Path(universe_path), markets=market_filter, symbols=symbol_filter))
        if source in {"db", "both"}:
            instruments.extend(read_db_instruments(conn, markets=market_filter, symbols=symbol_filter))
        should_include_cn_a_benchmark = (
            (include_benchmarks or symbol_filter is None)
            and (market_filter is None or "CN_A" in market_filter)
        )
        if should_include_cn_a_benchmark:
            instruments.extend(CN_A_BENCHMARKS)
        instruments = list({(item.market, item.ticker): item for item in instruments}.values())
        if not instruments:
            print("No active instruments matched the requested filters.")
            return
        upsert_many(conn, "instruments", [instrument.as_row() for instrument in instruments], ("market", "ticker"))
        bars, errors = fetch_bars(
            instruments,
            start=parse_date(start),
            end=parse_date(end),
            throttle_seconds=throttle,
            adjust=None if adjust == "none" else adjust,
        )
        upserted = upsert_many(conn, "price_bars", bars, ("market", "ticker", "date"))
    print(
        f"Fetched {upserted} price bars for {len(instruments)} instruments "
        f"from {start} to {end}."
    )
    if errors:
        print("Fetch errors:")
        for error in errors:
            print(f"- {error}")


def command_data_update(
    db_path: str,
    as_of: str,
    markets: str,
    throttle: float,
    adjust: str,
    skip_events: bool,
    skip_intraday: bool,
    intraday_period: str,
    repair_coverage: bool,
    repair_scope: str,
    core_only: bool = False,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = data_update(
            conn,
            as_of,
            markets,
            throttle_seconds=throttle,
            fetch_events=False if core_only else not skip_events,
            fetch_intraday=False if core_only else not skip_intraday,
            intraday_period=intraday_period,
            price_mode="core" if core_only else "full",
            repair_coverage=repair_coverage,
            repair_scope=repair_scope,
            adjust=None if core_only or adjust == "none" else adjust,
        )
    print(
        f"Data update #{result.run_id}: status={result.status}, range={result.start_date}..{result.end_date}, "
        f"symbols={result.requested_symbols}, price_bars={result.price_bars}, intraday_bars={result.intraday_bars}, "
        f"events={result.corporate_events}, financials={result.financial_metrics}, money_flows={result.money_flows}, "
        f"errors={result.error_count}"
    )
    if getattr(result, "source_summary", ""):
        print(f"Source summary: {result.source_summary}")


def command_data_audit(
    db_path: str,
    start: str,
    end: str,
    markets: str,
    probe_adjustment: bool,
    probe_sample_size: int,
    ignore_adjustment_for_short_term: bool,
    throttle: float,
) -> None:
    selected = _split_csv(markets) or {"CN_A"}
    with closing(connect(db_path)) as conn:
        init_db(conn)
        for market in sorted(selected):
            result = audit_data_coverage(
                conn,
                start,
                end,
                market,
                write=True,
                ignore_adjustment_for_short_term=ignore_adjustment_for_short_term,
            )
            print(f"Data audit {market} {start}..{end}: confidence={result.confidence_level}")
            print(f"- latest_price_date={result.latest_price_date}, trading_days={result.trading_days}")
            print(f"- price_bars={result.price_bar_count}, adjusted={result.adjusted_bar_count} ({result.adjustment_coverage_pct:.1f}%)")
            print(f"- layered_benchmark_coverage={result.benchmark_coverage_pct:.1f}%, events={result.event_count}, financials={result.financial_count}, intraday_symbols={result.intraday_symbol_count}")
            print(
                "- intraday_tradable_coverage="
                f"{result.intraday_tradable_symbol_count}/{result.intraday_tradable_target_count} "
                f"({result.intraday_coverage_pct:.1f}%), "
                f"missing_tradable={result.intraday_tradable_missing_count}, "
                f"no_trade_symbols={result.intraday_no_trade_symbol_count}"
            )
            print(f"- allow_formal_daily={'yes' if result.allow_formal_daily else 'no'}")
            if result.missing_dates:
                print(f"- missing_dates={', '.join(result.missing_dates[:10])}")
            for note in result.notes:
                print(f"- NOTE: {note}")
            if probe_adjustment and market == "CN_A":
                probe = probe_adjustment_sources(
                    conn,
                    start,
                    end,
                    market,
                    sample_size=probe_sample_size,
                    throttle_seconds=throttle,
                )
                print(
                    f"- adjustment_probe: samples={probe.sample_count}, success={probe.success_count}, "
                    f"partial={probe.partial_count}, failed={probe.failed_count}, "
                    f"success_rate={probe.success_rate_pct:.1f}%"
                )
                for sample in probe.samples:
                    detail = f" error={sample.error}" if sample.error else ""
                    print(
                        f"  - {sample.ticker}: {sample.status}, adjusted_rows={sample.adjusted_rows}, "
                        f"raw_rows={sample.raw_rows}{detail}"
                    )


def _missing_model_predictions(conn, as_of: str, market: str = "CN_A") -> list[str]:
    missing: list[str] = []
    rows = conn.execute(
        """
        SELECT model_name, model_version
        FROM model_registry
        WHERE status = 'PRODUCTION'
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        model_name = str(row["model_name"])
        model_version = str(row["model_version"])
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM model_scores
            WHERE market = ? AND model_name = ? AND model_version = ? AND score_date = ?
            """,
            (market, model_name, model_version, as_of),
        ).fetchone()
        if not row or int(row["count"]) == 0:
            missing.append(model_name)
    return missing


def _print_model_prediction_hint(missing_models: list[str], as_of: str) -> None:
    if not missing_models:
        return
    print(
        "WARNING: model_scores 缺少 "
        f"{as_of} 的模型预测 ({', '.join(missing_models)}). "
        "正式生产请运行 python -m alpha_ledger production-run --as-of "
        f"{as_of}"
    )


def command_daily_run(db_path: str, as_of: str, throttle: float, fast: bool = False, skip_model_update: bool = False) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        update = data_update(
            conn, as_of, "CN_A",
            throttle_seconds=throttle,
            adjust=None if fast else "qfq",
            fetch_events=not fast,
            fetch_intraday=not fast,
            price_mode="full",
        )
        audit = audit_data_coverage(
            conn,
            as_of,
            as_of,
            "CN_A",
            write=True,
            ignore_adjustment_for_short_term=True,
        )
        missing_models = _missing_model_predictions(conn, as_of)
        if missing_models and not skip_model_update:
            print("WARNING: daily-run is legacy/research; automatic legacy model update is disabled.")
        _print_model_prediction_hint(missing_models, as_of)
        candidate_count = screen_all(conn, as_of)
        confirmed = confirm_candidates(conn, as_of)
        confirmed_pb = confirm_pullback_candidates(conn, as_of)
        report_path = write_daily_plan(conn, as_of)
        start = (parse_date(as_of) - timedelta(days=20)).isoformat()
        portfolio = run_portfolio_backtest(conn, start, as_of, as_of, benchmark_ticker="auto")
        portfolio_path = write_portfolio_report(portfolio, Path("reports") / f"portfolio_backtest_latest_{as_of}.md")
    phase_label = "fast" if fast else "full"
    print(
        f"Daily run ({phase_label}) {as_of}: update={update.status}, confidence={audit.confidence_level}, "
        f"candidates={candidate_count}, confirmed={confirmed[0]}, cancelled={confirmed[1]}, "
        f"daily_plan={report_path}, portfolio={portfolio_path}"
    )


def command_daily_events(db_path: str, as_of: str, throttle: float) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        update = data_update(
            conn, as_of, "CN_A",
            throttle_seconds=throttle,
            adjust=None,
            fetch_events=True,
            fetch_intraday=True,
            price_mode="none",
        )
        candidate_count = screen_all(conn, as_of)
        confirmed = confirm_candidates(conn, as_of)
        confirmed_pb = confirm_pullback_candidates(conn, as_of)
        report_path = write_daily_plan(conn, as_of)
        start = (parse_date(as_of) - timedelta(days=20)).isoformat()
        portfolio = run_portfolio_backtest(conn, start, as_of, as_of, benchmark_ticker="auto")
        portfolio_path = write_portfolio_report(portfolio, Path("reports") / f"portfolio_backtest_latest_{as_of}.md")
    print(
        f"Daily events {as_of}: update={update.status}, candidates={candidate_count}, "
        f"confirmed={confirmed[0]}, cancelled={confirmed[1]}, "
        f"daily_plan={report_path}, portfolio={portfolio_path}"
    )


def command_data_backfill(
    db_path: str,
    start: str,
    end: str,
    markets: str,
    batch_days: int,
    throttle: float,
    adjust: str,
) -> None:
    selected = _split_csv(markets) or {"CN_A"}
    if selected != {"CN_A"}:
        raise SystemExit("data-backfill v1 only supports CN_A")
    start_day = parse_date(start)
    end_day = parse_date(end)
    with closing(connect(db_path)) as conn:
        init_db(conn)
        instruments = []
        instruments.extend(read_universe(DEFAULT_UNIVERSE_PATH, markets={"CN_A"}))
        instruments.extend(read_db_instruments(conn, markets={"CN_A"}))
        instruments.extend(CN_A_BENCHMARKS)
        instruments = list({(item.market, item.ticker): item for item in instruments}.values())
        upsert_many(conn, "instruments", [item.as_row() for item in instruments], ("market", "ticker"))
        cursor = start_day
        total_bars = 0
        total_errors = 0
        while cursor <= end_day:
            batch_end = min(cursor + timedelta(days=max(batch_days - 1, 0)), end_day)
            run = conn.execute(
                """
                INSERT INTO data_fetch_runs (run_type, market, start_date, end_date, status, requested_symbols, started_at)
                VALUES ('data-backfill', 'CN_A', ?, ?, 'RUNNING', ?, datetime('now'))
                """,
                (cursor.isoformat(), batch_end.isoformat(), len(instruments)),
            )
            run_id = int(run.lastrowid)
            bars, errors = fetch_bars(
                instruments,
                cursor,
                batch_end,
                throttle_seconds=throttle,
                adjust=None if adjust == "none" else adjust,
            )
            count = upsert_many(conn, "price_bars", bars, ("market", "ticker", "date"))
            total_bars += count
            total_errors += len(errors)
            for error in errors:
                conn.execute(
                    """
                    INSERT INTO data_fetch_errors (run_id, market, ticker, source, error_message, created_at)
                    VALUES (?, 'CN_A', ?, 'price_bars', ?, datetime('now'))
                    """,
                    (run_id, error.split(" ", 1)[0], error),
                )
            status = "SUCCESS" if not errors else ("PARTIAL_SUCCESS" if count else "FAILED")
            conn.execute(
                """
                UPDATE data_fetch_runs
                SET status = ?, price_bars = ?, error_count = ?, finished_at = datetime('now')
                WHERE id = ?
                """,
                (status, count, len(errors), run_id),
            )
            conn.commit()
            print(f"Backfill batch {cursor.isoformat()}..{batch_end.isoformat()}: status={status}, bars={count}, errors={len(errors)}")
            cursor = batch_end + timedelta(days=1)
    print(f"Backfill complete: bars={total_bars}, errors={total_errors}")


def command_fetch_intraday(
    db_path: str,
    universe_path: str,
    start: str,
    end: str,
    source: str,
    symbols: str | None,
    period: str,
    throttle: float,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        symbol_filter = _split_csv(symbols)
        market_filter = {"CN_A"}
        instruments = []
        if source in {"universe", "both"}:
            instruments.extend(read_universe(Path(universe_path), markets=market_filter, symbols=symbol_filter))
        if source in {"db", "both"}:
            instruments.extend(read_db_instruments(conn, markets=market_filter, symbols=symbol_filter))
        instruments = list({(item.market, item.ticker): item for item in instruments}.values())
        if not instruments:
            print("No active CN_A instruments matched the requested filters.")
            return
        upsert_many(conn, "instruments", [instrument.as_row() for instrument in instruments], ("market", "ticker"))
        bars, errors = fetch_intraday_bars(
            instruments,
            start=parse_date(start),
            end=parse_date(end),
            period=period,
            throttle_seconds=throttle,
        )
        upserted = upsert_many(conn, "intraday_bars", bars, ("market", "ticker", "datetime"))
    print(
        f"Fetched {upserted} intraday bars for {len(instruments)} CN_A instruments "
        f"from {start} to {end}, period={period}m."
    )
    if errors:
        print("Intraday fetch errors:")
        for error in errors:
            print(f"- {error}")


def command_fetch_events(
    db_path: str,
    start: str,
    end: str,
    notice_limit_per_day: int,
    markets: str | None,
    symbols: str | None,
    skip_money_flow: bool,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        market_filter = _split_csv(markets)
        symbol_filter = _split_csv(symbols)
        instruments = read_db_instruments(conn, markets=market_filter, symbols=symbol_filter)
        if not instruments:
            instruments = read_universe(DEFAULT_UNIVERSE_PATH, markets=market_filter, symbols=symbol_filter)
            upsert_many(conn, "instruments", [instrument.as_row() for instrument in instruments], ("market", "ticker"))
        result = fetch_events_to_db(
            conn,
            start=parse_date(start),
            end=parse_date(end),
            instruments=instruments,
            notice_limit_per_day=notice_limit_per_day,
            fetch_money_flow=not skip_money_flow,
        )
    print(
        "Fetched event data: "
        f"corporate_events={result.corporate_events}, "
        f"financial_metrics={result.financial_metrics}, "
        f"money_flows={result.money_flows}, "
        f"instruments={result.instruments}."
    )
    if result.errors:
        print("Event fetch warnings:")
        for error in result.errors:
            print(f"- {error}")


def command_import_events_csv(db_path: str, path: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = import_events_csv(conn, Path(path))
    print(
        "Imported events: "
        f"corporate_events={result.corporate_events}, "
        f"instruments={result.instruments}."
    )
    if result.errors:
        print("Import warnings:")
        for error in result.errors:
            print(f"- {error}")


def command_screen(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        count = screen_all(conn, as_of)
    print(f"Screened {count} candidates as of {as_of}.")


def command_evaluate_candidates(db_path: str, candidate_date: str, through: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        count = evaluate_candidates(conn, candidate_date, through)
        horizon_count = evaluate_candidate_horizons_for_date(conn, candidate_date, through)
    print(
        f"Evaluated {count} through-date candidates and {horizon_count} fixed-horizon rows "
        f"from {candidate_date} through {through}."
    )


def command_replay(
    db_path: str,
    start: str,
    end: str,
    through: str,
    out: str | None,
    fetch_cn_a_intraday: bool,
    intraday_period: str,
    intraday_throttle: float,
    require_adjusted: bool,
    benchmark: str | None,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = replay_candidates(
            conn,
            start,
            end,
            through,
            fetch_cn_a_intraday=fetch_cn_a_intraday,
            intraday_period=intraday_period,
            intraday_throttle_seconds=intraday_throttle,
            require_adjusted=require_adjusted,
            benchmark_ticker=benchmark,
        )
        path = write_replay_report(conn, start, end, through, Path(out) if out else None)
    print(
        "Replay complete: "
        f"dates={result.dates}, candidates={result.candidates}, "
        f"evaluations={result.evaluations}, horizon_evaluations={result.horizon_evaluations}, "
        f"intraday_bars={result.intraday_bars}, intraday_errors={result.intraday_errors}, report={path}"
    )


def command_tune_weights(
    db_path: str,
    start: str,
    end: str,
    through: str,
    min_samples: int,
    apply_changes: bool,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        suggestions = suggest_strategy_weight_adjustments(
            conn,
            start,
            end,
            through,
            min_samples=min_samples,
        )
        if apply_changes:
            changed = apply_strategy_weight_adjustments(
                conn,
                start,
                end,
                through,
                min_samples=min_samples,
            )
        else:
            changed = 0
    if not suggestions:
        print("No weight suggestions.")
        return
    for item in suggestions:
        print(
            f"{item['strategy_name']} ({item['strategy_id']}): "
            f"{item['recommendation']} current={float(item['current_weight']):.2f} "
            f"suggested={float(item['suggested_weight']):.2f} "
            f"samples={item['evaluated_count']} reason={item['reason']}"
        )
    if apply_changes:
        print(f"Applied {changed} DOWN_WEIGHT adjustments.")


def command_audit(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        count = audit_all(conn, as_of)
        rows = latest_audits(conn, as_of)
    print(f"Audited {count} strategies as of {as_of}.")
    for row in rows:
        print(
            f"{row['strategy_name']} ({row['strategy_id']}): "
            f"status={row['health_status']} edge={row['edge_score']:.1f} "
            f"sample={row['sample_quality_score']:.2f} crowding={row['crowding_risk_score']:.2f} "
            f"decay={row['decay_risk_score']:.2f} notes={row['notes']}"
        )


def command_daily_plan(db_path: str, as_of: str, out: str | None) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        path = write_daily_plan(conn, as_of, Path(out) if out else None)
    print(f"Wrote daily plan: {path}")


def command_candidates(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        rows = latest_candidates(conn, as_of)
    if not rows:
        print(f"No candidates as of {as_of}.")
        return
    for row in rows:
        print(
            f"#{row['id']} {row['as_of_date']} {row['name']} {row['ticker']} "
            f"{row['market']} strategy={row['strategy_name']} score={_fmt_float(row['candidate_score'], 1)} "
            f"entry={_fmt_float(row['entry_price'])} stop={_fmt_float(row['stop_loss'])} "
            f"target1={_fmt_float(row['target_1'])} action={row['action']}"
        )


def command_audit_tickers(db_path: str, out_dir: str, limit: int = 20) -> None:
    """Dry-run audit: check ticker normalization without modifying data."""
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = audit_ticker_normalization(conn)
        md_path, json_path = write_ticker_audit_report(result, Path(out_dir))
    print(
        f"Ticker audit: {result.total_instruments} instruments, "
        f"{result.canonical_count} canonical, "
        f"{result.needs_normalization} need normalization, "
        f"{result.unknown_suffix} unknown suffix. "
        f"Reports: {md_path}, {json_path}"
    )
    if result.issues:
        shown = result.issues[: max(0, limit)]
        print(f"Issues found (showing {len(shown)} of {len(result.issues)}):")
        for issue in shown:
            print(f"  - {issue['ticker']}: {issue['detail']}")


def command_repair_tickers(db_path: str, apply: bool, out_dir: str, limit: int = 20) -> None:
    """Audit and optionally repair .SH → .SS normalization."""
    with closing(connect(db_path)) as conn:
        init_db(conn)
        if apply:
            result = repair_tickers(conn)
            print(
                f"Ticker repair applied: "
                f"{result.instruments_merged} instruments, "
                f"{result.price_bars_merged} price bars, "
                f"{result.other_tables_merged} other rows merged "
                f"({result.total_merged} total)."
            )
        else:
            result = audit_ticker_repair(conn)
            print(
                f"Ticker repair (dry-run): "
                f"{result.total_canonical} canonical, "
                f"{result.total_needs_normalization} need normalization, "
                f"{result.total_unknown_suffix} unknown suffix, "
                f"{result.total_conflicts} conflicts."
            )
        md_path, json_path = write_ticker_repair_report(result, Path(out_dir))
        print(f"Reports: {md_path}, {json_path}")

        conflicts = result.all_conflict_examples
        if conflicts:
            shown = conflicts[:max(0, limit)]
            print(f"Conflicts (showing {len(shown)} of {len(conflicts)}):")
            for c in shown:
                print(f"  - {c['table']}: {c['sh_ticker']} → {c['canonical_ticker']}")


def command_backfill_qfq(
    db_path: str,
    start: str,
    end: str,
    source: str,
    throttle: float,
    limit: int | None,
    tickers: str | None,
    commit_every: int,
    out_dir: str,
    dry_run: bool,
) -> None:
    """Backfill forward-adjusted CN_A prices from BaoStock/AkShare."""
    ticker_subset = _split_csv(tickers)

    # For dry-run, only print per-ticker progress if limit is small (≤50)
    _quiet_dry_run = dry_run and (limit is None or limit > 50)

    def _progress(i: int, total: int, ticker: str, updated: int, errors: int) -> None:
        if total == 0:
            return
        if _quiet_dry_run:
            return  # dry-run with many tickers: only print final summary
        if (i + 1) % 50 == 0 or i == total - 1:
            print(f"[{i + 1}/{total}] {ticker} | updated={updated} errors={errors}")

    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = qfq_backfill(
            conn,
            start,
            end,
            source=source,
            throttle=throttle,
            limit=limit,
            tickers_subset=ticker_subset,
            commit_every=commit_every,
            dry_run=dry_run,
            progress_fn=_progress,
        )
        md_path, json_path = write_qfq_backfill_report(result, Path(out_dir))

    mode = "DRY-RUN" if dry_run else "done"
    print(
        f"QFQ backfill ({mode}): "
        f"target={result.target_count}, "
        f"benchmarks_skipped={result.skipped_benchmarks}, "
        f"updated_rows={result.updated_rows}, "
        f"errors={result.skipped_errors}, "
        f"baostock={result.baostock_count}, "
        f"akshare={result.akshare_count}, "
        f"elapsed={result.elapsed_seconds:.1f}s. "
        f"Reports: {md_path}, {json_path}"
    )
    if result.ticker_errors:
        shown = result.ticker_errors[:20]
        print(f"Errors (showing {len(shown)} of {len(result.ticker_errors)}):")
        for e in shown:
            print(f"  - {e}")


def command_qfq_maintenance(
    db_path: str,
    as_of: str,
    interval_days: int,
    lookback_days: int,
    source: str,
    throttle: float,
    limit: int | None,
    commit_every: int,
    out_dir: str,
    force: bool,
    dry_run: bool,
    mode: str = "backfill",
) -> None:
    """Run periodic qfq maintenance for a recent CN_A window.

    This is intentionally separate from the daily production pipeline.  It is a
    maintenance job for keeping short/medium-horizon research prices clean after
    dividends and ex-right events, not a blocking prerequisite for daily reports.
    """
    as_of_date = date.fromisoformat(as_of)
    start_date = as_of_date - timedelta(days=max(lookback_days, 0))
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    with closing(connect(db_path)) as conn:
        init_db(conn)
        last_row = conn.execute(
            """
            SELECT end_date FROM data_fetch_runs
            WHERE run_type = 'qfq-maintenance'
              AND market = 'CN_A'
              AND status = 'SUCCESS'
            ORDER BY end_date DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if last_row is not None and not force:
            last_date = date.fromisoformat(str(last_row["end_date"]))
            elapsed_days = (as_of_date - last_date).days
            if elapsed_days < interval_days:
                report_path = out_path / f"qfq_maintenance_{as_of_date:%Y%m%d}_SKIPPED.md"
                report_path.write_text(
                    "\n".join(
                        [
                            f"# QFQ Maintenance {as_of}",
                            "",
                            "- status: SKIPPED",
                            f"- last_success: {last_date.isoformat()}",
                            f"- elapsed_days: {elapsed_days}",
                            f"- interval_days: {interval_days}",
                            "",
                            "Use `--force` to run before the interval elapses.",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                print(
                    f"QFQ maintenance skipped: last_success={last_date.isoformat()}, "
                    f"elapsed_days={elapsed_days}, interval_days={interval_days}, report={report_path}"
                )
                return

        run_id: int | None = None
        if not dry_run:
            cursor = conn.execute(
                """
                INSERT INTO data_fetch_runs
                    (run_type, market, start_date, end_date, status, requested_symbols, started_at)
                VALUES ('qfq-maintenance', 'CN_A', ?, ?, 'RUNNING', 0, ?)
                """,
                (start_date.isoformat(), as_of_date.isoformat(), now_utc()),
            )
            run_id = int(cursor.lastrowid)
            conn.commit()

        result = qfq_backfill(
            conn,
            start_date.isoformat(),
            as_of_date.isoformat(),
            source=source,
            throttle=throttle,
            limit=limit,
            commit_every=commit_every,
            dry_run=dry_run,
        ) if mode == "backfill" else None
        if mode == "scan-and-repair":
            if dry_run:
                scan_dir = out_path / as_of
                scan_dir.mkdir(parents=True, exist_ok=True)
                md_path = scan_dir / "summary.md"
                json_path = scan_dir / "details.json"
                md_path.write_text(
                    "\n".join(
                        [
                            f"# QFQ Maintenance Scan {as_of}",
                            "",
                            "- status: DRY_RUN",
                            "- no database changes were made",
                            "- run without --dry-run to detect breaks and repair queued factors",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                json_path.write_text('{"status":"DRY_RUN"}\n', encoding="utf-8")
                result = type("ScanSummary", (), {
                    "target_count": 0,
                    "updated_rows": 0,
                    "skipped_errors": 0,
                    "ticker_errors": [],
                })()
            else:
                scan_result = qfq_maintenance_scan_and_repair(
                    conn,
                    as_of=as_of,
                    start_date=start_date.isoformat(),
                    end_date=as_of_date.isoformat(),
                    out_dir=out_path,
                )
                md_path = Path(scan_result.report_path)
                json_path = Path(scan_result.json_path)
                result = type("ScanSummary", (), {
                    "target_count": scan_result.repaired.target_count,
                    "updated_rows": scan_result.repaired.updated_rows,
                    "skipped_errors": scan_result.repaired.failed_count,
                    "ticker_errors": scan_result.repaired.errors,
                })()
        else:
            assert result is not None
            md_path, json_path = write_qfq_backfill_report(result, out_path)

        if not dry_run and run_id is not None:
            status = "SUCCESS" if result.skipped_errors == 0 else ("PARTIAL_SUCCESS" if result.target_count else "FAILED")
            conn.execute(
                """
                UPDATE data_fetch_runs
                SET status = ?,
                    requested_symbols = ?,
                    price_bars = ?,
                    error_count = ?,
                    notes = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    result.target_count,
                    result.updated_rows,
                    result.skipped_errors,
                    f"mode={mode}; source={source}; report={md_path}; json={json_path}",
                    now_utc(),
                    run_id,
                ),
            )
            for error in result.ticker_errors:
                conn.execute(
                    """
                    INSERT INTO data_fetch_errors (run_id, market, ticker, source, error_message, created_at)
                    VALUES (?, 'CN_A', ?, 'qfq-maintenance', ?, ?)
                    """,
                    (run_id, error.split(" ", 1)[0], error[:1000], now_utc()),
                )
            conn.commit()

    run_mode = f"{mode} DRY-RUN" if dry_run else mode
    print(
        f"QFQ maintenance ({run_mode}): "
        f"window={start_date.isoformat()}..{as_of_date.isoformat()}, "
        f"target={result.target_count}, updated_rows={result.updated_rows}, "
        f"errors={result.skipped_errors}, report={md_path}, json={json_path}"
    )


def command_detect_adjustment_breaks(db_path: str, as_of: str, market: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = detect_adjustment_breaks(conn, as_of, market=market)
    print(
        f"Adjustment breaks: as_of={as_of}, scanned={result.scanned}, "
        f"confirmed={result.confirmed}, suspected={result.suspected}, "
        f"ignored={result.ignored}, queued={result.queued}"
    )


def command_qfq_repair_daily(db_path: str, as_of: str, market: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = qfq_repair_daily(conn, as_of, market=market)
    print(
        f"QFQ repair daily: as_of={as_of}, targets={result.target_count}, "
        f"repaired={result.repaired_count}, failed={result.failed_count}, "
        f"updated_rows={result.updated_rows}"
    )
    if result.failed_count:
        for error in result.errors[:20]:
            print(f"  - {error}")
        raise SystemExit(1)


def command_enrich_daily_bars(
    db_path: str,
    start: str,
    end: str,
    throttle: float,
    limit: int | None,
    tickers: str | None,
    commit_every: int,
    out_dir: str,
    dry_run: bool,
) -> None:
    """Enrich CN_A price_bars with BaoStock amount and turnover_pct."""
    ticker_subset = _split_csv(tickers)

    _quiet_dry_run = dry_run and (limit is None or limit > 50)

    def _progress(i: int, total: int, ticker: str, updated: int, missing: int, errors: int) -> None:
        if total == 0:
            return
        if _quiet_dry_run:
            return
        if (i + 1) % 50 == 0 or i == total - 1:
            print(f"[{i + 1}/{total}] {ticker} | updated={updated} missing={missing} errors={errors}")

    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = enrich_daily_bars(
            conn,
            start,
            end,
            throttle=throttle,
            limit=limit,
            tickers_subset=ticker_subset,
            commit_every=commit_every,
            dry_run=dry_run,
            progress_fn=_progress,
        )
        md_path, json_path = write_enrichment_report(result, Path(out_dir))

    mode = "DRY-RUN" if dry_run else "done"
    print(
        f"Daily enrichment ({mode}): "
        f"target={result.target_count}, "
        f"benchmarks_skipped={result.skipped_benchmarks}, "
        f"updated_rows={result.updated_rows}, "
        f"missing_rows={result.missing_rows}, "
        f"errors={result.skipped_errors}, "
        f"elapsed={result.elapsed_seconds:.1f}s. "
        f"Reports: {md_path}, {json_path}"
    )
    if result.ticker_errors:
        shown = result.ticker_errors[:20]
        print(f"Errors (showing {len(shown)} of {len(result.ticker_errors)}):")
        for e in shown:
            print(f"  - {e}")


def command_fetch_northbound(db_path: str, start: str | None) -> None:
    from .data_ops import fetch_northbound_flows
    with closing(connect(db_path)) as conn:
        init_db(conn)
        written = fetch_northbound_flows(conn, start_date=start)
    print(f"Northbound flows: {written} rows written")


def command_fetch_margin(db_path: str, as_of: str) -> None:
    from .data_ops import fetch_margin_trading
    with closing(connect(db_path)) as conn:
        init_db(conn)
        written = fetch_margin_trading(conn, as_of)
    print(f"Margin trading ({as_of}): {written} rows written")


def command_verify(db_path: str) -> None:
    with closing(connect(db_path)) as conn:
        broken = verify_signals(conn)
    if not broken:
        print("Ledger verification passed: all immutable hashes match.")
        return
    print("Ledger verification failed:")
    for item in broken:
        print(
            f"signal #{item['id']} {item['ticker']} stored={item['stored_hash']} "
            f"expected={item['expected_hash']}"
        )
    raise SystemExit(1)


def command_confirm_candidates(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        confirmed, cancelled = confirm_candidates(conn, as_of)
        confirmed_pb, cancelled_pb, waiting_pb = confirm_pullback_candidates(conn, as_of)
    print(f"Confirmed {confirmed}, cancelled {cancelled} candidates as of {as_of}.")
    print(f"Pullback: confirmed {confirmed_pb}, cancelled {cancelled_pb}, waiting {waiting_pb}.")


def command_score_calibration(db_path: str, start: str, end: str, through: str, horizon: int) -> None:
    with closing(connect(db_path)) as conn:
        rows = score_calibration(conn, start, end, through, horizon)
    if not rows:
        print("No calibration data.")
        return
    print(f"Score calibration (T+{horizon}, {start} to {end}, through {through}):")
    print(f"{'Bucket':>10} {'Samples':>8} {'NetRet':>8} {'Bench':>8} {'Excess':>8} {'ExWR':>8} {'StopR':>8} {'TargetR':>8} {'Worst':>8}")
    for row in rows:
        nr = f"{float(row['avg_net_return']):.2f}%" if row['avg_net_return'] is not None else "-"
        br = f"{float(row['avg_benchmark_return']):.2f}%" if row['avg_benchmark_return'] is not None else "-"
        er = f"{float(row['avg_excess_return']):.2f}%" if row['avg_excess_return'] is not None else "-"
        ewr = f"{float(row['excess_win_rate'])*100:.1f}%" if row['excess_win_rate'] is not None else "-"
        sr = f"{float(row['stop_rate'])*100:.1f}%" if row['stop_rate'] is not None else "-"
        tr = f"{float(row['target_rate'])*100:.1f}%" if row['target_rate'] is not None else "-"
        wr = f"{float(row['worst_return']):.2f}%" if row['worst_return'] is not None else "-"
        print(f"{row['score_bucket']:>10} {row['sample_count']:>8} {nr:>8} {br:>8} {er:>8} {ewr:>8} {sr:>8} {tr:>8} {wr:>8}")
    high_bucket = rows[0] if rows else None
    low_bucket = rows[-1] if rows else None
    if high_bucket and low_bucket:
        hr = (
            high_bucket.get("avg_excess_return")
            if high_bucket.get("avg_excess_return") is not None
            else high_bucket.get("avg_net_return")
        )
        lr = (
            low_bucket.get("avg_excess_return")
            if low_bucket.get("avg_excess_return") is not None
            else low_bucket.get("avg_net_return")
        )
        if hr is not None and lr is not None and float(hr) <= float(lr):
            print("\nWARNING: 高分桶超额收益不优于低分桶，当前打分体系可能无效或未校准。")


def command_walk_forward(db_path: str, start: str, end: str, out: str | None) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = run_walk_forward(conn, start, end)
        path = write_walk_forward_report(result, Path(out) if out else None)
    print(
        f"Walk-forward: status={result.status}, trading_days={result.trading_days}, "
        f"candidates={result.formal_candidates}, trades={result.portfolio_trades}, report={path}"
    )


def command_portfolio_backtest(
    db_path: str, start: str, end: str, through: str,
    max_positions: int, capital: float, cost_bps: int | None, cooldown_days: int,
    execution: str, benchmark: str | None, require_intraday: bool,
    drawdown_reduce_threshold: float, drawdown_halt_threshold: float,
    max_per_sector: int, sizing_mode: str, risk_budget_pct: float,
    max_position_pct: float, out: str | None,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = run_portfolio_backtest(
            conn, start, end, through,
            max_positions=max_positions,
            initial_capital=capital,
            cost_bps=cost_bps,
            cooldown_days=cooldown_days,
            execution_mode=execution,
            benchmark_ticker=benchmark,
            require_intraday=require_intraday,
            drawdown_reduce_threshold=drawdown_reduce_threshold,
            drawdown_halt_threshold=drawdown_halt_threshold,
            max_per_sector=max_per_sector,
            sizing_mode=sizing_mode.replace("-", "_"),
            risk_budget_pct=risk_budget_pct,
            max_position_pct=max_position_pct,
        )
        path = write_portfolio_report(result, Path(out) if out else None)
    print(
        f"Portfolio backtest: capital={result.initial_capital:.0f} → {result.final_capital:.0f} "
        f"({result.total_return_pct:.2f}%), trades={result.trade_count}, "
        f"win_rate={result.win_rate:.1f}%, max_dd={result.max_drawdown_pct:.2f}%, report={path}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        command_init(args.db)
    elif args.command == "seed":
        command_seed(args.db)
    elif args.command == "bootstrap":
        command_bootstrap(args.db, args.as_of)
    elif args.command == "fetch-prices":
        command_fetch_prices(
            args.db,
            args.universe,
            args.start,
            args.end,
            args.source,
            args.markets,
            args.symbols,
            args.throttle,
            args.include_benchmarks,
            args.adjust,
        )
    elif args.command == "data-update":
        command_data_update(
            args.db,
            args.as_of,
            args.markets,
            args.throttle,
            args.adjust,
            args.skip_events,
            args.skip_intraday,
            args.intraday_period,
            args.repair_coverage,
            args.repair_scope,
            args.core_only,
        )
    elif args.command == "data-audit":
        command_data_audit(
            args.db,
            args.start,
            args.end,
            args.markets,
            args.probe_adjustment,
            args.probe_sample_size,
            args.ignore_adjustment_for_short_term,
            args.throttle,
        )
    elif args.command == "daily-run":
        command_daily_run(args.db, args.as_of, args.throttle, fast=args.fast, skip_model_update=args.skip_model_update)
    elif args.command == "daily-events":
        command_daily_events(args.db, args.as_of, args.throttle)
    elif args.command == "data-backfill":
        command_data_backfill(args.db, args.start, args.end, args.markets, args.batch_days, args.throttle, args.adjust)
    elif args.command == "fetch-intraday":
        command_fetch_intraday(
            args.db,
            args.universe,
            args.start,
            args.end,
            args.source,
            args.symbols,
            args.period,
            args.throttle,
        )
    elif args.command == "fetch-events":
        command_fetch_events(
            args.db,
            args.start,
            args.end,
            args.notice_limit_per_day,
            args.markets,
            args.symbols,
            args.skip_money_flow,
        )
    elif args.command == "import-events-csv":
        command_import_events_csv(args.db, args.path)
    elif args.command == "screen":
        command_screen(args.db, args.as_of)
    elif args.command == "evaluate-candidates":
        command_evaluate_candidates(args.db, args.candidate_date, args.through)
    elif args.command == "replay":
        command_replay(
            args.db,
            args.start,
            args.end,
            args.through,
            args.out,
            args.fetch_cn_a_intraday,
            args.intraday_period,
            args.intraday_throttle,
            args.require_adjusted,
            args.benchmark,
        )
    elif args.command == "tune-weights":
        command_tune_weights(args.db, args.start, args.end, args.through, args.min_samples, args.apply)
    elif args.command == "audit":
        command_audit(args.db, args.as_of)
    elif args.command == "daily-plan":
        command_daily_plan(args.db, args.as_of, args.out)
    elif args.command == "candidates":
        command_candidates(args.db, args.as_of)
    elif args.command == "verify":
        command_verify(args.db)
    elif args.command == "confirm-candidates":
        command_confirm_candidates(args.db, args.as_of)
    elif args.command == "score-calibration":
        command_score_calibration(args.db, args.start, args.end, args.through, args.horizon)
    elif args.command == "portfolio-backtest":
        command_portfolio_backtest(
            args.db, args.start, args.end, args.through,
            args.max_positions, args.capital, args.cost_bps, args.cooldown_days,
            args.execution, args.benchmark, args.require_intraday,
            args.drawdown_reduce_threshold, args.drawdown_halt_threshold,
            args.max_per_sector, args.sizing_mode, args.risk_budget_pct,
            args.max_position_pct, args.out,
        )
    elif args.command == "walk-forward":
        command_walk_forward(args.db, args.start, args.end, args.out)
    elif args.command == "validate":
        with closing(connect(args.db)) as conn:
            init_db(conn)
            result = run_portfolio_backtest(
                conn, args.start, args.end, args.through,
                max_positions=args.max_positions,
                initial_capital=args.capital,
                cooldown_days=args.cooldown_days,
                execution_mode=args.execution,
                benchmark_ticker=args.benchmark,
            )
            from .validation import monte_carlo_permutation, bootstrap_sharpe_ci, walk_forward_windows, render_validation_report
            mc = monte_carlo_permutation(result, permutations=args.mc_permutations)
            bs = bootstrap_sharpe_ci(result, samples=args.bootstrap_samples)
            wf = walk_forward_windows(result, n_windows=args.wf_windows)
            report = render_validation_report(result, mc, bs, wf)
            path = Path(args.out) if args.out else Path("reports") / f"validation_{args.start}_{args.end}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report, encoding="utf-8")
        print(f"Validation: trades={result.trade_count}, mc_sharpe_p={mc.sharpe_p_value:.4f}, bootstrap_ci=[{bs.ci_lower:.4f}, {bs.ci_upper:.4f}], wf_consistency={wf.consistency_rate:.2%}, report={path}")
    elif args.command == "loss-review":
        with closing(connect(args.db)) as conn:
            init_db(conn)
            path = write_loss_review(conn, args.start, args.end, args.through, Path(args.out) if args.out else None)
        print(f"Wrote loss review: {path}")
    elif args.command == "export-qlib-csv":
        with closing(connect(args.db)) as conn:
            init_db(conn)
            markets = {m.strip() for m in args.markets.split(",") if m.strip()}
            result = export_qlib_csv(conn, args.start, args.end, Path(args.output), markets=markets)
            md_path, json_path = write_quality_report(result, Path(args.output))
        print(
            f"Exported {result.csv_count} CSV files ({result.total_bars} bars) "
            f"to {args.output}. Warnings: {result.total_warnings}. "
            f"Reports: {md_path}, {json_path}"
        )
    elif args.command == "import-qlib-predictions":
        with closing(connect(args.db)) as conn:
            init_db(conn)
            result = import_qlib_predictions(
                conn, Path(args.artifact), args.model_name, args.model_version, args.market
            )
            md_path, json_path = write_import_report(result, Path(args.out_dir))
        print(
            f"Imported {result.imported_count} scores for {args.model_name}@{args.model_version}. "
            f"Date range: {result.date_range}. Failures: {result.ticker_mapping_failures}. "
            f"Reports: {md_path}, {json_path}"
        )
    elif args.command == "qlib-refresh":
        with closing(connect(args.db)) as conn:
            init_db(conn)
            result = qlib_refresh(
                conn,
                args.as_of,
                mode=args.mode,
                max_workers=args.max_workers,
                output_root=Path(args.output_root) if args.output_root else None,
            )
        print(
            f"Qlib refresh: version={result.version}, mode={result.mode}, status={result.status}, "
            f"range={result.start_date}..{result.end_date}, rows={result.row_count}, "
            f"tickers={result.ticker_count}, staging={result.staging_dir}"
        )
        if result.status != "SUCCESS":
            raise SystemExit(1)
    elif args.command == "model-predict":
        result = model_predict(
            args.db,
            args.as_of,
            models=args.models,
            output_dir=Path(args.output_dir) if args.output_dir else None,
        )
        print(
            f"Model predict: run_id={result.run_id}, status={result.status}, "
            f"models={result.model_count}, scores={result.score_count}, output={result.output_dir}"
        )
        if result.status != "SUCCESS":
            raise SystemExit(1)
    elif args.command == "production-daily":
        result = production_daily(
            args.db,
            args.as_of,
            allow_inline_data_update=args.allow_inline_data_update,
            output_dir=Path(args.output_dir) if args.output_dir else None,
            no_overwrite=args.no_overwrite,
        )
        if args.skip_data_update:
            print("Note: --skip-data-update is deprecated; production-daily is read-only by default.")
        print(f"Production daily: status={result.status}, report={result.report_path}")
        if result.status != "SUCCESS":
            print(result.error_message)
            raise SystemExit(1)
    elif args.command == "production-run":
        result = production_run(
            args.db,
            args.as_of,
            skip_data_update=args.skip_data_update,
            no_overwrite=args.no_overwrite,
            output_root=Path(args.output_root) if args.output_root else None,
        )
        print(
            f"Production run: run_id={result.run_id}, status={result.status}, "
            f"report={result.report_path}, summary={result.summary_path}"
        )
        if result.status != "SUCCESS":
            print(result.error_message)
            raise SystemExit(1)
    elif args.command == "production-async":
        result = production_async(
            args.db,
            args.as_of,
            output_dir=Path(args.output_dir) if args.output_dir else None,
        )
        print(
            f"Production async: status={result.status}, tasks={result.task_count}, "
            f"failed={result.failed_count}, report={result.report_path}"
        )
        if result.status == "FAILED":
            print(result.error_message)
            raise SystemExit(1)
    elif args.command == "model-governance":
        if args.governance_command == "review":
            result = model_governance_review(
                args.db,
                args.as_of,
                output_dir=Path(args.output_dir) if args.output_dir else None,
            )
            print(
                f"Model governance review: status={result.status}, models={result.model_count}, "
                f"needs_review={result.needs_review_count}, report={result.report_path}"
            )
            if result.status != "SUCCESS":
                print(result.error_message)
                raise SystemExit(1)
    elif args.command == "model-arena":
        result = model_arena(
            args.db,
            args.as_of,
            pool=args.pool,
            max_workers=args.max_workers,
            dry_run=args.dry_run,
            output_dir=Path(args.output_dir) if args.output_dir else None,
            resume_run_id=args.resume_run_id,
        )
        print(
            f"Model arena: run_id={result.run_id}, status={result.status}, "
            f"completed={result.completed_models}/{result.total_models}, failed={result.failed_models}, "
            f"report={result.report_path}, recommended={result.recommended_model}"
        )
        if result.status == "FAILED":
            raise SystemExit(1)
    elif args.command == "model-evaluate":
        result = model_evaluate(
            args.db,
            pool=args.pool,
            model_version=args.model_version,
            as_of=args.as_of,
            mode=args.mode,
            output_dir=Path(args.output_dir) if args.output_dir else None,
        )
        print(
            f"Model evaluate: run_id={result.run_id}, status={result.status}, "
            f"models={result.model_count}, pass={result.pass_count}, watch={result.watch_count}, "
            f"fail={result.fail_count}, insufficient={result.insufficient_count}, "
            f"report={result.report_path}, metrics={result.metrics_path}"
        )
        if result.status != "SUCCESS":
            print(result.error_message)
            raise SystemExit(1)
    elif args.command == "audit-tickers":
        command_audit_tickers(args.db, args.out_dir, args.limit)
    elif args.command == "repair-tickers":
        command_repair_tickers(args.db, args.apply, args.out_dir, args.limit)
    elif args.command == "backfill-qfq":
        command_backfill_qfq(
            args.db,
            args.start,
            args.end,
            args.source,
            args.throttle,
            args.limit,
            args.tickers,
            args.commit_every,
            args.out_dir,
            args.dry_run,
        )
    elif args.command == "qfq-maintenance":
        command_qfq_maintenance(
            args.db,
            args.as_of,
            args.interval_days,
            args.lookback_days,
            args.source,
            args.throttle,
            args.limit,
            args.commit_every,
            args.out_dir,
            args.force,
            args.dry_run,
            mode=args.mode,
        )
    elif args.command == "detect-adjustment-breaks":
        command_detect_adjustment_breaks(args.db, args.as_of, args.market)
    elif args.command == "qfq-repair-daily":
        command_qfq_repair_daily(args.db, args.as_of, args.market)
    elif args.command == "enrich-daily-bars":
        command_enrich_daily_bars(
            args.db,
            args.start,
            args.end,
            args.throttle,
            args.limit,
            args.tickers,
            args.commit_every,
            args.out_dir,
            args.dry_run,
        )
    elif args.command == "fetch-northbound":
        command_fetch_northbound(args.db, args.start)
    elif args.command == "fetch-margin":
        command_fetch_margin(args.db, args.as_of)
    else:
        parser.error(f"Unknown command: {args.command}")
    return 0
