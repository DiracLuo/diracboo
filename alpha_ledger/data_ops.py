from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .benchmarks import CN_A_BENCHMARKS
from .db import upsert_many
from .event_data import fetch_events_to_db
from .ledger import now_utc
from .market_data import (
    DEFAULT_UNIVERSE_PATH,
    Instrument,
    fetch_bars,
    fetch_intraday_bars,
    parse_date,
    read_db_instruments,
    read_universe,
)
from .screener import confirm_candidates, screen_all


CONFIDENCE_HIGH = "HIGH_CONFIDENCE"
CONFIDENCE_MEDIUM = "MEDIUM_CONFIDENCE"
CONFIDENCE_LOW = "LOW_CONFIDENCE"
CONFIDENCE_RESEARCH = "RESEARCH_ONLY"
REPAIR_SCOPE_BENCHMARKS = "benchmarks"
REPAIR_SCOPE_ADJUSTMENTS = "adjustments"
REPAIR_SCOPE_ALL = "all"
REPAIR_SCOPES = (REPAIR_SCOPE_BENCHMARKS, REPAIR_SCOPE_ADJUSTMENTS, REPAIR_SCOPE_ALL)


@dataclass(frozen=True)
class DataAuditResult:
    market: str
    start_date: str
    end_date: str
    trading_days: int
    latest_price_date: str | None
    price_bar_count: int
    adjusted_bar_count: int
    benchmark_count: int
    event_count: int
    financial_count: int
    intraday_symbol_count: int
    adjustment_coverage_pct: float
    benchmark_coverage_pct: float
    confidence_level: str
    allow_formal_daily: bool
    missing_dates: list[str]
    notes: list[str]


@dataclass(frozen=True)
class DataUpdateResult:
    run_id: int
    status: str
    start_date: str
    end_date: str
    requested_symbols: int
    price_bars: int
    intraday_bars: int
    corporate_events: int
    financial_metrics: int
    money_flows: int
    error_count: int


@dataclass(frozen=True)
class AdjustmentProbeSample:
    ticker: str
    status: str
    adjusted_rows: int
    raw_rows: int
    error: str | None = None


@dataclass(frozen=True)
class AdjustmentProbeResult:
    market: str
    start_date: str
    end_date: str
    sample_count: int
    success_count: int
    partial_count: int
    failed_count: int
    samples: list[AdjustmentProbeSample]

    @property
    def success_rate_pct(self) -> float:
        return self.success_count / self.sample_count * 100.0 if self.sample_count else 0.0


def _split_markets(markets: str | None) -> set[str]:
    if not markets:
        return {"CN_A"}
    return {item.strip() for item in markets.split(",") if item.strip()}


def _latest_price_date(conn: sqlite3.Connection, market: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM price_bars WHERE market = ?",
        (market,),
    ).fetchone()
    return str(row["d"]) if row and row["d"] else None


def _earliest_price_date(conn: sqlite3.Connection, market: str) -> str | None:
    row = conn.execute(
        "SELECT MIN(date) AS d FROM price_bars WHERE market = ?",
        (market,),
    ).fetchone()
    return str(row["d"]) if row and row["d"] else None


def _start_run(conn: sqlite3.Connection, run_type: str, market: str, start: str, end: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO data_fetch_runs (run_type, market, start_date, end_date, status, started_at)
        VALUES (?, ?, ?, ?, 'RUNNING', ?)
        """,
        (run_type, market, start, end, now_utc()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    requested_symbols: int,
    price_bars: int = 0,
    intraday_bars: int = 0,
    corporate_events: int = 0,
    financial_metrics: int = 0,
    money_flows: int = 0,
    error_count: int = 0,
    notes: str = "",
) -> None:
    conn.execute(
        """
        UPDATE data_fetch_runs
        SET status = ?, requested_symbols = ?, price_bars = ?, intraday_bars = ?,
            corporate_events = ?, financial_metrics = ?, money_flows = ?,
            error_count = ?, notes = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            status,
            requested_symbols,
            price_bars,
            intraday_bars,
            corporate_events,
            financial_metrics,
            money_flows,
            error_count,
            notes,
            now_utc(),
            run_id,
        ),
    )
    conn.commit()


