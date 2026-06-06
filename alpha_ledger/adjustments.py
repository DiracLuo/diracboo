"""Factor-based CN_A forward-adjustment maintenance.

Raw OHLCV stays immutable.  This module detects ex-right/ex-dividend breaks
from same-day ``pre_close`` and updates ``adj_factor`` for only the affected
stock/date history.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .ledger import now_utc
from .market_data import Instrument, _baostock_logout, fetch_baostock_cn_adjusted_daily_map


CONFIRMED_BY_PRECLOSE = "CONFIRMED_BY_PRECLOSE"
SUSPECTED_BY_PRICE_GAP = "SUSPECTED_BY_PRICE_GAP"
IGNORED_DATA_QUALITY = "IGNORED_DATA_QUALITY"
CONFIRMED_REASONS = {CONFIRMED_BY_PRECLOSE}


@dataclass(frozen=True)
class PriceFrameResult:
    rows: list[dict[str, Any]]
    fallback_count: int = 0


@dataclass(frozen=True)
class AdjustmentBreakResult:
    as_of: str
    scanned: int = 0
    confirmed: int = 0
    suspected: int = 0
    ignored: int = 0
    queued: int = 0


@dataclass(frozen=True)
class QfqRepairResult:
    as_of: str
    target_count: int = 0
    repaired_count: int = 0
    failed_count: int = 0
    updated_rows: int = 0
    errors: list[str] = field(default_factory=list)
    missing_rows: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class QfqMaintenanceScanResult:
    as_of: str
    start_date: str
    end_date: str
    detected: AdjustmentBreakResult
    repaired: QfqRepairResult
    report_path: str = ""
    json_path: str = ""


def _valid_factor(value: object) -> float | None:
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return None
    if factor <= 0:
        return None
    return factor


def get_price_frame(
    conn: sqlite3.Connection,
    market: str,
    tickers: Iterable[str],
    start: str,
    end: str,
    *,
    price_mode: str = "raw",
) -> PriceFrameResult:
    """Return price rows in raw or qfq mode.

    ``price_mode='qfq'`` computes adjusted prices as raw OHLC multiplied by
    ``adj_factor``. Missing/invalid factors are flagged as ``RAW_FALLBACK``.
    """
    if price_mode not in {"raw", "qfq"}:
        raise ValueError("price_mode must be 'raw' or 'qfq'")
    ticker_list = list(dict.fromkeys(str(item) for item in tickers))
    if not ticker_list:
        return PriceFrameResult([])
    placeholders = ",".join("?" for _ in ticker_list)
    rows = conn.execute(
        f"""
        SELECT *
        FROM price_bars
        WHERE market = ?
          AND ticker IN ({placeholders})
          AND date >= ?
          AND date <= ?
        ORDER BY ticker, date
        """,
        (market, *ticker_list, start, end),
    ).fetchall()
    result_rows: list[dict[str, Any]] = []
    fallback_count = 0
    for row in rows:
        item = dict(row)
        item["raw_open"] = item["open"]
        item["raw_high"] = item["high"]
        item["raw_low"] = item["low"]
        item["raw_close"] = item["close"]
        item["data_quality"] = item.get("adjustment_status") or "UNKNOWN"
        if price_mode == "qfq":
            factor = _valid_factor(item.get("adj_factor"))
            if factor is None:
                factor = 1.0
                fallback_count += 1
                item["data_quality"] = "RAW_FALLBACK"
                item["adjustment_status"] = "RAW_FALLBACK"
                item["adj_factor_was_missing"] = True
            else:
                item["adj_factor_was_missing"] = False
            item["open"] = float(item["open"]) * factor
            item["high"] = float(item["high"]) * factor
            item["low"] = float(item["low"]) * factor
            item["close"] = float(item["close"]) * factor
            item["adj_factor"] = factor
            item["adj_open"] = item["open"]
            item["adj_high"] = item["high"]
            item["adj_low"] = item["low"]
            item["adj_close"] = item["close"]
        result_rows.append(item)
    return PriceFrameResult(result_rows, fallback_count=fallback_count)


def _upsert_queue(
    conn: sqlite3.Connection,
    *,
    market: str,
    ticker: str,
    detected_date: str,
    previous_trade_date: str,
    raw_prev_close: float | None,
    pre_close: float | None,
    close: float | None,
    change_pct: float | None,
    raw_change_pct: float | None,
    reason: str,
    status: str,
    error_message: str = "",
    preserve_done: bool = True,
) -> None:
    now = now_utc()
    conn.execute(
        """
        INSERT INTO adjustment_maintenance_queue (
            market, ticker, detected_date, previous_trade_date, raw_prev_close,
            pre_close, close, change_pct, raw_change_pct, reason, status,
            error_message, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market, ticker, detected_date) DO UPDATE SET
            previous_trade_date = excluded.previous_trade_date,
            raw_prev_close = excluded.raw_prev_close,
            pre_close = excluded.pre_close,
            close = excluded.close,
            change_pct = excluded.change_pct,
            raw_change_pct = excluded.raw_change_pct,
            reason = excluded.reason,
            status = CASE
                WHEN ? = 1 AND adjustment_maintenance_queue.status = 'DONE' THEN 'DONE'
                ELSE excluded.status
            END,
            error_message = excluded.error_message,
            updated_at = excluded.updated_at
        """,
        (
            market,
            ticker,
            detected_date,
            previous_trade_date,
            raw_prev_close,
            pre_close,
            close,
            change_pct,
            raw_change_pct,
            reason,
            status,
            error_message,
            now,
            now,
            1 if preserve_done else 0,
        ),
    )


def _has_baostock_break_repair(
    conn: sqlite3.Connection,
    *,
    market: str,
    ticker: str,
    ex_date: str,
    previous_date: str,
) -> bool:
    event = conn.execute(
        """
        SELECT 1
        FROM adjustment_events
        WHERE market = ?
          AND ticker = ?
          AND ex_date = ?
          AND source = 'baostock_qfq_break_repair'
          AND status = 'SUCCESS'
        LIMIT 1
        """,
        (market, ticker, ex_date),
    ).fetchone()
    if event is not None:
        return True
    row = conn.execute(
        """
        SELECT 1
        FROM price_bars
        WHERE market = ?
          AND ticker = ?
          AND date IN (?, ?)
          AND adjustment_source = 'baostock_qfq_break_repair'
        LIMIT 1
        """,
        (market, ticker, previous_date, ex_date),
    ).fetchone()
    return row is not None


def detect_adjustment_breaks(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    market: str = "CN_A",
    preclose_threshold_pct: float = 3.0,
    price_gap_threshold_pct: float = 5.0,
    change_tolerance_pct: float = 0.75,
    continuity_tolerance_pct: float = 0.5,
) -> AdjustmentBreakResult:
    """Detect adjustment breaks for a market date and enqueue repairs."""
    rows = conn.execute(
        """
        SELECT p.market, p.ticker, p.date, p.close, p.pre_close, p.change_pct,
               p.volume, p.adj_factor, prev.date AS previous_date,
               prev.close AS previous_close, prev.adj_factor AS previous_adj_factor
        FROM price_bars p
        LEFT JOIN price_bars prev
          ON prev.market = p.market
         AND prev.ticker = p.ticker
         AND prev.date = (
             SELECT MAX(date)
             FROM price_bars
             WHERE market = p.market AND ticker = p.ticker AND date < p.date
         )
        WHERE p.market = ? AND p.date = ?
        """,
        (market, as_of),
    ).fetchall()
    scanned = confirmed = suspected = ignored = queued = 0
    for row in rows:
        scanned += 1
        ticker = str(row["ticker"])
        previous_date = row["previous_date"]
        raw_prev_close = row["previous_close"]
        previous_adj_factor = row["previous_adj_factor"]
        close = row["close"]
        pre_close = row["pre_close"]
        change_pct = row["change_pct"]
        if previous_date is None or raw_prev_close in (None, 0) or close in (None, 0):
            ignored += 1
            _upsert_queue(
                conn,
                market=market,
                ticker=ticker,
                detected_date=as_of,
                previous_trade_date=str(previous_date or ""),
                raw_prev_close=None if raw_prev_close is None else float(raw_prev_close),
                pre_close=None if pre_close is None else float(pre_close),
                close=None if close is None else float(close),
                change_pct=None if change_pct is None else float(change_pct),
                raw_change_pct=None,
                reason=IGNORED_DATA_QUALITY,
                status="SKIPPED",
                error_message="missing previous date/close or current close",
            )
            continue
        raw_prev = float(raw_prev_close)
        close_f = float(close)
        raw_change_pct = (close_f / raw_prev - 1.0) * 100.0
        pre: float | None = None
        if pre_close not in (None, 0):
            pre = float(pre_close)
        if pre not in (None, 0):
            official_change = (close_f / pre - 1.0) * 100.0
            preclose_gap = abs(pre / raw_prev - 1.0) * 100.0
            if preclose_gap >= preclose_threshold_pct and (
                change_pct is None or abs(official_change - float(change_pct)) <= change_tolerance_pct
            ):
                confirmed += 1
                already_baostock_repaired = _has_baostock_break_repair(
                    conn,
                    market=market,
                    ticker=ticker,
                    ex_date=as_of,
                    previous_date=str(previous_date),
                )
                if not already_baostock_repaired:
                    queued += 1
                _upsert_queue(
                    conn,
                    market=market,
                    ticker=ticker,
                    detected_date=as_of,
                    previous_trade_date=str(previous_date),
                    raw_prev_close=raw_prev,
                    pre_close=pre,
                    close=close_f,
                    change_pct=official_change,
                    raw_change_pct=raw_change_pct,
                    reason=CONFIRMED_BY_PRECLOSE,
                    status="DONE" if already_baostock_repaired else "PENDING",
                    preserve_done=already_baostock_repaired,
                )
                continue
        if change_pct is not None and abs(raw_change_pct - float(change_pct)) >= price_gap_threshold_pct:
            suspected += 1
            queued += 1
            _upsert_queue(
                conn,
                market=market,
                ticker=ticker,
                detected_date=as_of,
                previous_trade_date=str(previous_date),
                raw_prev_close=raw_prev,
                pre_close=None if pre_close is None else float(pre_close),
                close=close_f,
                change_pct=float(change_pct),
                raw_change_pct=raw_change_pct,
                reason=SUSPECTED_BY_PRICE_GAP,
                status="PENDING",
            )
    conn.commit()
    return AdjustmentBreakResult(as_of, scanned, confirmed, suspected, ignored, queued)


def _mark_queue_failed(conn: sqlite3.Connection, queue_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE adjustment_maintenance_queue
        SET status = 'FAILED', error_message = ?, updated_at = ?
        WHERE id = ?
        """,
        (error[:1000], now_utc(), queue_id),
    )


