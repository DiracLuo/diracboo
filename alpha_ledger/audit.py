from __future__ import annotations

import sqlite3

from .ledger import now_utc


CROWDING_RISK = {
    "trend_breakout": 0.72,
    "abnormal_volume_small_midcap": 0.55,
    "xingye_style_prepositioning": 0.38,
    "us_sec_event_momentum": 0.50,
    "us_news_event_momentum": 0.58,
    "hk_buyback_recovery": 0.48,
    "hk_southbound_recovery": 0.55,
    "hk_news_recovery": 0.58,
    "a_share_hard_event_catalyst": 0.46,
    "cn_a_pead_quality_surprise": 0.52,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _rate(values: list[bool]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def _strategy_evals(conn: sqlite3.Connection, strategy_id: str, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT
                e.candidate_id,
                e.horizon_days,
                MAX(e.through_date) AS through_date
            FROM candidate_horizon_evaluations e
            JOIN candidates c ON c.id = e.candidate_id
            WHERE c.strategy_id = ?
              AND c.as_of_date <= ?
              AND e.through_date <= ?
            GROUP BY e.candidate_id, e.horizon_days
        )
        SELECT e.*
        FROM candidate_horizon_evaluations e
        JOIN latest l
          ON l.candidate_id = e.candidate_id
         AND l.horizon_days = e.horizon_days
         AND l.through_date = e.through_date
        """,
        (strategy_id, as_of_date, as_of_date),
    ).fetchall()


def audit_strategy(conn: sqlite3.Connection, strategy_id: str, as_of_date: str) -> dict[str, object]:
    candidate_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM candidates
        WHERE strategy_id = ?
          AND as_of_date <= ?
        """,
        (strategy_id, as_of_date),
    ).fetchone()["count"]
    evals = _strategy_evals(conn, strategy_id, as_of_date)

    completed_5d = [
        row for row in evals if row["horizon_days"] == 5 and row["observed_days"] >= row["horizon_days"]
    ]
    completed_10d = [
        row for row in evals if row["horizon_days"] == 10 and row["observed_days"] >= row["horizon_days"]
    ]

    returns_5d = [float(row["net_return_pct"]) for row in completed_5d]
    returns_10d = [float(row["net_return_pct"]) for row in completed_10d]
    avg_return_5d = _avg(returns_5d)
    avg_return_10d = _avg(returns_10d)
    win_rate_5d = _rate([value > 0 for value in returns_5d])
    win_rate_10d = _rate([value > 0 for value in returns_10d])
    stop_rate_10d = _rate([bool(row["hit_stop"]) for row in completed_10d])
    target_rate_10d = _rate([bool(row["hit_target_1"]) for row in completed_10d])

    # The first danger in an alpha factory is overtrusting tiny samples.
    completed_sample_count = max(len(completed_5d), len(completed_10d))
    sample_quality_score = _clamp(completed_sample_count / 30.0)
    crowding_risk_score = CROWDING_RISK.get(strategy_id, 0.65)

    short_edge = 0.0
    if avg_return_5d is not None:
        short_edge += _clamp(avg_return_5d / 8.0, -1.0, 1.0) * 0.45
    if avg_return_10d is not None:
        short_edge += _clamp(avg_return_10d / 12.0, -1.0, 1.0) * 0.55
    if win_rate_10d is not None:
        short_edge += (win_rate_10d - 0.5) * 0.5
    if stop_rate_10d is not None:
        short_edge -= stop_rate_10d * 0.4

    decay_risk_score = _clamp(crowding_risk_score * 0.55 + (1.0 - sample_quality_score) * 0.45)
    edge_score = _clamp((short_edge + 1.0) * 50.0 * (0.35 + sample_quality_score * 0.65) - decay_risk_score * 20.0, 0, 100)

    if candidate_count == 0:
        health_status = "NO_LIVE_SAMPLE"
        notes = "策略尚无机器筛选候选，不能判断有效性。"
    elif sample_quality_score < 0.2:
        health_status = "INSUFFICIENT_SAMPLE"
        notes = "样本过少，只能作为观察策略，不能提高权重。"
    elif avg_return_10d is not None and avg_return_10d <= 0:
        health_status = "DECAY_WATCH"
        notes = "完整T+10样本收益不佳，需要降权观察。"
    elif win_rate_10d is not None and win_rate_10d < 0.45:
        health_status = "DECAY_WATCH"
        notes = "完整T+10胜率偏低，需要检查策略条件是否过宽。"
    elif crowding_risk_score >= 0.7:
        health_status = "CROWDING_WATCH"
        notes = "策略高度公开，必须依靠更严格过滤和样本外验证。"
    else:
        health_status = "ACTIVE_OBSERVE"
        notes = "策略可继续观察，但仍需更多样本验证。"

    return {
        "strategy_id": strategy_id,
        "as_of_date": as_of_date,
        "signal_count": int(candidate_count),
        "completed_5d": len(completed_5d),
        "completed_10d": len(completed_10d),
        "avg_return_5d": avg_return_5d,
        "avg_return_10d": avg_return_10d,
        "win_rate_5d": win_rate_5d,
        "win_rate_10d": win_rate_10d,
        "stop_rate_10d": stop_rate_10d,
        "target_rate_10d": target_rate_10d,
        "sample_quality_score": sample_quality_score,
        "crowding_risk_score": crowding_risk_score,
        "decay_risk_score": decay_risk_score,
        "edge_score": edge_score,
        "health_status": health_status,
        "notes": notes,
        "created_at": now_utc(),
    }


def audit_all(conn: sqlite3.Connection, as_of_date: str) -> int:
    strategies = conn.execute("SELECT id FROM strategies WHERE status = 'ACTIVE' ORDER BY id").fetchall()
    rows = [audit_strategy(conn, row["id"], as_of_date) for row in strategies]
    conn.execute(
        """
        DELETE FROM strategy_audits
        WHERE as_of_date = ?
          AND strategy_id IN (
              SELECT id FROM strategies WHERE status != 'ACTIVE'
          )
        """,
        (as_of_date,),
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO strategy_audits (
                strategy_id, as_of_date, signal_count, completed_5d, completed_10d,
                avg_return_5d, avg_return_10d, win_rate_5d, win_rate_10d,
                stop_rate_10d, target_rate_10d, sample_quality_score,
                crowding_risk_score, decay_risk_score, edge_score,
                health_status, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, as_of_date) DO UPDATE SET
                signal_count=excluded.signal_count,
                completed_5d=excluded.completed_5d,
                completed_10d=excluded.completed_10d,
                avg_return_5d=excluded.avg_return_5d,
                avg_return_10d=excluded.avg_return_10d,
                win_rate_5d=excluded.win_rate_5d,
                win_rate_10d=excluded.win_rate_10d,
                stop_rate_10d=excluded.stop_rate_10d,
                target_rate_10d=excluded.target_rate_10d,
                sample_quality_score=excluded.sample_quality_score,
                crowding_risk_score=excluded.crowding_risk_score,
                decay_risk_score=excluded.decay_risk_score,
                edge_score=excluded.edge_score,
                health_status=excluded.health_status,
                notes=excluded.notes,
                created_at=excluded.created_at
            """,
            (
                row["strategy_id"],
                row["as_of_date"],
                row["signal_count"],
                row["completed_5d"],
                row["completed_10d"],
                row["avg_return_5d"],
                row["avg_return_10d"],
                row["win_rate_5d"],
                row["win_rate_10d"],
                row["stop_rate_10d"],
                row["target_rate_10d"],
                row["sample_quality_score"],
                row["crowding_risk_score"],
                row["decay_risk_score"],
                row["edge_score"],
                row["health_status"],
                row["notes"],
                row["created_at"],
            ),
        )
    conn.commit()
    return len(rows)


def latest_audits(conn: sqlite3.Connection, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*, s.name AS strategy_name
        FROM strategy_audits a
        JOIN strategies s ON s.id = a.strategy_id
        WHERE a.as_of_date = ?
          AND s.status = 'ACTIVE'
        ORDER BY a.edge_score DESC, a.strategy_id
        """,
        (as_of_date,),
    ).fetchall()
