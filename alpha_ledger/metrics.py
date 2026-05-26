from __future__ import annotations

import sqlite3

from .ledger import now_utc


HORIZONS = (5, 10, 20, 60)
CANDIDATE_HORIZONS = HORIZONS
MIN_WEIGHT_SUGGESTION_SAMPLES = 5


def pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _exit_price_for_stop(open_price: float, stop_loss: float) -> float:
    return open_price if open_price <= stop_loss else stop_loss


def _exit_price_for_target(open_price: float, target: float) -> float:
    return open_price if open_price >= target else target


def _long_trade_path(
    bars: list[sqlite3.Row],
    entry_price: float,
    stop_loss: object,
    target_1: object,
    target_2: object,
) -> dict[str, object]:
    stop = None if stop_loss is None else float(stop_loss)
    first_target = None if target_1 is None else float(target_1)
    second_target = None if target_2 is None else float(target_2)
    observed_bars: list[sqlite3.Row] = []
    exit_type = "HOLD"
    exit_date = None
    exit_price = float(bars[-1]["close"])
    exit_note = "未触发止损/目标，按观察窗口最后收盘价计。"

    for bar in bars:
        observed_bars.append(bar)
        open_price = float(bar["open"])
        low = float(bar["low"])
        high = float(bar["high"])

        if stop is not None and open_price <= stop:
            exit_type = "STOP_LOSS"
            exit_date = bar["date"]
            exit_price = open_price
            exit_note = "开盘低于或等于止损价，按开盘价止损。"
            break
        if second_target is not None and open_price >= second_target:
            exit_type = "TARGET_2"
            exit_date = bar["date"]
            exit_price = open_price
            exit_note = "开盘高于或等于目标2，按开盘价止盈。"
            break
        if first_target is not None and open_price >= first_target:
            exit_type = "TARGET_1"
            exit_date = bar["date"]
            exit_price = open_price
            exit_note = "开盘高于或等于目标1，按开盘价止盈。"
            break

        stop_hit = stop is not None and low <= stop
        target_2_hit = second_target is not None and high >= second_target
        target_1_hit = first_target is not None and high >= first_target
        if stop_hit and (target_1_hit or target_2_hit):
            exit_type = "STOP_LOSS"
            exit_date = bar["date"]
            exit_price = _exit_price_for_stop(open_price, float(stop))
            exit_note = "日K同时触及止损和目标，因无日内顺序数据，采用保守假设：止损先发生。"
            break
        if stop_hit:
            exit_type = "STOP_LOSS"
            exit_date = bar["date"]
            exit_price = _exit_price_for_stop(open_price, float(stop))
            exit_note = "盘中触及止损价。"
            break
        if target_2_hit:
            exit_type = "TARGET_2"
            exit_date = bar["date"]
            exit_price = _exit_price_for_target(open_price, float(second_target))
            exit_note = "盘中触及目标2。"
            break
        if target_1_hit:
            exit_type = "TARGET_1"
            exit_date = bar["date"]
            exit_price = _exit_price_for_target(open_price, float(first_target))
            exit_note = "盘中触及目标1。"
            break

    lows = [float(row["low"]) for row in observed_bars]
    highs = [float(row["high"]) for row in observed_bars]
    end_bar = observed_bars[-1]
    if exit_date is None:
        exit_date = end_bar["date"]
    hit_stop = int(exit_type == "STOP_LOSS")
    hit_target_1 = int(exit_type in {"TARGET_1", "TARGET_2"})
    hit_target_2 = int(exit_type == "TARGET_2")
    return {
        "end_date": exit_date,
        "end_close": exit_price,
        "return_pct": pct_change(entry_price, exit_price),
        "max_gain_pct": pct_change(entry_price, max(highs)),
        "max_drawdown_pct": pct_change(entry_price, min(lows)),
        "hit_stop": hit_stop,
        "hit_target_1": hit_target_1,
        "hit_target_2": hit_target_2,
        "exit_type": exit_type,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_note": exit_note,
    }


