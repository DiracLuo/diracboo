from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_ledger.adjustments import (
    CONFIRMED_BY_PRECLOSE,
    SUSPECTED_BY_PRICE_GAP,
    detect_adjustment_breaks,
    get_price_frame,
    qfq_repair_breaks,
)
from alpha_ledger.db import connect, init_db
from alpha_ledger.qlib_export import export_qlib_csv


class AdjustmentFactorTest(unittest.TestCase):
    def _insert_bar(
        self,
        conn: sqlite3.Connection,
        ticker: str,
        date: str,
        close: float,
        *,
        pre_close: float | None = None,
        change_pct: float | None = None,
        adj_factor: float = 1.0,
        status: str = "RAW_FALLBACK",
    ) -> None:
        conn.execute(
            """
            INSERT INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', ?, ?, 'test', ?, 1, '[]', 'now')
            ON CONFLICT(market, ticker) DO NOTHING
            """,
            (ticker, ticker, ticker),
        )
        conn.execute(
            """
            INSERT INTO price_bars (
                market, ticker, date, open, high, low, close, volume, amount,
                pre_close, change_pct, adj_open, adj_high, adj_low, adj_close,
                adj_factor, adjustment_status
            ) VALUES ('CN_A', ?, ?, ?, ?, ?, ?, 1000, 10000,
                      ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                date,
                close,
                close,
                close,
                close,
                pre_close,
                change_pct,
                close * adj_factor,
                close * adj_factor,
                close * adj_factor,
                close * adj_factor,
                adj_factor,
                status,
            ),
        )

    def test_preclose_detect_and_repair_301213_keeps_raw_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(
                    conn,
                    "301213.SZ",
                    "2026-06-05",
                    46.89,
                    pre_close=45.61,
                    change_pct=2.8064,
                )
                detected = detect_adjustment_breaks(conn, "2026-06-05")
                self.assertEqual(detected.confirmed, 1)
                queue = conn.execute(
                    "SELECT reason FROM adjustment_maintenance_queue WHERE ticker='301213.SZ'"
                ).fetchone()
                self.assertEqual(queue["reason"], CONFIRMED_BY_PRECLOSE)

                with patch(
                    "alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map",
                    return_value={
                        "2026-06-04": {"adj_open": 45.61, "adj_high": 45.61, "adj_low": 45.61, "adj_close": 45.61},
                        "2026-06-05": {"adj_open": 46.89, "adj_high": 46.89, "adj_low": 46.89, "adj_close": 46.89},
                    },
                ), patch("alpha_ledger.adjustments._baostock_logout"):
                    repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(repaired.repaired_count, 1)
                raw = conn.execute(
                    "SELECT close, adj_close, adj_factor FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(raw["close"], 63.95)
                self.assertAlmostEqual(raw["adj_close"], 45.61, places=2)
                self.assertAlmostEqual(raw["adj_factor"], 45.61 / 63.95, places=6)
                current = conn.execute(
                    "SELECT close, adj_close, adjustment_status FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-05'"
                ).fetchone()
                self.assertAlmostEqual(current["close"], 46.89)
                self.assertAlmostEqual(current["adj_close"], 46.89)
                self.assertEqual(current["adjustment_status"], "ADJUSTED")

    def test_preclose_detect_and_repair_003026(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "003026.SZ", "2026-06-04", 46.47)
                self._insert_bar(
                    conn,
                    "003026.SZ",
                    "2026-06-05",
                    31.52,
                    pre_close=31.94,
                    change_pct=-1.315,
                )
                detect_adjustment_breaks(conn, "2026-06-05")
                with patch(
                    "alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map",
                    return_value={
                        "2026-06-04": {"adj_open": 31.94, "adj_high": 31.94, "adj_low": 31.94, "adj_close": 31.94},
                        "2026-06-05": {"adj_open": 31.52, "adj_high": 31.52, "adj_low": 31.52, "adj_close": 31.52},
                    },
                ), patch("alpha_ledger.adjustments._baostock_logout"):
                    repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(repaired.repaired_count, 1)
                row = conn.execute(
                    "SELECT close, adj_close FROM price_bars WHERE ticker='003026.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(row["close"], 46.47)
                self.assertAlmostEqual(row["adj_close"], 31.94, places=2)

    def test_missing_preclose_is_suspected_not_auto_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(
                    conn,
                    "301213.SZ",
                    "2026-06-05",
                    46.89,
                    pre_close=None,
                    change_pct=2.80640210480159,
                )
                detected = detect_adjustment_breaks(conn, "2026-06-05")
                self.assertEqual(detected.confirmed, 0)
                self.assertEqual(detected.suspected, 1)
                queue = conn.execute(
                    "SELECT reason, pre_close FROM adjustment_maintenance_queue WHERE ticker='301213.SZ'"
                ).fetchone()
                self.assertEqual(queue["reason"], SUSPECTED_BY_PRICE_GAP)
                self.assertIsNone(queue["pre_close"])

                repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(repaired.target_count, 0)
                self.assertEqual(repaired.repaired_count, 0)
                row = conn.execute(
                    "SELECT close, adj_close, adjustment_source FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(row["close"], 63.95)
                self.assertAlmostEqual(row["adj_close"], 63.95, places=2)
                self.assertIsNone(row["adjustment_source"])

    def test_detection_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                detect_adjustment_breaks(conn, "2026-06-05")
                detect_adjustment_breaks(conn, "2026-06-05")
                count = conn.execute("SELECT COUNT(*) AS n FROM adjustment_maintenance_queue").fetchone()["n"]
                self.assertEqual(count, 1)

    def test_break_repair_refreshes_existing_factor_from_baostock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "000001.SZ", "2026-06-04", 10.0, adj_factor=0.5, status="ADJUSTED")
                self._insert_bar(
                    conn,
                    "000001.SZ",
                    "2026-06-05",
                    8.1,
                    pre_close=8.0,
                    change_pct=1.25,
                    adj_factor=0.5,
                    status="ADJUSTED",
                )
                detect_adjustment_breaks(conn, "2026-06-05")
                with patch(
                    "alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map",
                    return_value={
                        "2026-06-04": {"adj_open": 8.0, "adj_high": 8.0, "adj_low": 8.0, "adj_close": 8.0},
                        "2026-06-05": {"adj_open": 8.1, "adj_high": 8.1, "adj_low": 8.1, "adj_close": 8.1},
                    },
                ), patch("alpha_ledger.adjustments._baostock_logout"):
                    qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                row = conn.execute(
                    "SELECT adj_factor, adj_close FROM price_bars WHERE ticker='000001.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(row["adj_factor"], 0.8)
                self.assertAlmostEqual(row["adj_close"], 8.0)

    def test_detect_keeps_baostock_repaired_break_done_without_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95, adj_factor=45.61 / 63.95, status="ADJUSTED")
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                conn.execute(
                    """
                    UPDATE price_bars
                    SET adjustment_source='baostock_qfq_break_repair'
                    WHERE ticker='301213.SZ'
                    """
                )
                detect_adjustment_breaks(conn, "2026-06-05")
                repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(repaired.target_count, 0)
                row = conn.execute(
                    "SELECT adj_factor, adj_close FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(row["adj_factor"], 45.61 / 63.95, places=6)
                self.assertAlmostEqual(row["adj_close"], 45.61, places=2)

    def test_detect_after_repair_does_not_requeue_same_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(
                    conn,
                    "301213.SZ",
                    "2026-06-05",
                    46.89,
                    pre_close=45.61,
                    change_pct=2.80640210480159,
                )
                first = detect_adjustment_breaks(conn, "2026-06-05")
                self.assertEqual(first.confirmed, 1)
                with patch(
                    "alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map",
                    return_value={
                        "2026-06-04": {"adj_open": 45.61, "adj_high": 45.61, "adj_low": 45.61, "adj_close": 45.61},
                        "2026-06-05": {"adj_open": 46.89, "adj_high": 46.89, "adj_low": 46.89, "adj_close": 46.89},
                    },
                ), patch("alpha_ledger.adjustments._baostock_logout"):
                    repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(repaired.repaired_count, 1)

                second = detect_adjustment_breaks(conn, "2026-06-05")
                self.assertEqual(second.confirmed, 1)
                self.assertEqual(second.suspected, 0)
                self.assertEqual(second.queued, 0)
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM adjustment_maintenance_queue WHERE ticker='301213.SZ'"
                ).fetchone()["n"]
                self.assertEqual(count, 1)

    def test_get_price_frame_and_qlib_export_use_raw_times_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95, adj_factor=45.61 / 63.95, status="ADJUSTED")
                frame = get_price_frame(conn, "CN_A", ["301213.SZ"], "2026-06-04", "2026-06-04", price_mode="qfq")
                self.assertAlmostEqual(frame.rows[0]["close"], 45.61, places=2)
                out_dir = Path(tmpdir) / "qlib"
                export_qlib_csv(conn, "2026-06-04", "2026-06-04", out_dir)
                csv_path = out_dir / "SZ301213.csv"
                text = csv_path.read_text(encoding="utf-8")
                self.assertIn("2026-06-04", text)
                self.assertIn("45.61", text)

    def test_qfq_maintenance_scan_and_repair_command_writes_report(self) -> None:
        from alpha_ledger.cli import command_qfq_maintenance

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                conn.commit()
            with patch(
                "alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map",
                return_value={
                    "2026-06-04": {"adj_open": 45.61, "adj_high": 45.61, "adj_low": 45.61, "adj_close": 45.61},
                    "2026-06-05": {"adj_open": 46.89, "adj_high": 46.89, "adj_low": 46.89, "adj_close": 46.89},
                },
            ), patch("alpha_ledger.adjustments._baostock_logout"):
                command_qfq_maintenance(
                    str(db_path),
                    "2026-06-05",
                    interval_days=14,
                    lookback_days=2,
                    source="auto",
                    throttle=0.0,
                    limit=None,
                    commit_every=50,
                    out_dir=str(Path(tmpdir) / "reports"),
                    force=True,
                    dry_run=False,
                    mode="scan-and-repair",
                )
            self.assertTrue((Path(tmpdir) / "reports" / "2026-06-05" / "summary.md").exists())
            with connect(db_path) as conn:
                row = conn.execute(
                    "SELECT adj_close FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(row["adj_close"], 45.61, places=2)

    def test_qfq_repair_breaks_only_processes_confirmed_preclose_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95, status="ADJUSTED")
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                self._insert_bar(conn, "003026.SZ", "2026-06-04", 46.47, status="ADJUSTED")
                self._insert_bar(conn, "003026.SZ", "2026-06-05", 31.52, pre_close=31.94, change_pct=-1.315)
                self._insert_bar(conn, "000001.SZ", "2026-06-04", 10.0)
                self._insert_bar(conn, "000001.SZ", "2026-06-05", 8.0, pre_close=None, change_pct=1.0)
                detect_adjustment_breaks(conn, "2026-06-05")

                def fake_fetch(instrument, start, end, adjust="qfq"):
                    if instrument.ticker == "301213.SZ":
                        return {
                            "2026-06-04": {"adj_open": 45.61, "adj_high": 45.61, "adj_low": 45.61, "adj_close": 45.61},
                            "2026-06-05": {"adj_open": 46.89, "adj_high": 46.89, "adj_low": 46.89, "adj_close": 46.89},
                        }
                    if instrument.ticker == "003026.SZ":
                        return {
                            "2026-06-04": {"adj_open": 31.94, "adj_high": 31.94, "adj_low": 31.94, "adj_close": 31.94},
                            "2026-06-05": {"adj_open": 31.52, "adj_high": 31.52, "adj_low": 31.52, "adj_close": 31.52},
                        }
                    raise AssertionError(f"unexpected ticker {instrument.ticker}")

                with patch("alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map", side_effect=fake_fetch) as fetch, \
                    patch("alpha_ledger.adjustments._baostock_logout"):
                    repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)

                self.assertEqual(repaired.target_count, 2)
                self.assertEqual(repaired.repaired_count, 2)
                self.assertEqual(fetch.call_count, 2)
                suspected = conn.execute(
                    "SELECT status FROM adjustment_maintenance_queue WHERE ticker='000001.SZ'"
                ).fetchone()
                self.assertEqual(suspected["status"], "PENDING")

    def test_qfq_repair_breaks_preserves_raw_spot_fields_and_refreshes_adjusted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95, adj_factor=1.0, status="ADJUSTED")
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                conn.execute(
                    """
                    UPDATE price_bars
                    SET amount=123456, volume=789, pre_close=45.61, change_pct=2.8064,
                        change_amount=1.28, bid_price=46.88, ask_price=46.90, quote_time='15:00:00'
                    WHERE ticker='301213.SZ' AND date='2026-06-05'
                    """
                )
                detect_adjustment_breaks(conn, "2026-06-05")
                before = dict(conn.execute(
                    """
                    SELECT open, high, low, close, volume, amount, pre_close, change_pct,
                           change_amount, bid_price, ask_price, quote_time
                    FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-05'
                    """
                ).fetchone())
                with patch(
                    "alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map",
                    return_value={
                        "2026-06-04": {"adj_open": 45.61, "adj_high": 45.61, "adj_low": 45.61, "adj_close": 45.61},
                        "2026-06-05": {"adj_open": 46.89, "adj_high": 46.89, "adj_low": 46.89, "adj_close": 46.89},
                    },
                ), patch("alpha_ledger.adjustments._baostock_logout"):
                    repaired = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(repaired.updated_rows, 2)
                after = dict(conn.execute(
                    """
                    SELECT open, high, low, close, volume, amount, pre_close, change_pct,
                           change_amount, bid_price, ask_price, quote_time
                    FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-05'
                    """
                ).fetchone())
                self.assertEqual(after, before)
                adjusted = conn.execute(
                    """
                    SELECT adj_close, adj_factor, adjustment_status, adjustment_source
                    FROM price_bars WHERE ticker='301213.SZ' AND date='2026-06-04'
                    """
                ).fetchone()
                self.assertAlmostEqual(adjusted["adj_close"], 45.61, places=2)
                self.assertAlmostEqual(adjusted["adj_factor"], 45.61 / 63.95, places=6)
                self.assertEqual(adjusted["adjustment_status"], "ADJUSTED")
                self.assertEqual(adjusted["adjustment_source"], "baostock_qfq_break_repair")

    def test_qfq_repair_breaks_dry_run_does_not_fetch_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                detect_adjustment_breaks(conn, "2026-06-05")
                with patch("alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map") as fetch:
                    result = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", dry_run=True)
                self.assertTrue(result.dry_run)
                self.assertEqual(result.target_count, 1)
                fetch.assert_not_called()
                queue = conn.execute(
                    "SELECT status FROM adjustment_maintenance_queue WHERE ticker='301213.SZ'"
                ).fetchone()
                self.assertEqual(queue["status"], "PENDING")

    def test_qfq_repair_breaks_records_missing_dates_and_failed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95)
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                self._insert_bar(conn, "003026.SZ", "2026-06-04", 46.47)
                self._insert_bar(conn, "003026.SZ", "2026-06-05", 31.52, pre_close=31.94, change_pct=-1.315)
                detect_adjustment_breaks(conn, "2026-06-05")

                def fake_fetch(instrument, start, end, adjust="qfq"):
                    if instrument.ticker == "301213.SZ":
                        return {
                            "2026-06-05": {"adj_open": 46.89, "adj_high": 46.89, "adj_low": 46.89, "adj_close": 46.89},
                        }
                    raise RuntimeError("network down")

                with patch("alpha_ledger.adjustments.fetch_baostock_cn_adjusted_daily_map", side_effect=fake_fetch), \
                    patch("alpha_ledger.adjustments._baostock_logout"):
                    result = qfq_repair_breaks(conn, "2026-06-05", start="2026-06-04", throttle=0.0)
                self.assertEqual(result.repaired_count, 1)
                self.assertEqual(result.failed_count, 1)
                self.assertEqual(result.missing_rows, 1)
                failed = conn.execute(
                    "SELECT status, error_message FROM adjustment_maintenance_queue WHERE ticker='003026.SZ'"
                ).fetchone()
                self.assertEqual(failed["status"], "FAILED")
                self.assertIn("network down", failed["error_message"])


if __name__ == "__main__":
    unittest.main()
