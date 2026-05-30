"""Tests for ticker normalization repair module."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from alpha_ledger.db import init_db
from alpha_ledger.ticker_repair import (
    audit_ticker_repair,
    repair_tickers,
    write_ticker_repair_report,
)


def _make_full_db() -> sqlite3.Connection:
    """Create in-memory DB with instruments and price_bars tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def _make_minimal_db() -> sqlite3.Connection:
    """Create in-memory DB with just instruments and price_bars (no schema upgrades)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE instruments (
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            source_symbol TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            PRIMARY KEY(market, ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE price_bars (
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            amount REAL,
            amplitude_pct REAL,
            change_pct REAL,
            turnover_pct REAL,
            adj_open REAL,
            adj_close REAL,
            adj_high REAL,
            adj_low REAL,
            adj_factor REAL,
            adjustment_status TEXT NOT NULL DEFAULT 'UNKNOWN',
            adjustment_error TEXT,
            PRIMARY KEY(market, ticker, date)
        )
    """)
    conn.commit()
    return conn


def _make_full_schema_db() -> sqlite3.Connection:
    """Create in-memory DB with instruments, price_bars, intraday_bars, signals, candidates."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE instruments (
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            source_symbol TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            PRIMARY KEY(market, ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE price_bars (
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            amount REAL,
            amplitude_pct REAL,
            change_pct REAL,
            turnover_pct REAL,
            adj_open REAL,
            adj_close REAL,
            adj_high REAL,
            adj_low REAL,
            adj_factor REAL,
            adjustment_status TEXT NOT NULL DEFAULT 'UNKNOWN',
            adjustment_error TEXT,
            PRIMARY KEY(market, ticker, date)
        )
    """)
    conn.execute("""
        CREATE TABLE intraday_bars (
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            datetime TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            open REAL NOT NULL,
            close REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            volume REAL NOT NULL,
            amount REAL,
            PRIMARY KEY(market, ticker, datetime)
        )
    """)
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            entry_price REAL NOT NULL,
            buy_zone_low REAL,
            buy_zone_high REAL,
            stop_loss REAL,
            target_1 REAL,
            target_2 REAL,
            horizon_days INTEGER NOT NULL,
            confidence TEXT NOT NULL,
            thesis TEXT NOT NULL,
            trigger_condition TEXT NOT NULL,
            risk_notes TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'OPEN',
            immutable_hash TEXT NOT NULL,
            UNIQUE(signal_date, ticker, market, strategy_id)
        )
    """)
    conn.execute("""
        CREATE TABLE strategies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            market_scope TEXT NOT NULL,
            thesis TEXT NOT NULL,
            entry_rules_json TEXT NOT NULL,
            exit_rules_json TEXT NOT NULL,
            target_horizon_days INTEGER NOT NULL DEFAULT 10,
            version TEXT NOT NULL DEFAULT 'v1',
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            weight REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of_date TEXT NOT NULL,
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            candidate_score REAL NOT NULL,
            action TEXT NOT NULL,
            entry_price REAL NOT NULL,
            signal_close REAL,
            buy_zone_low REAL,
            buy_zone_high REAL,
            stop_loss REAL,
            target_1 REAL,
            target_2 REAL,
            reward_risk_ratio REAL,
            expected_value_score REAL,
            trailing_stop_pct REAL,
            trailing_activation_pct REAL,
            thesis TEXT NOT NULL,
            trigger_condition TEXT NOT NULL,
            risk_notes TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'WATCHLIST',
            confirmation_status TEXT NOT NULL DEFAULT 'PENDING',
            confirmation_date TEXT,
            confirmation_reason TEXT,
            data_date TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(as_of_date, market, ticker, strategy_id)
        )
    """)
    conn.commit()
    return conn


def _insert_instrument(conn, market, ticker, name, source, source_symbol, active=1):
    conn.execute(
        "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (market, ticker, name, source, source_symbol, active, "[]", "2026-05-29"),
    )


def _insert_price_bar(conn, market, ticker, date, close=100.0, adj_close=100.0, status="ADJUSTED"):
    conn.execute(
        "INSERT INTO price_bars (market, ticker, date, open, close, high, low, volume, "
        "amount, adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (market, ticker, date, close, close, close, close, 1000, 100000,
         adj_close, adj_close, adj_close, adj_close, adj_close / close if close else 1.0, status),
    )


class InstrumentsRepairTest(unittest.TestCase):
    """Test instruments table repair (.SH -> .SS)."""

    def test_sh_only_becomes_ss_preserves_source_symbol(self):
        """When only .SH exists, rename to .SS and preserve source_symbol."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.instruments_merged, 1)
        self.assertEqual(result.total_merged, 1)

        # Verify .SH is gone, .SS exists with same source_symbol
        row = conn.execute(
            "SELECT ticker, source_symbol FROM instruments WHERE market = 'CN_A'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ticker"], "600519.SS")
        self.assertEqual(row["source_symbol"], "sh600519")

    def test_sh_ss_duplicate_merge_keeps_canonical(self):
        """When both .SH and .SS exist, canonical .SS is kept, .SH is deleted."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SS", "贵州茅台", "sina_cn", "sh600519")
        _insert_instrument(conn, "CN_A", "600519.SH", "贵州茅台SH", "sina_cn", "sh600519")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.instruments_merged, 1)
        self.assertEqual(result.total_merged, 1)

        # Only one row should remain, and it should be the canonical .SS
        rows = conn.execute(
            "SELECT ticker, name, source_symbol FROM instruments WHERE market = 'CN_A'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "600519.SS")
        self.assertEqual(rows[0]["name"], "贵州茅台")  # canonical name preserved
        self.assertEqual(rows[0]["source_symbol"], "sh600519")

    def test_repair_is_idempotent(self):
        """Running repair twice produces same result."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519")
        conn.commit()

        result1 = repair_tickers(conn)
        self.assertEqual(result1.total_merged, 1)

        # Second repair should merge 0 rows (nothing left to fix)
        result2 = repair_tickers(conn)
        self.assertEqual(result2.total_merged, 0)

        # Database state is the same either way
        rows = conn.execute(
            "SELECT ticker FROM instruments WHERE market = 'CN_A'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "600519.SS")

    def test_non_cn_a_tickers_unchanged(self):
        """Non-CN_A tickers are not modified."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "US", "NVDA", "NVIDIA", "sina_us", "NVDA")
        _insert_instrument(conn, "HK", "0700.HK", "Tencent", "tencent_hk", "0700")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.total_merged, 0)

        rows = conn.execute("SELECT ticker FROM instruments").fetchall()
        tickers = {r["ticker"] for r in rows}
        self.assertIn("NVDA", tickers)
        self.assertIn("0700.HK", tickers)


class PriceBarsRepairTest(unittest.TestCase):
    """Test price_bars table repair (.SH -> .SS)."""

    def test_sh_only_becomes_ss(self):
        """When only .SH price_bars exist, rename to .SS."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-29")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.price_bars_merged, 1)

        row = conn.execute(
            "SELECT ticker, adjustment_status FROM price_bars WHERE market = 'CN_A'"
        ).fetchone()
        self.assertEqual(row["ticker"], "600519.SS")
        self.assertEqual(row["adjustment_status"], "ADJUSTED")

    def test_conflict_chooses_adjusted_over_raw_fallback(self):
        """When .SH has ADJUSTED and .SS has RAW_FALLBACK, ADJUSTED wins."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-29",
                          close=99.0, adj_close=99.0, status="RAW_FALLBACK")
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-29",
                          close=100.0, adj_close=100.0, status="ADJUSTED")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.price_bars_merged, 1)

        # The .SH ADJUSTED data should have won
        row = conn.execute(
            "SELECT ticker, close, adjustment_status FROM price_bars WHERE market = 'CN_A'"
        ).fetchone()
        self.assertEqual(row["ticker"], "600519.SS")
        self.assertEqual(row["close"], 100.0)
        self.assertEqual(row["adjustment_status"], "ADJUSTED")

    def test_conflict_prefers_canonical_ss_when_statuses_tie(self):
        """When both have same adjustment_status, prefer existing .SS row."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-29",
                          close=100.0, adj_close=100.0, status="ADJUSTED")
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-29",
                          close=105.0, adj_close=105.0, status="ADJUSTED")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.price_bars_merged, 1)

        # The canonical .SS data should be preserved (statuses tie, prefer .SS)
        row = conn.execute(
            "SELECT ticker, close FROM price_bars WHERE market = 'CN_A'"
        ).fetchone()
        self.assertEqual(row["ticker"], "600519.SS")
        self.assertEqual(row["close"], 100.0)

    def test_conflict_raw_fallback_beats_unknown(self):
        """RAW_FALLBACK beats UNKNOWN."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-29",
                          close=99.0, adj_close=99.0, status="UNKNOWN")
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-29",
                          close=100.0, adj_close=100.0, status="RAW_FALLBACK")
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.price_bars_merged, 1)

        row = conn.execute(
            "SELECT ticker, close, adjustment_status FROM price_bars WHERE market = 'CN_A'"
        ).fetchone()
        self.assertEqual(row["ticker"], "600519.SS")
        self.assertEqual(row["close"], 100.0)
        self.assertEqual(row["adjustment_status"], "RAW_FALLBACK")

    def test_multiple_dates_merged(self):
        """Multiple dates for same ticker are all merged."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-27", close=98.0)
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-28", close=99.0)
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-29", close=100.0)
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.price_bars_merged, 3)

        rows = conn.execute(
            "SELECT ticker, date FROM price_bars WHERE market = 'CN_A' ORDER BY date"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["ticker"], "600519.SS")