def _record_errors(conn: sqlite3.Connection, run_id: int, market: str, source: str, errors: list[str]) -> None:
    if not errors:
        return
    for error in errors:
        conn.execute(
            """
            INSERT INTO data_fetch_errors (run_id, market, ticker, source, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, market, error.split(" ", 1)[0] if error else None, source, error, now_utc()),
        )
    conn.commit()


def _update_source_health(conn: sqlite3.Connection, market: str, source: str, errors: list[str]) -> None:
    if errors:
        conn.execute(
            """
            INSERT INTO data_source_health (
                source, market, last_failure_at, failure_count, last_error, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(source, market) DO UPDATE SET
                last_failure_at = excluded.last_failure_at,
                failure_count = data_source_health.failure_count + 1,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (source, market, now_utc(), errors[0][:500], now_utc()),
        )
    else:
        conn.execute(
            """
            INSERT INTO data_source_health (
                source, market, last_success_at, failure_count, updated_at
            ) VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(source, market) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                failure_count = 0,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (source, market, now_utc(), now_utc()),
        )
    conn.commit()


def _cn_a_instruments(conn: sqlite3.Connection, source: str = "both") -> list[Instrument]:
    instruments: list[Instrument] = []
    if source in {"universe", "both"}:
        instruments.extend(read_universe(DEFAULT_UNIVERSE_PATH, markets={"CN_A"}))
    if source in {"db", "both"}:
        instruments.extend(read_db_instruments(conn, markets={"CN_A"}))
    instruments.extend(CN_A_BENCHMARKS)
    return list({(item.market, item.ticker): item for item in instruments}.values())


def _essential_instruments(conn: sqlite3.Connection, as_of_date: str) -> list[Instrument]:
    """Return a small subset: universe CSV + active candidates + benchmarks."""
    instruments: list[Instrument] = []
    instruments.extend(read_universe(DEFAULT_UNIVERSE_PATH, markets={"CN_A"}))
    instruments.extend(CN_A_BENCHMARKS)
    rows = conn.execute(
        """
        SELECT DISTINCT i.market, i.ticker, i.name, i.source, i.source_symbol, i.tags_json
        FROM candidates c
        JOIN instruments i ON i.market = c.market AND i.ticker = c.ticker
        WHERE c.market = 'CN_A'
          AND c.as_of_date >= date(?, '-5 days')
          AND (c.action = 'BUY_CANDIDATE' OR c.action LIKE '%CONFIRM%' OR c.confirmation_status = 'CONFIRMED')
        """,
        (as_of_date,),
    ).fetchall()
    for row in rows:
        instruments.append(
            Instrument(
                market=str(row["market"]),
                ticker=str(row["ticker"]),
                name=str(row["name"]),
                source=str(row["source"]),
                source_symbol=str(row["source_symbol"]),
                active=True,
                tags=(),
            )
        )
    return list({(item.market, item.ticker): item for item in instruments}.values())


def _instrument_map(conn: sqlite3.Connection) -> dict[str, Instrument]:
    return {item.ticker: item for item in _cn_a_instruments(conn, "both")}


def _cn_a_benchmark_tickers() -> tuple[str, ...]:
    return tuple(item.ticker for item in CN_A_BENCHMARKS)


def _trade_calendar_dates(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    market: str,
) -> tuple[list[str], str]:
    if market == "CN_A":
        benchmark_rows = conn.execute(
            """
            SELECT DISTINCT date
            FROM price_bars
            WHERE market = ? AND ticker = '000300.SS' AND date >= ? AND date <= ?
            ORDER BY date
            """,
            (market, start_date, end_date),
        ).fetchall()
        benchmark_dates = [str(row["date"]) for row in benchmark_rows]
        if benchmark_dates:
            return benchmark_dates, "BENCHMARK"

    market_rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM price_bars
        WHERE market = ? AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (market, start_date, end_date),
    ).fetchall()
    market_dates = [str(row["date"]) for row in market_rows]
    if market_dates:
        return market_dates, "MARKET_PRICE_DATES"

    dates: list[str] = []
    cursor = parse_date(start_date)
    end = parse_date(end_date)
    while cursor <= end:
        if cursor.weekday() < 5:
            dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return dates, "WEEKDAY_FALLBACK"


def _repair_coverage_instruments(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    repair_scope: str = REPAIR_SCOPE_BENCHMARKS,
) -> list[Instrument]:
    if repair_scope not in REPAIR_SCOPES:
        raise ValueError(f"Unsupported repair scope: {repair_scope}")
    by_ticker = _instrument_map(conn)
    repair: dict[str, Instrument] = {}
    expected_dates, _ = _trade_calendar_dates(conn, start_date, end_date, "CN_A")
    expected_count = len(expected_dates)
    benchmark_tickers = set(_cn_a_benchmark_tickers())

    if repair_scope in {REPAIR_SCOPE_BENCHMARKS, REPAIR_SCOPE_ALL}:
        for benchmark in CN_A_BENCHMARKS:
            existing = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT date) AS c
                    FROM price_bars
                    WHERE market = 'CN_A' AND ticker = ? AND date >= ? AND date <= ?
                    """,
                    (benchmark.ticker, start_date, end_date),
                ).fetchone()["c"]
                or 0
            )
            if existing < expected_count or existing == 0:
                repair[benchmark.ticker] = benchmark

    if repair_scope in {REPAIR_SCOPE_ADJUSTMENTS, REPAIR_SCOPE_ALL}:
        raw_rows = conn.execute(
            """
            SELECT DISTINCT ticker
            FROM price_bars
            WHERE market = 'CN_A'
              AND date >= ? AND date <= ?
              AND adjustment_status != 'ADJUSTED'
              AND ticker NOT IN ('000300.SS','000905.SS','000852.SS','399006.SZ','000688.SS','899050.BJ')
            ORDER BY ticker
            """,
            (start_date, end_date),
        ).fetchall()
        for row in raw_rows:
            ticker = str(row["ticker"])
            instrument = by_ticker.get(ticker)
            if instrument and ticker not in benchmark_tickers:
                repair[ticker] = instrument
    return list(repair.values())