def evaluate_signal(conn: sqlite3.Connection, signal_id: int, as_of_date: str) -> list[dict[str, object]]:
    signal = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    if signal is None:
        raise ValueError(f"Signal {signal_id} does not exist")

    bars = conn.execute(
        """
        SELECT * FROM price_bars
        WHERE market = ? AND ticker = ? AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (signal["market"], signal["ticker"], signal["signal_date"], as_of_date),
    ).fetchall()
    if len(bars) < 2:
        return []

    reference = bars[0]
    future_bars = bars[1:]
    results: list[dict[str, object]] = []
    for horizon in HORIZONS:
        observed = min(horizon, len(future_bars))
        if observed <= 0:
            continue
        window = future_bars[:observed]
        reference_close = float(signal["entry_price"] or reference["close"])
        path = _long_trade_path(
            window,
            reference_close,
            signal["stop_loss"],
            signal["target_1"],
            signal["target_2"],
        )

        result = {
            "signal_id": signal_id,
            "as_of_date": as_of_date,
            "horizon_days": horizon,
            "observed_days": observed,
            "reference_date": signal["signal_date"],
            "reference_close": reference_close,
            "end_date": path["end_date"],
            "end_close": path["end_close"],
            "return_pct": path["return_pct"],
            "max_gain_pct": path["max_gain_pct"],
            "max_drawdown_pct": path["max_drawdown_pct"],
            "hit_stop": path["hit_stop"],
            "hit_target_1": path["hit_target_1"],
            "hit_target_2": path["hit_target_2"],
            "exit_type": path["exit_type"],
            "exit_date": path["exit_date"],
            "exit_price": path["exit_price"],
            "exit_note": path["exit_note"],
            "created_at": now_utc(),
        }
        results.append(result)
    return results


def evaluate_all(conn: sqlite3.Connection, as_of_date: str) -> int:
    signals = conn.execute("SELECT id FROM signals ORDER BY id").fetchall()
    rows: list[dict[str, object]] = []
    for signal in signals:
        rows.extend(evaluate_signal(conn, int(signal["id"]), as_of_date))

    for row in rows:
        conn.execute(
            """
            INSERT INTO evaluations (
                signal_id, as_of_date, horizon_days, observed_days, reference_date,
                reference_close, end_date, end_close, return_pct, max_gain_pct,
                max_drawdown_pct, hit_stop, hit_target_1, hit_target_2,
                exit_type, exit_date, exit_price, exit_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(signal_id, as_of_date, horizon_days) DO UPDATE SET
                observed_days=excluded.observed_days,
                reference_date=excluded.reference_date,
                reference_close=excluded.reference_close,
                end_date=excluded.end_date,
                end_close=excluded.end_close,
                return_pct=excluded.return_pct,
                max_gain_pct=excluded.max_gain_pct,
                max_drawdown_pct=excluded.max_drawdown_pct,
                hit_stop=excluded.hit_stop,
                hit_target_1=excluded.hit_target_1,
                hit_target_2=excluded.hit_target_2,
                exit_type=excluded.exit_type,
                exit_date=excluded.exit_date,
                exit_price=excluded.exit_price,
                exit_note=excluded.exit_note,
                created_at=excluded.created_at
            """,
            (
                row["signal_id"],
                row["as_of_date"],
                row["horizon_days"],
                row["observed_days"],
                row["reference_date"],
                row["reference_close"],
                row["end_date"],
                row["end_close"],
                row["return_pct"],
                row["max_gain_pct"],
                row["max_drawdown_pct"],
                row["hit_stop"],
                row["hit_target_1"],
                row["hit_target_2"],
                row["exit_type"],
                row["exit_date"],
                row["exit_price"],
                row["exit_note"],
                row["created_at"],
            ),
        )
    conn.commit()
    return len(rows)


def evaluate_candidate(conn: sqlite3.Connection, candidate_id: int, through_date: str) -> dict[str, object] | None:
    candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} does not exist")

    future_bars = conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date > ? AND date <= ?
        ORDER BY date
        """,
        (candidate["market"], candidate["ticker"], candidate["as_of_date"], through_date),
    ).fetchall()
    if not future_bars:
        return None

    execution_bar = future_bars[0]
    execution_price = float(execution_bar["open"])
    path = _long_trade_path(
        future_bars,
        execution_price,
        candidate["stop_loss"],
        candidate["target_1"],
        candidate["target_2"],
    )
    return {
        "candidate_id": candidate_id,
        "through_date": through_date,
        "observed_days": len(future_bars),
        "reference_date": execution_bar["date"],
        "reference_close": execution_price,
        "execution_date": execution_bar["date"],
        "execution_price": execution_price,
        "execution_type": "NEXT_OPEN",
        "execution_note": "候选日后第一个交易日开盘价，避免假设候选日收盘即可成交。",
        **path,
        "created_at": now_utc(),
    }


def evaluate_candidate_horizons(
    conn: sqlite3.Connection,
    candidate_id: int,
    through_date: str,
    horizons: tuple[int, ...] = CANDIDATE_HORIZONS,
) -> list[dict[str, object]]:
    candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} does not exist")

    future_bars = conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date > ? AND date <= ?
        ORDER BY date
        """,
        (candidate["market"], candidate["ticker"], candidate["as_of_date"], through_date),
    ).fetchall()
    if not future_bars:
        return []

    execution_bar = future_bars[0]
    execution_price = float(execution_bar["open"])
    rows: list[dict[str, object]] = []
    for horizon in horizons:
        observed = min(horizon, len(future_bars))
        if observed <= 0:
            continue
        window = future_bars[:observed]
        path = _long_trade_path(
            window,
            execution_price,
            candidate["stop_loss"],
            candidate["target_1"],
            candidate["target_2"],
        )
        rows.append(
            {
                "candidate_id": candidate_id,
                "horizon_days": horizon,
                "through_date": through_date,
                "observed_days": observed,
                "reference_date": execution_bar["date"],
                "reference_close": execution_price,
                "execution_date": execution_bar["date"],
                "execution_price": execution_price,
                "execution_type": "NEXT_OPEN",
                "execution_note": "候选日后第一个交易日开盘价，固定持有周期后验。",
                **path,
                "created_at": now_utc(),
            }
        )
    return rows


def evaluate_candidates(conn: sqlite3.Connection, candidate_date: str, through_date: str) -> int:
    candidates = conn.execute(
        "SELECT id FROM candidates WHERE as_of_date = ? ORDER BY id", (candidate_date,)
    ).fetchall()
    rows = []
    for candidate in candidates:
        row = evaluate_candidate(conn, int(candidate["id"]), through_date)
        if row is not None:
            rows.append(row)

    for row in rows:
        conn.execute(
            """
            INSERT INTO candidate_evaluations (
                candidate_id, through_date, observed_days, reference_date,
                reference_close, execution_date, execution_price, execution_type,
                execution_note, end_date, end_close, return_pct, max_gain_pct,
                max_drawdown_pct, hit_stop, hit_target_1, hit_target_2,
                exit_type, exit_date, exit_price, exit_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, through_date) DO UPDATE SET
                observed_days=excluded.observed_days,
                reference_date=excluded.reference_date,
                reference_close=excluded.reference_close,
                execution_date=excluded.execution_date,
                execution_price=excluded.execution_price,
                execution_type=excluded.execution_type,
                execution_note=excluded.execution_note,
                end_date=excluded.end_date,
                end_close=excluded.end_close,
                return_pct=excluded.return_pct,
                max_gain_pct=excluded.max_gain_pct,
                max_drawdown_pct=excluded.max_drawdown_pct,
                hit_stop=excluded.hit_stop,
                hit_target_1=excluded.hit_target_1,
                hit_target_2=excluded.hit_target_2,
                exit_type=excluded.exit_type,
                exit_date=excluded.exit_date,
                exit_price=excluded.exit_price,
                exit_note=excluded.exit_note,
                created_at=excluded.created_at
            """,
            (
                row["candidate_id"],
                row["through_date"],
                row["observed_days"],
                row["reference_date"],
                row["reference_close"],
                row["execution_date"],
                row["execution_price"],
                row["execution_type"],
                row["execution_note"],
                row["end_date"],
                row["end_close"],
                row["return_pct"],
                row["max_gain_pct"],
                row["max_drawdown_pct"],
                row["hit_stop"],
                row["hit_target_1"],
                row["hit_target_2"],
                row["exit_type"],
                row["exit_date"],
                row["exit_price"],
                row["exit_note"],
                row["created_at"],
            ),
        )
    conn.commit()
    return len(rows)


def evaluate_candidate_horizons_for_date(conn: sqlite3.Connection, candidate_date: str, through_date: str) -> int:
    candidates = conn.execute(
        "SELECT id FROM candidates WHERE as_of_date = ? ORDER BY id", (candidate_date,)
    ).fetchall()
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        rows.extend(evaluate_candidate_horizons(conn, int(candidate["id"]), through_date))

    for row in rows:
        conn.execute(
            """
            INSERT INTO candidate_horizon_evaluations (
                candidate_id, horizon_days, through_date, observed_days,
                reference_date, reference_close, execution_date, execution_price,
                execution_type, execution_note, end_date, end_close, return_pct,
                max_gain_pct, max_drawdown_pct, hit_stop, hit_target_1,
                hit_target_2, exit_type, exit_date, exit_price, exit_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, horizon_days, through_date) DO UPDATE SET
                observed_days=excluded.observed_days,
                reference_date=excluded.reference_date,
                reference_close=excluded.reference_close,
                execution_date=excluded.execution_date,
                execution_price=excluded.execution_price,
                execution_type=excluded.execution_type,
                execution_note=excluded.execution_note,
                end_date=excluded.end_date,
                end_close=excluded.end_close,
                return_pct=excluded.return_pct,
                max_gain_pct=excluded.max_gain_pct,
                max_drawdown_pct=excluded.max_drawdown_pct,
                hit_stop=excluded.hit_stop,
                hit_target_1=excluded.hit_target_1,
                hit_target_2=excluded.hit_target_2,
                exit_type=excluded.exit_type,
                exit_date=excluded.exit_date,
                exit_price=excluded.exit_price,
                exit_note=excluded.exit_note,
                created_at=excluded.created_at
            """,
            (
                row["candidate_id"],
                row["horizon_days"],
                row["through_date"],
                row["observed_days"],
                row["reference_date"],
                row["reference_close"],
                row["execution_date"],
                row["execution_price"],
                row["execution_type"],
                row["execution_note"],
                row["end_date"],
                row["end_close"],
                row["return_pct"],
                row["max_gain_pct"],
                row["max_drawdown_pct"],
                row["hit_stop"],
                row["hit_target_1"],
                row["hit_target_2"],
                row["exit_type"],
                row["exit_date"],
                row["exit_price"],
                row["exit_note"],
                row["created_at"],
            ),
        )
    conn.commit()
    return len(rows)


def strategy_leaderboard(conn: sqlite3.Connection, as_of_date: str | None = None) -> list[sqlite3.Row]:
    signal_date_filter = ""
    params: tuple[object, ...] = ()
    if as_of_date is not None:
        signal_date_filter = "WHERE s.signal_date <= ?"
        params = (as_of_date,)
    return conn.execute(
        f"""
        WITH latest_eval AS (
            SELECT e.*
            FROM evaluations e
            JOIN (
                SELECT signal_id, horizon_days, MAX(as_of_date) AS max_as_of
                FROM evaluations
                GROUP BY signal_id, horizon_days
            ) latest
              ON latest.signal_id = e.signal_id
             AND latest.horizon_days = e.horizon_days
             AND latest.max_as_of = e.as_of_date
        )
        SELECT
            s.strategy_id,
            st.name AS strategy_name,
            st.weight,
            st.target_horizon_days,
            COUNT(DISTINCT s.id) AS signal_count,
            AVG(
                CASE
                    WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days
                    THEN e.return_pct
                END
            ) AS avg_return_5d,
            AVG(
                CASE
                    WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days
                    THEN e.return_pct
                END
            ) AS avg_return_10d,
            AVG(
                CASE
                    WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days
                    THEN e.return_pct
                END
            ) AS avg_return_20d,
            AVG(
                CASE
                    WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days
                    THEN e.return_pct
                END
            ) AS avg_return_60d,
            AVG(
                CASE
                    WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days
                    THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END
                END
            ) AS win_rate_10d
        FROM signals s
        JOIN strategies st ON st.id = s.strategy_id
        LEFT JOIN latest_eval e ON e.signal_id = s.id
        {signal_date_filter}
        {"AND" if signal_date_filter else "WHERE"} st.status != 'RETIRED'
        GROUP BY s.strategy_id, st.name, st.weight, st.target_horizon_days
        ORDER BY COALESCE(avg_return_10d, avg_return_5d, 0) DESC
        """,
        params,
    ).fetchall()


def candidate_strategy_leaderboard(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    through_date: str | None = None,
    *,
    dedupe: bool = False,
) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[object] = []
    latest_filters: list[str] = []
    latest_params: list[object] = []
    if start_date is not None:
        filters.append("c.as_of_date >= ?")
        params.append(start_date)
    if end_date is not None:
        filters.append("c.as_of_date <= ?")
        params.append(end_date)
    if through_date is not None:
        latest_filters.append("through_date <= ?")
        latest_params.append(through_date)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    latest_where_sql = f"WHERE {' AND '.join(latest_filters)}" if latest_filters else ""
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            {latest_where_sql}
            GROUP BY candidate_id
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        ),
        base_candidates AS (
            SELECT
                c.*,
                st.name AS strategy_name,
                st.weight,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            {where_sql}
            {"AND" if where_sql else "WHERE"} st.status != 'RETIRED'
        ),
        selected_candidates AS (
            SELECT *
            FROM base_candidates
            {dedupe_sql}
        )
        SELECT
            c.strategy_id,
            c.strategy_name,
            c.weight,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(e.id) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(e.return_pct) AS avg_return_pct,
            AVG(e.max_gain_pct) AS avg_max_gain_pct,
            AVG(e.max_drawdown_pct) AS avg_max_drawdown_pct,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_2 = 1 THEN 1.0 ELSE 0.0 END END) AS target_2_rate
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name, c.weight
        ORDER BY COALESCE(avg_return_pct, -999) DESC, evaluated_count DESC, candidate_count DESC
        """,
        tuple(latest_params + params),
    ).fetchall()