class AuditRepairTest(unittest.TestCase):
    """Test audit before and after repair."""

    def test_audit_reports_conflicts_before_repair(self):
        """Audit detects .SH rows and conflicts before repair."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SS", "贵州茅台", "sina_cn", "sh600519")
        _insert_instrument(conn, "CN_A", "600519.SH", "贵州茅台SH", "sina_cn", "sh600519")
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-29")
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-28")
        conn.commit()

        result = audit_ticker_repair(conn)
        self.assertTrue(result.dry_run)
        self.assertGreater(result.total_needs_normalization, 0)
        self.assertGreater(result.total_conflicts, 0)

        # After repair, audit should show no issues
        repair_tickers(conn)
        result_after = audit_ticker_repair(conn)
        self.assertEqual(result_after.total_needs_normalization, 0)
        self.assertEqual(result_after.total_conflicts, 0)

    def test_audit_clean_database(self):
        """Audit of clean database shows no issues."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SS", "贵州茅台", "sina_cn", "sh600519")
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-29")
        conn.commit()

        result = audit_ticker_repair(conn)
        self.assertEqual(result.total_needs_normalization, 0)
        self.assertEqual(result.total_unknown_suffix, 0)
        self.assertEqual(result.total_conflicts, 0)
        self.assertEqual(result.total_merged, 0)