def actionable_intraday_instruments(conn: sqlite3.Connection, as_of_date: str) -> list[Instrument]:
    rows = conn.execute(
        """
        SELECT DISTINCT c.market, c.ticker, c.name, i.source, i.source_symbol, i.tags_json
        FROM candidates c
        JOIN instruments i ON i.market = c.market AND i.ticker = c.ticker
        WHERE c.market = 'CN_A'
          AND (c.as_of_date = ? OR c.confirmation_date = ?)
          AND (
              c.action = 'BUY_CANDIDATE'
              OR c.action LIKE '%CONFIRM%'
              OR c.confirmation_status = 'CONFIRMED'
          )
        """,
        (as_of_date, as_of_date),
    ).fetchall()
    instruments = []
    for row in rows:
        instruments.append(
            Instrument(
                market=str(row["market"]),
                ticker=str(row["ticker"]),
                name=str(row["name"]),
                source=str(row["source"]),
                source_symbol=str(row["source_symbol"]),
                active=True,
                tags=(),
            )
        )
    return instruments


def audit_data_coverage(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    market: str = "CN_A",
    write: bool = True,
    ignore_adjustment_for_short_term: bool = False,
) -> DataAuditResult:
    expected_dates, calendar_source = _trade_calendar_dates(conn, start_date, end_date, market)
    price = conn.execute(
        """
        SELECT COUNT(*) AS bars,
               SUM(CASE WHEN adjustment_status = 'ADJUSTED' THEN 1 ELSE 0 END) AS adjusted,
               COUNT(DISTINCT date) AS trading_days,
               MAX(date) AS latest_date
        FROM price_bars
        WHERE market = ? AND date >= ? AND date <= ?
        """,
        (market, start_date, end_date),
    ).fetchone()
    price_bar_count = int(price["bars"] or 0)
    adjusted_bar_count = int(price["adjusted"] or 0)
    price_trading_days = int(price["trading_days"] or 0)
    trading_days = len(expected_dates) if expected_dates else price_trading_days
    latest_price_date = str(price["latest_date"]) if price and price["latest_date"] else None
    adjusted_pct = adjusted_bar_count / price_bar_count * 100.0 if price_bar_count else 0.0

    benchmark_count = conn.execute(
        """
        SELECT COUNT(DISTINCT ticker || ':' || date) AS c
        FROM price_bars
        WHERE market = ? AND ticker IN ('000300.SS','000905.SS','000852.SS','399006.SZ','000688.SS','899050.BJ')
          AND date >= ? AND date <= ?
        """,
        (market, start_date, end_date),
    ).fetchone()["c"] or 0
    benchmark_denominator = max(trading_days * len(CN_A_BENCHMARKS), 1)
    benchmark_pct = int(benchmark_count) / benchmark_denominator * 100.0

    event_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM corporate_events WHERE market = ? AND event_date >= ? AND event_date <= ?",
        (market, start_date, end_date),
    ).fetchone()["c"] or 0)
    financial_count = int(conn.execute(
        "SELECT COUNT(*) AS c FROM financial_metrics WHERE market = ? AND published_date <= ?",
        (market, end_date),
    ).fetchone()["c"] or 0)
    intraday_symbol_count = int(conn.execute(
        "SELECT COUNT(DISTINCT ticker) AS c FROM intraday_bars WHERE market = ? AND date >= ? AND date <= ?",
        (market, start_date, end_date),
    ).fetchone()["c"] or 0)

    day_rows = conn.execute(
        "SELECT DISTINCT date FROM price_bars WHERE market = ? AND date >= ? AND date <= ? ORDER BY date",
        (market, start_date, end_date),
    ).fetchall()
    present_dates = {str(row["date"]) for row in day_rows}
    missing_dates = [day for day in expected_dates if day not in present_dates]

    notes: list[str] = []
    if calendar_source != "BENCHMARK":
        notes.append(f"交易日历来源为 {calendar_source}，可能无法识别全部 A 股休市日。")
    if latest_price_date is None or latest_price_date < end_date:
        notes.append("行情未覆盖到审计截止日。")
    if adjusted_pct < 95.0:
        if ignore_adjustment_for_short_term:
            notes.append(f"复权覆盖不足：{adjusted_pct:.1f}%，当前按未复权短线研究口径处理，置信度最高为 MEDIUM_CONFIDENCE。")
        else:
            notes.append(f"复权覆盖不足：{adjusted_pct:.1f}%。")
    if benchmark_pct < 95.0:
        notes.append(f"分层基准覆盖不足：{benchmark_pct:.1f}%。")
    if intraday_symbol_count == 0:
        notes.append("候选分时覆盖不足。")

    if price_bar_count == 0 or latest_price_date is None or latest_price_date < end_date:
        confidence = CONFIDENCE_RESEARCH
    elif adjusted_pct >= 95.0 and benchmark_pct >= 95.0 and intraday_symbol_count > 0:
        confidence = CONFIDENCE_HIGH
    elif benchmark_pct >= 80.0 and (adjusted_pct >= 95.0 or ignore_adjustment_for_short_term):
        confidence = CONFIDENCE_MEDIUM
    else:
        confidence = CONFIDENCE_LOW
    allow_formal_daily = confidence == CONFIDENCE_HIGH

    result = DataAuditResult(
        market=market,
        start_date=start_date,
        end_date=end_date,
        trading_days=trading_days,
        latest_price_date=latest_price_date,
        price_bar_count=price_bar_count,
        adjusted_bar_count=adjusted_bar_count,
        benchmark_count=int(benchmark_count),
        event_count=event_count,
        financial_count=financial_count,
        intraday_symbol_count=intraday_symbol_count,
        adjustment_coverage_pct=adjusted_pct,
        benchmark_coverage_pct=benchmark_pct,
        confidence_level=confidence,
        allow_formal_daily=allow_formal_daily,
        missing_dates=missing_dates[:20],
        notes=notes,
    )
    if write:
        conn.execute(
            """
            INSERT INTO data_coverage_daily (
                market, date, instrument_count, price_bar_count, adjusted_bar_count,
                benchmark_count, event_count, financial_count, intraday_symbol_count,
                confidence_level, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market, date) DO UPDATE SET
                instrument_count = excluded.instrument_count,
                price_bar_count = excluded.price_bar_count,
                adjusted_bar_count = excluded.adjusted_bar_count,
                benchmark_count = excluded.benchmark_count,
                event_count = excluded.event_count,
                financial_count = excluded.financial_count,
                intraday_symbol_count = excluded.intraday_symbol_count,
                confidence_level = excluded.confidence_level,
                notes = excluded.notes,
                created_at = excluded.created_at
            """,
            (
                market,
                end_date,
                len(_cn_a_instruments(conn, "both")) if market == "CN_A" else 0,
                price_bar_count,
                adjusted_bar_count,
                int(benchmark_count),
                event_count,
                financial_count,
                intraday_symbol_count,
                confidence,
                "; ".join(notes),
                now_utc(),
            ),
        )
        conn.commit()
    return result


