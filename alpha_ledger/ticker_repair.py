"""Ticker normalization repair and audit for historical .SH/.SS confusion.

Provides dry-run audit and safe repair for ticker-bearing tables where
historical .SH suffixes may have been stored instead of the canonical .SS
form for CN_A Shanghai tickers.

Repair rules (apply path):
  - instruments: merge .SH into .SS. Preserve canonical .SS when both exist.
  - price_bars: merge .SH into .SS by (market, normalized_ticker, date).
    ADJUSTED beats RAW_FALLBACK/UNKNOWN; otherwise prefer existing .SS.

Other ticker-bearing tables (intraday_bars, signals, candidates, etc.) are
audited and reported but NOT repaired automatically. This avoids the P0 safety
issue where generic .SH→.SS repair can cause IntegrityError by updating rows
without the old ticker in the WHERE clause.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .tickers import normalize_cn_a_ticker


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TableAudit:
    """Per-table audit detail."""
    table: str
    total_tickers: int = 0
    canonical_count: int = 0
    needs_normalization: int = 0
    unknown_suffix: int = 0
    conflicts: int = 0
    conflict_examples: list[dict[str, str]] = field(default_factory=list)


@dataclass
class TickerRepairResult:
    """Full audit and repair result."""
    tables: list[TableAudit] = field(default_factory=list)
    instruments_merged: int = 0
    price_bars_merged: int = 0
    other_tables_merged: int = 0
    total_merged: int = 0
    dry_run: bool = True

    @property
    def total_canonical(self) -> int:
        return sum(t.canonical_count for t in self.tables)

    @property
    def total_needs_normalization(self) -> int:
        return sum(t.needs_normalization for t in self.tables)

    @property
    def total_unknown_suffix(self) -> int:
        return sum(t.unknown_suffix for t in self.tables)

    @property
    def total_conflicts(self) -> int:
        return sum(t.conflicts for t in self.tables)

    @property
    def all_conflict_examples(self) -> list[dict[str, str]]:
        examples = []
        for t in self.tables:
            examples.extend(t.conflict_examples)
        return examples


# ---------------------------------------------------------------------------
# Ticker-bearing tables (ordered by importance for repair)
# ---------------------------------------------------------------------------

_TICKER_TABLES = [
    "instruments",
    "price_bars",
    "intraday_bars",
    "signals",
    "candidates",
    "model_scores",
    "research_events",
    "corporate_events",
    "financial_metrics",
    "money_flows",
]

# Tables that repair_tickers() will mutate in the default apply path.
# Only instruments and price_bars are safe to auto-repair because they have
# well-understood row keys and no complex unique constraints that could cause
# IntegrityError when two CN_A tickers resolve to the same canonical ticker.
# Other tables are audited/reported but NOT repaired automatically.
_APPLY_REPAIR_TABLES = frozenset({"instruments", "price_bars"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cn_a_needs_normalization(ticker: str, market: str) -> bool:
    """Return True if this ticker is a CN_A .SH that should be .SS."""
    if market != "CN_A":
        return False
    canonical = normalize_cn_a_ticker(ticker)
    return canonical != ticker


def _cn_a_canonical(ticker: str, market: str) -> str:
    """Return canonical ticker for CN_A, unchanged for others."""
    if market != "CN_A":
        return ticker
    return normalize_cn_a_ticker(ticker)


def _adjustment_rank(status: str | None) -> int:
    """Higher is better. ADJUSTED=2, RAW_FALLBACK=1, UNKNOWN=0."""
    if status == "ADJUSTED":
        return 2
    if status == "RAW_FALLBACK":
        return 1
    return 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Get column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


# ---------------------------------------------------------------------------
# Audit functions
# ---------------------------------------------------------------------------

def _audit_table(conn: sqlite3.Connection, table: str) -> TableAudit:
    """Audit a single table for normalization issues.

    Conflict detection is row-key level:
      - instruments: ticker-level (both .SH and .SS exist as rows).
      - price_bars: row-key level (same market, canonical ticker, same date).
      - intraday_bars: row-key level (same market, canonical ticker, same datetime).
      - other tables: only count needs_normalization; no false conflict inflation.
    """
    if not _table_exists(conn, table):
        return TableAudit(table=table)

    columns = _get_table_columns(conn, table)
    if "market" not in columns or "ticker" not in columns:
        return TableAudit(table=table)

    rows = conn.execute(
        f"SELECT DISTINCT market, ticker FROM {table} ORDER BY market, ticker"
    ).fetchall()

    audit = TableAudit(table=table, total_tickers=len(rows))
    seen_conflicts: set[str] = set()

    for row in rows:
        market = str(row["market"])
        ticker = str(row["ticker"])

        if market != "CN_A":
            audit.canonical_count += 1
            continue

        # Check for known suffixes
        upper = ticker.upper()
        has_known = any(upper.endswith(s) for s in (".SS", ".SZ", ".BJ", ".SH"))
        if not has_known:
            audit.unknown_suffix += 1
            continue

        canonical = _cn_a_canonical(ticker, market)
        if canonical == ticker:
            audit.canonical_count += 1
        else:
            audit.needs_normalization += 1

    # Conflict detection — row-key level per table type
    if table == "instruments":
        # instruments: ticker-level conflict (both .SH and .SS exist)
        sh_rows = conn.execute(
            "SELECT DISTINCT ticker FROM instruments "
            "WHERE market = 'CN_A' AND ticker LIKE '%.SH'"
        ).fetchall()
        for sh_row in sh_rows:
            sh_ticker = str(sh_row["ticker"])
            canonical = _cn_a_canonical(sh_ticker, "CN_A")
            existing = conn.execute(
                "SELECT 1 FROM instruments WHERE market = 'CN_A' AND ticker = ?",
                (canonical,),
            ).fetchone()
            if existing:
                key = f"instruments:{canonical}"
                if key not in seen_conflicts:
                    audit.conflicts += 1
                    seen_conflicts.add(key)
                    if len(audit.conflict_examples) < 5:
                        audit.conflict_examples.append({
                            "table": table,
                            "sh_ticker": sh_ticker,
                            "canonical_ticker": canonical,
                        })
    elif table == "price_bars":
        # price_bars: row-key level — both .SH and .SS with same date
        sh_rows = conn.execute(
            "SELECT DISTINCT ticker, date FROM price_bars "
            "WHERE market = 'CN_A' AND ticker LIKE '%.SH'"
        ).fetchall()
        for sh_row in sh_rows:
            sh_ticker = str(sh_row["ticker"])
            bar_date = str(sh_row["date"])
            canonical = _cn_a_canonical(sh_ticker, "CN_A")
            existing = conn.execute(
                "SELECT 1 FROM price_bars "
                "WHERE market = 'CN_A' AND ticker = ? AND date = ?",
                (canonical, bar_date),
            ).fetchone()
            if existing:
                key = f"price_bars:{canonical}:{bar_date}"
                if key not in seen_conflicts:
                    audit.conflicts += 1
                    seen_conflicts.add(key)
                    if len(audit.conflict_examples) < 5:
                        audit.conflict_examples.append({
                            "table": table,
                            "sh_ticker": sh_ticker,
                            "canonical_ticker": canonical,
                        })
    elif table == "intraday_bars":
        # intraday_bars: row-key level — both .SH and .SS with same datetime
        sh_rows = conn.execute(
            "SELECT DISTINCT ticker, datetime FROM intraday_bars "
            "WHERE market = 'CN_A' AND ticker LIKE '%.SH'"
        ).fetchall()
        for sh_row in sh_rows:
            sh_ticker = str(sh_row["ticker"])
            bar_datetime = str(sh_row["datetime"])
            canonical = _cn_a_canonical(sh_ticker, "CN_A")
            existing = conn.execute(
                "SELECT 1 FROM intraday_bars "
                "WHERE market = 'CN_A' AND ticker = ? AND datetime = ?",
                (canonical, bar_datetime),
            ).fetchone()
            if existing:
                key = f"intraday_bars:{canonical}:{bar_datetime}"
                if key not in seen_conflicts:
                    audit.conflicts += 1
                    seen_conflicts.add(key)
                    if len(audit.conflict_examples) < 5:
                        audit.conflict_examples.append({
                            "table": table,
                            "sh_ticker": sh_ticker,
                            "canonical_ticker": canonical,
                        })

    return audit


def audit_ticker_repair(conn: sqlite3.Connection) -> TickerRepairResult:
    """Dry-run audit: detect .SH rows, conflicts, and unknown suffixes.

    No database changes are made. Returns a TickerRepairResult with per-table
    breakdown and examples of conflicts that would need careful merge.
    """
    result = TickerRepairResult(dry_run=True)
    for table in _TICKER_TABLES:
        audit = _audit_table(conn, table)
        if audit.total_tickers > 0:
            result.tables.append(audit)
    return result


# ---------------------------------------------------------------------------
# Repair: instruments
# ---------------------------------------------------------------------------

def _repair_instruments(conn: sqlite3.Connection) -> int:
    """Merge .SH instrument rows into .SS. Returns count of merged rows."""
    if not _table_exists(conn, "instruments"):
        return 0

    sh_rows = conn.execute(
        "SELECT market, ticker, name, source, source_symbol, active, tags_json, created_at "
        "FROM instruments WHERE market = 'CN_A' AND ticker LIKE '%.SH'"
    ).fetchall()

    merged = 0
    for row in sh_rows:
        sh_ticker = str(row["ticker"])
        canonical = _cn_a_canonical(sh_ticker, "CN_A")
        if canonical == sh_ticker:
            continue  # already canonical

        # Check if canonical row already exists
        existing = conn.execute(
            "SELECT source_symbol, name, source, active, tags_json "
            "FROM instruments WHERE market = 'CN_A' AND ticker = ?",
            (canonical,),
        ).fetchone()

        if existing:
            # Canonical .SS already exists: delete the .SH row.
            # The canonical row is authoritative; source_symbol is preserved.
            conn.execute(
                "DELETE FROM instruments WHERE market = 'CN_A' AND ticker = ?",
                (sh_ticker,),
            )
        else:
            # Only .SH exists: rename to canonical .SS
            conn.execute(
                "UPDATE instruments SET ticker = ? "
                "WHERE market = 'CN_A' AND ticker = ?",
                (canonical, sh_ticker),
            )
        merged += 1

    return merged


# ---------------------------------------------------------------------------
# Repair: price_bars
# ---------------------------------------------------------------------------

def _repair_price_bars(conn: sqlite3.Connection) -> int:
    """Merge .SH price_bars into .SS by (market, ticker, date).

    Conflict resolution: ADJUSTED beats RAW_FALLBACK/UNKNOWN; otherwise
    prefer existing canonical .SS over .SH. Returns count of merged rows.
    """
    if not _table_exists(conn, "price_bars"):
        return 0

    # Get columns to build dynamic INSERT/UPDATE
    columns = _get_table_columns(conn, "price_bars")

    sh_rows = conn.execute(
        "SELECT * FROM price_bars WHERE market = 'CN_A' AND ticker LIKE '%.SH' "
        "ORDER BY ticker, date"
    ).fetchall()

    merged = 0
    for row in sh_rows:
        row_dict = {col: row[col] for col in columns}
        sh_ticker = str(row_dict["ticker"])
        bar_date = str(row_dict["date"])
        canonical = _cn_a_canonical(sh_ticker, "CN_A")
        if canonical == sh_ticker:
            continue

        # Check if canonical row already exists for this date
        existing = conn.execute(
            "SELECT * FROM price_bars WHERE market = 'CN_A' AND ticker = ? AND date = ?",
            (canonical, bar_date),
        ).fetchone()

        if existing:
            existing_dict = {col: existing[col] for col in columns}
            # Deterministic priority: ADJUSTED > RAW_FALLBACK > UNKNOWN
            sh_rank = _adjustment_rank(row_dict.get("adjustment_status"))
            ex_rank = _adjustment_rank(existing_dict.get("adjustment_status"))
            if sh_rank > ex_rank:
                # .SH row has better data: update the canonical row
                update_cols = [c for c in columns if c not in ("market", "ticker", "date")]
                set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                values = [row_dict[c] for c in update_cols] + [canonical, bar_date]
                conn.execute(
                    f"UPDATE price_bars SET {set_clause} "
                    f"WHERE market = 'CN_A' AND ticker = ? AND date = ?",
                    values,
                )
            # If ranks tie or existing is better, keep existing .SS row.
            # Delete the .SH row either way.
            conn.execute(
                "DELETE FROM price_bars WHERE market = 'CN_A' AND ticker = ? AND date = ?",
                (sh_ticker, bar_date),
            )
        else:
            # No conflict: rename .SH -> .SS
            conn.execute(
                "UPDATE price_bars SET ticker = ? "
                "WHERE market = 'CN_A' AND ticker = ? AND date = ?",
                (canonical, sh_ticker, bar_date),
            )
        merged += 1

    return merged


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def repair_tickers(
    conn: sqlite3.Connection,
    *,
    tables: list[str] | None = None,
) -> TickerRepairResult:
    """Run ticker normalization repair in a single transaction.

    Idempotent: running twice produces the same result. All changes are
    wrapped in a transaction that rolls back on error.

    By default, only repairs ``instruments`` and ``price_bars``. Other
    ticker-bearing tables (intraday_bars, signals, candidates, etc.) are
    audited and reported but NOT mutated. This avoids the P0 safety issue
    where generic .SH→.SS repair can cause IntegrityError by updating rows
    without the old ticker in the WHERE clause.

    Args:
        conn: Database connection (will be committed on success).
        tables: Optional list of tables to repair. If None, repairs only
                instruments and price_bars.

    Returns:
        TickerRepairResult with merge counts.
    """
    # Default: only the two safe tables. Other tables are audited only.
    target_tables = tables or list(_APPLY_REPAIR_TABLES)
    result = TickerRepairResult(dry_run=False)

    try:
        # Audit all ticker-bearing tables (for reporting)
        for table in _TICKER_TABLES:
            audit = _audit_table(conn, table)
            if audit.total_tickers > 0:
                result.tables.append(audit)

        # Repair instruments first (other tables may reference ticker)
        if "instruments" in target_tables:
            result.instruments_merged = _repair_instruments(conn)

        # Repair price_bars
        if "price_bars" in target_tables:
            result.price_bars_merged = _repair_price_bars(conn)

        # other_tables_merged stays 0 — only instruments and price_bars are
        # safe to auto-repair. Other tables are audited/reported but NOT
        # mutated. To repair additional tables safely, implement per-table
        # logic with proper row-key conflict handling and tests.

        result.total_merged = (
            result.instruments_merged
            + result.price_bars_merged
            + result.other_tables_merged
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return result


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def write_ticker_repair_report(
    result: TickerRepairResult,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write ticker repair report as md and json."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON report
    json_data = {
        "summary": {
            "dry_run": result.dry_run,
            "total_canonical": result.total_canonical,
            "total_needs_normalization": result.total_needs_normalization,
            "total_unknown_suffix": result.total_unknown_suffix,
            "total_conflicts": result.total_conflicts,
            "instruments_merged": result.instruments_merged,
            "price_bars_merged": result.price_bars_merged,
            "other_tables_merged": result.other_tables_merged,
            "total_merged": result.total_merged,
        },
        "tables": [
            {
                "table": t.table,
                "total_tickers": t.total_tickers,
                "canonical_count": t.canonical_count,
                "needs_normalization": t.needs_normalization,
                "unknown_suffix": t.unknown_suffix,
                "conflicts": t.conflicts,
                "conflict_examples": t.conflict_examples,
            }
            for t in result.tables
        ],
    }

    json_path = output_dir / "ticker_repair_report.json"
    json_path.write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Markdown report
    lines: list[str] = []
    lines.append("# Ticker Normalization Repair Report")
    lines.append("")
    lines.append(f"- Mode: {'dry-run' if result.dry_run else 'applied'}")
    lines.append(f"- Total canonical: {result.total_canonical}")
    lines.append(f"- Need normalization (.SH → .SS): {result.total_needs_normalization}")
    lines.append(f"- Unknown suffix: {result.total_unknown_suffix}")
    lines.append(f"- Conflicts (both .SH and .SS exist): {result.total_conflicts}")
    lines.append("")

    if not result.dry_run:
        lines.append("## Repair Summary")
        lines.append("")
        lines.append(f"- Instruments merged: {result.instruments_merged}")
        lines.append(f"- Price bars merged: {result.price_bars_merged}")
        lines.append(f"- Other tables merged: {result.other_tables_merged}")
        lines.append(f"- Total merged: {result.total_merged}")
        lines.append("")

    lines.append("## Per-Table Breakdown")
    lines.append("")
    lines.append("| Table | Tickers | Canonical | Need Norm | Unknown | Conflicts |")
    lines.append("|-------|---------|-----------|-----------|---------|-----------|")
    for t in result.tables:
        lines.append(
            f"| {t.table} | {t.total_tickers} | {t.canonical_count} "
            f"| {t.needs_normalization} | {t.unknown_suffix} | {t.conflicts} |"
        )
    lines.append("")

    conflicts = result.all_conflict_examples
    if conflicts:
        lines.append("## Conflict Examples")
        lines.append("")
        lines.append("| Table | .SH Ticker | Canonical |")
        lines.append("|-------|-----------|-----------|")
        for c in conflicts:
            lines.append(f"| {c['table']} | {c['sh_ticker']} | {c['canonical_ticker']} |")
        lines.append("")

    md_path = output_dir / "ticker_repair_report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return md_path, json_path
