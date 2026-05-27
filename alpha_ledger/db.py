from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from .metrics import DEFAULT_TRADE_COST_PCT, MARKET_TRADE_COST_PCT


DEFAULT_DB_PATH = Path("data/alpha_ledger.sqlite")

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_VALID_TABLES: frozenset[str] = frozenset({
    "instruments", "strategies", "signals", "price_bars", "intraday_bars",
    "tracking_events", "evaluations", "research_events", "corporate_events",
    "financial_metrics", "money_flows", "candidates", "candidate_evaluations",
    "candidate_horizon_evaluations", "strategy_audits", "data_fetch_runs",
    "data_fetch_errors", "data_coverage_daily", "data_source_health",
})


def _validate_identifier(name: str, label: str = "identifier") -> None:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid {label}: {name!r}")


def _validate_table_name(table: str) -> None:
    if table not in _VALID_TABLES:
        raise ValueError(f"Unknown table: {table!r}")


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS instruments (
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        source TEXT NOT NULL,
        source_symbol TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        tags_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        PRIMARY KEY(market, ticker)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategies (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        market_scope TEXT NOT NULL,
        thesis TEXT NOT NULL,
        entry_rules_json TEXT NOT NULL,
        exit_rules_json TEXT NOT NULL,
        target_horizon_days INTEGER NOT NULL DEFAULT 10,
        version TEXT NOT NULL DEFAULT 'v1',
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        weight REAL NOT NULL DEFAULT 1.0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        market TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        entry_type TEXT NOT NULL,
        entry_price REAL NOT NULL,
        buy_zone_low REAL,
        buy_zone_high REAL,
        stop_loss REAL,
        target_1 REAL,
        target_2 REAL,
        horizon_days INTEGER NOT NULL,
        confidence TEXT NOT NULL,
        thesis TEXT NOT NULL,
        trigger_condition TEXT NOT NULL,
        risk_notes TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'OPEN',
        immutable_hash TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id),
        UNIQUE(signal_date, ticker, market, strategy_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_bars (
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        open REAL NOT NULL,
        close REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        volume REAL NOT NULL,
        amount REAL,
        amplitude_pct REAL,
        change_pct REAL,
        turnover_pct REAL,
        adj_open REAL,
        adj_close REAL,
        adj_high REAL,
        adj_low REAL,
        adj_factor REAL,
        adjustment_status TEXT NOT NULL DEFAULT 'UNKNOWN',
        adjustment_error TEXT,
        PRIMARY KEY(market, ticker, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS intraday_bars (
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        datetime TEXT NOT NULL,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        open REAL NOT NULL,
        close REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        volume REAL NOT NULL,
        amount REAL,
        PRIMARY KEY(market, ticker, datetime)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracking_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        event_date TEXT NOT NULL,
        event_type TEXT NOT NULL,
        price REAL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id INTEGER NOT NULL,
        as_of_date TEXT NOT NULL,
        horizon_days INTEGER NOT NULL,
        observed_days INTEGER NOT NULL,
        reference_date TEXT NOT NULL,
        reference_close REAL NOT NULL,
        end_date TEXT NOT NULL,
        end_close REAL NOT NULL,
        return_pct REAL NOT NULL,
        gross_return_pct REAL NOT NULL DEFAULT 0,
        cost_pct REAL NOT NULL DEFAULT 0,
        net_return_pct REAL NOT NULL DEFAULT 0,
        net_win INTEGER NOT NULL DEFAULT 0,
        benchmark_ticker TEXT,
        benchmark_return_pct REAL,
        excess_return_pct REAL,
        max_gain_pct REAL NOT NULL,
        max_drawdown_pct REAL NOT NULL,
        hit_stop INTEGER NOT NULL,
        hit_target_1 INTEGER NOT NULL,
        hit_target_2 INTEGER NOT NULL,
        exit_type TEXT NOT NULL DEFAULT 'HOLD',
        exit_date TEXT,
        exit_price REAL,
        exit_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(signal_id) REFERENCES signals(id),
        UNIQUE(signal_id, as_of_date, horizon_days)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        event_date TEXT NOT NULL,
        published_date TEXT NOT NULL,
        event_type TEXT NOT NULL,
        participant_count INTEGER,
        quality_score REAL NOT NULL,
        revaluation_tags_json TEXT NOT NULL DEFAULT '[]',
        summary TEXT NOT NULL,
        source_url TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(market, ticker, event_date, event_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corporate_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        event_date TEXT NOT NULL,
        event_type TEXT NOT NULL,
        title TEXT NOT NULL,
        source TEXT NOT NULL,
        source_url TEXT,
        importance_score REAL NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(market, ticker, event_date, event_type, title)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        report_date TEXT NOT NULL,
        published_date TEXT,
        metric_name TEXT NOT NULL,
        metric_value REAL,
        unit TEXT,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(market, ticker, report_date, metric_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS money_flows (
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        name TEXT NOT NULL,
        net_inflow REAL,
        inflow REAL,
        outflow REAL,
        turnover_amount REAL,
        turnover_rate REAL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(market, ticker, date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        as_of_date TEXT NOT NULL,
        market TEXT NOT NULL,
        ticker TEXT NOT NULL,
        name TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        candidate_score REAL NOT NULL,
        action TEXT NOT NULL,
        entry_price REAL NOT NULL,
        buy_zone_low REAL,
        buy_zone_high REAL,
        stop_loss REAL,
        target_1 REAL,
        target_2 REAL,
        reward_risk_ratio REAL,
        expected_value_score REAL,
        trailing_stop_pct REAL,
        trailing_activation_pct REAL,
        thesis TEXT NOT NULL,
        trigger_condition TEXT NOT NULL,
        risk_notes TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'WATCHLIST',
        confirmation_status TEXT NOT NULL DEFAULT 'PENDING',
        confirmation_date TEXT,
        confirmation_reason TEXT,
        data_date TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id),
        UNIQUE(as_of_date, market, ticker, strategy_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        through_date TEXT NOT NULL,
        observed_days INTEGER NOT NULL,
        reference_date TEXT NOT NULL,
        reference_close REAL NOT NULL,
        execution_date TEXT,
        execution_price REAL,
        execution_type TEXT NOT NULL DEFAULT 'NEXT_OPEN',
        execution_note TEXT NOT NULL DEFAULT '',
        end_date TEXT NOT NULL,
        end_close REAL NOT NULL,
        return_pct REAL NOT NULL,
        gross_return_pct REAL NOT NULL DEFAULT 0,
        cost_pct REAL NOT NULL DEFAULT 0,
        net_return_pct REAL NOT NULL DEFAULT 0,
        net_win INTEGER NOT NULL DEFAULT 0,
        benchmark_ticker TEXT,
        benchmark_return_pct REAL,
        excess_return_pct REAL,
        max_gain_pct REAL NOT NULL,
        max_drawdown_pct REAL NOT NULL,
        hit_stop INTEGER NOT NULL,
        hit_target_1 INTEGER NOT NULL,
        hit_target_2 INTEGER NOT NULL,
        exit_type TEXT NOT NULL DEFAULT 'HOLD',
        exit_date TEXT,
        exit_price REAL,
        exit_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(candidate_id) REFERENCES candidates(id),
        UNIQUE(candidate_id, through_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_horizon_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        horizon_days INTEGER NOT NULL,
        through_date TEXT NOT NULL,
        observed_days INTEGER NOT NULL,
        reference_date TEXT NOT NULL,
        reference_close REAL NOT NULL,
        execution_date TEXT,
        execution_price REAL,
        execution_type TEXT NOT NULL DEFAULT 'NEXT_OPEN',
        execution_note TEXT NOT NULL DEFAULT '',
        end_date TEXT NOT NULL,
        end_close REAL NOT NULL,
        return_pct REAL NOT NULL,
        gross_return_pct REAL NOT NULL DEFAULT 0,
        cost_pct REAL NOT NULL DEFAULT 0,
        net_return_pct REAL NOT NULL DEFAULT 0,
        net_win INTEGER NOT NULL DEFAULT 0,
        benchmark_ticker TEXT,
        benchmark_return_pct REAL,
        excess_return_pct REAL,
        max_gain_pct REAL NOT NULL,
        max_drawdown_pct REAL NOT NULL,
        hit_stop INTEGER NOT NULL,
        hit_target_1 INTEGER NOT NULL,
        hit_target_2 INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(candidate_id) REFERENCES candidates(id),
        UNIQUE(candidate_id, horizon_days, through_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_audits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        as_of_date TEXT NOT NULL,
        signal_count INTEGER NOT NULL,
        completed_5d INTEGER NOT NULL,
        completed_10d INTEGER NOT NULL,
        avg_return_5d REAL,
        avg_return_10d REAL,
        win_rate_5d REAL,
        win_rate_10d REAL,
        stop_rate_10d REAL,
        target_rate_10d REAL,
        sample_quality_score REAL NOT NULL,
        crowding_risk_score REAL NOT NULL,
        decay_risk_score REAL NOT NULL,
        edge_score REAL NOT NULL,
        health_status TEXT NOT NULL,
        notes TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id),
        UNIQUE(strategy_id, as_of_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_fetch_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_type TEXT NOT NULL,
        market TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_symbols INTEGER NOT NULL DEFAULT 0,
        price_bars INTEGER NOT NULL DEFAULT 0,
        intraday_bars INTEGER NOT NULL DEFAULT 0,
        corporate_events INTEGER NOT NULL DEFAULT 0,
        financial_metrics INTEGER NOT NULL DEFAULT 0,
        money_flows INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_fetch_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        market TEXT NOT NULL,
        ticker TEXT,
        source TEXT NOT NULL,
        error_message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES data_fetch_runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_coverage_daily (
        market TEXT NOT NULL,
        date TEXT NOT NULL,
        instrument_count INTEGER NOT NULL DEFAULT 0,
        price_bar_count INTEGER NOT NULL DEFAULT 0,
        adjusted_bar_count INTEGER NOT NULL DEFAULT 0,
        benchmark_count INTEGER NOT NULL DEFAULT 0,
        event_count INTEGER NOT NULL DEFAULT 0,
        financial_count INTEGER NOT NULL DEFAULT 0,
        intraday_symbol_count INTEGER NOT NULL DEFAULT 0,
        confidence_level TEXT NOT NULL DEFAULT 'RESEARCH_ONLY',
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY(market, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_source_health (
        source TEXT NOT NULL,
        market TEXT NOT NULL,
        last_success_at TEXT,
        last_failure_at TEXT,
        failure_count INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(source, market)
    )
    """,
]


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema_upgrades(conn)
    conn.commit()
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    ensure_schema_upgrades(conn)
    conn.commit()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    _validate_table_name(table)
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    _validate_table_name(table)
    _validate_identifier(column, "column name")
    if column in _table_columns(conn, table):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def estimate_financial_published_date(report_date: str) -> str:
    """Return the latest common disclosure deadline for a financial period."""
    try:
        value = date.fromisoformat(report_date)
    except ValueError:
        return report_date
    if value.month == 3 and value.day == 31:
        return date(value.year, 4, 30).isoformat()
    if value.month == 6 and value.day == 30:
        return date(value.year, 8, 31).isoformat()
    if value.month == 9 and value.day == 30:
        return date(value.year, 10, 31).isoformat()
    if value.month == 12 and value.day == 31:
        return date(value.year + 1, 4, 30).isoformat()
    return (value + timedelta(days=45)).isoformat()


def _trade_cost_case_sql(market_expr: str) -> str:
    lines = [f"CASE {market_expr}"]
    for market, cost in sorted(MARKET_TRADE_COST_PCT.items()):
        lines.append(f"    WHEN '{market}' THEN {cost}")
    lines.append(f"    ELSE {DEFAULT_TRADE_COST_PCT}")
    lines.append("END")
    return "\n".join(lines)


def _backfill_net_returns(conn: sqlite3.Connection) -> None:
    updates = [
        (
            "evaluations",
            "e",
            "(SELECT sig.market FROM signals sig WHERE sig.id = e.signal_id)",
        ),
        (
            "candidate_evaluations",
            "e",
            "(SELECT c.market FROM candidates c WHERE c.id = e.candidate_id)",
        ),
        (
            "candidate_horizon_evaluations",
            "e",
            "(SELECT c.market FROM candidates c WHERE c.id = e.candidate_id)",
        ),
    ]
    for table, alias, market_sub in updates:
        if not _table_exists(conn, table):
            continue
        cols = _table_columns(conn, table)
        if "gross_return_pct" not in cols or "return_pct" not in cols:
            continue
        cost_case = _trade_cost_case_sql(market_sub)
        conn.execute(
            f"""
            UPDATE {table} AS {alias}
            SET gross_return_pct = {alias}.return_pct,
                cost_pct = {cost_case},
                net_return_pct = {alias}.return_pct - {cost_case},
                net_win = CASE
                    WHEN {alias}.return_pct - {cost_case} > 0 THEN 1
                    ELSE 0
                END
            WHERE {alias}.gross_return_pct = 0 AND {alias}.return_pct != 0
            """
        )
    conn.commit()


def _backfill_adjusted_prices(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "price_bars"):
        return
    cols = _table_columns(conn, "price_bars")
    required = {"adj_open", "adj_close", "adj_high", "adj_low", "adj_factor", "adjustment_status"}
    if not required.issubset(cols):
        return
    conn.execute(
        """
        UPDATE price_bars
        SET adj_open = COALESCE(adj_open, open),
            adj_close = COALESCE(adj_close, close),
            adj_high = COALESCE(adj_high, high),
            adj_low = COALESCE(adj_low, low),
            adj_factor = COALESCE(adj_factor, 1.0),
            adjustment_status = CASE
                WHEN adjustment_status IS NULL OR adjustment_status = 'UNKNOWN'
                THEN 'RAW_FALLBACK'
                ELSE adjustment_status
            END
        WHERE adj_close IS NULL
           OR adj_open IS NULL
           OR adj_high IS NULL
           OR adj_low IS NULL
           OR adj_factor IS NULL
           OR adjustment_status IS NULL
           OR adjustment_status = 'UNKNOWN'
        """
    )
    conn.commit()


def ensure_schema_upgrades(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "financial_metrics") or not _table_exists(conn, "candidate_evaluations"):
        return

    for statement in SCHEMA_STATEMENTS:
        if "data_fetch_" in statement or "data_coverage_daily" in statement or "data_source_health" in statement:
            conn.execute(statement)

    if not _table_exists(conn, "intraday_bars"):
        conn.execute(
            """
            CREATE TABLE intraday_bars (
                market TEXT NOT NULL,
                ticker TEXT NOT NULL,
                datetime TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                open REAL NOT NULL,
                close REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL,
                PRIMARY KEY(market, ticker, datetime)
            )
            """
        )

    if _table_exists(conn, "price_bars"):
        for column, definition in (
            ("adj_open", "REAL"),
            ("adj_close", "REAL"),
            ("adj_high", "REAL"),
            ("adj_low", "REAL"),
            ("adj_factor", "REAL"),
            ("adjustment_status", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
            ("adjustment_error", "TEXT"),
        ):
            _add_column_if_missing(conn, "price_bars", column, definition)

    strategy_columns = _table_columns(conn, "strategies") if _table_exists(conn, "strategies") else set()
    if "target_horizon_days" not in strategy_columns:
        _add_column_if_missing(conn, "strategies", "target_horizon_days", "INTEGER NOT NULL DEFAULT 10")
    if "version" not in strategy_columns:
        _add_column_if_missing(conn, "strategies", "version", "TEXT NOT NULL DEFAULT 'v1'")

    financial_columns = _table_columns(conn, "financial_metrics")
    if "published_date" not in financial_columns:
        conn.execute("ALTER TABLE financial_metrics ADD COLUMN published_date TEXT")
        rows = conn.execute("SELECT id, report_date FROM financial_metrics").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE financial_metrics SET published_date = ? WHERE id = ?",
                (estimate_financial_published_date(str(row["report_date"])), row["id"]),
            )

    for column, definition in (
        ("exit_type", "TEXT NOT NULL DEFAULT 'HOLD'"),
        ("exit_date", "TEXT"),
        ("exit_price", "REAL"),
        ("exit_note", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column_if_missing(conn, "evaluations", column, definition)

    for column, definition in (
        ("gross_return_pct", "REAL NOT NULL DEFAULT 0"),
        ("cost_pct", "REAL NOT NULL DEFAULT 0"),
        ("net_return_pct", "REAL NOT NULL DEFAULT 0"),
        ("net_win", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column_if_missing(conn, "evaluations", column, definition)

    _add_column_if_missing(conn, "candidate_evaluations", "execution_date", "TEXT")
    _add_column_if_missing(conn, "candidate_evaluations", "execution_price", "REAL")
    _add_column_if_missing(
        conn, "candidate_evaluations", "execution_type", "TEXT NOT NULL DEFAULT 'NEXT_OPEN'"
    )
    _add_column_if_missing(
        conn, "candidate_evaluations", "execution_note", "TEXT NOT NULL DEFAULT ''"
    )
    for column, definition in (
        ("exit_type", "TEXT NOT NULL DEFAULT 'HOLD'"),
        ("exit_date", "TEXT"),
        ("exit_price", "REAL"),
        ("exit_note", "TEXT NOT NULL DEFAULT ''"),
    ):
        _add_column_if_missing(conn, "candidate_evaluations", column, definition)

    for column, definition in (
        ("gross_return_pct", "REAL NOT NULL DEFAULT 0"),
        ("cost_pct", "REAL NOT NULL DEFAULT 0"),
        ("net_return_pct", "REAL NOT NULL DEFAULT 0"),
        ("net_win", "INTEGER NOT NULL DEFAULT 0"),
        ("benchmark_ticker", "TEXT"),
        ("benchmark_return_pct", "REAL"),
        ("excess_return_pct", "REAL"),
    ):
        _add_column_if_missing(conn, "candidate_evaluations", column, definition)

    if _table_exists(conn, "candidate_horizon_evaluations"):
        for column, definition in (
            ("exit_type", "TEXT NOT NULL DEFAULT 'HOLD'"),
            ("exit_date", "TEXT"),
            ("exit_price", "REAL"),
            ("exit_note", "TEXT NOT NULL DEFAULT ''"),
        ):
            _add_column_if_missing(conn, "candidate_horizon_evaluations", column, definition)
        for column, definition in (
            ("gross_return_pct", "REAL NOT NULL DEFAULT 0"),
            ("cost_pct", "REAL NOT NULL DEFAULT 0"),
            ("net_return_pct", "REAL NOT NULL DEFAULT 0"),
            ("net_win", "INTEGER NOT NULL DEFAULT 0"),
            ("benchmark_ticker", "TEXT"),
            ("benchmark_return_pct", "REAL"),
            ("excess_return_pct", "REAL"),
        ):
            _add_column_if_missing(conn, "candidate_horizon_evaluations", column, definition)

    if _table_exists(conn, "candidates"):
        _add_column_if_missing(conn, "candidates", "reward_risk_ratio", "REAL")
        _add_column_if_missing(conn, "candidates", "expected_value_score", "REAL")
        _add_column_if_missing(conn, "candidates", "trailing_stop_pct", "REAL")
        _add_column_if_missing(conn, "candidates", "trailing_activation_pct", "REAL")
        _add_column_if_missing(conn, "candidates", "confirmation_status", "TEXT NOT NULL DEFAULT 'PENDING'")
        _add_column_if_missing(conn, "candidates", "confirmation_date", "TEXT")
        _add_column_if_missing(conn, "candidates", "confirmation_reason", "TEXT")
        _add_column_if_missing(conn, "candidates", "data_date", "TEXT")

    _backfill_adjusted_prices(conn)
    _backfill_net_returns(conn)


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def load_json(value: str) -> Any:
    return json.loads(value)


def upsert_many(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[dict[str, Any]],
    conflict_columns: tuple[str, ...],
) -> int:
    _validate_table_name(table)
    rows = list(rows)
    if not rows:
        return 0
    if table == "price_bars":
        for row in rows:
            close = float(row["close"]) if row.get("close") not in (None, 0) else 0.0
            row.setdefault("adj_open", row.get("open"))
            row.setdefault("adj_close", row.get("close"))
            row.setdefault("adj_high", row.get("high"))
            row.setdefault("adj_low", row.get("low"))
            row.setdefault(
                "adj_factor",
                float(row["adj_close"]) / close if close and row.get("adj_close") is not None else 1.0,
            )
            row.setdefault("adjustment_status", "RAW_FALLBACK")
            row.setdefault("adjustment_error", None)

    columns = list(rows[0].keys())
    for col in columns:
        _validate_identifier(col, "column name")
    for col in conflict_columns:
        _validate_identifier(col, "conflict column name")
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(columns)
    conflict_sql = ", ".join(conflict_columns)
    update_columns = [column for column in columns if column not in conflict_columns]
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in update_columns)

    sql = (
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_sql}) DO UPDATE SET {update_sql}"
    )
    values = [[row[column] for column in columns] for row in rows]
    conn.executemany(sql, values)
    conn.commit()
    return len(rows)
