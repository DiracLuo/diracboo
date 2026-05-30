"""Tests for Qlib CSV export functionality."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

from alpha_ledger.qlib_export import (
    QLIB_CSV_COLUMNS,
    STATUS_BAD_ADJUSTMENT,
    STATUS_MISSING_PRICE,
    STATUS_OK,
    STATUS_SUSPENDED,
    STATUS_ZERO_VOLUME,
    audit_ticker_normalization,
    export_qlib_csv,
    normalize_ticker_suffix,
    qlib_filename_to_ticker,
    ticker_to_qlib_filename,
    write_quality_report,
    write_ticker_audit_report,
)

EXPECTED_QLIB_CSV_COLUMNS = ["date", "open", "close", "high", "low", "volume", "vwap", "money", "factor", "change"]


class TickerMappingTest(TestCase):
    def test_ss_to_sh(self):
        self.assertEqual(ticker_to_qlib_filename("600519.SS"), "SH600519.csv")
        self.assertEqual(ticker_to_qlib_filename("000300.SS"), "SH000300.csv")

    def test_sz_to_sz(self):
        self.assertEqual(ticker_to_qlib_filename("002674.SZ"), "SZ002674.csv")
        self.assertEqual(ticker_to_qlib_filename("399006.SZ"), "SZ399006.csv")

    def test_bj_to_bj(self):
        self.assertEqual(ticker_to_qlib_filename("430047.BJ"), "BJ430047.csv")

    def test_unknown_suffix_raises(self):
        with self.assertRaises(ValueError):
            ticker_to_qlib_filename("AAPL.US")

    def test_roundtrip(self):
        for ticker in ("600519.SS", "002674.SZ", "430047.BJ", "000300.SS"):
            filename = ticker_to_qlib_filename(ticker)
            self.assertEqual(qlib_filename_to_ticker(filename), ticker)

    def test_sh_suffix_accepted(self):
        """Test that .SH suffix is accepted and converted to .SS at boundaries."""
        # normalize_ticker_suffix should convert .SH to .SS
        self.assertEqual(normalize_ticker_suffix("600519.SH"), "600519.SS")
        self.assertEqual(normalize_ticker_suffix("600519.sh"), "600519.SS")
        self.assertEqual(normalize_ticker_suffix("SH600519"), "600519.SS")
        self.assertEqual(normalize_ticker_suffix("sh.600519"), "600519.SS")
        self.assertEqual(normalize_ticker_suffix("000300.SH"), "000300.SS")

        # ticker_to_qlib_filename should accept .SH
        self.assertEqual(ticker_to_qlib_filename("600519.SH"), "SH600519.csv")
        self.assertEqual(ticker_to_qlib_filename("SH600519"), "SH600519.csv")
        self.assertEqual(ticker_to_qlib_filename("000300.SH"), "SH000300.csv")

    def test_sh_roundtrip_via_qlib(self):
        """Test roundtrip: .SH → Qlib filename → .SS (canonical)."""
        # .SH input should produce same Qlib filename as .SS
        self.assertEqual(ticker_to_qlib_filename("600519.SH"), ticker_to_qlib_filename("600519.SS"))
        # Qlib filename always maps back to canonical .SS
        self.assertEqual(qlib_filename_to_ticker("SH600519.csv"), "600519.SS")

    def test_sz_bj_pass_through(self):
        """Test that .SZ and .BJ suffixes pass through normalization unchanged."""
        self.assertEqual(normalize_ticker_suffix("002674.SZ"), "002674.SZ")
        self.assertEqual(normalize_ticker_suffix("430047.BJ"), "430047.BJ")
        self.assertEqual(normalize_ticker_suffix("600519.SS"), "600519.SS")


def _make_db(bars: list[dict]) -> sqlite3.Connection:
    """Create an in-memory DB with price_bars for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE price_bars (
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            close REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            volume REAL NOT NULL,
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
    for bar in bars:
        conn.execute(
            "INSERT INTO price_bars (market, ticker, date, open, close, high, low, volume, amount, "
            "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status, change_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bar["market"], bar["ticker"], bar["date"],
                bar.get("open", 10.0), bar.get("close", 10.0),
                bar.get("high", 10.0), bar.get("low", 10.0),
                bar.get("volume", 1000),
                bar.get("amount", 10000.0),
                bar.get("adj_open", 10.0), bar.get("adj_close", 10.0),
                bar.get("adj_high", 10.0), bar.get("adj_low", 10.0),
                bar.get("adj_factor", 1.0),
                bar.get("adjustment_status", "ADJUSTED"),
                bar.get("change_pct", 1.5),
            ),
        )
    conn.commit()
    return conn


