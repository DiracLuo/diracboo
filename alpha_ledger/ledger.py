from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


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
