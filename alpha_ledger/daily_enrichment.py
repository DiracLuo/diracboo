"""Daily amount/turnover enrichment for CN_A price_bars.

Fetches BaoStock daily fields (``amount``, ``turn``) for CN_A stocks over a
date range and UPDATEs existing ``price_bars`` rows in place.  Only
``amount`` and ``turnover_pct`` are mutated; OHLC, volume, and ``adj_*``
fields are never touched.

Source semantics:
    - BaoStock ``query_history_k_data_plus`` with ``adjustflag=3``
      (no adjustment) — we want raw traded value and turnover rate, not
      forward/backward-adjusted figures.  ``adjustflag=3`` returns
      unadjusted OHLC plus the raw ``amount`` and ``turn`` fields.

BaoStock fields requested:
    ``date, code, volume, amount, turn, pctChg, isST``

    - ``amount``: total traded value (CNY) → ``price_bars.amount``
    - ``turn``: turnover rate (%) → ``price_bars.turnover_pct``
    - Other fields are fetched for potential future use but not stored
      in this pass.

Resume-safe:
    Only rows matching the selection criteria are touched:
    ``amount IS NULL OR amount <= 0 OR turnover_pct IS NULL``

Design mirrors :mod:`alpha_ledger.qfq_backfill` for consistency.
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
    _ensure_baostock_login,
)
from .tickers import normalize_cn_a_ticker


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DailyEnrichmentResult:
    """Summary produced by :func:`enrich_daily_bars`."""

    start: str
    end: str
    total_tickers: int = 0
    skipped_benchmarks: int = 0
    skipped_errors: int = 0
    updated_rows: int = 0
    missing_rows: int = 0
    ticker_errors: list[str] = field(default_factory=list)
    benchmark_tickers: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    # number of tickers selected after benchmark filtering and --limit
    target_count: int = 0

    @property
    def ticker_count(self) -> int:
        """Number of tickers actually selected for processing."""
        return self.target_count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BENCHMARK_SET: frozenset[str] = frozenset(b.ticker for b in CN_A_BENCHMARKS)


def _is_benchmark(ticker: str) -> bool:
    return normalize_cn_a_ticker(ticker) in _BENCHMARK_SET


def tickers_needing_enrichment(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    tickers_filter: set[str] | None = None,
) -> list[str]:
    """Return canonical CN_A tickers that have price_bars needing enrichment.

    A row needs enrichment when: ``amount IS NULL OR amount <= 0 OR
    turnover_pct IS NULL``. Valuation fields may be filled opportunistically
    when a row is already being enriched, but they do not make a row a target
    by themselves.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    start, end : str, optional
        ISO date strings to restrict the date range.
    tickers_filter : set[str], optional
        If given, only consider these tickers (canonical or normalized).
    """
    conditions = [
        "market = 'CN_A'",
        "(amount IS NULL OR amount <= 0 OR turnover_pct IS NULL)",
    ]
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


def _fetch_baostock_daily_enrichment_map(
    ticker: str,
    start_d: date,
    end_d: date,
) -> dict[str, dict[str, float]]:
    """Fetch BaoStock daily amount/turnover for *ticker*.

    Uses ``adjustflag=3`` (no adjustment) to get raw traded value and
    turnover rate.

    Returns a dict mapping date strings to ``{"amount": ..., "turnover_pct": ...}``.

    Raises ``MarketDataError`` on BaoStock query failure.
    """
    from .tickers import cn_a_to_baostock_symbol

    bs_symbol = cn_a_to_baostock_symbol(ticker)
    _ensure_baostock_login()
    import baostock as bs  # type: ignore

    rs = bs.query_history_k_data_plus(
        bs_symbol,
        "date,code,volume,amount,turn,pctChg,isST,peTTM,psTTM",
        start_date=start_d.isoformat(),
        end_date=end_d.isoformat(),
        frequency="d",
        adjustflag="3",  # no adjustment — raw market metrics
    )
    if rs.error_code != "0":
        raise MarketDataError(
            f"BaoStock enrichment query error for {bs_symbol}: {rs.error_msg}"
        )

    rows: dict[str, dict[str, float]] = {}
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        # row layout: [date, code, volume, amount, turn, pctChg, isST]
        row_date = row[0]
        if not row_date:
            continue
        try:
            amount_str = row[3]
            turn_str = row[4]
            amount_val = float(amount_str) if amount_str not in (None, "") else None
            turn_val = float(turn_str) if turn_str not in (None, "") else None
        except (ValueError, IndexError):
            continue
        # peTTM (index 7), psTTM (index 8)
        pe_ttm_val = None
        ps_ttm_val = None
        try:
            pe_str = row[7] if len(row) > 7 else None
            if pe_str not in (None, ""):
                pe_ttm_val = float(pe_str)
        except (ValueError, IndexError):
            pass
        try:
            ps_str = row[8] if len(row) > 8 else None
            if ps_str not in (None, ""):
                ps_ttm_val = float(ps_str)
        except (ValueError, IndexError):
            pass
        # Only store rows where we have at least one useful value
        if amount_val is None and turn_val is None and pe_ttm_val is None and ps_ttm_val is None:
            continue
        rows[row_date] = {}
        if amount_val is not None:
            rows[row_date]["amount"] = amount_val
        if turn_val is not None:
            rows[row_date]["turnover_pct"] = turn_val
        if pe_ttm_val is not None:
            rows[row_date]["pe_ttm"] = pe_ttm_val
        if ps_ttm_val is not None:
            rows[row_date]["ps_ttm"] = ps_ttm_val
    return rows


def _update_enriched_bars(
    conn: sqlite3.Connection,
    ticker: str,
    enrichment_map: dict[str, dict[str, float]],
) -> tuple[int, int]:
    """Update price_bars with enrichment data.

    Only rows matching the resume-safe selection are touched:
    ``amount IS NULL OR amount <= 0 OR turnover_pct IS NULL``

    Returns ``(updated_rows, missing_rows)`` where missing_rows counts
    dates in the enrichment map that had no matching price_bars row.
    """
    updated = 0
    missing = 0
    for row_date, values in enrichment_map.items():
        amount = values.get("amount")
        turnover = values.get("turnover_pct")
        pe_ttm = values.get("pe_ttm")
        ps_ttm = values.get("ps_ttm")
        cursor = conn.execute(
            """
            UPDATE price_bars
            SET amount = CASE
                    WHEN ? IS NOT NULL AND (amount IS NULL OR amount <= 0) THEN ?
                    ELSE amount
                END,
                turnover_pct = CASE
                    WHEN ? IS NOT NULL AND turnover_pct IS NULL THEN ?
                    ELSE turnover_pct
                END,
                pe_ttm = CASE
                    WHEN ? IS NOT NULL AND pe_ttm IS NULL THEN ?
                    ELSE pe_ttm
                END,
                ps_ttm = CASE
                    WHEN ? IS NOT NULL AND ps_ttm IS NULL THEN ?
                    ELSE ps_ttm
                END
            WHERE market = 'CN_A'
              AND ticker = ?
              AND date = ?
              AND (
                    (? IS NOT NULL AND (amount IS NULL OR amount <= 0))
                 OR (? IS NOT NULL AND turnover_pct IS NULL)
                 OR (? IS NOT NULL AND pe_ttm IS NULL)
                 OR (? IS NOT NULL AND ps_ttm IS NULL)
              )
            """,
            (
                amount, amount,
                turnover, turnover,
                pe_ttm, pe_ttm,
                ps_ttm, ps_ttm,
                ticker, row_date,
                amount, turnover, pe_ttm, ps_ttm,
            ),
        )
        if cursor.rowcount == 0:
            # Check if the row exists but was already enriched
            existing = conn.execute(
                "SELECT 1 FROM price_bars WHERE market='CN_A' AND ticker=? AND date=?",
                (ticker, row_date),
            ).fetchone()
            if existing is None:
                missing += 1
        else:
            updated += cursor.rowcount
    return updated, missing


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def enrich_daily_bars(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    throttle: float = 0.3,
    limit: int | None = None,
    tickers_subset: set[str] | None = None,
    commit_every: int = 50,
    dry_run: bool = False,
    progress_fn: object | None = None,
) -> DailyEnrichmentResult:
    """Enrich CN_A price_bars with BaoStock amount and turnover_pct.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open DB connection (with ``row_factory = sqlite3.Row``).
    start, end : str
        ISO date strings ``YYYY-MM-DD``.
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
        ``progress_fn(i, total, ticker, updated, missing, errors)`` called
        after each ticker.

    Returns
    -------
    DailyEnrichmentResult
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    result = DailyEnrichmentResult(start=start, end=end, dry_run=dry_run)

    # Discover tickers
    tickers = tickers_needing_enrichment(conn, start, end, tickers_subset)
    result.total_tickers = len(tickers)

    # Filter benchmarks (BaoStock does not support them for amount/turn)
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
                progress_fn(i, total, ticker, 0, 0, 0)
        return result

    # Live run — ensure BaoStock session is cleaned up
    t0 = time.monotonic()
    pending_commits = 0
    try:
        for i, ticker in enumerate(non_benchmark):
            try:
                enrichment_map = _fetch_baostock_daily_enrichment_map(
                    ticker, start_d, end_d
                )
                if enrichment_map:
                    updated, missing = _update_enriched_bars(
                        conn, ticker, enrichment_map
                    )
                    result.updated_rows += updated
                    result.missing_rows += missing
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
                progress_fn(
                    i, total, ticker,
                    result.updated_rows, result.missing_rows,
                    result.skipped_errors,
                )

            if throttle > 0 and i < total - 1:
                time.sleep(throttle)

        # Final commit
        if pending_commits > 0:
            conn.commit()
    finally:
        _baostock_logout()

    result.elapsed_seconds = time.monotonic() - t0
    return result


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_enrichment_report(
    result: DailyEnrichmentResult,
    out_dir: Path | str = "reports",
) -> tuple[Path, Path]:
    """Write markdown + JSON reports.  Returns ``(md_path, json_path)``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = out / f"daily_enrichment_{ts}.md"
    json_path = out / f"daily_enrichment_{ts}.json"

    # --- Markdown ---
    lines: list[str] = [
        "# Daily Enrichment Report",
        "",
        f"- **Date range**: {result.start} to {result.end}",
        f"- **Dry run**: {result.dry_run}",
        f"- **Elapsed**: {result.elapsed_seconds:.1f}s",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total tickers found | {result.total_tickers} |",
        f"| Skipped (benchmarks) | {result.skipped_benchmarks} |",
        f"| Target (after filter/limit) | {result.target_count} |",
        f"| Skipped (errors) | {result.skipped_errors} |",
        f"| Updated rows | {result.updated_rows} |",
        f"| Missing rows (no price_bar) | {result.missing_rows} |",
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
                "dry_run": result.dry_run,
                "elapsed_seconds": round(result.elapsed_seconds, 2),
                "total_tickers": result.total_tickers,
                "skipped_benchmarks": result.skipped_benchmarks,
                "target_count": result.target_count,
                "skipped_errors": result.skipped_errors,
                "ticker_count": result.ticker_count,
                "updated_rows": result.updated_rows,
                "missing_rows": result.missing_rows,
                "benchmark_tickers": result.benchmark_tickers,
                "errors": result.ticker_errors[:100],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return md_path, json_path
