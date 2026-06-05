"""Export Alpha Ledger price_bars to Qlib-compatible CSV format.

Each stock gets one CSV file named by Qlib convention (e.g. SH600519.csv).
Fields: date, open, close, high, low, volume, vwap, money, factor, change.

vwap and money are optional fields for Alpha158 compatibility:
  - money = amount (total traded value)
  - vwap = amount / volume (volume-weighted average price)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adjustments import get_price_frame
from .tickers import (
    CN_A_SUFFIX_TO_QLIB_PREFIX as TICKER_SUFFIX_MAP,
    normalize_cn_a_ticker,
    qlib_filename_to_ticker,
    ticker_to_qlib_filename,
)


def normalize_ticker_suffix(ticker: str) -> str:
    """Compatibility wrapper for older imports; use tickers.normalize_cn_a_ticker."""
    return normalize_cn_a_ticker(ticker)

QLIB_CSV_COLUMNS = ["date", "open", "close", "high", "low", "volume", "vwap", "money", "factor", "change"]

# Quality status codes
STATUS_OK = "ok"
STATUS_SUSPENDED = "possible_suspended"
STATUS_MISSING_PRICE = "missing_price"
STATUS_ZERO_VOLUME = "zero_volume_with_price"
STATUS_BAD_ADJUSTMENT = "bad_adjustment"
STATUS_UNKNOWN = "unknown"


def _classify_bar(row: dict[str, Any]) -> str:
    """Classify a price bar into a quality status."""
    adj_close = row.get("adj_close")
    adj_open = row.get("adj_open")
    adj_high = row.get("adj_high")
    adj_low = row.get("adj_low")
    adj_factor = row.get("adj_factor")
    volume = row.get("volume")
    adjustment_status = row.get("adjustment_status", "")

    # Check for missing adjusted prices
    has_adj = all(
        v is not None
        for v in (adj_close, adj_open, adj_high, adj_low)
    )
    if not has_adj:
        return STATUS_MISSING_PRICE

    # Check for bad adjustment
    if adjustment_status and adjustment_status != "ADJUSTED":
        return STATUS_BAD_ADJUSTMENT

    # Check for zero volume with valid prices
    vol = float(volume) if volume is not None else 0.0
    if vol <= 0:
        # Distinguish suspended (all prices same or zero) from just zero volume
        if adj_close == adj_open == adj_high == adj_low:
            return STATUS_SUSPENDED
        return STATUS_ZERO_VOLUME

    return STATUS_OK


@dataclass(frozen=True)
class QualityStats:
    ticker: str
    qlib_filename: str
    total_bars: int = 0
    status_ok: int = 0
    possible_suspended: int = 0
    missing_price: int = 0
    zero_volume_with_price: int = 0
    bad_adjustment: int = 0
    unknown: int = 0
    vwap_unavailable: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportResult:
    csv_count: int = 0
    total_bars: int = 0
    total_warnings: int = 0
    quality_stats: tuple[QualityStats, ...] = ()
    output_dir: str = ""


def export_qlib_csv(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    output_dir: Path,
    markets: set[str] | None = None,
) -> ExportResult:
    """Export price_bars to Qlib-compatible CSV files.

    Args:
        conn: Database connection.
        start: Start date (inclusive), YYYY-MM-DD.
        end: End date (inclusive), YYYY-MM-DD.
        output_dir: Directory to write CSV files.
        markets: Set of markets to export. None means all.

    Returns:
        ExportResult with statistics and quality info.
    """
    if markets is None:
        markets = {"CN_A"}

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query all tickers in scope
    market_placeholders = ",".join("?" for _ in markets)
    tickers = conn.execute(
        f"SELECT DISTINCT market, ticker FROM price_bars "
        f"WHERE market IN ({market_placeholders}) AND date >= ? AND date <= ? "
        f"ORDER BY market, ticker",
        (*sorted(markets), start, end),
    ).fetchall()

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tickers:
        market = str(row["market"])
        ticker = str(row["ticker"])
        try:
            qlib_filename = ticker_to_qlib_filename(ticker)
        except ValueError as exc:
            groups[(market, f"__invalid__:{ticker}")] = {
                "market": market,
                "canonical_ticker": ticker,
                "qlib_filename": "",
                "aliases": {ticker},
                "error": str(exc),
            }
            continue
        canonical_ticker = normalize_ticker_suffix(ticker)
        group = groups.setdefault(
            (market, qlib_filename),
            {
                "market": market,
                "canonical_ticker": canonical_ticker,
                "qlib_filename": qlib_filename,
                "aliases": set(),
                "error": "",
            },
        )
        group["aliases"].add(ticker)

    quality_stats_list: list[QualityStats] = []
    total_bars = 0
    total_warnings = 0

    for group in groups.values():
        market = str(group["market"])
        canonical_ticker = str(group["canonical_ticker"])
        qlib_filename = str(group["qlib_filename"])
        aliases = sorted(group["aliases"])
        if group.get("error"):
            quality_stats_list.append(
                QualityStats(
                    ticker=canonical_ticker,
                    qlib_filename="",
                    warnings=(str(group["error"]),),
                )
            )
            continue

        # Fetch bars for this ticker through the unified price reader.
        # Qlib receives qfq prices computed as raw OHLC * adj_factor.
        bars = get_price_frame(conn, market, aliases, start, end, price_mode="qfq").rows

        if not bars:
            continue
        canonical_bars: dict[str, dict[str, Any]] = {}
        for bar in bars:
            bar_date = str(bar["date"])
            current = canonical_bars.get(bar_date)
            if current is None or str(bar["ticker"]) == canonical_ticker:
                canonical_bars[bar_date] = bar
        bars = [canonical_bars[bar_date] for bar_date in sorted(canonical_bars)]

        csv_lines: list[str] = []
        csv_lines.append(",".join(QLIB_CSV_COLUMNS))

        counts = {
            STATUS_OK: 0,
            STATUS_SUSPENDED: 0,
            STATUS_MISSING_PRICE: 0,
            STATUS_ZERO_VOLUME: 0,
            STATUS_BAD_ADJUSTMENT: 0,
            STATUS_UNKNOWN: 0,
        }
        warnings: list[str] = []
        if len(aliases) > 1:
            warnings.append(f"merged ticker aliases into {canonical_ticker}: {', '.join(aliases)}")
        bar_count = 0
        vwap_unavailable_count = 0

        for bar in bars:
            bar_dict = dict(bar)
            bar_count += 1

            status = _classify_bar(bar_dict)
            counts[status] = counts.get(status, 0) + 1

            # Extract values
            adj_open = bar_dict.get("adj_open")
            adj_close = bar_dict.get("adj_close")
            adj_high = bar_dict.get("adj_high")
            adj_low = bar_dict.get("adj_low")
            adj_factor = bar_dict.get("adj_factor")
            volume = bar_dict.get("volume")
            amount = bar_dict.get("amount")
            change_pct = bar_dict.get("change_pct")

            # Handle missing adj_factor
            if bar_dict.get("adj_factor_was_missing") or adj_factor is None or (isinstance(adj_factor, (int, float)) and adj_factor <= 0):
                warnings.append(f"adj_factor missing/invalid on {bar_dict.get('date')}, using 1.0")
                adj_factor = 1.0

            # Handle missing change_pct
            if change_pct is None:
                change_val = 0.0
            else:
                change_val = float(change_pct) / 100.0

            # Compute vwap and money
            vol = float(volume) if volume is not None else 0.0
            amt = float(amount) if amount is not None else None
            money_val = amt if amt is not None else ""
            if amt is not None and vol > 0:
                vwap_val = amt / vol
            elif adj_high is not None and adj_low is not None and adj_close is not None:
                # Fallback: typical price approximation when amount is missing
                vwap_val = (float(adj_high) + float(adj_low) + float(adj_close)) / 3.0
            else:
                vwap_val = ""
                vwap_unavailable_count += 1

            # For suspended bars, emit NaN (Qlib convention)
            if status == STATUS_SUSPENDED:
                csv_lines.append(
                    f"{bar_dict['date']},,,,,,{vwap_val},{money_val},{adj_factor},{change_val}"
                )
            elif status == STATUS_MISSING_PRICE:
                # Emit what we can, leave missing as empty
                o = adj_open if adj_open is not None else ""
                c = adj_close if adj_close is not None else ""
                h = adj_high if adj_high is not None else ""
                l = adj_low if adj_low is not None else ""
                v = volume if volume is not None else ""
                csv_lines.append(
                    f"{bar_dict['date']},{o},{c},{h},{l},{v},{vwap_val},{money_val},{adj_factor},{change_val}"
                )
            else:
                # Normal or zero_volume_with_price or bad_adjustment — emit all values
                csv_lines.append(
                    f"{bar_dict['date']},{adj_open},{adj_close},{adj_high},{adj_low},"
                    f"{volume},{vwap_val},{money_val},{adj_factor},{change_val}"
                )

        if vwap_unavailable_count > 0:
            warnings.append(f"vwap unavailable on {vwap_unavailable_count} bars (missing amount, volume, and prices)")
        if amt is None and bar_count > 0:
            warnings.append("amount field is NULL for all bars; vwap uses (high+low+close)/3 approximation")

        # Write CSV
        csv_path = output_dir / qlib_filename
        csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")

        quality_stats_list.append(
            QualityStats(
                ticker=canonical_ticker,
                qlib_filename=qlib_filename,
                total_bars=bar_count,
                status_ok=counts[STATUS_OK],
                possible_suspended=counts[STATUS_SUSPENDED],
                missing_price=counts[STATUS_MISSING_PRICE],
                zero_volume_with_price=counts[STATUS_ZERO_VOLUME],
                bad_adjustment=counts[STATUS_BAD_ADJUSTMENT],
                unknown=counts[STATUS_UNKNOWN],
                vwap_unavailable=vwap_unavailable_count,
                warnings=tuple(warnings),
            )
        )

        total_bars += bar_count
        total_warnings += len(warnings)

    return ExportResult(
        csv_count=len(quality_stats_list),
        total_bars=total_bars,
        total_warnings=total_warnings,
        quality_stats=tuple(quality_stats_list),
        output_dir=str(output_dir),
    )


def write_quality_report(result: ExportResult, output_dir: Path) -> tuple[Path, Path]:
    """Write quality_report.md and quality_report.json."""
    output_dir = Path(output_dir)

    # JSON report
    json_data: dict[str, Any] = {
        "export_summary": {
            "csv_count": result.csv_count,
            "total_bars": result.total_bars,
            "total_warnings": result.total_warnings,
        },
        "tickers": [],
    }

    for qs in result.quality_stats:
        json_data["tickers"].append(
            {
                "ticker": qs.ticker,
                "qlib_filename": qs.qlib_filename,
                "total_bars": qs.total_bars,
                "status": {
                    "ok": qs.status_ok,
                    "possible_suspended": qs.possible_suspended,
                    "missing_price": qs.missing_price,
                    "zero_volume_with_price": qs.zero_volume_with_price,
                    "bad_adjustment": qs.bad_adjustment,
                    "unknown": qs.unknown,
                },
                "vwap_unavailable": qs.vwap_unavailable,
                "warnings": list(qs.warnings),
            }
        )

    json_path = output_dir / "quality_report.json"
    json_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )

    # Markdown report
    lines: list[str] = []
    lines.append("# Qlib Export Quality Report")
    lines.append("")
    lines.append(f"- CSV files exported: {result.csv_count}")
    lines.append(f"- Total bars: {result.total_bars}")
    lines.append(f"- Total warnings: {result.total_warnings}")
    lines.append("")

    # Summary table
    lines.append("## Ticker Summary")
    lines.append("")
    lines.append("| Ticker | Qlib File | Bars | OK | Suspended | Missing | ZeroVol | BadAdj | NoVWAP |")
    lines.append("|--------|-----------|-----:|---:|----------:|--------:|--------:|-------:|-------:|")
    for qs in result.quality_stats:
        lines.append(
            f"| {qs.ticker} | {qs.qlib_filename} | {qs.total_bars} "
            f"| {qs.status_ok} | {qs.possible_suspended} | {qs.missing_price} "
            f"| {qs.zero_volume_with_price} | {qs.bad_adjustment} | {qs.vwap_unavailable} |"
        )
    lines.append("")

    # Warnings section
    tickers_with_warnings = [qs for qs in result.quality_stats if qs.warnings]
    if tickers_with_warnings:
        lines.append("## Warnings")
        lines.append("")
        for qs in tickers_with_warnings:
            for w in qs.warnings:
                lines.append(f"- **{qs.ticker}**: {w}")
        lines.append("")
    else:
        lines.append("## Warnings")
        lines.append("")
        lines.append("No warnings.")
        lines.append("")

    md_path = output_dir / "quality_report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return md_path, json_path


@dataclass(frozen=True)
class TickerAuditResult:
    """Result of a dry-run ticker normalization audit."""
    total_instruments: int = 0
    canonical_count: int = 0  # Already .SS/.SZ/.BJ
    needs_normalization: int = 0  # .SH → .SS conversions needed
    unknown_suffix: int = 0  # Tickers with unrecognized suffix
    issues: tuple[dict[str, str], ...] = ()


def audit_ticker_normalization(conn: sqlite3.Connection) -> TickerAuditResult:
    """Dry-run audit: check stored tickers for normalization issues without modifying data.

    Reports which tickers need .SH → .SS conversion and which have unknown suffixes.
    No database changes are made.
    """
    rows: list[sqlite3.Row] = []
    for table in ("instruments", "price_bars", "candidates", "model_scores"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        rows.extend(
            conn.execute(
                f"SELECT DISTINCT '{table}' AS table_name, market, ticker FROM {table} ORDER BY market, ticker"
            ).fetchall()
        )

    canonical = 0
    needs_norm = 0
    unknown = 0
    issues: list[dict[str, str]] = []

    for row in rows:
        ticker = str(row["ticker"])
        market = str(row["market"])
        table_name = str(row["table_name"])

        if market != "CN_A":
            # Only audit CN_A tickers for .SH/.SS normalization
            canonical += 1
            continue

        # Check if ticker has a known suffix
        has_known_suffix = any(ticker.upper().endswith(s) for s in (".SS", ".SZ", ".BJ", ".SH"))

        if not has_known_suffix:
            unknown += 1
            issues.append({
                "ticker": ticker,
                "market": market,
                "table": table_name,
                "issue": "unknown_suffix",
                "detail": f"No recognized exchange suffix",
            })
            continue

        # Check if normalization would change anything
        normalized = normalize_ticker_suffix(ticker)
        if normalized == ticker:
            canonical += 1
        else:
            needs_norm += 1
            issues.append({
                "ticker": ticker,
                "market": market,
                "table": table_name,
                "issue": "needs_normalization",
                "detail": f"{ticker} → {normalized}",
            })

    return TickerAuditResult(
        total_instruments=len(rows),
        canonical_count=canonical,
        needs_normalization=needs_norm,
        unknown_suffix=unknown,
        issues=tuple(issues),
    )


def write_ticker_audit_report(result: TickerAuditResult, output_dir: Path) -> tuple[Path, Path]:
    """Write ticker audit report as md and json."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON report
    json_data = {
        "summary": {
            "total_instruments": result.total_instruments,
            "canonical_count": result.canonical_count,
            "needs_normalization": result.needs_normalization,
            "unknown_suffix": result.unknown_suffix,
        },
        "issues": list(result.issues),
    }

    json_path = output_dir / "ticker_audit_report.json"
    json_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Markdown report
    lines: list[str] = []
    lines.append("# Ticker Normalization Audit Report")
    lines.append("")
    lines.append(f"- Total instruments: {result.total_instruments}")
    lines.append(f"- Already canonical: {result.canonical_count}")
    lines.append(f"- Need normalization (.SH → .SS): {result.needs_normalization}")
    lines.append(f"- Unknown suffix: {result.unknown_suffix}")
    lines.append("")

    if result.issues:
        lines.append("## Issues")
        lines.append("")
        lines.append("| Ticker | Market | Issue | Detail |")
        lines.append("|--------|--------|-------|--------|")
        for issue in result.issues:
            lines.append(f"| {issue['ticker']} | {issue['market']} | {issue['issue']} | {issue['detail']} |")
        lines.append("")
    else:
        lines.append("## Issues")
        lines.append("")
        lines.append("No issues found. All tickers are in canonical form.")
        lines.append("")

    md_path = output_dir / "ticker_audit_report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return md_path, json_path
