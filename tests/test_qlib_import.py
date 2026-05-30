"""Tests for Qlib predictions import functionality."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase

import pandas as pd

from alpha_ledger.qlib_import import (
    ImportResult,
    import_qlib_predictions,
    qlib_instrument_to_ticker,
    write_import_report,
)


class TickerReverseMappingTest(TestCase):
    def test_sh_to_ss(self):
        self.assertEqual(qlib_instrument_to_ticker("SH600519"), "600519.SS")
        self.assertEqual(qlib_instrument_to_ticker("SH000300"), "000300.SS")

    def test_sz_to_sz(self):
        self.assertEqual(qlib_instrument_to_ticker("SZ002674"), "002674.SZ")
        self.assertEqual(qlib_instrument_to_ticker("SZ399006"), "399006.SZ")

    def test_bj_to_bj(self):
        self.assertEqual(qlib_instrument_to_ticker("BJ430047"), "430047.BJ")

    def test_unknown_returns_none(self):
        self.assertIsNone(qlib_instrument_to_ticker("AAPL"))
        self.assertIsNone(qlib_instrument_to_ticker("US_AAPL"))


def _make_db() -> sqlite3.Connection:
    """Create an in-memory DB with model_scores schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE model_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            model_version TEXT NOT NULL,
            market TEXT NOT NULL,
            ticker TEXT NOT NULL,
            score_date TEXT NOT NULL,
            score REAL NOT NULL,
            rank INTEGER,
            percentile REAL,
            source_artifact TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(model_name, model_version, market, ticker, score_date)
        )
    """)
    conn.commit()
    return conn


def _make_pred_pkl(records: list[dict], path: Path) -> Path:
    """Create a mock pred.pkl file."""
    df = pd.DataFrame(records)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index(["datetime", "instrument"])
    df.columns = ["score"]
    pkl_path = path / "pred.pkl"
    df.to_pickle(str(pkl_path))
    return pkl_path


class ImportPredictionsTest(TestCase):
    def test_basic_import(self):
        conn = _make_db()
        with tempfile.TemporaryDirectory() as tmpdir:
            pkl_path = _make_pred_pkl(
                [
                    {"datetime": "2026-04-01", "instrument": "SH600519", "score": 0.8},
                    {"datetime": "2026-04-01", "instrument": "SZ002674", "score": 0.6},
                    {"datetime": "2026-04-02", "instrument": "SH600519", "score": 0.9},
                    {"datetime": "2026-04-02", "instrument": "SZ002674", "score": 0.5},
                ],
                Path(tmpdir),
            )
            result = import_qlib_predictions(
                conn, pkl_path, "test_model", "v1", "CN_A"
            )
            self.assertEqual(result.imported_count, 4)
            self.assertEqual(result.ticker_mapping_failures, 0)
            self.assertEqual(result.date_range, ("2026-04-01", "2026-04-02"))
            self.assertEqual(result.avg_stocks_per_day, 2.0)

            # Verify DB contents
            rows = conn.execute("SELECT * FROM model_scores ORDER BY score_date, rank").fetchall()
            self.assertEqual(len(rows), 4)
            # First day: SH600519 (0.8) rank=1, SZ002674 (0.6) rank=2
            self.assertEqual(rows[0]["ticker"], "600519.SS")
            self.assertEqual(rows[0]["rank"], 1)
            self.assertAlmostEqual(rows[0]["percentile"], 1.0, places=2)
            self.assertEqual(rows[1]["ticker"], "002674.SZ")
            self.assertEqual(rows[1]["rank"], 2)

    def test_rank_percentile_calculation(self):
        conn = _make_db()
        with tempfile.TemporaryDirectory() as tmpdir:
            pkl_path = _make_pred_pkl(
                [
                    {"datetime": "2026-04-01", "instrument": "SH600519", "score": 0.9},
                    {"datetime": "2026-04-01", "instrument": "SZ002674", "score": 0.7},
                    {"datetime": "2026-04-01", "instrument": "SZ000002", "score": 0.5},
                ],
                Path(tmpdir),
            )
            import_qlib_predictions(conn, pkl_path, "test", "v1")

            rows = conn.execute(
                "SELECT ticker, score, rank, percentile FROM model_scores ORDER BY rank"
            ).fetchall()
            self.assertEqual(rows[0]["rank"], 1)
            self.assertAlmostEqual(rows[0]["score"], 0.9)
            self.assertEqual(rows[1]["rank"], 2)
            self.assertEqual(rows[2]["rank"], 3)
            # percentile: rank ascending pct
            self.assertGreater(rows[0]["percentile"], rows[2]["percentile"])

    def test_duplicate_import_upserts(self):
        conn = _make_db()
        with tempfile.TemporaryDirectory() as tmpdir:
            records = [
                {"datetime": "2026-04-01", "instrument": "SH600519", "score": 0.8},
            ]
            pkl_path = _make_pred_pkl(records, Path(tmpdir))

            # Import twice
            import_qlib_predictions(conn, pkl_path, "test", "v1")
            import_qlib_predictions(conn, pkl_path, "test", "v1")

            rows = conn.execute("SELECT COUNT(*) as c FROM model_scores").fetchone()
            self.assertEqual(rows["c"], 1)  # UPSERT, not duplicate

    def test_ticker_mapping_failure(self):
        conn = _make_db()
        with tempfile.TemporaryDirectory() as tmpdir:
            pkl_path = _make_pred_pkl(
                [
                    {"datetime": "2026-04-01", "instrument": "SH600519", "score": 0.8},
                    {"datetime": "2026-04-01", "instrument": "AAPL", "score": 0.9},
                ],
                Path(tmpdir),
            )
            result = import_qlib_predictions(conn, pkl_path, "test", "v1")
            self.assertEqual(result.imported_count, 1)
            self.assertEqual(result.ticker_mapping_failures, 1)

    def test_missing_artifact(self):
        conn = _make_db()
        result = import_qlib_predictions(
            conn, Path("/nonexistent/pred.pkl"), "test", "v1"
        )
        self.assertEqual(result.imported_count, 0)
        self.assertTrue(any("not found" in w for w in result.warnings))

    def test_candidates_table_unchanged(self):
        """Verify import does not touch candidates table."""
        conn = _make_db()
        # Create a candidates table to verify it's untouched
        conn.execute("""
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY,
                candidate_score REAL
            )
        """)
        conn.execute("INSERT INTO candidates (id, candidate_score) VALUES (1, 85.0)")
        conn.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            pkl_path = _make_pred_pkl(
                [{"datetime": "2026-04-01", "instrument": "SH600519", "score": 0.8}],
                Path(tmpdir),
            )
            import_qlib_predictions(conn, pkl_path, "test", "v1")

        cand = conn.execute("SELECT * FROM candidates WHERE id=1").fetchone()
        self.assertEqual(cand["candidate_score"], 85.0)

    def test_import_report_json_valid(self):
        result = ImportResult(
            model_name="test",
            model_version="v1",
            artifact_path="/tmp/pred.pkl",
            imported_count=100,
            date_range=("2026-04-01", "2026-04-30"),
            ticker_mapping_failures=2,
            avg_stocks_per_day=50.0,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path, json_path = write_import_report(result, Path(tmpdir))
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text())
            self.assertEqual(data["model_name"], "test")
            self.assertEqual(data["imported_count"], 100)

    def test_import_report_md_content(self):
        result = ImportResult(
            model_name="test",
            model_version="v1",
            artifact_path="/tmp/pred.pkl",
            imported_count=50,
            date_range=("2026-04-01", "2026-04-10"),
            warnings=("Some warning",),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path, _ = write_import_report(result, Path(tmpdir))
            content = md_path.read_text()
            self.assertIn("test", content)
            self.assertIn("50", content)
            self.assertIn("Some warning", content)
