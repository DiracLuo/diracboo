from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .db import upsert_many
from .db import dump_json
from .ledger import now_utc
from .strategy_library import rows as strategy_rows


XINGYE_CSV = Path("data/reference/xingye_002674_20260401_20260525.csv")
DEPRECATED_STRATEGY_IDS = (
    "institutional_research_revaluation",
    "event_catalyst_reaction",
    "post_earnings_momentum",
    "crowded_short_reversal",
    "hk_value_repair",
    "hk_internet_trend_recovery",
)


def seed_strategies(conn: sqlite3.Connection) -> int:
    count = upsert_many(conn, "strategies", strategy_rows(), ("id",))
    purge_deprecated_strategies(conn)
    return count


def purge_deprecated_strategies(conn: sqlite3.Connection) -> None:
    placeholders = ", ".join("?" for _ in DEPRECATED_STRATEGY_IDS)
    candidate_ids = [
        int(row["id"])
        for row in conn.execute(
            f"SELECT id FROM candidates WHERE strategy_id IN ({placeholders})",
            DEPRECATED_STRATEGY_IDS,
        ).fetchall()
    ]
    if candidate_ids:
        candidate_placeholders = ", ".join("?" for _ in candidate_ids)
        conn.execute(
            f"DELETE FROM candidate_horizon_evaluations WHERE candidate_id IN ({candidate_placeholders})",
            candidate_ids,
        )
        conn.execute(
            f"DELETE FROM candidate_evaluations WHERE candidate_id IN ({candidate_placeholders})",
            candidate_ids,
        )
        conn.execute(f"DELETE FROM candidates WHERE id IN ({candidate_placeholders})", candidate_ids)
    signal_ids = [
        int(row["id"])
        for row in conn.execute(
            f"SELECT id FROM signals WHERE strategy_id IN ({placeholders})",
            DEPRECATED_STRATEGY_IDS,
        ).fetchall()
    ]
    if signal_ids:
        signal_placeholders = ", ".join("?" for _ in signal_ids)
        conn.execute(f"DELETE FROM tracking_events WHERE signal_id IN ({signal_placeholders})", signal_ids)
        conn.execute(f"DELETE FROM evaluations WHERE signal_id IN ({signal_placeholders})", signal_ids)
        conn.execute(f"DELETE FROM signals WHERE id IN ({signal_placeholders})", signal_ids)
    conn.execute(f"DELETE FROM strategy_audits WHERE strategy_id IN ({placeholders})", DEPRECATED_STRATEGY_IDS)
    conn.execute(f"DELETE FROM strategies WHERE id IN ({placeholders})", DEPRECATED_STRATEGY_IDS)
    conn.commit()


def seed_price_bars(conn: sqlite3.Connection, csv_path: Path = XINGYE_CSV) -> int:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(
                {
                    "market": row["market"],
                    "ticker": row["ticker"],
                    "date": row["date"],
                    "open": float(row["open"]),
                    "close": float(row["close"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "volume": float(row["volume"]),
                    "amount": float(row["amount"]) if row["amount"] else None,
                    "amplitude_pct": float(row["amplitude_pct"]) if row["amplitude_pct"] else None,
                    "change_pct": float(row["change_pct"]) if row["change_pct"] else None,
                    "turnover_pct": float(row["turnover_pct"]) if row["turnover_pct"] else None,
                }
            )
    return upsert_many(conn, "price_bars", rows, ("market", "ticker", "date"))


def seed_research_events(conn: sqlite3.Connection) -> int:
    rows = [
        {
            "market": "CN_A",
            "ticker": "002674.SZ",
            "name": "兴业科技",
            "event_date": "2026-04-28",
            "published_date": "2026-05-06",
            "event_type": "INVESTOR_CALL",
            "participant_count": 24,
            "quality_score": 0.82,
            "revaluation_tags_json": dump_json(
                ["新能源车内饰", "理想供应链", "蔚来供应链", "尊界S800", "海外产能"]
            ),
            "summary": "投资者活动记录披露汽车内饰皮革业务进入头部新能源车企供应链，并被公司称为核心增长引擎。",
            "source_url": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12298774&stockid=002674",
            "created_at": now_utc(),
        }
    ]
    return upsert_many(conn, "research_events", rows, ("market", "ticker", "event_date", "event_type"))


def seed_all(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "strategies": seed_strategies(conn),
        "price_bars": seed_price_bars(conn),
        "research_events": seed_research_events(conn),
    }
