from __future__ import annotations

import argparse
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from .audit import audit_all, latest_audits
from .benchmarks import CN_A_BENCHMARKS
from .data_ops import REPAIR_SCOPES, audit_data_coverage, data_update, probe_adjustment_sources
from .db import DEFAULT_DB_PATH, connect, init_db, upsert_many
from .event_data import fetch_events_to_db, import_events_csv
from .ledger import verify_signals
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
from .replay import replay_candidates
from .reporting import write_daily_plan, write_replay_report
from .screener import confirm_candidates, latest_candidates, screen_all
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

    daily_run_parser = subparsers.add_parser("daily-run", help="Run local daily data, screen, confirm, and report flow")
    daily_run_parser.add_argument("--as-of", required=True)
    daily_run_parser.add_argument("--throttle", type=float, default=0.15)
    daily_run_parser.add_argument("--fast", action="store_true", help="Fast mode: prices + screening + report only, skip slow event fetching")

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

    daily_plan = subparsers.add_parser("daily-plan", help="Generate daily actionable candidate plan")
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

    loss_review = subparsers.add_parser("loss-review", help="Review losing samples and tag recurring failure modes")
    loss_review.add_argument("--start", required=True)
    loss_review.add_argument("--end", required=True)
    loss_review.add_argument("--through", required=True)
    loss_review.add_argument("--out", help="Output markdown path")

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
    repair_coverage: bool,
    repair_scope: str,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = data_update(
            conn,
            as_of,
            markets,
            throttle_seconds=throttle,
            fetch_events=not skip_events,
            fetch_intraday=not skip_intraday,
            repair_coverage=repair_coverage,
            repair_scope=repair_scope,
            adjust=None if adjust == "none" else adjust,
        )
    print(
        f"Data update #{result.run_id}: status={result.status}, range={result.start_date}..{result.end_date}, "
        f"symbols={result.requested_symbols}, price_bars={result.price_bars}, intraday_bars={result.intraday_bars}, "
        f"events={result.corporate_events}, financials={result.financial_metrics}, money_flows={result.money_flows}, "
        f"errors={result.error_count}"
    )


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


def command_daily_run(db_path: str, as_of: str, throttle: float, fast: bool = False) -> None:
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
        candidate_count = screen_all(conn, as_of)
        confirmed = confirm_candidates(conn, as_of)
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
    print(f"Confirmed {confirmed}, cancelled {cancelled} candidates as of {as_of}.")


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
            args.repair_coverage,
            args.repair_scope,
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
        command_daily_run(args.db, args.as_of, args.throttle, fast=args.fast)
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
    elif args.command == "loss-review":
        with closing(connect(args.db)) as conn:
            init_db(conn)
            path = write_loss_review(conn, args.start, args.end, args.through, Path(args.out) if args.out else None)
        print(f"Wrote loss review: {path}")
    else:
        parser.error(f"Unknown command: {args.command}")
    return 0
