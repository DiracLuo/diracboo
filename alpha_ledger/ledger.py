from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import dump_json


IMMUTABLE_SIGNAL_FIELDS = (
    "signal_date",
    "ticker",
    "name",
    "market",
    "strategy_id",
    "entry_type",
    "entry_price",
    "buy_zone_low",
    "buy_zone_high",
    "stop_loss",
    "target_1",
    "target_2",
    "horizon_days",
    "confidence",
    "thesis",
    "trigger_condition",
    "risk_notes",
    "evidence_json",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def immutable_hash(signal: dict[str, Any]) -> str:
    payload = {field: signal.get(field) for field in IMMUTABLE_SIGNAL_FIELDS}
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_signal(raw: dict[str, Any]) -> dict[str, Any]:
    signal = dict(raw)
    signal.setdefault("created_at", now_utc())
    signal.setdefault("entry_type", "BUY_CANDIDATE")
    signal.setdefault("status", "OPEN")
    if not isinstance(signal.get("evidence_json"), str):
        signal["evidence_json"] = dump_json(signal.get("evidence_json", []))
    signal["immutable_hash"] = immutable_hash(signal)
    return signal


def add_signal(conn: sqlite3.Connection, raw_signal: dict[str, Any]) -> int:
    signal = normalize_signal(raw_signal)
    columns = [
        "created_at",
        "signal_date",
        "ticker",
        "name",
        "market",
        "strategy_id",
        "entry_type",
        "entry_price",
        "buy_zone_low",
        "buy_zone_high",
        "stop_loss",
        "target_1",
        "target_2",
        "horizon_days",
        "confidence",
        "thesis",
        "trigger_condition",
        "risk_notes",
        "evidence_json",
        "status",
        "immutable_hash",
    ]
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"""
        INSERT INTO signals ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(signal_date, ticker, market, strategy_id) DO UPDATE SET
            status=excluded.status
    """
    conn.execute(sql, [signal[column] for column in columns])
    conn.commit()
    row = conn.execute(
        """
        SELECT id FROM signals
        WHERE signal_date = ? AND ticker = ? AND market = ? AND strategy_id = ?
        """,
        (signal["signal_date"], signal["ticker"], signal["market"], signal["strategy_id"]),
    ).fetchone()
    return int(row["id"])


def verify_signals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    broken: list[dict[str, Any]] = []
    rows = conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
    for row in rows:
        signal = dict(row)
        expected = immutable_hash(signal)
        if expected != signal["immutable_hash"]:
            broken.append(
                {
                    "id": signal["id"],
                    "ticker": signal["ticker"],
                    "stored_hash": signal["immutable_hash"],
                    "expected_hash": expected,
                }
            )
    return broken


def add_tracking_event(
    conn: sqlite3.Connection,
    signal_id: int,
    event_date: str,
    event_type: str,
    note: str,
    price: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO tracking_events
            (signal_id, event_date, event_type, price, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (signal_id, event_date, event_type, price, note, now_utc()),
    )
    conn.commit()

