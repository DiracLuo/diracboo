from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from alpha_ledger.adjustments import (
    CONFIRMED_BY_PRECLOSE,
    SUSPECTED_BY_PRICE_GAP,
    detect_adjustment_breaks,
    get_price_frame,
    qfq_repair_daily,
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

                repaired = qfq_repair_daily(conn, "2026-06-05")
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
                repaired = qfq_repair_daily(conn, "2026-06-05")
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

                repaired = qfq_repair_daily(conn, "2026-06-05")
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

    def test_repair_multiplies_existing_factor_instead_of_overwriting(self) -> None:
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
                qfq_repair_daily(conn, "2026-06-05")
                row = conn.execute(
                    "SELECT adj_factor, adj_close FROM price_bars WHERE ticker='000001.SZ' AND date='2026-06-04'"
                ).fetchone()
                self.assertAlmostEqual(row["adj_factor"], 0.4)
                self.assertAlmostEqual(row["adj_close"], 4.0)

    def test_repair_does_not_double_adjust_already_continuous_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with connect(Path(tmpdir) / "test.sqlite") as conn:
                init_db(conn)
                self._insert_bar(conn, "301213.SZ", "2026-06-04", 63.95, adj_factor=45.61 / 63.95, status="ADJUSTED")
                self._insert_bar(conn, "301213.SZ", "2026-06-05", 46.89, pre_close=45.61, change_pct=2.8064)
                detect_adjustment_breaks(conn, "2026-06-05")
                repaired = qfq_repair_daily(conn, "2026-06-05")
                self.assertEqual(repaired.updated_rows, 0)
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
                repaired = qfq_repair_daily(conn, "2026-06-05")
                self.assertEqual(repaired.repaired_count, 1)

                second = detect_adjustment_breaks(conn, "2026-06-05")
                self.assertEqual(second.confirmed, 0)
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


if __name__ == "__main__":
    unittest.main()
