from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .db import upsert_many
from .db import dump_json
from .ledger import add_signal, add_tracking_event, immutable_hash, now_utc
from .strategy_library import rows as strategy_rows


XINGYE_CSV = Path("data/reference/xingye_002674_20260401_20260525.csv")


def seed_strategies(conn: sqlite3.Connection) -> int:
    count = upsert_many(conn, "strategies", strategy_rows(), ("id",))
    retire_merged_strategies(conn)
    retire_split_event_strategy(conn)
    return count


def retire_merged_strategies(conn: sqlite3.Connection) -> None:
    old_id = "institutional_research_revaluation"
    new_id = "xingye_style_prepositioning"
    conn.execute(
        """
        UPDATE signals AS s
        SET strategy_id = ?
        WHERE strategy_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM signals dup
              WHERE dup.signal_date = s.signal_date
                AND dup.ticker = s.ticker
                AND dup.market = s.market
                AND dup.strategy_id = ?
          )
        """,
        (new_id, old_id, new_id),
    )
    conn.execute(
        """
        UPDATE candidates AS c
        SET strategy_id = ?
        WHERE strategy_id = ?
          AND NOT EXISTS (
              SELECT 1
              FROM candidates dup
              WHERE dup.as_of_date = c.as_of_date
                AND dup.market = c.market
                AND dup.ticker = c.ticker
                AND dup.strategy_id = ?
          )
        """,
        (new_id, old_id, new_id),
    )
    conn.execute(
        """
        UPDATE strategies
        SET status = 'RETIRED',
            weight = 0,
            name = '已合并：机构调研后重估首阳'
        WHERE id = ?
        """,
        (old_id,),
    )
    for row in conn.execute("SELECT * FROM signals WHERE strategy_id = ?", (new_id,)).fetchall():
        conn.execute(
            "UPDATE signals SET immutable_hash = ? WHERE id = ?",
            (immutable_hash(dict(row)), int(row["id"])),
        )
    conn.commit()


def retire_split_event_strategy(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE strategies
        SET status = 'RETIRED',
            weight = 0,
            name = '已拆分：泛公告调研事件催化'
        WHERE id = 'event_catalyst_reaction'
        """
    )
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


def seed_xingye_signal(conn: sqlite3.Connection) -> int:
    signal_id = add_signal(
        conn,
        {
            "signal_date": "2026-05-13",
            "ticker": "002674.SZ",
            "name": "兴业科技",
            "market": "CN_A",
            "strategy_id": "xingye_style_prepositioning",
            "entry_type": "BUY_CANDIDATE",
            "entry_price": 13.73,
            "buy_zone_low": 13.45,
            "buy_zone_high": 13.85,
            "stop_loss": 13.12,
            "target_1": 14.70,
            "target_2": 15.97,
            "horizon_days": 15,
            "confidence": "B+",
            "thesis": (
                "4月28日投资者交流会、5月6日披露记录后，传统皮革公司出现新能源车内饰"
                "供应链重估线索；5月13日放量中阳，像重估首阳而非涨停追高。"
            ),
            "trigger_condition": (
                "调研后整理数日，5月13日涨4.09%，成交量67102手，约为此前5日均量1.87倍；"
                "买点应在启动日收盘附近或次日回踩不破启动日中枢时。"
            ),
            "risk_notes": (
                "公开资金流在5月13日未显示强主力净流入，不能把该案例简单归因于主力提前埋伏；"
                "若跌破13.12或后续催化无法兑现，应视为重估失败。"
            ),
            "evidence_json": [
                {
                    "type": "announcement",
                    "title": "兴业科技：2026年4月28日投资者活动记录",
                    "url": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12298774&stockid=002674",
                },
                {
                    "type": "price_action",
                    "title": "5月13日放量中阳，5月25日涨停",
                    "url": "https://finance.sina.com.cn/stock/aiassist/ydfx/2026-05-25/doc-inhzamvr5186304.shtml",
                },
                {
                    "type": "capital_event",
                    "title": "5月18日4亿元无抵押授信公告转载",
                    "url": "https://www.aastocks.com/sc/cnhk/news/china-hot-topic-content.aspx?catg=4&id=YLC6167810N&source=YOULIAN",
                },
            ],
        },
    )
    existing_events = conn.execute(
        "SELECT COUNT(*) AS count FROM tracking_events WHERE signal_id = ?", (signal_id,)
    ).fetchone()["count"]
    if existing_events == 0:
        add_tracking_event(
            conn,
            signal_id,
            "2026-05-13",
            "TRIGGERED",
            "放量中阳触发兴业科技型重估埋伏启动观察买点。",
            13.73,
        )
        add_tracking_event(
            conn,
            signal_id,
            "2026-05-20",
            "CONFIRMED",
            "收盘14.51，成交额显著放大，进入突破确认阶段。",
            14.51,
        )
        add_tracking_event(
            conn,
            signal_id,
            "2026-05-25",
            "TARGET_2_HIT",
            "触及15.97涨停价，样本进入情绪兑现区，需要复盘是否继续持有。",
            15.97,
        )
    return signal_id


def seed_all(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "strategies": seed_strategies(conn),
        "price_bars": seed_price_bars(conn),
        "research_events": seed_research_events(conn),
        "xingye_signal_id": seed_xingye_signal(conn),
    }