def data_update(
    conn: sqlite3.Connection,
    as_of_date: str,
    markets: str | None = "CN_A",
    *,
    throttle_seconds: float = 0.15,
    fetch_events: bool = True,
    fetch_intraday: bool = True,
    price_mode: str = "full",
    repair_coverage: bool = False,
    repair_scope: str = REPAIR_SCOPE_BENCHMARKS,
    adjust: str | None = None,
) -> DataUpdateResult:
    selected_markets = _split_markets(markets)
    if selected_markets != {"CN_A"}:
        raise ValueError("data-update v1 only supports CN_A")
    if repair_scope not in REPAIR_SCOPES:
        raise ValueError(f"Unsupported repair scope: {repair_scope}")
    latest = _latest_price_date(conn, "CN_A")
    start = (date.fromisoformat(latest) + timedelta(days=1)).isoformat() if latest else as_of_date
    if start > as_of_date:
        start = as_of_date
    run_id = _start_run(conn, "data-update", "CN_A", start, as_of_date)
    if price_mode == "essential":
        instruments = _essential_instruments(conn, as_of_date)
    else:
        instruments = _cn_a_instruments(conn, "both")
    price_count = 0
    intraday_count = 0
    corp_events = 0
    fin_metrics = 0
    money_flows = 0
    errors: list[str] = []

    fetched_full_missing_range = price_mode != "none" and (latest is None or latest < as_of_date)
    if fetched_full_missing_range:
        bars, price_errors = fetch_bars(
            instruments,
            start=parse_date(start),
            end=parse_date(as_of_date),
            throttle_seconds=throttle_seconds,
            adjust=adjust,
        )
        price_count = upsert_many(conn, "price_bars", bars, ("market", "ticker", "date"))
        errors.extend(price_errors)
        _record_errors(conn, run_id, "CN_A", "price_bars", price_errors)
        _update_source_health(conn, "CN_A", "price_bars", price_errors)

    if price_mode != "none" and repair_coverage:
        repair_start = _earliest_price_date(conn, "CN_A") or start
        repair_instruments = _repair_coverage_instruments(conn, repair_start, as_of_date, repair_scope)
        if repair_instruments:
            bars, repair_errors = fetch_bars(
                repair_instruments,
                start=parse_date(repair_start),
                end=parse_date(as_of_date),
                throttle_seconds=throttle_seconds,
                adjust=adjust,
            )
            price_count += upsert_many(conn, "price_bars", bars, ("market", "ticker", "date"))
            errors.extend(repair_errors)
            _record_errors(conn, run_id, "CN_A", "repair_coverage", repair_errors)
            _update_source_health(conn, "CN_A", "repair_coverage", repair_errors)

    if fetch_events:
        try:
            event_result = fetch_events_to_db(
                conn,
                start=parse_date(as_of_date),
                end=parse_date(as_of_date),
                instruments=instruments,
                fetch_money_flow=True,
            )
            corp_events = event_result.corporate_events
            fin_metrics = event_result.financial_metrics
            money_flows = event_result.money_flows
            errors.extend(event_result.errors)
            _record_errors(conn, run_id, "CN_A", "events", event_result.errors)
            _update_source_health(conn, "CN_A", "events", event_result.errors)
        except Exception as exc:
            errors.append(str(exc))
            _record_errors(conn, run_id, "CN_A", "events", [str(exc)])
            _update_source_health(conn, "CN_A", "events", [str(exc)])

    if fetch_intraday:
        candidates = actionable_intraday_instruments(conn, as_of_date)
        if candidates:
            bars, intraday_errors = fetch_intraday_bars(
                candidates,
                start=parse_date(as_of_date),
                end=parse_date(as_of_date),
                period="5",
                throttle_seconds=throttle_seconds,
            )
            intraday_count = upsert_many(conn, "intraday_bars", bars, ("market", "ticker", "datetime"))
            errors.extend(intraday_errors)
            _record_errors(conn, run_id, "CN_A", "intraday_bars", intraday_errors)
            _update_source_health(conn, "CN_A", "intraday_bars", intraday_errors)

    status = "SUCCESS" if not errors else ("PARTIAL_SUCCESS" if price_count or corp_events or intraday_count else "FAILED")
    _finish_run(
        conn,
        run_id,
        status=status,
        requested_symbols=len(instruments),
        price_bars=price_count,
        intraday_bars=intraday_count,
        corporate_events=corp_events,
        financial_metrics=fin_metrics,
        money_flows=money_flows,
        error_count=len(errors),
        notes="; ".join(errors[:3]),
    )
    audit_data_coverage(conn, as_of_date, as_of_date, "CN_A", write=True)
    return DataUpdateResult(
        run_id=run_id,
        status=status,
        start_date=start,
        end_date=as_of_date,
        requested_symbols=len(instruments),
        price_bars=price_count,
        intraday_bars=intraday_count,
        corporate_events=corp_events,
        financial_metrics=fin_metrics,
        money_flows=money_flows,
        error_count=len(errors),
    )