def _insert_adjustment_event(
    conn: sqlite3.Connection,
    *,
    market: str,
    ticker: str,
    ex_date: str,
    previous_date: str,
    previous_raw_close: float,
    pre_close: float,
    factor_ratio: float,
    source: str,
    status: str,
    error_message: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO adjustment_events (
            market, ticker, ex_date, previous_date, previous_raw_close,
            pre_close, factor_ratio, source, confidence, status,
            error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'HIGH', ?, ?, ?)
        ON CONFLICT(market, ticker, ex_date, source) DO UPDATE SET
            previous_date = excluded.previous_date,
            previous_raw_close = excluded.previous_raw_close,
            pre_close = excluded.pre_close,
            factor_ratio = excluded.factor_ratio,
            confidence = excluded.confidence,
            status = excluded.status,
            error_message = excluded.error_message,
            created_at = excluded.created_at
        """,
        (
            market,
            ticker,
            ex_date,
            previous_date,
            previous_raw_close,
            pre_close,
            factor_ratio,
            source,
            status,
            error_message[:1000],
            now_utc(),
        ),
    )


def _fetch_baostock_qfq_map(
    ticker: str,
    start: str,
    end: str,
) -> dict[str, dict[str, float]]:
    instrument = Instrument(
        market="CN_A",
        ticker=ticker,
        name="",
        source="baostock",
        source_symbol="",
        active=True,
        tags=(),
    )
    return fetch_baostock_cn_adjusted_daily_map(
        instrument,
        date.fromisoformat(start),
        date.fromisoformat(end),
        adjust="qfq",
    )


def _update_break_adjusted_bars(
    conn: sqlite3.Connection,
    *,
    market: str,
    ticker: str,
    start: str,
    end: str,
    adjusted_map: dict[str, dict[str, float]],
    source: str,
) -> tuple[int, int]:
    existing_dates = {
        str(row["date"])
        for row in conn.execute(
            """
            SELECT date
            FROM price_bars
            WHERE market = ? AND ticker = ? AND date >= ? AND date <= ?
            """,
            (market, ticker, start, end),
        ).fetchall()
    }
    updated_rows = 0
    for row_date in sorted(existing_dates.intersection(adjusted_map)):
        values = adjusted_map[row_date]
        adj_close = float(values["adj_close"])
        cursor = conn.execute(
            """
            UPDATE price_bars
            SET adj_open = ?,
                adj_high = ?,
                adj_low = ?,
                adj_close = ?,
                adj_factor = CASE WHEN close IS NOT NULL AND close != 0 THEN ? / close ELSE adj_factor END,
                adjustment_status = 'ADJUSTED',
                adjustment_source = ?,
                adjusted_at = ?,
                adjustment_error = NULL
            WHERE market = ? AND ticker = ? AND date = ?
            """,
            (
                float(values["adj_open"]),
                float(values["adj_high"]),
                float(values["adj_low"]),
                adj_close,
                adj_close,
                source,
                now_utc(),
                market,
                ticker,
                row_date,
            ),
        )
        updated_rows += cursor.rowcount
    missing_rows = len(existing_dates.difference(adjusted_map))
    return updated_rows, missing_rows


def qfq_repair_breaks(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    start: str = "2024-01-01",
    market: str = "CN_A",
    source: str = "baostock",
    throttle: float = 0.3,
    dry_run: bool = False,
) -> QfqRepairResult:
    """Refresh qfq fields for same-day confirmed adjustment-break stocks only.

    This is the production daily path. Target discovery is intentionally narrow:
    it only reads ``CONFIRMED_BY_PRECLOSE`` rows from
    ``adjustment_maintenance_queue`` for ``detected_date=as_of``. It never
    expands targets from ``adjustment_status`` and never updates raw OHLCV,
    amount, ``pre_close`` or spot change fields.
    """
    if source != "baostock":
        raise ValueError("qfq-repair-breaks currently supports source='baostock' only")
    queue_rows = conn.execute(
        """
        SELECT *
        FROM adjustment_maintenance_queue
        WHERE market = ?
          AND detected_date = ?
          AND status = 'PENDING'
          AND reason = ?
        ORDER BY ticker
        """,
        (market, as_of, CONFIRMED_BY_PRECLOSE),
    ).fetchall()
    if dry_run:
        return QfqRepairResult(as_of, len(queue_rows), dry_run=True)

    errors: list[str] = []
    repaired = failed = updated_rows = missing_rows = 0
    try:
        for index, row in enumerate(queue_rows, start=1):
            qid = int(row["id"])
            ticker = str(row["ticker"])
            ex_date = str(row["detected_date"])
            previous_date = str(row["previous_trade_date"])
            raw_prev_close = row["raw_prev_close"]
            pre_close = row["pre_close"]
            factor_ratio = (
                float(pre_close) / float(raw_prev_close)
                if raw_prev_close not in (None, 0) and pre_close not in (None, 0)
                else 0.0
            )
            conn.execute("SAVEPOINT qfq_repair_break_one")
            try:
                if raw_prev_close in (None, 0) or pre_close in (None, 0):
                    raise ValueError("missing raw_prev_close/pre_close")
                adjusted_map = _fetch_baostock_qfq_map(ticker, start, as_of)
                if not adjusted_map:
                    raise ValueError("BaoStock returned no qfq rows")
                updated, missing = _update_break_adjusted_bars(
                    conn,
                    market=market,
                    ticker=ticker,
                    start=start,
                    end=as_of,
                    adjusted_map=adjusted_map,
                    source="baostock_qfq_break_repair",
                )
                if updated == 0:
                    raise ValueError("no existing price_bars rows matched BaoStock qfq data")
                conn.execute(
                    """
                    UPDATE adjustment_maintenance_queue
                    SET status = 'DONE', error_message = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (now_utc(), qid),
                )
                _insert_adjustment_event(
                    conn,
                    market=market,
                    ticker=ticker,
                    ex_date=ex_date,
                    previous_date=previous_date,
                    previous_raw_close=float(raw_prev_close),
                    pre_close=float(pre_close),
                    factor_ratio=factor_ratio,
                    source="baostock_qfq_break_repair",
                    status="SUCCESS",
                    error_message=f"missing_dates={missing}",
                )
                conn.execute("RELEASE qfq_repair_break_one")
                repaired += 1
                updated_rows += updated
                missing_rows += missing
            except Exception as exc:
                conn.execute("ROLLBACK TO qfq_repair_break_one")
                conn.execute("RELEASE qfq_repair_break_one")
                failed += 1
                message = f"{ticker} {exc}"
                errors.append(message)
                _mark_queue_failed(conn, qid, str(exc))
                _insert_adjustment_event(
                    conn,
                    market=market,
                    ticker=ticker,
                    ex_date=ex_date,
                    previous_date=previous_date,
                    previous_raw_close=float(raw_prev_close or 0),
                    pre_close=float(pre_close or 0),
                    factor_ratio=factor_ratio,
                    source="baostock_qfq_break_repair",
                    status="FAILED",
                    error_message=str(exc),
                )
            if throttle > 0 and index < len(queue_rows):
                time.sleep(throttle)
    finally:
        if queue_rows:
            _baostock_logout()
    conn.commit()
    return QfqRepairResult(
        as_of,
        target_count=len(queue_rows),
        repaired_count=repaired,
        failed_count=failed,
        updated_rows=updated_rows,
        errors=errors,
        missing_rows=missing_rows,
    )