class ReportTest(unittest.TestCase):
    """Test report generation."""

    def test_repair_report_files(self):
        """Report generates valid md and json files."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519")
        conn.commit()

        result = audit_ticker_repair(conn)
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path, json_path = write_ticker_repair_report(result, Path(tmpdir))
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())

            data = json.loads(json_path.read_text())
            self.assertEqual(data["summary"]["dry_run"], True)
            self.assertGreater(data["summary"]["total_needs_normalization"], 0)

            content = md_path.read_text()
            self.assertIn("Ticker Normalization Repair Report", content)
            self.assertIn("dry-run", content)

    def test_applied_repair_report(self):
        """Applied repair report shows merge counts."""
        conn = _make_minimal_db()
        _insert_instrument(conn, "CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519")
        conn.commit()

        result = repair_tickers(conn)
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path, json_path = write_ticker_repair_report(result, Path(tmpdir))
            data = json.loads(json_path.read_text())
            self.assertEqual(data["summary"]["dry_run"], False)
            self.assertGreater(data["summary"]["instruments_merged"], 0)

            content = md_path.read_text()
            self.assertIn("applied", content)


class IntradayBarsSafetyTest(unittest.TestCase):
    """Test that repair_tickers does NOT mutate intraday_bars by default."""

    def test_intraday_bars_same_datetime_not_mutated(self):
        """intraday_bars with 600519.SH and 000001.SZ at same datetime must not
        cause IntegrityError and must not be modified by default repair."""
        conn = _make_full_schema_db()
        # Insert 600519.SH intraday bar
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '600519.SH', '2026-05-28 09:35:00', '2026-05-28', '09:35', "
            "1800.0, 1805.0, 1810.0, 1795.0, 5000)"
        )
        # Insert 000001.SZ at the same datetime (different stock, not a conflict)
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '000001.SZ', '2026-05-28 09:35:00', '2026-05-28', '09:35', "
            "15.0, 15.1, 15.2, 14.9, 10000)"
        )
        conn.commit()

        # repair_tickers should NOT raise IntegrityError
        result = repair_tickers(conn)
        self.assertEqual(result.other_tables_merged, 0)

        # intraday_bars must be unchanged
        rows = conn.execute(
            "SELECT ticker, datetime FROM intraday_bars ORDER BY ticker"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        tickers = {r["ticker"] for r in rows}
        self.assertIn("600519.SH", tickers)
        self.assertIn("000001.SZ", tickers)

    def test_intraday_bars_sh_only_not_renamed(self):
        """intraday_bars with only .SH ticker should NOT be renamed by default."""
        conn = _make_full_schema_db()
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '600519.SH', '2026-05-28 09:35:00', '2026-05-28', '09:35', "
            "1800.0, 1805.0, 1810.0, 1795.0, 5000)"
        )
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.other_tables_merged, 0)

        # .SH should still be there — no mutation
        row = conn.execute(
            "SELECT ticker FROM intraday_bars"
        ).fetchone()
        self.assertEqual(row["ticker"], "600519.SH")


class OtherTablesNotMutatedTest(unittest.TestCase):
    """Test that repair_tickers does NOT mutate signals/candidates by default."""

    def test_signals_not_mutated(self):
        """signals with .SH ticker should not be renamed by default repair."""
        conn = _make_full_schema_db()
        conn.execute(
            "INSERT INTO strategies (id, name, market_scope, thesis, entry_rules_json, "
            "exit_rules_json, created_at) VALUES ('s1', 'Test', 'CN_A', 't', '[]', '[]', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO signals "
            "(created_at, signal_date, ticker, name, market, strategy_id, entry_type, "
            "entry_price, horizon_days, confidence, thesis, trigger_condition, risk_notes, "
            "immutable_hash) "
            "VALUES ('2026-05-28', '2026-05-28', '600519.SH', '贵州茅台', 'CN_A', 's1', "
            "'BREAKOUT', 1800.0, 10, 'HIGH', 'test', 'trigger', 'risk', 'hash1')",
        )
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.other_tables_merged, 0)

        row = conn.execute("SELECT ticker FROM signals").fetchone()
        self.assertEqual(row["ticker"], "600519.SH")

    def test_candidates_not_mutated(self):
        """candidates with .SH ticker should not be renamed by default repair."""
        conn = _make_full_schema_db()
        conn.execute(
            "INSERT INTO strategies (id, name, market_scope, thesis, entry_rules_json, "
            "exit_rules_json, created_at) VALUES ('s1', 'Test', 'CN_A', 't', '[]', '[]', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO candidates "
            "(as_of_date, market, ticker, name, strategy_id, candidate_score, action, "
            "entry_price, thesis, trigger_condition, risk_notes, created_at) "
            "VALUES ('2026-05-28', 'CN_A', '600519.SH', '贵州茅台', 's1', 80.0, 'BUY', "
            "1800.0, 'test', 'trigger', 'risk', '2026-05-28')"
        )
        conn.commit()

        result = repair_tickers(conn)
        self.assertEqual(result.other_tables_merged, 0)

        row = conn.execute("SELECT ticker FROM candidates").fetchone()
        self.assertEqual(row["ticker"], "600519.SH")


class PriceBarsAuditRowKeyTest(unittest.TestCase):
    """Test that price_bars audit uses row-key level conflict detection."""

    def test_no_conflict_different_dates(self):
        """600519.SH on 2026-05-28 and 600519.SS on 2026-05-29 is NOT a conflict."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-28")
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-29")
        conn.commit()

        result = audit_ticker_repair(conn)
        # price_bars audit: no conflict (different dates)
        pb_audit = next(t for t in result.tables if t.table == "price_bars")
        self.assertEqual(pb_audit.conflicts, 0)
        self.assertEqual(pb_audit.needs_normalization, 1)

    def test_conflict_same_date(self):
        """600519.SH and 600519.SS on the SAME date IS a conflict."""
        conn = _make_minimal_db()
        _insert_price_bar(conn, "CN_A", "600519.SH", "2026-05-28")
        _insert_price_bar(conn, "CN_A", "600519.SS", "2026-05-28")
        conn.commit()

        result = audit_ticker_repair(conn)
        pb_audit = next(t for t in result.tables if t.table == "price_bars")
        self.assertEqual(pb_audit.conflicts, 1)
        self.assertEqual(pb_audit.needs_normalization, 1)

    def test_intraday_bars_audit_row_key_level(self):
        """intraday_bars audit: conflict only when same datetime, not different."""
        conn = _make_full_schema_db()
        # Same datetime = conflict
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '600519.SH', '2026-05-28 09:35:00', '2026-05-28', '09:35', "
            "1800.0, 1805.0, 1810.0, 1795.0, 5000)"
        )
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '600519.SS', '2026-05-28 09:35:00', '2026-05-28', '09:35', "
            "1800.0, 1805.0, 1810.0, 1795.0, 5000)"
        )
        conn.commit()

        result = audit_ticker_repair(conn)
        ib_audit = next(t for t in result.tables if t.table == "intraday_bars")
        self.assertEqual(ib_audit.conflicts, 1)

    def test_intraday_bars_no_conflict_different_datetime(self):
        """intraday_bars audit: different datetime = no conflict."""
        conn = _make_full_schema_db()
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '600519.SH', '2026-05-28 09:35:00', '2026-05-28', '09:35', "
            "1800.0, 1805.0, 1810.0, 1795.0, 5000)"
        )
        conn.execute(
            "INSERT INTO intraday_bars "
            "(market, ticker, datetime, date, time, open, close, high, low, volume) "
            "VALUES ('CN_A', '600519.SS', '2026-05-28 09:40:00', '2026-05-28', '09:40', "
            "1805.0, 1810.0, 1815.0, 1800.0, 5000)"
        )
        conn.commit()

        result = audit_ticker_repair(conn)
        ib_audit = next(t for t in result.tables if t.table == "intraday_bars")
        self.assertEqual(ib_audit.conflicts, 0)
        self.assertEqual(ib_audit.needs_normalization, 1)


if __name__ == "__main__":
    unittest.main()