def _adjustment_probe_instruments(conn: sqlite3.Connection, sample_size: int) -> list[Instrument]:
    instruments = [
        item
        for item in _cn_a_instruments(conn, "both")
        if item.market == "CN_A" and "benchmark" not in item.tags and "index" not in item.tags
    ]
    by_ticker = {item.ticker: item for item in instruments}
    preferred = [
        "600000.SS",
        "000001.SZ",
        "002674.SZ",
        "300750.SZ",
        "301000.SZ",
        "688001.SS",
        "688981.SS",
        "920001.BJ",
        "430047.BJ",
        "000333.SZ",
    ]
    selected: list[Instrument] = []
    seen: set[str] = set()
    for ticker in preferred:
        item = by_ticker.get(ticker)
        if item and ticker not in seen:
            selected.append(item)
            seen.add(ticker)
    prefix_groups = (("600", "601", "603", "605"), ("000", "001"), ("002", "003"), ("300", "301"), ("688",), ("8", "920", "430"))
    for prefixes in prefix_groups:
        if len(selected) >= sample_size:
            break
        for item in instruments:
            code = item.ticker.split(".")[0]
            if item.ticker not in seen and code.startswith(prefixes):
                selected.append(item)
                seen.add(item.ticker)
                break
    for item in instruments:
        if len(selected) >= sample_size:
            break
        if item.ticker not in seen:
            selected.append(item)
            seen.add(item.ticker)
    return selected[:sample_size]