def write_qfq_maintenance_scan_report(
    result: QfqMaintenanceScanResult,
    out_dir: Path,
) -> tuple[Path, Path]:
    out = out_dir / result.as_of
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "summary.md"
    json_path = out / "details.json"
    payload = {
        "as_of": result.as_of,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "detected": result.detected.__dict__,
        "repaired": result.repaired.__dict__,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        "\n".join(
            [
                f"# QFQ Maintenance Scan {result.as_of}",
                "",
                f"- window: {result.start_date}..{result.end_date}",
                f"- scanned: {result.detected.scanned}",
                f"- confirmed_by_preclose: {result.detected.confirmed}",
                f"- suspected_by_price_gap: {result.detected.suspected}",
                f"- ignored_data_quality: {result.detected.ignored}",
                f"- queued: {result.detected.queued}",
                f"- repair_targets: {result.repaired.target_count}",
                f"- repaired: {result.repaired.repaired_count}",
                f"- failed: {result.repaired.failed_count}",
                f"- updated_rows: {result.repaired.updated_rows}",
                "",
                "## Errors",
                "",
                *[f"- {error}" for error in result.repaired.errors],
                "",
            ]
        ),
        encoding="utf-8",
    )
    return md_path, json_path


def qfq_maintenance_scan_and_repair(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    start_date: str,
    end_date: str,
    market: str = "CN_A",
    out_dir: Path | None = None,
) -> QfqMaintenanceScanResult:
    dates = conn.execute(
        """
        SELECT DISTINCT date
        FROM price_bars
        WHERE market = ? AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (market, start_date, end_date),
    ).fetchall()
    total = AdjustmentBreakResult(as_of=end_date)
    total_repair = QfqRepairResult(as_of=end_date)
    for row in dates:
        day = str(row["date"])
        daily = detect_adjustment_breaks(conn, day, market=market)
        total = AdjustmentBreakResult(
            as_of=end_date,
            scanned=total.scanned + daily.scanned,
            confirmed=total.confirmed + daily.confirmed,
            suspected=total.suspected + daily.suspected,
            ignored=total.ignored + daily.ignored,
            queued=total.queued + daily.queued,
        )
        repaired = qfq_repair_breaks(conn, day, start="2024-01-01", market=market, source="baostock")
        total_repair = QfqRepairResult(
            as_of=end_date,
            target_count=total_repair.target_count + repaired.target_count,
            repaired_count=total_repair.repaired_count + repaired.repaired_count,
            failed_count=total_repair.failed_count + repaired.failed_count,
            updated_rows=total_repair.updated_rows + repaired.updated_rows,
            errors=[*total_repair.errors, *repaired.errors],
        )
    result = QfqMaintenanceScanResult(as_of, start_date, end_date, total, total_repair)
    if out_dir is not None:
        md, js = write_qfq_maintenance_scan_report(result, out_dir)
        result = QfqMaintenanceScanResult(as_of, start_date, end_date, total, total_repair, str(md), str(js))
    return result
