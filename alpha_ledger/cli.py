from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from .audit import audit_all, latest_audits
from .db import DEFAULT_DB_PATH, connect, init_db, upsert_many
from .event_data import fetch_events_to_db, import_events_csv
from .ledger import add_signal, verify_signals
from .market_data import DEFAULT_UNIVERSE_PATH, fetch_bars, parse_date, read_db_instruments, read_universe
from .metrics import (
    apply_strategy_weight_adjustments,
    evaluate_all,
    evaluate_candidate_horizons_for_date,
    evaluate_candidates,
    strategy_leaderboard,
    suggest_strategy_weight_adjustments,
)
from .replay import replay_candidates
from .reporting import write_daily_plan, write_report, write_replay_report
from .screener import latest_candidates, screen_all
from .seed import seed_all


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

    bootstrap = subparsers.add_parser("bootstrap", help="Init, seed, evaluate, and report")
    bootstrap.add_argument("--as-of", required=True, help="Evaluation/report date, e.g. 2026-05-25")

    evaluate = subparsers.add_parser("evaluate", help="Evaluate all signals")
    evaluate.add_argument("--as-of", required=True, help="Evaluation date")

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

    tune_weights = subparsers.add_parser("tune-weights", help="Suggest or apply strategy weight changes from replay")
    tune_weights.add_argument("--start", required=True, help="Replay start date")
    tune_weights.add_argument("--end", required=True, help="Replay end date")
    tune_weights.add_argument("--through", required=True, help="Evaluation through date")
    tune_weights.add_argument("--min-samples", type=int, default=5, help="Minimum evaluated samples before changing weight")
    tune_weights.add_argument("--apply", action="store_true", help="Apply DOWN_WEIGHT recommendations")

    audit = subparsers.add_parser("audit", help="Audit strategy health and decay risk")
    audit.add_argument("--as-of", required=True, help="Audit date")

    report = subparsers.add_parser("report", help="Generate markdown report")
    report.add_argument("--as-of", required=True, help="Report date")
    report.add_argument("--through", help="Evaluate candidates through this date before writing report")
    report.add_argument("--out", help="Output markdown path")

    daily_plan = subparsers.add_parser("daily-plan", help="Generate daily actionable candidate plan")
    daily_plan.add_argument("--as-of", required=True, help="Candidate date")
    daily_plan.add_argument("--out", help="Output markdown path")

    subparsers.add_parser("signals", help="List signals")
    candidates = subparsers.add_parser("candidates", help="List candidates for a date")
    candidates.add_argument("--as-of", required=True, help="Candidate date")
    subparsers.add_parser("leaderboard", help="Show strategy leaderboard")
    subparsers.add_parser("verify", help="Verify immutable signal hashes")

    add = subparsers.add_parser("add-signal", help="Add a manually researched signal")
    add.add_argument("--date", required=True)
    add.add_argument("--ticker", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--market", required=True)
    add.add_argument("--strategy-id", required=True)
    add.add_argument("--entry-price", type=float, required=True)
    add.add_argument("--buy-zone-low", type=float)
    add.add_argument("--buy-zone-high", type=float)
    add.add_argument("--stop-loss", type=float)
    add.add_argument("--target-1", type=float)
    add.add_argument("--target-2", type=float)
    add.add_argument("--horizon-days", type=int, default=20)
    add.add_argument("--confidence", default="B")
    add.add_argument("--thesis", required=True)
    add.add_argument("--trigger-condition", required=True)
    add.add_argument("--risk-notes", required=True)

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
        f"{counts['research_events']} research events, "
        f"Xingye signal #{counts['xingye_signal_id']}."
    )


