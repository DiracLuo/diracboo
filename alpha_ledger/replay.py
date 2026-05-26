from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from .db import upsert_many
from .market_data import Instrument, fetch_intraday_bars, parse_date
from .metrics import evaluate_candidate_horizons_for_date, evaluate_candidates
from .screener import screen_all


@dataclass(frozen=True)
class ReplayResult:
    start_date: str
    end_date: str
    through_date: str
    dates: int
    candidates: int
    evaluations: int
    horizon_evaluations: int
    intraday_bars: int = 0
    intraday_errors: int = 0


def cn_a_candidate_instruments(conn: sqlite3.Connection, as_of_date: str) -> list[Instrument]:
    rows = conn.execute(
        """
        SELECT DISTINCT
               c.market,
               c.ticker,
               COALESCE(i.name, c.name) AS name,
               COALESCE(i.source, 'sina_cn') AS source,
               COALESCE(
                   i.source_symbol,
                   CASE
                       WHEN c.ticker LIKE '%.SS' THEN 'sh' || SUBSTR(c.ticker, 1, 6)
                       WHEN c.ticker LIKE '%.SZ' THEN 'sz' || SUBSTR(c.ticker, 1, 6)
                       WHEN c.ticker LIKE '%.BJ' THEN 'bj' || SUBSTR(c.ticker, 1, 6)
                       ELSE SUBSTR(c.ticker, 1, 6)
                   END
               ) AS source_symbol,
               COALESCE(i.active, 1) AS active,
               COALESCE(i.tags_json, '[]') AS tags_json
        FROM candidates c
        LEFT JOIN instruments i
          ON i.market = c.market AND i.ticker = c.ticker
        WHERE c.as_of_date = ?
          AND c.market = 'CN_A'
        ORDER BY c.ticker
        """,
        (as_of_date,),
    ).fetchall()
    instruments = []
    for row in rows:
        instruments.append(
            Instrument(
                market=row["market"],
                ticker=row["ticker"],
                name=row["name"],
                source=row["source"],
                source_symbol=row["source_symbol"],
                active=bool(row["active"]),
                tags=(),
            )
        )
    return instruments


def trading_dates(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM price_bars
        WHERE date >= ? AND date <= ?
        ORDER BY date
        """,
        (start_date, end_date),
    ).fetchall()
    return [str(row["date"]) for row in rows]


def replay_candidates(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    *,
    fetch_cn_a_intraday: bool = False,
    intraday_period: str = "5",
    intraday_throttle_seconds: float = 0.15,
    require_adjusted: bool = False,
    benchmark_ticker: str | None = "000300.SS",
) -> ReplayResult:
    dates = trading_dates(conn, start_date, end_date)
    total_candidates = 0
    total_evaluations = 0
    total_horizon_evaluations = 0
    total_intraday_bars = 0
    total_intraday_errors = 0
    for as_of_date in dates:
        total_candidates += screen_all(conn, as_of_date)
        if fetch_cn_a_intraday:
            instruments = cn_a_candidate_instruments(conn, as_of_date)
            intraday_start = parse_date(as_of_date) + timedelta(days=1)
            bars, errors = fetch_intraday_bars(
                instruments,
                start=intraday_start,
                end=parse_date(through_date),
                period=intraday_period,
                throttle_seconds=intraday_throttle_seconds,
            )
            total_intraday_bars += upsert_many(conn, "intraday_bars", bars, ("market", "ticker", "datetime"))
            total_intraday_errors += len(errors)
        total_evaluations += evaluate_candidates(
            conn,
            as_of_date,
            through_date,
            require_adjusted=require_adjusted,
            benchmark_ticker=benchmark_ticker,
        )
        total_horizon_evaluations += evaluate_candidate_horizons_for_date(
            conn,
            as_of_date,
            through_date,
            require_adjusted=require_adjusted,
            benchmark_ticker=benchmark_ticker,
        )
    return ReplayResult(
        start_date=start_date,
        end_date=end_date,
        through_date=through_date,
        dates=len(dates),
        candidates=total_candidates,
        evaluations=total_evaluations,
        horizon_evaluations=total_horizon_evaluations,
        intraday_bars=total_intraday_bars,
        intraday_errors=total_intraday_errors,
    )
