#!/usr/bin/env python3
"""Backfill forward-adjusted (qfq) prices for CN_A stocks.

Reads tickers from price_bars where adjustment_status != 'ADJUSTED',
fetches qfq data from BaoStock (primary) or AkShare (fallback),
and UPDATEs existing records in place.

Usage:
    python scripts/backfill_qfq.py [--db data/alpha_ledger.sqlite] [--start 2025-12-01] [--end 2026-05-27] [--throttle 0.3]

Features:
    - Resume capability: skips tickers already fully ADJUSTED
    - Progress output every 50 tickers
    - BaoStock primary, AkShare fallback
    - Updates adj_open/adj_close/adj_high/adj_low/adj_factor/adjustment_status in place
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_ledger.market_data import (
    Instrument,
    _cn_a_to_baostock_symbol,
    _cn_a_plain_symbol,
    _within_range,
    MarketDataError,
)

BENCHMARK_TICKERS = {"000300.SS", "000905.SS", "000852.SS", "399006.SZ", "000688.SS", "899050.BJ"}


def _baostock_qfq(ticker: str, start: date, end: date) -> dict[str, dict[str, float]]:
    """Fetch forward-adjusted prices from BaoStock."""
    import baostock as bs

    bs_symbol = _cn_a_to_baostock_symbol(ticker)
    rs = bs.query_history_k_data_plus(
        bs_symbol,
        "date,open,high,low,close",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        frequency="d",
        adjustflag="2",
    )
    if rs.error_code != "0":
        raise MarketDataError(f"BaoStock error for {bs_symbol}: {rs.error_msg}")
    result: dict[str, dict[str, float]] = {}
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        row_date = row[0]
        if not _within_range(row_date, start, end):
            continue
        try:
            result[row_date] = {
                "adj_open": float(row[1]),
                "adj_high": float(row[2]),
                "adj_low": float(row[3]),
                "adj_close": float(row[4]),
            }
        except (ValueError, IndexError):
            continue
    return result


def _akshare_qfq(ticker: str, start: date, end: date) -> dict[str, dict[str, float]]:
    """Fetch forward-adjusted prices from AkShare (fallback)."""
    import akshare as ak

    symbol = _cn_a_plain_symbol(Instrument("CN_A", ticker, "", "sina_cn", ticker, True, ()))
    try:
        frame = ak.stock_zh_a_daily(symbol=f"{'sh' if ticker.endswith('.SS') else 'sz'}{symbol}", adjust="qfq")
    except Exception as exc:
        raise MarketDataError(f"AkShare error for {ticker}: {exc}") from exc
    if frame is None or getattr(frame, "empty", False):
        return {}
    result: dict[str, dict[str, float]] = {}
    for _, row in frame.iterrows():
        row_date = str(row.get("date", ""))[:10]
        if not _within_range(row_date, start, end):
            continue
        try:
            result[row_date] = {
                "adj_open": float(row["open"]),
                "adj_high": float(row["high"]),
                "adj_low": float(row["low"]),
                "adj_close": float(row["close"]),
            }
        except (KeyError, ValueError):
            continue
    return result


def _tickers_needing_adjustment(conn: sqlite3.Connection) -> list[str]:
    """Get tickers that have RAW_FALLBACK bars (excluding benchmarks)."""
    rows = conn.execute(
        """
        SELECT DISTINCT ticker
        FROM price_bars
        WHERE market = 'CN_A'
          AND adjustment_status != 'ADJUSTED'
          AND ticker NOT IN ({})
        ORDER BY ticker
        """.format(", ".join("?" for _ in BENCHMARK_TICKERS)),
        tuple(BENCHMARK_TICKERS),
    ).fetchall()
    return [str(row["ticker"]) for row in rows]


def _update_adjusted_bars(
    conn: sqlite3.Connection,
    ticker: str,
    adjusted_map: dict[str, dict[str, float]],
) -> int:
    """Update price_bars with adjusted values. Returns count of updated bars."""
    updated = 0
    for row_date, values in adjusted_map.items():
        cursor = conn.execute(
            """
            UPDATE price_bars
            SET adj_open = ?, adj_close = ?, adj_high = ?, adj_low = ?,
                adj_factor = CASE WHEN close != 0 THEN ? / close ELSE 1.0 END,
                adjustment_status = 'ADJUSTED',
                adjustment_error = NULL
            WHERE market = 'CN_A' AND ticker = ? AND date = ?
              AND adjustment_status != 'ADJUSTED'
            """,
            (
                values["adj_open"],
                values["adj_close"],
                values["adj_high"],
                values["adj_low"],
                values["adj_close"],
                ticker,
                row_date,
            ),
        )
        updated += cursor.rowcount
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill forward-adjusted prices for CN_A stocks")
    parser.add_argument("--db", default="data/alpha_ledger.sqlite", help="Database path")
    parser.add_argument("--start", default="2025-12-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-27", help="End date (YYYY-MM-DD)")
    parser.add_argument("--throttle", type=float, default=0.3, help="Seconds between API calls")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # Ensure BaoStock is logged in
    import baostock as bs
    login_result = bs.login()
    if login_result.error_code != "0":
        print(f"ERROR: BaoStock login failed: {login_result.error_msg}")
        sys.exit(1)
    print("BaoStock login success")

    tickers = _tickers_needing_adjustment(conn)
    total = len(tickers)
    print(f"Tickers needing adjustment: {total}")
    print(f"Date range: {start} to {end}")
    print(f"Throttle: {args.throttle}s")
    print()

    total_updated = 0
    total_errors = 0
    t0 = time.time()

    for i, ticker in enumerate(tickers):
        # Try BaoStock first
        adjusted_map: dict[str, dict[str, float]] = {}
        source = "baostock"
        try:
            adjusted_map = _baostock_qfq(ticker, start, end)
        except Exception:
            source = "akshare"
            try:
                adjusted_map = _akshare_qfq(ticker, start, end)
            except Exception as exc:
                total_errors += 1
                if (i + 1) % 50 == 0 or i == total - 1:
                    elapsed = time.time() - t0
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    eta = (total - i - 1) / rate if rate > 0 else 0
                    print(f"[{i+1}/{total}] {ticker}: FAILED ({exc}) | updated={total_updated} errors={total_errors} | {elapsed:.0f}s elapsed, {eta:.0f}s remaining")
                if args.throttle > 0:
                    time.sleep(args.throttle)
                continue

        # Update database
        if adjusted_map:
            updated = _update_adjusted_bars(conn, ticker, adjusted_map)
            total_updated += updated
            conn.commit()

        # Progress output
        if (i + 1) % 50 == 0 or i == total - 1:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"[{i+1}/{total}] {ticker}: {len(adjusted_map)} bars from {source} | updated={total_updated} errors={total_errors} | {elapsed:.0f}s elapsed, {eta:.0f}s remaining")

        if args.throttle > 0:
            time.sleep(args.throttle)

    bs.logout()
    conn.close()

    elapsed = time.time() - t0
    print()
    print(f"Done in {elapsed:.0f}s")
    print(f"Total bars updated: {total_updated}")
    print(f"Total errors: {total_errors}")


if __name__ == "__main__":
    main()