def candidate_horizon_strategy_leaderboard(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    through_date: str | None = None,
    *,
    horizon_days: int = 10,
    dedupe: bool = False,
) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[object] = []
    eval_filters = ["horizon_days = ?"]
    eval_params: list[object] = [horizon_days]
    if start_date is not None:
        filters.append("c.as_of_date >= ?")
        params.append(start_date)
    if end_date is not None:
        filters.append("c.as_of_date <= ?")
        params.append(end_date)
    if through_date is not None:
        eval_filters.append("through_date <= ?")
        eval_params.append(through_date)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    eval_where_sql = f"WHERE {' AND '.join(eval_filters)}"
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, horizon_days, MAX(through_date) AS through_date
            FROM candidate_horizon_evaluations
            {eval_where_sql}
            GROUP BY candidate_id, horizon_days
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_horizon_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.horizon_days = e.horizon_days
             AND l.through_date = e.through_date
        ),
        base_candidates AS (
            SELECT
                c.*,
                st.name AS strategy_name,
                st.weight,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            {where_sql}
            {"AND" if where_sql else "WHERE"} st.status != 'RETIRED'
        ),
        selected_candidates AS (
            SELECT *
            FROM base_candidates
            {dedupe_sql}
        )
        SELECT
            c.strategy_id,
            c.strategy_name,
            c.weight,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(CASE WHEN e.observed_days >= e.horizon_days THEN e.id END) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.max_gain_pct END) AS avg_max_gain_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.max_drawdown_pct END) AS avg_max_drawdown_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.hit_target_2 = 1 THEN 1.0 ELSE 0.0 END END) AS target_2_rate
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name, c.weight
        ORDER BY COALESCE(avg_return_pct, -999) DESC, evaluated_count DESC, candidate_count DESC
        """,
        tuple(eval_params + params),
    ).fetchall()


def _candidate_segment_leaderboard(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None,
    segment_sql: str,
    *,
    dedupe: bool = True,
) -> list[sqlite3.Row]:
    latest_where_sql = "WHERE through_date <= ?" if through_date is not None else ""
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    latest_params: list[object] = [through_date] if through_date is not None else []
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            {latest_where_sql}
            GROUP BY candidate_id
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        ),
        base_candidates AS (
            SELECT
                c.*,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            WHERE c.as_of_date >= ?
              AND c.as_of_date <= ?
              AND st.status != 'RETIRED'
        ),
        selected_candidates AS (
            SELECT *
            FROM base_candidates
            {dedupe_sql}
        )
        SELECT
            {segment_sql} AS segment,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(e.id) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(e.return_pct) AS avg_return_pct,
            AVG(e.max_gain_pct) AS avg_max_gain_pct,
            AVG(e.max_drawdown_pct) AS avg_max_drawdown_pct,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_2 = 1 THEN 1.0 ELSE 0.0 END END) AS target_2_rate
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY segment
        ORDER BY COALESCE(avg_return_pct, -999) DESC, evaluated_count DESC, candidate_count DESC
        """,
        tuple(latest_params + [start_date, end_date]),
    ).fetchall()


