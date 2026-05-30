"""Tests for QFQ backfill module."""

import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

from alpha_ledger.db import init_db
from alpha_ledger.qfq_backfill import (
    QfqBackfillResult,
    _is_benchmark,
    _update_adjusted_bars,
    qfq_backfill,
    tickers_needing_adjustment,
    vwap_sanity_check,
    write_qfq_backfill_report,
)


def _make_db() -> sqlite3.Connection:
    """Create in-memory DB with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def _insert_bar(
    conn: sqlite3.Connection,
    ticker: str,
    dt: str,
    close: float = 10.0,
    status: str = "RAW_FALLBACK",
) -> None:
    """Insert a minimal price_bars row."""
    conn.execute(
        """
        INSERT INTO price_bars
            (market, ticker, date, open, close, high, low, volume,
             adj_open, adj_close, adj_high, adj_low, adj_factor,
             adjustment_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1000, ?, ?, ?, ?, ?, ?)
        """,
        ("CN_A", ticker, dt, close, close, close, close,
         close, close, close, close, 1.0, status),
    )
    conn.commit()


class TestTickersNeedingAdjustment(unittest.TestCase):
    """Test ticker discovery from price_bars."""

    def test_finds_raw_fallback_tickers(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02")
        _insert_bar(conn, "000001.SZ", "2026-01-02")
        tickers = tickers_needing_adjustment(conn)
        self.assertEqual(sorted(tickers), ["000001.SZ", "600519.SS"])

    def test_excludes_adjusted_tickers(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="ADJUSTED")
        _insert_bar(conn, "000001.SZ", "2026-01-02", status="RAW_FALLBACK")
        tickers = tickers_needing_adjustment(conn)
        self.assertEqual(tickers, ["000001.SZ"])

    def test_date_range_filter(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02")
        _insert_bar(conn, "600519.SS", "2026-06-02")
        tickers = tickers_needing_adjustment(conn, start="2026-03-01")
        self.assertEqual(tickers, ["600519.SS"])  # still distinct
        # Verify the date filter works at the row level
        count = conn.execute(
            "SELECT COUNT(*) FROM price_bars WHERE market='CN_A' AND date >= '2026-03-01'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_tickers_filter(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02")
        _insert_bar(conn, "000001.SZ", "2026-01-02")
        tickers = tickers_needing_adjustment(conn, tickers_filter={"600519.SS"})
        self.assertEqual(tickers, ["600519.SS"])

    def test_dot_sh_input_normalizes_to_dot_ss(self) -> None:
        """Input .SH resolves to canonical .SS via normalize_cn_a_ticker."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02")
        # Filter with .SH form — should normalize to .SS and find the row
        tickers = tickers_needing_adjustment(conn, tickers_filter={"600519.SH"})
        self.assertEqual(tickers, ["600519.SS"])


class TestIsBenchmark(unittest.TestCase):
    """Test benchmark detection."""

    def test_benchmarks_detected(self) -> None:
        self.assertTrue(_is_benchmark("000300.SS"))
        self.assertTrue(_is_benchmark("000905.SS"))
        self.assertTrue(_is_benchmark("399006.SZ"))
        self.assertTrue(_is_benchmark("899050.BJ"))

    def test_non_benchmarks_pass(self) -> None:
        self.assertFalse(_is_benchmark("600519.SS"))
        self.assertFalse(_is_benchmark("000001.SZ"))