def probe_adjustment_sources(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    market: str = "CN_A",
    *,
    sample_size: int = 10,
    throttle_seconds: float = 0.15,
) -> AdjustmentProbeResult:
    if market != "CN_A":
        raise ValueError("adjustment probe v1 only supports CN_A")
    instruments = _adjustment_probe_instruments(conn, sample_size)
    samples: list[AdjustmentProbeSample] = []
    success = 0
    partial = 0
    failed = 0
    for instrument in instruments:
        bars, errors = fetch_bars(
            [instrument],
            start=parse_date(start_date),
            end=parse_date(end_date),
            throttle_seconds=throttle_seconds,
            adjust="qfq",
        )
        adjusted_rows = sum(1 for row in bars if row.get("adjustment_status") == "ADJUSTED")
        raw_rows = sum(1 for row in bars if row.get("adjustment_status") != "ADJUSTED")
        fallback_reasons = [
            str(row.get("adjustment_error"))
            for row in bars
            if row.get("adjustment_status") != "ADJUSTED" and row.get("adjustment_error")
        ]
        error_parts = list(dict.fromkeys([*errors[:2], *fallback_reasons[:2]]))
        error = "; ".join(error_parts) if error_parts else None
        if adjusted_rows and not raw_rows and not errors:
            status = "SUCCESS"
            success += 1
        elif adjusted_rows:
            status = "PARTIAL"
            partial += 1
        else:
            status = "FAILED"
            failed += 1
        samples.append(
            AdjustmentProbeSample(
                ticker=instrument.ticker,
                status=status,
                adjusted_rows=adjusted_rows,
                raw_rows=raw_rows,
                error=error,
            )
        )
    return AdjustmentProbeResult(
        market=market,
        start_date=start_date,
        end_date=end_date,
        sample_count=len(samples),
        success_count=success,
        partial_count=partial,
        failed_count=failed,
        samples=samples,
    )