def candidate_market_leaderboard(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    dedupe: bool = True,
) -> list[sqlite3.Row]:
    return _candidate_segment_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        "c.market",
        dedupe=dedupe,
    )


def candidate_action_leaderboard(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    dedupe: bool = True,
) -> list[sqlite3.Row]:
    return _candidate_segment_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        """
        CASE
            WHEN UPPER(c.action) LIKE '%CONFIRM%' THEN '次日确认/确认后触发'
            ELSE '买入当天触发'
        END
        """,
        dedupe=dedupe,
    )


def suggest_strategy_weight_adjustments(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    min_samples: int = MIN_WEIGHT_SUGGESTION_SAMPLES,
) -> list[dict[str, object]]:
    rows = candidate_horizon_strategy_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        horizon_days=10,
        dedupe=True,
    )
    suggestions: list[dict[str, object]] = []
    for row in rows:
        evaluated = int(row["evaluated_count"] or 0)
        weight = float(row["weight"] or 1.0)
        stop_rate = row["stop_rate"]
        win_rate = row["win_rate"]
        avg_return = row["avg_return_pct"]
        target_1_rate = row["target_1_rate"]
        recommendation = "KEEP_COLLECTING"
        suggested_weight = weight
        reason = "样本仍少，继续观察。"
        if evaluated < min_samples:
            recommendation = "INSUFFICIENT_SAMPLE"
        elif stop_rate is not None and float(stop_rate) >= 0.45:
            recommendation = "DOWN_WEIGHT"
            suggested_weight = max(0.25, weight * 0.70)
            reason = "去重后止损率过高，先自动降权。"
        elif avg_return is not None and win_rate is not None and float(avg_return) < 0 and float(win_rate) < 0.45:
            recommendation = "DOWN_WEIGHT"
            suggested_weight = max(0.25, weight * 0.80)
            reason = "平均收益和胜率同时偏弱，先自动降权。"
        elif target_1_rate is not None and win_rate is not None and float(target_1_rate) >= 0.50 and float(win_rate) >= 0.50:
            recommendation = "KEEP_OR_UP_WEIGHT"
            suggested_weight = min(2.0, weight * 1.05)
            reason = "目标触达率和胜率较好，可保留或小幅加权。"
        suggestions.append(
            {
                "strategy_id": row["strategy_id"],
                "strategy_name": row["strategy_name"],
                "evaluated_count": evaluated,
                "current_weight": weight,
                "suggested_weight": suggested_weight,
                "recommendation": recommendation,
                "reason": reason,
                "stop_rate": stop_rate,
                "win_rate": win_rate,
                "avg_return_pct": avg_return,
            }
        )
    return suggestions


def apply_strategy_weight_adjustments(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    min_samples: int = MIN_WEIGHT_SUGGESTION_SAMPLES,
) -> int:
    suggestions = suggest_strategy_weight_adjustments(
        conn,
        start_date,
        end_date,
        through_date,
        min_samples=min_samples,
    )
    changed = 0
    for item in suggestions:
        if item["recommendation"] != "DOWN_WEIGHT":
            continue
        if float(item["suggested_weight"]) == float(item["current_weight"]):
            continue
        conn.execute(
            "UPDATE strategies SET weight = ? WHERE id = ?",
            (item["suggested_weight"], item["strategy_id"]),
        )
        changed += 1
    conn.commit()
    return changed