def command_bootstrap(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        counts = seed_all(conn)
        candidate_count = screen_all(conn, as_of)
        eval_count = evaluate_all(conn, as_of)
        audit_count = audit_all(conn, as_of)
        report_path = write_report(conn, as_of)
    print(
        "Bootstrap complete: "
        f"{counts['strategies']} strategies, "
        f"{counts['price_bars']} price bars, "
        f"{counts['research_events']} research events, "
        f"{candidate_count} candidates, "
        f"{eval_count} evaluations, "
        f"{audit_count} strategy audits, "
        f"report={report_path}"
    )


def command_evaluate(db_path: str, as_of: str) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        count = evaluate_all(conn, as_of)
    print(f"Evaluated {count} signal horizons as of {as_of}.")


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


def command_replay(db_path: str, start: str, end: str, through: str, out: str | None) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        result = replay_candidates(conn, start, end, through)
        path = write_replay_report(conn, start, end, through, Path(out) if out else None)
    print(
        "Replay complete: "
        f"dates={result.dates}, candidates={result.candidates}, "
        f"evaluations={result.evaluations}, horizon_evaluations={result.horizon_evaluations}, report={path}"
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


def command_report(db_path: str, as_of: str, through: str | None, out: str | None) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        if through:
            evaluate_candidates(conn, as_of, through)
            evaluate_candidate_horizons_for_date(conn, as_of, through)
        path = write_report(conn, as_of, Path(out) if out else None)
    print(f"Wrote report: {path}")


def command_daily_plan(db_path: str, as_of: str, out: str | None) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        path = write_daily_plan(conn, as_of, Path(out) if out else None)
    print(f"Wrote daily plan: {path}")


def command_signals(db_path: str) -> None:
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.signal_date, s.ticker, s.name, s.market, st.name AS strategy_name,
                   s.entry_price, s.confidence, s.status
            FROM signals s
            JOIN strategies st ON st.id = s.strategy_id
            ORDER BY s.signal_date DESC, s.id
            """
        ).fetchall()
    if not rows:
        print("No signals.")
        return
    for row in rows:
        print(
            f"#{row['id']} {row['signal_date']} {row['name']} {row['ticker']} "
            f"{row['market']} strategy={row['strategy_name']} "
            f"entry={row['entry_price']:.2f} confidence={row['confidence']} status={row['status']}"
        )


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


def command_leaderboard(db_path: str) -> None:
    with closing(connect(db_path)) as conn:
        rows = strategy_leaderboard(conn)
    if not rows:
        print("No leaderboard data.")
        return
    for row in rows:
        avg_5d = "-" if row["avg_return_5d"] is None else f"{row['avg_return_5d']:.2f}%"
        avg_10d = "-" if row["avg_return_10d"] is None else f"{row['avg_return_10d']:.2f}%"
        print(
            f"{row['strategy_name']} ({row['strategy_id']}): "
            f"samples={row['signal_count']} weight={row['weight']:.2f} "
            f"T+5={avg_5d} T+10={avg_10d}"
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


def command_add_signal(db_path: str, args: argparse.Namespace) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        signal_id = add_signal(
            conn,
            {
            "signal_date": args.date,
            "ticker": args.ticker,
            "name": args.name,
            "market": args.market,
            "strategy_id": args.strategy_id,
            "entry_type": "BUY_CANDIDATE",
            "entry_price": args.entry_price,
            "buy_zone_low": args.buy_zone_low,
            "buy_zone_high": args.buy_zone_high,
            "stop_loss": args.stop_loss,
            "target_1": args.target_1,
            "target_2": args.target_2,
            "horizon_days": args.horizon_days,
            "confidence": args.confidence,
            "thesis": args.thesis,
            "trigger_condition": args.trigger_condition,
            "risk_notes": args.risk_notes,
            "evidence_json": [],
            },
        )
    print(f"Added signal #{signal_id}: {args.name} {args.ticker}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        command_init(args.db)
    elif args.command == "seed":
        command_seed(args.db)
    elif args.command == "bootstrap":
        command_bootstrap(args.db, args.as_of)
    elif args.command == "evaluate":
        command_evaluate(args.db, args.as_of)
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
        command_replay(args.db, args.start, args.end, args.through, args.out)
    elif args.command == "tune-weights":
        command_tune_weights(args.db, args.start, args.end, args.through, args.min_samples, args.apply)
    elif args.command == "audit":
        command_audit(args.db, args.as_of)
    elif args.command == "report":
        command_report(args.db, args.as_of, args.through, args.out)
    elif args.command == "daily-plan":
        command_daily_plan(args.db, args.as_of, args.out)
    elif args.command == "signals":
        command_signals(args.db)
    elif args.command == "candidates":
        command_candidates(args.db, args.as_of)
    elif args.command == "leaderboard":
        command_leaderboard(args.db)
    elif args.command == "verify":
        command_verify(args.db)
    elif args.command == "add-signal":
        command_add_signal(args.db, args)
    else:
        parser.error(f"Unknown command: {args.command}")
    return 0
