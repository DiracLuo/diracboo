from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
) -> ReplayResult:
    dates = trading_dates(conn, start_date, end_date)
    total_candidates = 0
    total_evaluations = 0
    total_horizon_evaluations = 0
    for as_of_date in dates:
        total_candidates += screen_all(conn, as_of_date)
        total_evaluations += evaluate_candidates(conn, as_of_date, through_date)
        total_horizon_evaluations += evaluate_candidate_horizons_for_date(conn, as_of_date, through_date)
    return ReplayResult(
        start_date=start_date,
        end_date=end_date,
        through_date=through_date,
        dates=len(dates),
        candidates=total_candidates,
        evaluations=total_evaluations,
        horizon_evaluations=total_horizon_evaluations,
    )