class ExportQlibCsvTest(TestCase):
    def test_basic_export(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "amount": 5250000.0,
                "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 2.5,
            },
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-26",
                "open": 105.0, "close": 107.0, "high": 108.0, "low": 104.0,
                "volume": 60000, "amount": 6420000.0,
                "adj_open": 105.0, "adj_close": 107.0,
                "adj_high": 108.0, "adj_low": 104.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.9,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-26", Path(tmpdir))
            self.assertEqual(result.csv_count, 1)
            self.assertEqual(result.total_bars, 2)
            self.assertEqual(result.total_warnings, 0)

            csv_path = Path(tmpdir) / "SH600519.csv"
            self.assertTrue(csv_path.exists())
            content = csv_path.read_text()
            lines = content.strip().split("\n")
            self.assertEqual(lines[0], ",".join(EXPECTED_QLIB_CSV_COLUMNS))
            self.assertEqual(len(lines), 3)  # header + 2 rows

            # Check field values
            fields = lines[1].split(",")
            self.assertEqual(fields[0], "2026-05-25")
            self.assertEqual(fields[1], "100.0")  # open = adj_open
            self.assertEqual(fields[2], "105.0")  # close = adj_close
            self.assertAlmostEqual(float(fields[6]), 105.0, places=2)  # vwap = amount/volume = 5250000/50000
            self.assertAlmostEqual(float(fields[7]), 5250000.0)  # money = amount
            self.assertAlmostEqual(float(fields[9]), 0.025, places=5)  # change = change_pct/100

    def test_change_field_conversion(self):
        bars = [
            {
                "market": "CN_A", "ticker": "002674.SZ", "date": "2026-05-25",
                "open": 20.0, "close": 21.0, "high": 22.0, "low": 19.5,
                "volume": 30000, "amount": 630000.0,
                "adj_open": 20.0, "adj_close": 21.0,
                "adj_high": 22.0, "adj_low": 19.5, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": -3.2,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            csv_path = Path(tmpdir) / "SZ002674.csv"
            content = csv_path.read_text()
            fields = content.strip().split("\n")[1].split(",")
            self.assertAlmostEqual(float(fields[9]), -0.032, places=5)

    def test_raw_adjusted_mode(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "amount": 5250000.0,
                "adj_open": 150.0, "adj_close": 157.5,
                "adj_high": 159.0, "adj_low": 148.5, "adj_factor": 1.5,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            csv_path = Path(tmpdir) / "SH600519.csv"
            fields = csv_path.read_text().strip().split("\n")[1].split(",")
            # raw_adjusted: open = adj_open (150.0), not raw open (100.0)
            self.assertEqual(fields[1], "150.0")
            self.assertEqual(fields[2], "157.5")  # adj_close
            self.assertEqual(float(fields[5]), 50000.0)   # volume
            self.assertEqual(float(fields[8]), 1.5)       # factor

    def test_adj_factor_missing_warning(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": None,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            self.assertGreater(result.total_warnings, 0)
            qs = result.quality_stats[0]
            self.assertTrue(any("adj_factor" in w for w in qs.warnings))

    def test_zero_volume_classified_correctly(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 100.0, "high": 100.0, "low": 100.0,
                "volume": 0, "adj_open": 100.0, "adj_close": 100.0,
                "adj_high": 100.0, "adj_low": 100.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 0.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            qs = result.quality_stats[0]
            # All prices same + volume=0 → suspended
            self.assertEqual(qs.possible_suspended, 1)
            self.assertEqual(qs.zero_volume_with_price, 0)

    def test_zero_volume_with_different_prices(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 0, "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            qs = result.quality_stats[0]
            self.assertEqual(qs.zero_volume_with_price, 1)
            self.assertEqual(qs.possible_suspended, 0)

    def test_bad_adjustment_flagged(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "RAW_FALLBACK", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            qs = result.quality_stats[0]
            self.assertEqual(qs.bad_adjustment, 1)

    def test_quality_report_json_valid(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            md_path, json_path = write_quality_report(result, Path(tmpdir))
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text())
            self.assertIn("export_summary", data)
            self.assertEqual(data["export_summary"]["csv_count"], 1)

    def test_quality_report_md_exists(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            md_path, _ = write_quality_report(result, Path(tmpdir))
            content = md_path.read_text()
            self.assertIn("Qlib Export Quality Report", content)
            self.assertIn("SH600519", content)

    def test_multiple_tickers(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
            {
                "market": "CN_A", "ticker": "002674.SZ", "date": "2026-05-25",
                "open": 20.0, "close": 21.0, "high": 22.0, "low": 19.5,
                "volume": 30000, "adj_open": 20.0, "adj_close": 21.0,
                "adj_high": 22.0, "adj_low": 19.5, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 2.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            self.assertEqual(result.csv_count, 2)
            self.assertTrue((Path(tmpdir) / "SH600519.csv").exists())
            self.assertTrue((Path(tmpdir) / "SZ002674.csv").exists())

    def test_sh_ss_aliases_export_one_qlib_file_without_overwrite(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SH", "date": "2026-05-25",
                "open": 99.0, "close": 99.0, "high": 99.0, "low": 99.0,
                "volume": 50000, "amount": 4950000.0,
                "adj_open": 99.0, "adj_close": 99.0,
                "adj_high": 99.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 0.0,
            },
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "amount": 5250000.0,
                "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 2.5,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            csv_files = list(Path(tmpdir).glob("*.csv"))
            self.assertEqual(result.csv_count, 1)
            self.assertEqual(len(csv_files), 1)
            self.assertEqual(csv_files[0].name, "SH600519.csv")
            fields = csv_files[0].read_text().strip().split("\n")[1].split(",")
            self.assertEqual(fields[2], "105.0")
            self.assertTrue(any("merged ticker aliases" in w for w in result.quality_stats[0].warnings))

    def test_csv_readable_by_pandas(self):
        import pandas as pd

        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "amount": 5250000.0,
                "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            df = pd.read_csv(Path(tmpdir) / "SH600519.csv")
            self.assertEqual(len(df), 1)
            self.assertListEqual(list(df.columns), EXPECTED_QLIB_CSV_COLUMNS)

    def test_vwap_field_export(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "amount": 5250000.0,
                "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            csv_path = Path(tmpdir) / "SH600519.csv"
            fields = csv_path.read_text().strip().split("\n")[1].split(",")
            # vwap = amount / volume = 5250000 / 50000 = 105.0
            self.assertAlmostEqual(float(fields[6]), 105.0, places=2)
            # money = amount = 5250000
            self.assertAlmostEqual(float(fields[7]), 5250000.0)

    def test_vwap_fallback_to_typical_price_when_no_amount(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 50000, "amount": None,
                "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 1.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            qs = result.quality_stats[0]
            # amount is None but prices exist → fallback to typical price
            self.assertEqual(qs.vwap_unavailable, 0)
            csv_path = Path(tmpdir) / "SH600519.csv"
            fields = csv_path.read_text().strip().split("\n")[1].split(",")
            # vwap = (106 + 99 + 105) / 3 = 103.333...
            self.assertAlmostEqual(float(fields[6]), 103.333, places=2)
            self.assertEqual(fields[7], "")  # money is still empty

    def test_vwap_fallback_when_zero_volume_with_amount_zero(self):
        bars = [
            {
                "market": "CN_A", "ticker": "600519.SS", "date": "2026-05-25",
                "open": 100.0, "close": 105.0, "high": 106.0, "low": 99.0,
                "volume": 0, "amount": 0.0,
                "adj_open": 100.0, "adj_close": 105.0,
                "adj_high": 106.0, "adj_low": 99.0, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 0.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            qs = result.quality_stats[0]
            # volume=0 and amount=0, but prices exist → fallback to typical price
            self.assertEqual(qs.vwap_unavailable, 0)
            csv_path = Path(tmpdir) / "SH600519.csv"
            fields = csv_path.read_text().strip().split("\n")[1].split(",")
            self.assertAlmostEqual(float(fields[6]), 103.333, places=2)  # (106+99+105)/3

    def test_money_field_export(self):
        bars = [
            {
                "market": "CN_A", "ticker": "002674.SZ", "date": "2026-05-25",
                "open": 20.0, "close": 21.0, "high": 22.0, "low": 19.5,
                "volume": 30000, "amount": 630000.0,
                "adj_open": 20.0, "adj_close": 21.0,
                "adj_high": 22.0, "adj_low": 19.5, "adj_factor": 1.0,
                "adjustment_status": "ADJUSTED", "change_pct": 2.0,
            },
        ]
        conn = _make_db(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_qlib_csv(conn, "2026-05-25", "2026-05-25", Path(tmpdir))
            csv_path = Path(tmpdir) / "SZ002674.csv"
            fields = csv_path.read_text().strip().split("\n")[1].split(",")
            self.assertAlmostEqual(float(fields[7]), 630000.0)  # money = amount


def _make_instruments_db() -> sqlite3.Connection:
    """Create an in-memory DB with instruments table for audit tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    conn.commit()
    return conn


class TickerAuditTest(TestCase):
    def test_all_canonical(self):
        """Test audit with all canonical tickers (no issues expected)."""
        conn = _make_instruments_db()
        conn.execute(
            "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "600519.SS", "贵州茅台", "sina_cn", "sh600519", 1, "[]", "2026-05-29"),
        )
        conn.execute(
            "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "002674.SZ", "兴业银行", "sina_cn", "sz002674", 1, "[]", "2026-05-29"),
        )
        conn.commit()

        result = audit_ticker_normalization(conn)
        self.assertEqual(result.total_instruments, 2)
        self.assertEqual(result.canonical_count, 2)
        self.assertEqual(result.needs_normalization, 0)
        self.assertEqual(result.unknown_suffix, 0)
        self.assertEqual(len(result.issues), 0)

    def test_sh_suffix_detected(self):
        """Test audit detects .SH suffix that needs normalization."""
        conn = _make_instruments_db()
        conn.execute(
            "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519", 1, "[]", "2026-05-29"),
        )
        conn.commit()

        result = audit_ticker_normalization(conn)
        self.assertEqual(result.total_instruments, 1)
        self.assertEqual(result.canonical_count, 0)
        self.assertEqual(result.needs_normalization, 1)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0]["issue"], "needs_normalization")
        self.assertIn("600519.SH → 600519.SS", result.issues[0]["detail"])

    def test_unknown_suffix_detected(self):
        """Test audit detects unknown suffix."""
        conn = _make_instruments_db()
        conn.execute(
            "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "600519.XX", "贵州茅台", "sina_cn", "sh600519", 1, "[]", "2026-05-29"),
        )
        conn.commit()

        result = audit_ticker_normalization(conn)
        self.assertEqual(result.total_instruments, 1)
        self.assertEqual(result.unknown_suffix, 1)
        self.assertEqual(result.issues[0]["issue"], "unknown_suffix")

    def test_non_cn_a_skipped(self):
        """Test that non-CN_A tickers are counted as canonical (not audited)."""
        conn = _make_instruments_db()
        conn.execute(
            "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("US", "NVDA", "NVIDIA", "sina_us", "NVDA", 1, "[]", "2026-05-29"),
        )
        conn.commit()

        result = audit_ticker_normalization(conn)
        self.assertEqual(result.total_instruments, 1)
        self.assertEqual(result.canonical_count, 1)
        self.assertEqual(result.needs_normalization, 0)

    def test_mixed_tickers(self):
        """Test audit with mix of canonical, .SH, and unknown suffixes."""
        conn = _make_instruments_db()
        tickers = [
            ("CN_A", "600519.SS", "贵州茅台"),
            ("CN_A", "600519.SH", "贵州茅台SH"),
            ("CN_A", "002674.SZ", "兴业银行"),
            ("CN_A", "600519.XX", "贵州茅台XX"),
        ]
        for market, ticker, name in tickers:
            conn.execute(
                "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (market, ticker, name, "sina_cn", "sh600519", 1, "[]", "2026-05-29"),
            )
        conn.commit()

        result = audit_ticker_normalization(conn)
        self.assertEqual(result.total_instruments, 4)
        self.assertEqual(result.canonical_count, 2)  # .SS and .SZ
        self.assertEqual(result.needs_normalization, 1)  # .SH
        self.assertEqual(result.unknown_suffix, 1)  # .XX

    def test_audit_report_files(self):
        """Test that audit report generates valid files."""
        conn = _make_instruments_db()
        conn.execute(
            "INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519", 1, "[]", "2026-05-29"),
        )
        conn.commit()

        result = audit_ticker_normalization(conn)
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path, json_path = write_ticker_audit_report(result, Path(tmpdir))
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())

            data = json.loads(json_path.read_text())
            self.assertEqual(data["summary"]["needs_normalization"], 1)
            self.assertEqual(len(data["issues"]), 1)

            content = md_path.read_text()
            self.assertIn("Ticker Normalization Audit Report", content)
            self.assertIn("600519.SH", content)
