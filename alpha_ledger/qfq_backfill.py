"""Forward-adjusted (qfq) price backfill for CN_A stocks.

Reads tickers from ``price_bars`` where ``adjustment_status != 'ADJUSTED'``,
fetches qfq-adjusted OHLC from BaoStock (strict) or AkShare, and UPDATEs
existing rows in place.  Raw OHLCV fields are never overwritten.

Source semantics:
    - ``source="baostock"`` (default): BaoStock only; no fallback on error.
    - ``source="auto"``: BaoStock first, AkShare fallback.
    - ``source="akshare"``: AkShare only.

Design:
    - One BaoStock ``query_history_k_data_plus`` call per ticker over the full
      requested date range (``adjustflag="2"``).
    - When BaoStock returns ``amount`` and ``turn`` alongside adjusted OHLC,
      these are also written to ``price_bars.amount`` and
      ``price_bars.turnover_pct`` with no-overwrite semantics (existing
      positive amount and non-null turnover_pct are preserved).
    - ``amount`` and ``turnover_pct`` are raw market metrics carried along
      with qfq OHLC — they should pass VWAP sanity checks before full trust.
    - Benchmarks/indexes excluded (BaoStock does not support them).
    - Resume-capable: only rows with ``adjustment_status != 'ADJUSTED'`` are
      touched; already-adjusted rows are preserved.
    - Commit batching for large runs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .benchmarks import CN_A_BENCHMARKS
from .market_data import (
    Instrument,
    MarketDataError,
    _baostock_logout,
    fetch_akshare_cn_adjusted_daily_map,
    fetch_baostock_cn_adjusted_daily_map,
)
from .tickers import normalize_cn_a_ticker


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class QfqBackfillResult:
    """Summary produced by :func:`qfq_backfill`."""

    start: str
    end: str
    source: str
    total_tickers: int = 0
    skipped_benchmarks: int = 0
    skipped_errors: int = 0
    updated_rows: int = 0
    ticker_errors: list[str] = field(default_factory=list)
    benchmark_tickers: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    # populated after run
    baostock_count: int = 0
    akshare_count: int = 0

    # number of tickers selected after benchmark filtering and --limit
    target_count: int = 0

    @property
    def ticker_count(self) -> int:
        """Number of tickers actually selected for processing (after benchmarks and limit)."""
        return self.target_count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BENCHMARK_SET: frozenset[str] = frozenset(b.ticker for b in CN_A_BENCHMARKS)


def _is_benchmark(ticker: str) -> bool:
    return normalize_cn_a_ticker(ticker) in _BENCHMARK_SET


def tickers_needing_adjustment(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    tickers_filter: set[str] | None = None,
) -> list[str]:
    """Return canonical CN_A tickers that have non-ADJUSTED price_bars.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    start, end : str, optional
        ISO date strings to restrict the date range.
    tickers_filter : set[str], optional
        If given, only consider these tickers (canonical or normalized).
    """
    conditions = ["market = 'CN_A'", "adjustment_status != 'ADJUSTED'"]
    params: list[object] = []

    if start:
        conditions.append("date >= ?")
        params.append(start)
    if end:
        conditions.append("date <= ?")
        params.append(end)

    if tickers_filter:
        normalized = [normalize_cn_a_ticker(t) for t in tickers_filter]
        placeholders = ", ".join("?" for _ in normalized)
        conditions.append(f"ticker IN ({placeholders})")
        params.extend(normalized)

    sql = f"""
        SELECT DISTINCT ticker FROM price_bars
        WHERE {' AND '.join(conditions)}
        ORDER BY ticker
    """
    rows = conn.execute(sql, params).fetchall()
    return [str(row["ticker"]) for row in rows]


def _build_adjustment_map(
    ticker: str,
    start_d: date,
    end_d: date,
    source: str,
) -> tuple[dict[str, dict[str, float]], str]:
    """Fetch qfq-adjusted prices for *ticker*.

    Returns ``(date_to_adj_map, actual_source)`` where *actual_source* is
    ``"baostock"`` or ``"akshare"``.

    When sourced from BaoStock, the map may also contain ``amount`` and
    ``turnover_pct`` keys (raw market metrics from the same API call).

    Source semantics:
    - ``"baostock"``: BaoStock only; raises on failure (no fallback).
    - ``"auto"``: tries BaoStock first, falls back to AkShare on error.
    - ``"akshare"``: AkShare only.
    """
    instrument = Instrument(
        market="CN_A",
        ticker=ticker,
        name="",
        source="sina_cn",
        source_symbol="",
        active=True,
        tags=(),
    )

    if source == "baostock":
        # Strict: BaoStock only, no fallback
        result = fetch_baostock_cn_adjusted_daily_map(instrument, start_d, end_d, adjust="qfq")
        return result, "baostock"

    if source == "auto":
        # Try BaoStock first, fallback to AkShare
        try:
            result = fetch_baostock_cn_adjusted_daily_map(instrument, start_d, end_d, adjust="qfq")
            return result, "baostock"
        except (MarketDataError, Exception):
            pass
        result = fetch_akshare_cn_adjusted_daily_map(instrument, start_d, end_d, adjust="qfq")
        return result, "akshare"

    # source == "akshare"
    result = fetch_akshare_cn_adjusted_daily_map(instrument, start_d, end_d, adjust="qfq")
    return result, "akshare"


def _update_adjusted_bars(
    conn: sqlite3.Connection,
    ticker: str,
    adjusted_map: dict[str, dict[str, float]],
) -> int:
    """Update RAW_FALLBACK price_bars with adjusted values.

    Only rows where ``adjustment_status != 'ADJUSTED'`` are touched.
    Returns the number of rows actually updated.

    When the adjusted_map includes ``amount`` or ``turnover_pct`` (from
    BaoStock QFQ path), these are also written to price_bars subject to
    no-overwrite semantics:
    - ``amount`` is set only if current value is NULL or <= 0.
    - ``turnover_pct`` is set only if current value is NULL.
    """
    updated = 0
    for row_date, values in adjusted_map.items():
        amount = values.get("amount")
        turnover = values.get("turnover_pct")
        cursor = conn.execute(
            """
            UPDATE price_bars
            SET adj_open = ?,
                adj_close = ?,
                adj_high = ?,
                adj_low = ?,
                adj_factor = CASE WHEN close != 0 THEN ? / close ELSE 1.0 END,
                adjustment_status = 'ADJUSTED',
                adjustment_error = NULL,
                amount = CASE
                    WHEN ? IS NOT NULL AND (amount IS NULL OR amount <= 0) THEN ?
                    ELSE amount
                END,
                turnover_pct = CASE
                    WHEN ? IS NOT NULL AND turnover_pct IS NULL THEN ?
                    ELSE turnover_pct
                END
            WHERE market = 'CN_A'
              AND ticker = ?
              AND date = ?
              AND adjustment_status != 'ADJUSTED'
            """,
            (
                values["adj_open"],
                values["adj_close"],
                values["adj_high"],
                values["adj_low"],
                values["adj_close"],
                amount, amount,
                turnover, turnover,
                ticker,
                row_date,
            ),
        )
        updated += cursor.rowcount
    return updated


# ---------------------------------------------------------------------------
# VWAP sanity check
# ---------------------------------------------------------------------------

def vwap_sanity_check(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    ticker: str | None = None,
    low_ratio: float = 0.2,
    high_ratio: float = 5.0,
) -> dict[str, object]:
    """Check VWAP sanity for rows with non-null amount.

    Computes ``vwap = amount / volume`` and compares to ``close``.
    Flags rows where ``vwap / close`` falls outside ``[low_ratio, high_ratio]``.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open DB connection.
    start, end : str, optional
        ISO date strings to restrict the date range.
    ticker : str, optional
        Restrict to a single ticker.
    low_ratio, high_ratio : float
        Acceptable ``vwap / close`` range.

    Returns
    -------
    dict
        Keys: ``total_checked``, ``suspicious_low``, ``suspicious_high``,
        ``suspicious_total``, ``low_ratio_threshold``, ``high_ratio_threshold``.
    """
    conditions = [
        "market = 'CN_A'",
        "amount IS NOT NULL",
        "amount > 0",
        "volume > 0",
        "close > 0",
    ]
    params: list[object] = []

    if start:
        conditions.append("date >= ?")
        params.append(start)
    if end:
        conditions.append("date <= ?")
        params.append(end)
    if ticker:
        conditions.append("ticker = ?")
        params.append(ticker)

    where = " AND ".join(conditions)

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN (amount / volume) / close < ? THEN 1 ELSE 0 END) as too_low,
            SUM(CASE WHEN (amount / volume) / close > ? THEN 1 ELSE 0 END) as too_high
        FROM price_bars
        WHERE {where}
        """,
        [low_ratio, high_ratio] + params,
    ).fetchone()

    total = row["total"] or 0
    too_low = row["too_low"] or 0
    too_high = row["too_high"] or 0

    return {
        "total_checked": total,
        "suspicious_low": too_low,
        "suspicious_high": too_high,
        "suspicious_total": too_low + too_high,
        "low_ratio_threshold": low_ratio,
        "high_ratio_threshold": high_ratio,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def qfq_backfill(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    source: str = "baostock",
    throttle: float = 0.3,
    limit: int | None = None,
    tickers_subset: set[str] | None = None,
    commit_every: int = 50,
    dry_run: bool = False,
    progress_fn: object | None = None,
) -> QfqBackfillResult:
    """Backfill forward-adjusted CN_A prices.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open DB connection (with ``row_factory = sqlite3.Row``).
    start, end : str
        ISO date strings ``YYYY-MM-DD``.
    source : str
        ``"baostock"`` (BaoStock only, no fallback), ``"auto"`` (BaoStock
        first with AkShare fallback), or ``"akshare"`` (AkShare only).
    throttle : float
        Seconds to sleep between per-ticker API calls.
    limit : int, optional
        Maximum number of tickers to process (for smoke runs).
    tickers_subset : set[str], optional
        Restrict to these tickers only.
    commit_every : int
        Commit to DB every N tickers.
    dry_run : bool
        If True, report what would be done without network or DB writes.
    progress_fn : callable, optional
        ``progress_fn(i, total, ticker, updated, errors)`` called after each ticker.

    Returns
    -------
    QfqBackfillResult
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    result = QfqBackfillResult(start=start, end=end, source=source, dry_run=dry_run)

    # Discover tickers
    tickers = tickers_needing_adjustment(conn, start, end, tickers_subset)
    result.total_tickers = len(tickers)

    # Filter benchmarks
    non_benchmark: list[str] = []
    for t in tickers:
        if _is_benchmark(t):
            result.skipped_benchmarks += 1
            result.benchmark_tickers.append(t)
        else:
            non_benchmark.append(t)

    if limit is not None:
        non_benchmark = non_benchmark[:limit]

    total = len(non_benchmark)
    result.target_count = total

    if dry_run:
        result.elapsed_seconds = 0.0
        if progress_fn:
            for i, ticker in enumerate(non_benchmark):
                progress_fn(i, total, ticker, 0, 0)
        return result

    # Live run — ensure BaoStock session is cleaned up
    t0 = time.monotonic()
    pending_commits = 0
    try:
        for i, ticker in enumerate(non_benchmark):
            try:
                adj_map, actual_source = _build_adjustment_map(ticker, start_d, end_d, source)
                if actual_source == "baostock":
                    result.baostock_count += 1
                else:
                    result.akshare_count += 1

                if adj_map:
                    updated = _update_adjusted_bars(conn, ticker, adj_map)
                    result.updated_rows += updated
                    pending_commits += 1

                    if pending_commits >= commit_every:
                        conn.commit()
                        pending_commits = 0
                else:
                    result.skipped_errors += 1
                    result.ticker_errors.append(f"{ticker}: empty response")

            except Exception as exc:
                result.skipped_errors += 1
                result.ticker_errors.append(f"{ticker}: {exc}")

            if progress_fn:
                progress_fn(i, total, ticker, result.updated_rows, result.skipped_errors)

            if throttle > 0 and i < total - 1:
                time.sleep(throttle)

        # Final commit
        if pending_commits > 0:
            conn.commit()
    finally:
        # Logout BaoStock if the source could have touched it
        if source in ("baostock", "auto"):
            _baostock_logout()

    result.elapsed_seconds = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_qfq_backfill_report(
    result: QfqBackfillResult,
    out_dir: Path | str = "reports",
) -> tuple[Path, Path]:
    """Write markdown + JSON reports.  Returns ``(md_path, json_path)``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = out / f"qfq_backfill_{ts}.md"
    json_path = out / f"qfq_backfill_{ts}.json"

    # --- Markdown ---
    lines: list[str] = [
        "# QFQ Backfill Report",
        "",
        f"- **Date range**: {result.start} to {result.end}",
        f"- **Source**: {result.source}",
        f"- **Dry run**: {result.dry_run}",
        f"- **Elapsed**: {result.elapsed_seconds:.1f}s",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total tickers found | {result.total_tickers} |",
        f"| Skipped (benchmarks) | {result.skipped_benchmarks} |",
        f"| Target (after filter/limit) | {result.target_count} |",
        f"| Skipped (errors) | {result.skipped_errors} |",
        f"| Updated rows | {result.updated_rows} |",
        f"| BaoStock hits | {result.baostock_count} |",
        f"| AkShare fallback | {result.akshare_count} |",
        "",
    ]

    if result.benchmark_tickers:
        lines.append("## Skipped Benchmarks")
        lines.append("")
        for t in result.benchmark_tickers:
            lines.append(f"- {t}")
        lines.append("")

    if result.ticker_errors:
        lines.append("## Errors")
        lines.append("")
        shown = result.ticker_errors[:50]
        for e in shown:
            lines.append(f"- {e}")
        if len(result.ticker_errors) > 50:
            lines.append(f"- ... and {len(result.ticker_errors) - 50} more")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    # --- JSON ---
    json_path.write_text(
        json.dumps(
            {
                "start": result.start,
                "end": result.end,
                "source": result.source,
                "dry_run": result.dry_run,
                "elapsed_seconds": round(result.elapsed_seconds, 2),
                "total_tickers": result.total_tickers,
                "skipped_benchmarks": result.skipped_benchmarks,
                "target_count": result.target_count,
                "skipped_errors": result.skipped_errors,
                "ticker_count": result.ticker_count,
                "updated_rows": result.updated_rows,
                "baostock_count": result.baostock_count,
                "akshare_count": result.akshare_count,
                "benchmark_tickers": result.benchmark_tickers,
                "errors": result.ticker_errors[:100],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return md_path, json_path