def daily_run(conn: sqlite3.Connection, as_of_date: str) -> tuple[DataUpdateResult, DataAuditResult, int, tuple[int, int]]:
    update = data_update(conn, as_of_date, "CN_A")
    pre_audit = audit_data_coverage(
        conn,
        as_of_date,
        as_of_date,
        "CN_A",
        write=True,
        ignore_adjustment_for_short_term=True,
    )
    candidate_count = screen_all(conn, as_of_date)
    confirmation = confirm_candidates(conn, as_of_date)
    candidates = actionable_intraday_instruments(conn, as_of_date)
    if candidates:
        bars, errors = fetch_intraday_bars(candidates, parse_date(as_of_date), parse_date(as_of_date), period="5")
        upsert_many(conn, "intraday_bars", bars, ("market", "ticker", "datetime"))
        if errors:
            run_id = _start_run(conn, "daily-run-intraday", "CN_A", as_of_date, as_of_date)
            _record_errors(conn, run_id, "CN_A", "intraday_bars", errors)
            _finish_run(conn, run_id, status="PARTIAL_SUCCESS", requested_symbols=len(candidates), intraday_bars=len(bars), error_count=len(errors))
    audit = audit_data_coverage(
        conn,
        as_of_date,
        as_of_date,
        "CN_A",
        write=True,
        ignore_adjustment_for_short_term=True,
    )
    return update, audit if audit else pre_audit, candidate_count, confirmation
