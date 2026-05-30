"""Tests for daily amount/turnover enrichment module."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from alpha_ledger.daily_enrichment import (
    DailyEnrichmentResult,
    _fetch_baostock_daily_enrichment_map,
    _is_benchmark,
    enrich_daily_bars,
    tickers_needing_enrichment,
    write_enrichment_report,
)
from alpha_ledger.db import init_db


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
    *,
    amount: float | None = None,
    turnover_pct: float | None = None,
    close: float = 10.0,
    volume: float = 1000.0,
    status: str = "ADJUSTED",
) -> None:
    """Insert a price_bars row with optional amount/turnover."""
    conn.execute(
        """
        INSERT INTO price_bars
            (market, ticker, date, open, close, high, low, volume,
             amount, turnover_pct,
             adj_open, adj_close, adj_high, adj_low, adj_factor,
             adjustment_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "CN_A", ticker, dt, close, close, close, close, volume,
            amount, turnover_pct,
            close, close, close, close, 1.0, status,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Test: _is_benchmark
# ---------------------------------------------------------------------------

class TestIsBenchmark(unittest.TestCase):
    def test_benchmarks_detected(self) -> None:
        self.assertTrue(_is_benchmark("000300.SS"))
        self.assertTrue(_is_benchmark("399006.SZ"))

    def test_regular_stocks_not_benchmarks(self) -> None:
        self.assertFalse(_is_benchmark("600519.SS"))
        self.assertFalse(_is_benchmark("002674.SZ"))


# ---------------------------------------------------------------------------
# Test: tickers_needing_enrichment
# ---------------------------------------------------------------------------

class TestTickersNeedingEnrichment(unittest.TestCase):
    def test_finds_null_amount(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None, turnover_pct=1.0)
        tickers = tickers_needing_enrichment(conn)
        self.assertEqual(tickers, ["600519.SS"])

    def test_finds_zero_amount(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=0.0, turnover_pct=1.0)
        tickers = tickers_needing_enrichment(conn)
        self.assertEqual(tickers, ["600519.SS"])

    def test_finds_null_turnover(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=100.0, turnover_pct=None)
        tickers = tickers_needing_enrichment(conn)
        self.assertEqual(tickers, ["600519.SS"])

    def test_excludes_enriched_rows(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=100.0, turnover_pct=1.5)
        _insert_bar(conn, "000001.SZ", "2026-01-02", amount=None, turnover_pct=None)
        tickers = tickers_needing_enrichment(conn)
        self.assertEqual(tickers, ["000001.SZ"])

    def test_date_range_filter(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None)
        _insert_bar(conn, "600519.SS", "2026-06-02", amount=None)
        tickers = tickers_needing_enrichment(conn, start="2026-03-01")
        self.assertEqual(tickers, ["600519.SS"])  # still distinct

    def test_tickers_filter(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None)
        _insert_bar(conn, "000001.SZ", "2026-01-02", amount=None)
        tickers = tickers_needing_enrichment(conn, tickers_filter={"600519.SS"})
        self.assertEqual(tickers, ["600519.SS"])

    def test_dot_sh_input_normalizes_to_dot_ss(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None)
        tickers = tickers_needing_enrichment(conn, tickers_filter={"600519.SH"})
        self.assertEqual(tickers, ["600519.SS"])


# ---------------------------------------------------------------------------
# Test: BaoStock enrichment map parsing
# ---------------------------------------------------------------------------

def _make_mock_baostock(rows: list[list[str]], error_code: str = "0"):
    """Create a mock baostock module with query_history_k_data_plus returning *rows*."""
    mock_bs = MagicMock()
    mock_rs = MagicMock()
    mock_rs.error_code = error_code
    mock_rs.next.side_effect = [True] * len(rows) + [False]
    mock_rs.get_row_data.side_effect = rows
    mock_bs.query_history_k_data_plus.return_value = mock_rs
    return mock_bs


class TestFetchBaostockEnrichmentMap(unittest.TestCase):
    """Test that BaoStock result rows are parsed into numeric fields."""

    @patch("alpha_ledger.daily_enrichment._ensure_baostock_login")
    @patch("alpha_ledger.tickers.cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_parses_amount_and_turn(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        mock_bs = _make_mock_baostock([
            ["2026-01-02", "sh.600519", "1000000", "50000000.00", "1.23", "2.50", "0"]
        ])

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = _fetch_baostock_daily_enrichment_map("600519.SS", date(2026, 1, 2), date(2026, 1, 2))

        self.assertIn("2026-01-02", result)
        self.assertAlmostEqual(result["2026-01-02"]["amount"], 50000000.0)
        self.assertAlmostEqual(result["2026-01-02"]["turnover_pct"], 1.23)

    @patch("alpha_ledger.daily_enrichment._ensure_baostock_login")
    @patch("alpha_ledger.tickers.cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_skips_empty_amount_and_turn(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        mock_bs = _make_mock_baostock([
            ["2026-01-02", "sh.600519", "1000000", "", "", "2.50", "0"]
        ])

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = _fetch_baostock_daily_enrichment_map("600519.SS", date(2026, 1, 2), date(2026, 1, 2))

        self.assertEqual(len(result), 0)

    @patch("alpha_ledger.daily_enrichment._ensure_baostock_login")
    @patch("alpha_ledger.tickers.cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_partial_data_stored(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """When only amount is available (turn is empty), store amount only."""
        mock_bs = _make_mock_baostock([
            ["2026-01-02", "sh.600519", "1000000", "50000000.00", "", "2.50", "0"]
        ])

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            result = _fetch_baostock_daily_enrichment_map("600519.SS", date(2026, 1, 2), date(2026, 1, 2))

        self.assertIn("2026-01-02", result)
        self.assertAlmostEqual(result["2026-01-02"]["amount"], 50000000.0)
        self.assertNotIn("turnover_pct", result["2026-01-02"])

    @patch("alpha_ledger.daily_enrichment._ensure_baostock_login")
    @patch("alpha_ledger.tickers.cn_a_to_baostock_symbol", return_value="sh.600519")
    def test_uses_adjustflag_3(self, mock_symbol: MagicMock, mock_login: MagicMock) -> None:
        """Verify we call BaoStock with adjustflag=3 (no adjustment)."""
        mock_bs = _make_mock_baostock([])

        with patch.dict("sys.modules", {"baostock": mock_bs}):
            _fetch_baostock_daily_enrichment_map("600519.SS", date(2026, 1, 2), date(2026, 1, 2))

        mock_bs.query_history_k_data_plus.assert_called_once()
        call_args = mock_bs.query_history_k_data_plus.call_args
        self.assertEqual(call_args[1]["adjustflag"], "3")
        self.assertEqual(call_args[1]["frequency"], "d")


# ---------------------------------------------------------------------------
# Test: updater preserves OHLC and adj_* fields
# ---------------------------------------------------------------------------

class TestUpdateEnrichedBars(unittest.TestCase):
    """Test that enrichment only modifies amount and turnover_pct."""

    def test_fills_amount_and_turnover(self) -> None:
        from alpha_ledger.daily_enrichment import _update_enriched_bars

        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None, turnover_pct=None, close=15.0)

        enrichment_map = {"2026-01-02": {"amount": 50000000.0, "turnover_pct": 1.23}}
        updated, missing = _update_enriched_bars(conn, "600519.SS", enrichment_map)

        self.assertEqual(updated, 1)
        self.assertEqual(missing, 0)

        row = conn.execute(
            "SELECT * FROM price_bars WHERE market='CN_A' AND ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        self.assertAlmostEqual(row["amount"], 50000000.0)
        self.assertAlmostEqual(row["turnover_pct"], 1.23)

    def test_preserves_ohlc_and_adj_fields(self) -> None:
        from alpha_ledger.daily_enrichment import _update_enriched_bars

        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None, turnover_pct=None, close=15.0)

        enrichment_map = {"2026-01-02": {"amount": 50000000.0, "turnover_pct": 1.23}}
        _update_enriched_bars(conn, "600519.SS", enrichment_map)

        row = conn.execute(
            "SELECT * FROM price_bars WHERE market='CN_A' AND ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        # Original values must be unchanged
        self.assertAlmostEqual(row["open"], 15.0)
        self.assertAlmostEqual(row["close"], 15.0)
        self.assertAlmostEqual(row["high"], 15.0)
        self.assertAlmostEqual(row["low"], 15.0)
        self.assertAlmostEqual(row["adj_open"], 15.0)
        self.assertAlmostEqual(row["adj_close"], 15.0)
        self.assertAlmostEqual(row["adj_high"], 15.0)
        self.assertAlmostEqual(row["adj_low"], 15.0)
        self.assertAlmostEqual(row["adj_factor"], 1.0)

    def test_does_not_overwrite_existing_amount(self) -> None:
        """Rows already having amount > 0 should not be touched."""
        from alpha_ledger.daily_enrichment import _update_enriched_bars

        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=99.0, turnover_pct=2.0, close=15.0)

        enrichment_map = {"2026-01-02": {"amount": 50000000.0, "turnover_pct": 1.23}}
        updated, missing = _update_enriched_bars(conn, "600519.SS", enrichment_map)

        self.assertEqual(updated, 0)
        row = conn.execute(
            "SELECT * FROM price_bars WHERE market='CN_A' AND ticker='600519.SS' AND date='2026-01-02'"
        ).fetchone()
        # Original values preserved
        self.assertAlmostEqual(row["amount"], 99.0)
        self.assertAlmostEqual(row["turnover_pct"], 2.0)

    def test_counts_missing_rows(self) -> None:
        from alpha_ledger.daily_enrichment import _update_enriched_bars

        conn = _make_db()
        # No row inserted for this date
        enrichment_map = {"2026-01-02": {"amount": 50000000.0, "turnover_pct": 1.23}}
        updated, missing = _update_enriched_bars(conn, "600519.SS", enrichment_map)

        self.assertEqual(updated, 0)
        self.assertEqual(missing, 1)


# ---------------------------------------------------------------------------
# Test: dry-run performs no network and no DB writes
# ---------------------------------------------------------------------------

class TestDryRunNoWrites(unittest.TestCase):
    def test_dry_run_no_network_no_db_writes(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None, turnover_pct=None)

        with patch("alpha_ledger.daily_enrichment._fetch_baostock_daily_enrichment_map") as mock_fetch:
            result = enrich_daily_bars(
                conn, "2026-01-01", "2026-12-31", dry_run=True
            )
            # Network fetch should NOT be called
            mock_fetch.assert_not_called()

        self.assertTrue(result.dry_run)
        self.assertEqual(result.updated_rows, 0)
        self.assertEqual(result.target_count, 1)

        # Verify DB is unchanged
        row = conn.execute(
            "SELECT amount, turnover_pct FROM price_bars WHERE market='CN_A' AND ticker='600519.SS'"
        ).fetchone()
        self.assertIsNone(row["amount"])
        self.assertIsNone(row["turnover_pct"])

    def test_dry_run_with_limit(self) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None)
        _insert_bar(conn, "000001.SZ", "2026-01-02", amount=None)

        result = enrich_daily_bars(
            conn, "2026-01-01", "2026-12-31", dry_run=True, limit=1
        )
        self.assertEqual(result.target_count, 1)


# ---------------------------------------------------------------------------
# Test: enrich_daily_bars live run with mocked BaoStock
# ---------------------------------------------------------------------------

class TestEnrichDailyBarsLiveRun(unittest.TestCase):
    @patch("alpha_ledger.daily_enrichment._baostock_logout")
    @patch("alpha_ledger.daily_enrichment._fetch_baostock_daily_enrichment_map")
    def test_live_run_updates_bars(self, mock_fetch: MagicMock, mock_logout: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None, turnover_pct=None)

        mock_fetch.return_value = {
            "2026-01-02": {"amount": 50000000.0, "turnover_pct": 1.23}
        }

        result = enrich_daily_bars(
            conn, "2026-01-01", "2026-12-31", throttle=0.0
        )

        self.assertEqual(result.updated_rows, 1)
        self.assertEqual(result.target_count, 1)
        self.assertEqual(result.skipped_errors, 0)
        mock_logout.assert_called_once()

        row = conn.execute(
            "SELECT amount, turnover_pct FROM price_bars WHERE market='CN_A' AND ticker='600519.SS'"
        ).fetchone()
        self.assertAlmostEqual(row["amount"], 50000000.0)
        self.assertAlmostEqual(row["turnover_pct"], 1.23)

    @patch("alpha_ledger.daily_enrichment._baostock_logout")
    @patch("alpha_ledger.daily_enrichment._fetch_baostock_daily_enrichment_map")
    def test_live_run_skips_benchmarks(self, mock_fetch: MagicMock, mock_logout: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "000300.SS", "2026-01-02", amount=None)

        result = enrich_daily_bars(
            conn, "2026-01-01", "2026-12-31", throttle=0.0
        )

        self.assertEqual(result.skipped_benchmarks, 1)
        self.assertEqual(result.target_count, 0)
        mock_fetch.assert_not_called()

    @patch("alpha_ledger.daily_enrichment._baostock_logout")
    @patch("alpha_ledger.daily_enrichment._fetch_baostock_daily_enrichment_map")
    def test_live_run_handles_errors(self, mock_fetch: MagicMock, mock_logout: MagicMock) -> None:
        conn = _make_db()
        _insert_bar(conn, "600519.SS", "2026-01-02", amount=None)

        mock_fetch.side_effect = Exception("network timeout")

        result = enrich_daily_bars(
            conn, "2026-01-01", "2026-12-31", throttle=0.0
        )

        self.assertEqual(result.skipped_errors, 1)
        self.assertIn("network timeout", result.ticker_errors[0])
        mock_logout.assert_called_once()


# ---------------------------------------------------------------------------
# Test: CLI parser and handler
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):
    def test_parser_has_enrich_daily_bars(self) -> None:
        from alpha_ledger.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "enrich-daily-bars",
            "--start", "2024-01-01",
            "--end", "2024-12-31",
        ])
        self.assertEqual(args.command, "enrich-daily-bars")
        self.assertEqual(args.start, "2024-01-01")
        self.assertEqual(args.end, "2024-12-31")
        self.assertFalse(args.dry_run)
        self.assertEqual(args.throttle, 0.3)
        self.assertIsNone(args.limit)
        self.assertIsNone(args.tickers)
        self.assertEqual(args.commit_every, 50)
        self.assertEqual(args.out_dir, "reports")

    def test_parser_dry_run_flag(self) -> None:
        from alpha_ledger.cli import build_parser
        parser = build_parser()
        args = parser.parse_args([
            "enrich-daily-bars",
            "--start", "2024-01-01",
            "--end", "2024-12-31",
            "--dry-run",
            "--limit", "5",
            "--tickers", "600519.SS,000001.SZ",
        ])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.limit, 5)
        self.assertEqual(args.tickers, "600519.SS,000001.SZ")

    @patch("alpha_ledger.cli.enrich_daily_bars")
    @patch("alpha_ledger.cli.write_enrichment_report")
    def test_handler_invokes_enrichment(self, mock_report: MagicMock, mock_enrich: MagicMock) -> None:
        from alpha_ledger.cli import command_enrich_daily_bars

        mock_result = DailyEnrichmentResult(
            start="2024-01-01", end="2024-12-31", dry_run=True,
            target_count=5, updated_rows=0,
        )
        mock_enrich.return_value = mock_result
        mock_report.return_value = (Path("/tmp/report.md"), Path("/tmp/report.json"))

        with patch("alpha_ledger.cli.connect") as mock_connect, \
             patch("alpha_ledger.cli.init_db"):
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_connect.return_value = mock_conn

            command_enrich_daily_bars(
                ":memory:", "2024-01-01", "2024-12-31",
                throttle=0.0, limit=None, tickers=None,
                commit_every=50, out_dir="reports", dry_run=True,
            )

        mock_enrich.assert_called_once()
        call_kwargs = mock_enrich.call_args
        self.assertTrue(call_kwargs[1]["dry_run"])


# ---------------------------------------------------------------------------
# Test: qlib_export uses real amount for vwap/money
# ---------------------------------------------------------------------------

class TestQlibExportWithEnrichedAmount(unittest.TestCase):
    """Verify that qlib_export uses real amount for vwap/money when available."""

    def _make_db_with_bars(self, bars: list[dict]) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        for bar in bars:
            conn.execute(
                """
                INSERT INTO price_bars
                    (market, ticker, date, open, close, high, low, volume,
                     amount, turnover_pct,
                     adj_open, adj_close, adj_high, adj_low, adj_factor,
                     adjustment_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bar.get("market", "CN_A"),
                    bar["ticker"],
                    bar["date"],
                    bar["open"],
                    bar["close"],
                    bar["high"],
                    bar["low"],
                    bar["volume"],
                    bar.get("amount"),
                    bar.get("turnover_pct"),
                    bar.get("adj_open", bar["open"]),
                    bar.get("adj_close", bar["close"]),
                    bar.get("adj_high", bar["high"]),
                    bar.get("adj_low", bar["low"]),
                    bar.get("adj_factor", 1.0),
                    bar.get("adjustment_status", "ADJUSTED"),
                ),
            )
        conn.commit()
        return conn

    def test_vwap_uses_real_amount_when_present(self) -> None:
        from alpha_ledger.qlib_export import export_qlib_csv

        bars = [{
            "ticker": "600519.SS", "date": "2026-01-02",
            "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0,
            "volume": 1000000.0, "amount": 50000000.0,
        }]
        conn = self._make_db_with_bars(bars)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-01-02", "2026-01-02", Path(tmpdir))
            csv_path = Path(tmpdir) / "SH600519.csv"
            lines = csv_path.read_text().strip().split("\n")
            # Skip header row
            data_line = lines[1]
            parts = data_line.split(",")
            vwap_val = parts[6]
            money_val = parts[7]
            # vwap should be amount/volume = 50000000/1000000 = 50.0
            # money should be amount = 50000000.0
            self.assertAlmostEqual(float(vwap_val), 50.0)
            self.assertAlmostEqual(float(money_val), 50000000.0)

    def test_vwap_falls_back_to_typical_price_when_amount_missing(self) -> None:
        from alpha_ledger.qlib_export import export_qlib_csv

        bars = [{
            "ticker": "600519.SS", "date": "2026-01-02",
            "open": 10.0, "close": 10.0, "high": 12.0, "low": 8.0,
            "volume": 1000000.0, "amount": None,
        }]
        conn = self._make_db_with_bars(bars)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_qlib_csv(conn, "2026-01-02", "2026-01-02", Path(tmpdir))
            csv_path = Path(tmpdir) / "SH600519.csv"
            lines = csv_path.read_text().strip().split("\n")
            # Skip header row
            data_line = lines[1]
            parts = data_line.split(",")
            vwap_val = parts[6]
            money_val = parts[7]
            # vwap = (12 + 8 + 10) / 3 = 10.0
            self.assertAlmostEqual(float(vwap_val), 10.0)
            # money should be empty when amount is None
            self.assertEqual(money_val, "")


# ---------------------------------------------------------------------------
# Test: report writing
# ---------------------------------------------------------------------------

class TestReportWriting(unittest.TestCase):
    def test_writes_md_and_json(self) -> None:
        result = DailyEnrichmentResult(
            start="2024-01-01", end="2024-12-31",
            total_tickers=100, target_count=80,
            skipped_benchmarks=5, skipped_errors=2,
            updated_rows=500, missing_rows=10,
            ticker_errors=["600519.SS: timeout"],
            benchmark_tickers=["000300.SS"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            md_path, json_path = write_enrichment_report(result, tmpdir)
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())

            md_content = md_path.read_text()
            self.assertIn("Daily Enrichment Report", md_content)
            self.assertIn("500", md_content)
            self.assertIn("600519.SS: timeout", md_content)

            import json
            data = json.loads(json_path.read_text())
            self.assertEqual(data["updated_rows"], 500)
            self.assertEqual(data["missing_rows"], 10)


if __name__ == "__main__":
    unittest.main()