class TestUpdateAdjustedBars(unittest.TestCase):
    """Test the UPDATE logic for price_bars."""

    def test_updates_only_raw_fallback_rows(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")
        _insert_bar(conn, "600519.SS", "2026-01-03", close=10.0, status="ADJUSTED")

        adj_map = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
            "2026-01-03": {"adj_open": 12.0, "adj_close": 12.0, "adj_high": 12.0, "adj_low": 12.0},
        }
        updated = _update_adjusted_bars(conn, "600519.SS", adj_map)
        self.assertEqual(updated, 1)  # only the RAW_FALLBACK row

        # Verify the RAW_FALLBACK row was updated
        row = conn.execute(
            "SELECT adj_close, adjustment_status FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_close"], 11.0)
        self.assertEqual(row["adjustment_status"], "ADJUSTED")

        # Verify the ADJUSTED row was NOT touched
        row2 = conn.execute(
            "SELECT adj_close, adjustment_status FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-03'"
        ).fetchone()
        self.assertAlmostEqual(row2["adj_close"], 10.0)  # unchanged
        self.assertEqual(row2["adjustment_status"], "ADJUSTED")

    def test_adj_factor_computed_from_close(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")

        adj_map = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 12.0, "adj_high": 13.0, "adj_low": 9.0},
        }
        _update_adjusted_bars(conn, "600519.SS", adj_map)

        row = conn.execute(
            "SELECT adj_factor, adjustment_status, adjustment_error FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_factor"], 1.2)  # 12.0 / 10.0
        self.assertEqual(row["adjustment_status"], "ADJUSTED")
        self.assertIsNone(row["adjustment_error"])

    def test_clears_adjustment_error(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")
        conn.execute(
            "UPDATE price_bars SET adjustment_error='old error' WHERE ticker='600519.SS' AND date='2026-01-02'"
        )
        conn.commit()

        adj_map = {
            "2026-01-02": {"adj_open": 10.0, "adj_close": 10.0, "adj_high": 10.0, "adj_low": 10.0},
        }
        _update_adjusted_bars(conn, "600519.SS", adj_map)

        row = conn.execute(
            "SELECT adjustment_error FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertIsNone(row["adjustment_error"])


class TestQfqBackfillDryRun(unittest.TestCase):
    """Test dry-run mode: no network, no DB writes."""

    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_dry_run_no_network(self, mock_akshare: MagicMock, mock_baostock: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", dry_run=True)

        mock_baostock.assert_not_called()
        mock_akshare.assert_not_called()
        self.assertTrue(result.dry_run)
        self.assertEqual(result.updated_rows, 0)
        self.assertEqual(result.total_tickers, 1)

    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_dry_run_no_db_writes(self, mock_akshare: MagicMock, mock_baostock: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")

        qfq_backfill(conn, "2026-01-01", "2026-01-31", dry_run=True)

        # Verify the row is still RAW_FALLBACK
        row = conn.execute(
            "SELECT adjustment_status FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertEqual(row["adjustment_status"], "RAW_FALLBACK")

    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_dry_run_skips_benchmarks(self, mock_akshare: MagicMock, mock_baostock: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        _insert_bar(conn, "000300.SS", "2026-01-02", status="RAW_FALLBACK")  # benchmark

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", dry_run=True)

        self.assertEqual(result.total_tickers, 2)
        self.assertEqual(result.skipped_benchmarks, 1)
        self.assertEqual(result.ticker_count, 1)
        self.assertEqual(result.target_count, 1)
        self.assertIn("000300.SS", result.benchmark_tickers)

    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_dry_run_with_limit_reports_target_count(self, mock_akshare: MagicMock, mock_baostock: MagicMock) -> None:
        """Dry-run with --limit should report the limited target count accurately."""
        conn = _make_db()
        for i, ticker in enumerate(["600519.SS", "000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"]):
            _insert_bar(conn, ticker, "2026-01-02", status="RAW_FALLBACK")

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", limit=2, dry_run=True)

        self.assertEqual(result.total_tickers, 5)
        self.assertEqual(result.target_count, 2)
        self.assertEqual(result.ticker_count, 2)
        # target_count should match ticker_count
        self.assertEqual(result.target_count, result.ticker_count)

    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_dry_run_no_limit_reports_full_target_count(self, mock_akshare: MagicMock, mock_baostock: MagicMock) -> None:
        """Dry-run without --limit should report all non-benchmark tickers."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        _insert_bar(conn, "000001.SZ", "2026-01-02", status="RAW_FALLBACK")
        _insert_bar(conn, "000300.SS", "2026-01-02", status="RAW_FALLBACK")  # benchmark

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", dry_run=True)

        self.assertEqual(result.total_tickers, 3)
        self.assertEqual(result.skipped_benchmarks, 1)
        self.assertEqual(result.target_count, 2)
        self.assertEqual(result.ticker_count, 2)


class TestQfqBackfillLive(unittest.TestCase):
    """Test live (mocked-network) backfill behavior."""

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_baostock_called_once_per_ticker_with_date_range(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """Verify BaoStock is called once per ticker with full date range and adjustflag=2."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        _insert_bar(conn, "000001.SZ", "2026-01-05", status="RAW_FALLBACK")

        mock_bs.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
        }

        qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)

        self.assertEqual(mock_bs.call_count, 2)

        # Verify each call used the correct Instrument and date range
        for call_args in mock_bs.call_args_list:
            instrument, start_d, end_d = call_args[0][0], call_args[0][1], call_args[0][2]
            self.assertEqual(instrument.market, "CN_A")
            self.assertEqual(start_d, date(2026, 1, 1))
            self.assertEqual(end_d, date(2026, 1, 31))
            self.assertEqual(call_args[1].get("adjust", "qfq"), "qfq")

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_auto_fallback_to_akshare_on_baostock_error(self, mock_ak: MagicMock, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """With source='auto', when BaoStock raises, AkShare is tried as fallback."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")

        mock_bs.side_effect = Exception("BaoStock down")
        mock_ak.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
        }

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", source="auto", throttle=0)

        self.assertEqual(result.updated_rows, 1)
        self.assertEqual(result.akshare_count, 1)
        self.assertEqual(result.baostock_count, 0)
        mock_ak.assert_called_once()

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_baostock_only_no_fallback(self, mock_ak: MagicMock, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """With source='baostock', failure records an error — no AkShare fallback."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")

        mock_bs.side_effect = Exception("BaoStock down")

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)

        # AkShare should NOT have been called
        mock_ak.assert_not_called()
        self.assertEqual(result.updated_rows, 0)
        self.assertEqual(result.skipped_errors, 1)
        self.assertIn("600519.SS", result.ticker_errors[0])

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_updates_only_existing_rows(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """Backfill only touches rows that exist in price_bars."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        # No row for 2026-01-03

        mock_bs.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
            "2026-01-03": {"adj_open": 12.0, "adj_close": 12.0, "adj_high": 12.0, "adj_low": 12.0},
        }

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)

        self.assertEqual(result.updated_rows, 1)  # only the existing row

        # Verify no new row was inserted
        count = conn.execute("SELECT COUNT(*) FROM price_bars WHERE ticker='600519.SS'").fetchone()[0]
        self.assertEqual(count, 1)

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_leaves_already_adjusted_rows_untouched(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="ADJUSTED")

        mock_bs.return_value = {
            "2026-01-02": {"adj_open": 99.0, "adj_close": 99.0, "adj_high": 99.0, "adj_low": 99.0},
        }

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)

        self.assertEqual(result.updated_rows, 0)
        row = conn.execute(
            "SELECT adj_close FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_close"], 10.0)  # unchanged

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_limit_restricts_ticker_count(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        _insert_bar(conn, "000001.SZ", "2026-01-02", status="RAW_FALLBACK")
        _insert_bar(conn, "000002.SZ", "2026-01-02", status="RAW_FALLBACK")

        mock_bs.return_value = {}

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", limit=1, source="baostock", throttle=0)

        self.assertEqual(mock_bs.call_count, 1)
        self.assertEqual(result.target_count, 1)

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_dot_sh_subset_resolves_to_canonical(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """--tickers with .SH input resolves to canonical .SS in DB."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")

        mock_bs.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
        }

        result = qfq_backfill(
            conn, "2026-01-01", "2026-01-31",
            tickers_subset={"600519.SH"},  # .SH input
            source="baostock", throttle=0,
        )

        self.assertEqual(result.updated_rows, 1)
        self.assertEqual(result.total_tickers, 1)


class TestBaoStockLogout(unittest.TestCase):
    """Test BaoStock logout helper is called at the right times."""

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_logout_called_after_live_baostock_run(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """Logout should be called after a live run with source='baostock'."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        mock_bs.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
        }

        qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)
        mock_logout.assert_called_once()

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_logout_called_after_live_auto_run(self, mock_ak: MagicMock, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """Logout should be called after a live run with source='auto'."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        mock_bs.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
        }

        qfq_backfill(conn, "2026-01-01", "2026-01-31", source="auto", throttle=0)
        mock_logout.assert_called_once()

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_logout_not_called_for_dry_run(self, mock_ak: MagicMock, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """Logout should NOT be called during dry-run (no network)."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")

        qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", dry_run=True)
        mock_logout.assert_not_called()

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_akshare_cn_adjusted_daily_map")
    def test_logout_not_called_for_akshare_source(self, mock_ak: MagicMock, mock_logout: MagicMock) -> None:
        """Logout should NOT be called when source='akshare' (no BaoStock usage)."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        mock_ak.return_value = {
            "2026-01-02": {"adj_open": 11.0, "adj_close": 11.0, "adj_high": 11.0, "adj_low": 11.0},
        }

        qfq_backfill(conn, "2026-01-01", "2026-01-31", source="akshare", throttle=0)
        mock_logout.assert_not_called()

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_logout_called_even_on_error(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """Logout should be called even if the run encounters errors (finally block)."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", status="RAW_FALLBACK")
        mock_bs.side_effect = Exception("BaoStock down")

        qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)
        mock_logout.assert_called_once()


class TestReportWriting(unittest.TestCase):
    """Test report generation."""

    def test_writes_markdown_and_json(self) -> None:
        result = QfqBackfillResult(
            start="2026-01-01",
            end="2026-01-31",
            source="baostock",
            total_tickers=10,
            skipped_benchmarks=2,
            skipped_errors=1,
            updated_rows=500,
            baostock_count=6,
            akshare_count=1,
            ticker_errors=["600000.SS: timeout"],
            benchmark_tickers=["000300.SS", "000905.SS"],
            elapsed_seconds=12.5,
        )

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            md_path, json_path = write_qfq_backfill_report(result, tmp)

            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())

            md_text = md_path.read_text()
            self.assertIn("QFQ Backfill Report", md_text)
            self.assertIn("600000.SS: timeout", md_text)
            self.assertIn("000300.SS", md_text)

            import json
            data = json.loads(json_path.read_text())
            self.assertEqual(data["total_tickers"], 10)
            self.assertEqual(data["updated_rows"], 500)


# ---------------------------------------------------------------------------
# Test: amount/turnover integration in _update_adjusted_bars
# ---------------------------------------------------------------------------

class TestUpdateAdjustedBarsWithAmountTurnover(unittest.TestCase):
    """Test that _update_adjusted_bars fills amount and turnover_pct."""

    def test_fills_amount_and_turnover_from_map(self) -> None:
        """When map has amount/turnover_pct and row has none, they are filled."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")

        adj_map = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
                "amount": 5000000.0, "turnover_pct": 1.5,
            },
        }
        updated = _update_adjusted_bars(conn, "600519.SS", adj_map)
        self.assertEqual(updated, 1)

        row = conn.execute(
            "SELECT adj_close, amount, turnover_pct, adjustment_status "
            "FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_close"], 11.0)
        self.assertAlmostEqual(row["amount"], 5000000.0)
        self.assertAlmostEqual(row["turnover_pct"], 1.5)
        self.assertEqual(row["adjustment_status"], "ADJUSTED")

    def test_does_not_overwrite_existing_positive_amount(self) -> None:
        """Existing positive amount should not be overwritten."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")
        conn.execute(
            "UPDATE price_bars SET amount = 999.0 WHERE ticker='600519.SS' AND date='2026-01-02'"
        )
        conn.commit()

        adj_map = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
                "amount": 5000000.0, "turnover_pct": 1.5,
            },
        }
        _update_adjusted_bars(conn, "600519.SS", adj_map)

        row = conn.execute(
            "SELECT amount, turnover_pct FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["amount"], 999.0)  # preserved
        self.assertAlmostEqual(row["turnover_pct"], 1.5)  # filled (was NULL)

    def test_does_not_overwrite_existing_turnover_pct(self) -> None:
        """Existing turnover_pct should not be overwritten."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")
        conn.execute(
            "UPDATE price_bars SET turnover_pct = 3.0 WHERE ticker='600519.SS' AND date='2026-01-02'"
        )
        conn.commit()

        adj_map = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
                "amount": 5000000.0, "turnover_pct": 1.5,
            },
        }
        _update_adjusted_bars(conn, "600519.SS", adj_map)

        row = conn.execute(
            "SELECT amount, turnover_pct FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["amount"], 5000000.0)  # filled (was NULL)
        self.assertAlmostEqual(row["turnover_pct"], 3.0)  # preserved

    def test_fills_amount_when_existing_is_zero(self) -> None:
        """Existing amount=0 should be overwritten (treated as missing)."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")
        conn.execute(
            "UPDATE price_bars SET amount = 0 WHERE ticker='600519.SS' AND date='2026-01-02'"
        )
        conn.commit()

        adj_map = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
                "amount": 5000000.0,
            },
        }
        _update_adjusted_bars(conn, "600519.SS", adj_map)

        row = conn.execute(
            "SELECT amount FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["amount"], 5000000.0)  # filled

    def test_no_amount_turnover_in_map_still_updates_adj(self) -> None:
        """When map has no amount/turnover_pct, adj_* still updates normally."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")

        adj_map = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
            },
        }
        updated = _update_adjusted_bars(conn, "600519.SS", adj_map)
        self.assertEqual(updated, 1)

        row = conn.execute(
            "SELECT adj_close, amount, turnover_pct, adjustment_status "
            "FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_close"], 11.0)
        self.assertEqual(row["adjustment_status"], "ADJUSTED")
        # amount and turnover_pct remain NULL (not in map)
        self.assertIsNone(row["amount"])
        self.assertIsNone(row["turnover_pct"])

    def test_none_amount_turnover_does_not_clear_existing(self) -> None:
        """Explicit None values in map should not clear existing data."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")
        conn.execute(
            "UPDATE price_bars SET amount = 999.0, turnover_pct = 2.0 "
            "WHERE ticker='600519.SS' AND date='2026-01-02'"
        )
        conn.commit()

        adj_map = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
                "amount": None, "turnover_pct": None,
            },
        }
        _update_adjusted_bars(conn, "600519.SS", adj_map)

        row = conn.execute(
            "SELECT amount, turnover_pct FROM price_bars WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["amount"], 999.0)  # preserved
        self.assertAlmostEqual(row["turnover_pct"], 2.0)  # preserved


# ---------------------------------------------------------------------------
# Test: BaoStock fetcher returns amount/turnover_pct fields
# ---------------------------------------------------------------------------

class TestBaoStockAmountTurnoverParsing(unittest.TestCase):
    """Test that fetch_baostock_cn_adjusted_daily_map parses amount/turn."""

    @patch("alpha_ledger.market_data._ensure_baostock_login")
    @patch("alpha_ledger.market_data._cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_parses_10_field_rows(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """New 10-field rows are fully parsed including amount and turnover_pct."""
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument

        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.side_effect = [True, False]
        # [date, open, high, low, close, volume, amount, turn, tradestatus, isST]
        mock_rs.get_row_data.return_value = [
            "2026-01-02", "11.0", "12.0", "10.0", "11.5",
            "1000000", "50000000.00", "1.23", "1", "0"
        ]
        mock_bs.query_history_k_data_plus.return_value = mock_rs

        instrument = Instrument(
            market="CN_A", ticker="600519.SS", name="test",
            source="sina_cn", source_symbol="sh600519",
            active=True, tags=(),
        )

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = fetch_baostock_cn_adjusted_daily_map(
                instrument, date(2026, 1, 1), date(2026, 1, 31)
            )

        self.assertIn("2026-01-02", result)
        entry = result["2026-01-02"]
        self.assertAlmostEqual(entry["adj_open"], 11.0)
        self.assertAlmostEqual(entry["adj_high"], 12.0)
        self.assertAlmostEqual(entry["adj_low"], 10.0)
        self.assertAlmostEqual(entry["adj_close"], 11.5)
        self.assertAlmostEqual(entry["amount"], 50000000.0)
        self.assertAlmostEqual(entry["turnover_pct"], 1.23)

    @patch("alpha_ledger.market_data._ensure_baostock_login")
    @patch("alpha_ledger.market_data._cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_handles_old_5_field_rows(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """Old 5-field rows still work; amount/turnover_pct not in result."""
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument

        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.side_effect = [True, False]
        mock_rs.get_row_data.return_value = [
            "2026-01-02", "11.0", "12.0", "10.0", "11.5"
        ]
        mock_bs.query_history_k_data_plus.return_value = mock_rs

        instrument = Instrument(
            market="CN_A", ticker="600519.SS", name="test",
            source="sina_cn", source_symbol="sh600519",
            active=True, tags=(),
        )

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = fetch_baostock_cn_adjusted_daily_map(
                instrument, date(2026, 1, 1), date(2026, 1, 31)
            )

        entry = result["2026-01-02"]
        self.assertAlmostEqual(entry["adj_close"], 11.5)
        self.assertNotIn("amount", entry)
        self.assertNotIn("turnover_pct", entry)

    @patch("alpha_ledger.market_data._ensure_baostock_login")
    @patch("alpha_ledger.market_data._cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_skips_empty_optional_fields(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """Empty amount/turn fields are skipped, adj_* still parsed."""
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument

        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.side_effect = [True, False]
        mock_rs.get_row_data.return_value = [
            "2026-01-02", "11.0", "12.0", "10.0", "11.5",
            "1000000", "", "", "1", "0"
        ]
        mock_bs.query_history_k_data_plus.return_value = mock_rs

        instrument = Instrument(
            market="CN_A", ticker="600519.SS", name="test",
            source="sina_cn", source_symbol="sh600519",
            active=True, tags=(),
        )

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = fetch_baostock_cn_adjusted_daily_map(
                instrument, date(2026, 1, 1), date(2026, 1, 31)
            )

        entry = result["2026-01-02"]
        self.assertAlmostEqual(entry["adj_close"], 11.5)
        self.assertNotIn("amount", entry)
        self.assertNotIn("turnover_pct", entry)

    @patch("alpha_ledger.market_data._ensure_baostock_login")
    @patch("alpha_ledger.market_data._cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_skips_non_numeric_optional_fields(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """Non-numeric amount/turn are skipped without breaking adj_*."""
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument

        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.side_effect = [True, False]
        mock_rs.get_row_data.return_value = [
            "2026-01-02", "11.0", "12.0", "10.0", "11.5",
            "1000000", "N/A", "bad", "1", "0"
        ]
        mock_bs.query_history_k_data_plus.return_value = mock_rs

        instrument = Instrument(
            market="CN_A", ticker="600519.SS", name="test",
            source="sina_cn", source_symbol="sh600519",
            active=True, tags=(),
        )

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = fetch_baostock_cn_adjusted_daily_map(
                instrument, date(2026, 1, 1), date(2026, 1, 31)
            )

        entry = result["2026-01-02"]
        self.assertAlmostEqual(entry["adj_close"], 11.5)
        self.assertNotIn("amount", entry)
        self.assertNotIn("turnover_pct", entry)

    @patch("alpha_ledger.market_data._ensure_baostock_login")
    @patch("alpha_ledger.market_data._cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_partial_optional_fields(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """Only amount present, turn empty → only amount in result."""
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument

        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.side_effect = [True, False]
        mock_rs.get_row_data.return_value = [
            "2026-01-02", "11.0", "12.0", "10.0", "11.5",
            "1000000", "50000000.00", "", "1", "0"
        ]
        mock_bs.query_history_k_data_plus.return_value = mock_rs

        instrument = Instrument(
            market="CN_A", ticker="600519.SS", name="test",
            source="sina_cn", source_symbol="sh600519",
            active=True, tags=(),
        )

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = fetch_baostock_cn_adjusted_daily_map(
                instrument, date(2026, 1, 1), date(2026, 1, 31)
            )

        entry = result["2026-01-02"]
        self.assertAlmostEqual(entry["amount"], 50000000.0)
        self.assertNotIn("turnover_pct", entry)

    @patch("alpha_ledger.market_data._ensure_baostock_login")
    @patch("alpha_ledger.market_data._cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_requests_amount_and_turn_fields(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """Verify the BaoStock query requests amount and turn fields."""
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument

        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.return_value = False
        mock_bs.query_history_k_data_plus.return_value = mock_rs

        instrument = Instrument(
            market="CN_A", ticker="600519.SS", name="test",
            source="sina_cn", source_symbol="sh600519",
            active=True, tags=(),
        )

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            fetch_baostock_cn_adjusted_daily_map(
                instrument, date(2026, 1, 1), date(2026, 1, 31)
            )

        call_args = mock_bs.query_history_k_data_plus.call_args
        fields_str = call_args[0][1]
        self.assertIn("amount", fields_str)
        self.assertIn("turn", fields_str)
        self.assertIn("tradestatus", fields_str)
        self.assertIn("isST", fields_str)
        self.assertEqual(call_args[1]["adjustflag"], "2")


# ---------------------------------------------------------------------------
# Test: VWAP sanity check
# ---------------------------------------------------------------------------

class TestVwapSanityCheck(unittest.TestCase):
    """Test vwap_sanity_check helper."""

    def test_counts_suspicious_ratios(self) -> None:
        conn = _make_db()
        # Normal: vwap = 100000/10000 = 10.0, close=10.0 → ratio=1.0
        conn.execute(
            "INSERT INTO price_bars "
            "(market, ticker, date, open, close, high, low, volume, amount, "
            "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
            "VALUES ('CN_A', '600519.SS', '2026-01-02', 10, 10, 10, 10, 10000, 100000, "
            "10, 10, 10, 10, 1.0, 'ADJUSTED')"
        )
        # Suspicious low: vwap = 100/10000 = 0.01, close=10.0 → ratio=0.001 < 0.2
        conn.execute(
            "INSERT INTO price_bars "
            "(market, ticker, date, open, close, high, low, volume, amount, "
            "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
            "VALUES ('CN_A', '600519.SS', '2026-01-03', 10, 10, 10, 10, 10000, 100, "
            "10, 10, 10, 10, 1.0, 'ADJUSTED')"
        )
        # Suspicious high: vwap = 1000000/10000 = 100, close=10.0 → ratio=10 > 5
        conn.execute(
            "INSERT INTO price_bars "
            "(market, ticker, date, open, close, high, low, volume, amount, "
            "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
            "VALUES ('CN_A', '600519.SS', '2026-01-04', 10, 10, 10, 10, 10000, 1000000, "
            "10, 10, 10, 10, 1.0, 'ADJUSTED')"
        )
        conn.commit()

        result = vwap_sanity_check(conn)
        self.assertEqual(result["total_checked"], 3)
        self.assertEqual(result["suspicious_low"], 1)
        self.assertEqual(result["suspicious_high"], 1)
        self.assertEqual(result["suspicious_total"], 2)

    def test_skips_null_amount(self) -> None:
        conn = _make_db()
        conn.execute(
            "INSERT INTO price_bars "
            "(market, ticker, date, open, close, high, low, volume, amount, "
            "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
            "VALUES ('CN_A', '600519.SS', '2026-01-02', 10, 10, 10, 10, 10000, NULL, "
            "10, 10, 10, 10, 1.0, 'ADJUSTED')"
        )
        conn.commit()

        result = vwap_sanity_check(conn)
        self.assertEqual(result["total_checked"], 0)

    def test_date_range_filter(self) -> None:
        conn = _make_db()
        for d in ["2026-01-02", "2026-06-02"]:
            conn.execute(
                "INSERT INTO price_bars "
                "(market, ticker, date, open, close, high, low, volume, amount, "
                "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
                f"VALUES ('CN_A', '600519.SS', '{d}', 10, 10, 10, 10, 10000, 100000, "
                "10, 10, 10, 10, 1.0, 'ADJUSTED')"
            )
        conn.commit()

        result = vwap_sanity_check(conn, start="2026-03-01")
        self.assertEqual(result["total_checked"], 1)

    def test_ticker_filter(self) -> None:
        conn = _make_db()
        for t in ["600519.SS", "000001.SZ"]:
            conn.execute(
                "INSERT INTO price_bars "
                "(market, ticker, date, open, close, high, low, volume, amount, "
                "adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status) "
                f"VALUES ('CN_A', '{t}', '2026-01-02', 10, 10, 10, 10, 10000, 100000, "
                "10, 10, 10, 10, 1.0, 'ADJUSTED')"
            )
        conn.commit()

        result = vwap_sanity_check(conn, ticker="600519.SS")
        self.assertEqual(result["total_checked"], 1)


# ---------------------------------------------------------------------------
# Test: end-to-end amount/turnover via mocked BaoStock
# ---------------------------------------------------------------------------

class TestQfqBackfillAmountTurnoverIntegration(unittest.TestCase):
    """Test that the full qfq_backfill path fills amount/turnover_pct."""

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_baostock_amount_turnover_fills_bars(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")

        mock_bs.return_value = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
                "amount": 50000000.0, "turnover_pct": 1.23,
            },
        }

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)
        self.assertEqual(result.updated_rows, 1)

        row = conn.execute(
            "SELECT adj_close, amount, turnover_pct FROM price_bars "
            "WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_close"], 11.0)
        self.assertAlmostEqual(row["amount"], 50000000.0)
        self.assertAlmostEqual(row["turnover_pct"], 1.23)

    @patch("alpha_ledger.qfq_backfill._baostock_logout")
    @patch("alpha_ledger.qfq_backfill.fetch_baostock_cn_adjusted_daily_map")
    def test_baostock_no_amount_turnover_still_works(self, mock_bs: MagicMock, mock_logout: MagicMock) -> None:
        """AkShare-sourced maps without amount/turnover_pct still update adj_*."""
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", close=10.0, status="RAW_FALLBACK")

        mock_bs.return_value = {
            "2026-01-02": {
                "adj_open": 11.0, "adj_close": 11.0,
                "adj_high": 11.0, "adj_low": 11.0,
            },
        }

        result = qfq_backfill(conn, "2026-01-01", "2026-01-31", source="baostock", throttle=0)
        self.assertEqual(result.updated_rows, 1)

        row = conn.execute(
            "SELECT adj_close, amount, turnover_pct FROM price_bars "
            "WHERE ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["adj_close"], 11.0)
        self.assertIsNone(row["amount"])
        self.assertIsNone(row["turnover_pct"])


if __name__ == "__main__":
    unittest.main()
