from __future__ import annotations

import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from alpha_ledger.cli import command_data_update, command_qfq_maintenance
from alpha_ledger.benchmarks import CN_A_BENCHMARKS
from alpha_ledger.data_ops import actionable_intraday_instruments, data_update
from alpha_ledger.db import connect, init_db
from alpha_ledger.market_data import Instrument
from alpha_ledger.pipeline_ops import (
    baseline18_specs,
    model_arena,
    model_evaluate,
    model_governance_review,
    model_predict,
    model_validate,
    production_async,
    production_daily,
    production_run,
    qlib_refresh,
)
from alpha_ledger.reporting import _gate_actionable_rows


def _seed_price_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO instruments
            (market, ticker, name, source, source_symbol, active, tags_json, created_at)
        VALUES ('CN_A', '600519.SS', '贵州茅台', 'test', 'sh600519', 1, '[]', datetime('now'))
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO price_bars
            (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
        VALUES ('CN_A', '600519.SS', '2026-06-03', 100, 101, 102, 99, 1000, 100000, 'RAW_FALLBACK')
        """
    )
    conn.commit()


class QlibRefreshTest(TestCase):
    def test_incremental_uses_dump_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            qlib_dir = Path(tmpdir) / "qlib"
            (qlib_dir / "calendars").mkdir(parents=True)
            (qlib_dir / "calendars" / "day.txt").write_text("2026-06-02\n", encoding="utf-8")
            dump_bin = Path(tmpdir) / "dump_bin.py"
            dump_bin.write_text("# test\n", encoding="utf-8")

            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)
                with patch("alpha_ledger.pipeline_ops.QLIB_DIR", qlib_dir), \
                     patch("alpha_ledger.pipeline_ops.DUMP_BIN", dump_bin), \
                     patch("alpha_ledger.pipeline_ops.subprocess.run") as mock_run:
                    mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                    result = qlib_refresh(conn, "2026-06-03", mode="incremental", output_root=Path(tmpdir) / "out")

                self.assertEqual(result.status, "SUCCESS")
                self.assertEqual(result.mode, "incremental")
                cmd = mock_run.call_args.args[0]
                self.assertIn("dump_update", cmd)
                self.assertNotIn("dump_all", cmd)
                row = conn.execute("SELECT mode, status FROM qlib_dataset_versions").fetchone()
                self.assertEqual(row["mode"], "incremental")
                self.assertEqual(row["status"], "SUCCESS")

    def test_full_uses_dump_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            qlib_dir = Path(tmpdir) / "qlib"
            dump_bin = Path(tmpdir) / "dump_bin.py"
            dump_bin.write_text("# test\n", encoding="utf-8")

            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)
                with patch("alpha_ledger.pipeline_ops.QLIB_DIR", qlib_dir), \
                     patch("alpha_ledger.pipeline_ops.DUMP_BIN", dump_bin), \
                     patch("alpha_ledger.pipeline_ops.subprocess.run") as mock_run:
                    mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                    result = qlib_refresh(conn, "2026-06-03", mode="full", output_root=Path(tmpdir) / "out")

                self.assertEqual(result.status, "SUCCESS")
                cmd = mock_run.call_args.args[0]
                self.assertIn("dump_all", cmd)
                self.assertNotIn("dump_update", cmd)


class DataUpdateCoreOnlyTest(TestCase):
    def test_core_only_disables_slow_tasks_and_qfq(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            result = SimpleNamespace(
                run_id=1,
                status="SUCCESS",
                start_date="2026-06-03",
                end_date="2026-06-03",
                requested_symbols=1,
                price_bars=1,
                intraday_bars=0,
                corporate_events=0,
                financial_metrics=0,
                money_flows=0,
                error_count=0,
            )
            with patch("alpha_ledger.cli.data_update", return_value=result) as mock_update:
                command_data_update(
                    db_path,
                    "2026-06-03",
                    "CN_A",
                    0.0,
                    "qfq",
                    False,
                    False,
                    "5",
                    False,
                    "benchmarks",
                    core_only=True,
                )
            kwargs = mock_update.call_args.kwargs
            self.assertFalse(kwargs["fetch_events"])
            self.assertFalse(kwargs["fetch_intraday"])
            self.assertEqual(kwargs["price_mode"], "core")
            self.assertIsNone(kwargs["adjust"])

    def test_core_price_mode_uses_spot_snapshot_for_stocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)

                def fake_spot(instruments: list[Instrument], as_of):
                    return [
                        {
                            "market": "CN_A",
                            "ticker": "600519.SS",
                            "date": as_of.isoformat(),
                            "open": 100.0,
                            "close": 101.0,
                            "high": 102.0,
                            "low": 99.0,
                            "volume": 1000.0,
                            "amount": 100000.0,
                            "change_pct": 1.0,
                            "adjustment_status": "RAW_FALLBACK",
                        }
                    ], []

                def fake_benchmarks(instruments, start, end, throttle_seconds=0.0, adjust=None):
                    rows = []
                    for instrument in instruments:
                        rows.append({
                            "market": "CN_A",
                            "ticker": instrument.ticker,
                            "date": end.isoformat(),
                            "open": 10.0,
                            "close": 10.1,
                            "high": 10.2,
                            "low": 9.9,
                            "volume": 1000.0,
                            "amount": None,
                            "change_pct": 1.0,
                            "adjustment_status": "ADJUSTED",
                        })
                    return rows, []

                with patch("alpha_ledger.data_ops.fetch_akshare_cn_spot_bars", side_effect=fake_spot) as mock_spot, \
                     patch("alpha_ledger.data_ops.fetch_bars", side_effect=fake_benchmarks) as mock_fetch:
                    result = data_update(
                        conn,
                        "2026-06-04",
                        fetch_events=False,
                        fetch_intraday=False,
                        price_mode="core",
                        adjust=None,
                    )

                self.assertEqual(result.status, "SUCCESS")
                self.assertEqual(result.price_bars, 1 + len(CN_A_BENCHMARKS))
                mock_spot.assert_called_once()
                mock_fetch.assert_called_once()
                row = conn.execute(
                    """
                    SELECT amount
                    FROM price_bars
                    WHERE market = 'CN_A' AND ticker = '600519.SS' AND date = '2026-06-04'
                    """
                ).fetchone()
            self.assertEqual(row["amount"], 100000.0)

    def test_core_price_mode_marks_missing_benchmarks_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)

                def fake_spot(instruments: list[Instrument], as_of):
                    return [
                        {
                            "market": "CN_A",
                            "ticker": "600519.SS",
                            "date": as_of.isoformat(),
                            "open": 100.0,
                            "close": 101.0,
                            "high": 102.0,
                            "low": 99.0,
                            "volume": 1000.0,
                            "amount": 100000.0,
                            "change_pct": 1.0,
                            "adjustment_status": "RAW_FALLBACK",
                        }
                    ], []

                def fake_incomplete_benchmarks(instruments, start, end, throttle_seconds=0.0, adjust=None):
                    return [], []

                with patch("alpha_ledger.data_ops.fetch_akshare_cn_spot_bars", side_effect=fake_spot), \
                     patch("alpha_ledger.data_ops.fetch_bars", side_effect=fake_incomplete_benchmarks):
                    result = data_update(
                        conn,
                        "2026-06-04",
                        fetch_events=False,
                        fetch_intraday=False,
                        price_mode="core",
                        adjust=None,
                    )

                self.assertEqual(result.status, "PARTIAL_SUCCESS")
                self.assertIn("benchmarks=sina rows=0", result.source_summary)

    def test_actionable_intraday_includes_watch_pullback_and_model_top_picks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                instruments = [
                    ("600001.SS", "策略买入", "sh600001"),
                    ("600002.SS", "回调观察", "sh600002"),
                    ("600003.SS", "模型高分", "sh600003"),
                ]
                for ticker, name, source_symbol in instruments:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO instruments
                            (market, ticker, name, source, source_symbol, active, tags_json, created_at)
                        VALUES ('CN_A', ?, ?, 'sina_cn', ?, 1, '[]', datetime('now'))
                        """,
                        (ticker, name, source_symbol),
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO price_bars
                            (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                        VALUES ('CN_A', ?, '2026-06-05', 10, 10, 10, 10, 1000, 10000, 'RAW_FALLBACK')
                        """,
                        (ticker,),
                    )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO strategies
                        (id, name, market_scope, thesis, entry_rules_json, exit_rules_json, created_at)
                    VALUES ('s1', '测试策略', 'CN_A', 'x', '{}', '{}', datetime('now'))
                    """
                )
                for ticker, action in (("600001.SS", "BUY_CANDIDATE"), ("600002.SS", "WATCH_PULLBACK")):
                    conn.execute(
                        """
                        INSERT INTO candidates
                            (as_of_date, market, ticker, name, strategy_id, candidate_score, action,
                             entry_price, signal_close, buy_zone_low, buy_zone_high, stop_loss,
                             target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                             risk_notes, evidence_json, status, confirmation_status, data_date, created_at)
                        VALUES ('2026-06-05', 'CN_A', ?, ?, 's1', 90, ?,
                                10, 10, 9.8, 10.2, 9, 12, 13, 2.0, 'x', 'x',
                                'x', '[]', 'WATCHLIST', 'PENDING', '2026-06-05', datetime('now'))
                        """,
                        (ticker, "策略买入" if ticker == "600001.SS" else "回调观察", action),
                    )
                conn.execute(
                    """
                    INSERT INTO model_registry
                        (model_name, model_version, model_family, status, feature_set, label_name, label_expr, horizon_days,
                         train_start, train_end, valid_start, valid_end, test_start, test_end,
                         artifact_path, metrics_json, created_at, updated_at)
                    VALUES ('m1', 'v1', 'LGBM', 'PRODUCTION', 'Alpha158', 'T+2', 'Ref($close, -2) / $close - 1', 2,
                            '2026-01-01', '2026-05-01', '2026-05-02', '2026-05-15',
                            '2026-05-16', '2026-06-05', 'x', '{}', datetime('now'), datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO model_scores
                        (model_name, model_version, market, ticker, score_date, score, percentile, created_at)
                    VALUES ('m1', 'v1', 'CN_A', '600003.SS', '2026-06-05', 1.0, 0.99, datetime('now'))
                    """
                )
                conn.commit()

                tickers = {item.ticker for item in actionable_intraday_instruments(conn, "2026-06-05")}

            self.assertIn("600001.SS", tickers)
            self.assertIn("600002.SS", tickers)
            self.assertIn("600003.SS", tickers)

    def test_core_price_mode_repairs_existing_same_day_amount_gap(self) -> None:
        today = date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO instruments
                        (market, ticker, name, source, source_symbol, active, tags_json, created_at)
                    VALUES ('CN_A', '600519.SS', '贵州茅台', 'test', 'sh600519', 1, '[]', datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO price_bars
                        (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                    VALUES ('CN_A', '600519.SS', ?, 100, 101, 102, 99, 1000, NULL, 'RAW_FALLBACK')
                    """,
                    (today,),
                )
                conn.commit()

                def fake_spot(instruments: list[Instrument], as_of):
                    return [
                        {
                            "market": "CN_A",
                            "ticker": "600519.SS",
                            "date": as_of.isoformat(),
                            "open": 100.0,
                            "close": 101.0,
                            "high": 102.0,
                            "low": 99.0,
                            "volume": 1000.0,
                            "amount": 100000.0,
                            "change_pct": 1.0,
                            "adjustment_status": "RAW_FALLBACK",
                        }
                    ], []

                def fake_benchmarks(instruments, start, end, throttle_seconds=0.0, adjust=None):
                    return [
                        {
                            "market": "CN_A",
                            "ticker": instrument.ticker,
                            "date": end.isoformat(),
                            "open": 10.0,
                            "close": 10.1,
                            "high": 10.2,
                            "low": 9.9,
                            "volume": 1000.0,
                            "amount": None,
                            "change_pct": 1.0,
                            "adjustment_status": "ADJUSTED",
                        }
                        for instrument in instruments
                    ], []

                with patch("alpha_ledger.data_ops.fetch_akshare_cn_spot_bars", side_effect=fake_spot) as mock_spot, \
                     patch("alpha_ledger.data_ops.fetch_bars", side_effect=fake_benchmarks):
                    result = data_update(
                        conn,
                        today,
                        fetch_events=False,
                        fetch_intraday=False,
                        price_mode="core",
                        adjust=None,
                    )

                self.assertEqual(result.status, "SUCCESS")
                mock_spot.assert_called_once()
                row = conn.execute(
                    """
                    SELECT amount
                    FROM price_bars
                    WHERE market = 'CN_A' AND ticker = '600519.SS' AND date = ?
                    """,
                    (today,),
                ).fetchone()
                self.assertEqual(row["amount"], 100000.0)


class ModelPredictTest(TestCase):
    def test_prediction_run_links_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                conn.execute(
                    """
                    INSERT INTO qlib_dataset_versions
                        (version, as_of_date, start_date, end_date, mode, status,
                         provider_uri, qlib_dir, fields_json, created_at)
                    VALUES ('v1', '2026-06-03', '2026-06-03', '2026-06-03',
                            'metadata_bootstrap', 'SUCCESS', 'x', 'x', '[]', datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO model_scores
                        (model_name, model_version, market, ticker, score_date, score, created_at)
                    VALUES ('qlib_alpha360', 't2_18m_20260603', 'CN_A',
                            '600519.SS', '2026-06-03', 0.5, datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO model_registry (
                        model_name, model_version, model_family, feature_set, label_name,
                        label_expr, horizon_days, status, metrics_json, created_at, updated_at
                    )
                    VALUES ('qlib_alpha360', 't2_18m_20260603', 'qlib_lgbm',
                            'Alpha360', 'T+2', 'Ref($close, -2) / $close - 1',
                            2, 'PRODUCTION', '{}', datetime('now'), datetime('now'))
                    """
                )
                conn.commit()

            with patch("alpha_ledger.pipeline_ops.subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                result = model_predict(
                    str(db_path),
                    "2026-06-03",
                    output_dir=Path(tmpdir) / "predict",
                )

            self.assertEqual(result.status, "SUCCESS")
            with connect(db_path) as conn:
                row = conn.execute("SELECT COUNT(*) AS c FROM prediction_runs WHERE status='SUCCESS'").fetchone()
                self.assertEqual(row["c"], 1)
                score = conn.execute("SELECT prediction_run_id FROM model_scores").fetchone()
                self.assertIsNotNone(score["prediction_run_id"])

    def test_no_production_models_does_not_fallback_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                conn.execute(
                    """
                    INSERT INTO qlib_dataset_versions
                        (version, as_of_date, start_date, end_date, mode, status,
                         provider_uri, qlib_dir, fields_json, created_at)
                    VALUES ('v1', '2026-06-03', '2026-06-03', '2026-06-03',
                            'metadata_bootstrap', 'SUCCESS', 'x', 'x', '[]', datetime('now'))
                    """
                )
                conn.commit()

            result = model_predict(str(db_path), "2026-06-03", output_dir=Path(tmpdir) / "predict")

            self.assertEqual(result.status, "FAILED")
            self.assertIn("No PRODUCTION models", result.error_message)


class ProductionDailyTest(TestCase):
    def test_actionable_gate_rejects_incomplete_model_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)
                row = conn.execute(
                    """
                    SELECT
                        '600519.SS' AS ticker,
                        '贵州茅台' AS name,
                        '强趋势突破' AS strategy_name,
                        'BUY_CANDIDATE' AS action,
                        80.0 AS candidate_score,
                        100.0 AS entry_price,
                        90.0 AS stop_loss,
                        120.0 AS target_1,
                        2.0 AS reward_risk,
                        0.8 AS model_percentile,
                        NULL AS model_percentile_2,
                        0.7 AS model_percentile_3
                    """
                ).fetchone()

                accepted, rejected = _gate_actionable_rows(conn, "2026-06-03", [row])

            self.assertEqual(accepted, [])
            self.assertEqual(len(rejected), 1)
            self.assertIn("model_percentile_2", rejected[0]["reasons"])

    def test_default_output_dir_is_production_daily_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            production_dir = Path(tmpdir) / "production_daily"
            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)
                conn.execute(
                    """
                    INSERT INTO qlib_dataset_versions
                        (version, as_of_date, start_date, end_date, mode, status,
                         provider_uri, qlib_dir, fields_json, created_at)
                    VALUES ('v1', '2026-06-03', '2026-06-03', '2026-06-03',
                            'metadata_bootstrap', 'SUCCESS', 'x', 'x', '[]', datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO prediction_runs
                        (run_id, as_of_date, models_scope, status, model_count,
                         score_count, started_at, finished_at)
                    VALUES ('p1', '2026-06-03', 'production', 'SUCCESS',
                            1, 1, datetime('now'), datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO instruments
                        (market, ticker, name, source, source_symbol, active, tags_json, created_at)
                    VALUES ('CN_A', '399001.SZ', '深证成指', 'test', 'sz399001', 1,
                            '["index"]', datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO price_bars
                        (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                    VALUES ('CN_A', '399001.SZ', '2026-06-03',
                            10, 10, 10, 10, 1000, NULL, 'ADJUSTED')
                    """
                )
                conn.commit()

            def fake_write_daily_plan(_conn: sqlite3.Connection, _as_of: str, path: Path) -> Path:
                path.write_text("# Daily\n", encoding="utf-8")
                return path

            with patch("alpha_ledger.pipeline_ops.PRODUCTION_DAILY_DIR", production_dir), \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage"), \
                 patch("alpha_ledger.pipeline_ops.screen_all"), \
                 patch("alpha_ledger.pipeline_ops.write_daily_plan", side_effect=fake_write_daily_plan):
                result = production_daily(str(db_path), "2026-06-03")

            expected_path = production_dir / "daily_plan_2026-06-03.md"
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(Path(result.report_path), expected_path)
            self.assertTrue(expected_path.exists())

    def test_production_daily_default_does_not_update_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)
                conn.execute(
                    """
                    INSERT INTO qlib_dataset_versions
                        (version, as_of_date, start_date, end_date, mode, status,
                         provider_uri, qlib_dir, fields_json, created_at)
                    VALUES ('v1', '2026-06-03', '2026-06-03', '2026-06-03',
                            'metadata_bootstrap', 'SUCCESS', 'x', 'x', '[]', datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO prediction_runs
                        (run_id, as_of_date, models_scope, status, model_count,
                         score_count, started_at, finished_at)
                    VALUES ('p1', '2026-06-03', 'production', 'SUCCESS',
                            1, 1, datetime('now'), datetime('now'))
                    """
                )
                conn.commit()

            with patch("alpha_ledger.pipeline_ops.data_update") as mock_update, \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage"), \
                 patch("alpha_ledger.pipeline_ops.screen_all"), \
                 patch("alpha_ledger.pipeline_ops.write_daily_plan", return_value=Path(tmpdir) / "daily.md"):
                result = production_daily(str(db_path), "2026-06-03", output_dir=Path(tmpdir))

            self.assertEqual(result.status, "SUCCESS")
            mock_update.assert_not_called()

    def test_production_daily_inline_update_is_core_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            with connect(db_path) as conn:
                init_db(conn)
                _seed_price_data(conn)
                conn.execute(
                    """
                    INSERT INTO qlib_dataset_versions
                        (version, as_of_date, start_date, end_date, mode, status,
                         provider_uri, qlib_dir, fields_json, created_at)
                    VALUES ('v1', '2026-06-03', '2026-06-03', '2026-06-03',
                            'metadata_bootstrap', 'SUCCESS', 'x', 'x', '[]', datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO prediction_runs
                        (run_id, as_of_date, models_scope, status, model_count,
                         score_count, started_at, finished_at)
                    VALUES ('p1', '2026-06-03', 'production', 'SUCCESS',
                            1, 1, datetime('now'), datetime('now'))
                    """
                )
                conn.commit()

            with patch("alpha_ledger.pipeline_ops.data_update") as mock_update, \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage"), \
                 patch("alpha_ledger.pipeline_ops.screen_all"), \
                 patch("alpha_ledger.pipeline_ops.write_daily_plan", return_value=Path(tmpdir) / "daily.md"):
                result = production_daily(
                    str(db_path),
                    "2026-06-03",
                    allow_inline_data_update=True,
                    output_dir=Path(tmpdir),
                )

            self.assertEqual(result.status, "SUCCESS")
            kwargs = mock_update.call_args.kwargs
            self.assertEqual(kwargs["price_mode"], "core")
            self.assertFalse(kwargs["fetch_events"])
            self.assertFalse(kwargs["fetch_intraday"])
            self.assertIsNone(kwargs["adjust"])

    def test_production_run_stops_after_qlib_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
            data_result = SimpleNamespace(status="SUCCESS", price_bars=1, error_count=0)
            qlib_result = SimpleNamespace(
                status="FAILED",
                version="vbad",
                mode="incremental",
                row_count=0,
                error_message="dump failed",
            )
            with patch("alpha_ledger.pipeline_ops.data_update", return_value=data_result) as mock_data, \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage", return_value=SimpleNamespace(confidence_level="HIGH_CONFIDENCE")), \
                 patch("alpha_ledger.pipeline_ops.qlib_refresh", return_value=qlib_result), \
                 patch("alpha_ledger.pipeline_ops.model_predict") as mock_predict, \
                 patch("alpha_ledger.pipeline_ops.production_daily") as mock_daily:
                result = production_run(db_path, "2026-06-03", output_root=Path(tmpdir) / "runs")

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.failed_step, "qlib-refresh")
            mock_data.assert_called_once()
            mock_predict.assert_not_called()
            mock_daily.assert_not_called()
            self.assertTrue(Path(result.summary_path).exists())

    def test_production_run_stops_after_partial_data_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
            data_result = SimpleNamespace(status="PARTIAL_SUCCESS", price_bars=1, error_count=2)
            with patch("alpha_ledger.pipeline_ops.data_update", return_value=data_result), \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage") as mock_audit, \
                 patch("alpha_ledger.pipeline_ops.qlib_refresh") as mock_qlib, \
                 patch("alpha_ledger.pipeline_ops.model_predict") as mock_predict, \
                 patch("alpha_ledger.pipeline_ops.production_daily") as mock_daily:
                result = production_run(db_path, "2026-06-03", output_root=Path(tmpdir) / "runs")

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.failed_step, "data-update")
            self.assertIn("PARTIAL_SUCCESS", result.error_message)
            mock_audit.assert_not_called()
            mock_qlib.assert_not_called()
            mock_predict.assert_not_called()
            mock_daily.assert_not_called()

    def test_production_run_success_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
            calls: list[str] = []

            def fake_data(*args, **kwargs):
                calls.append("data-update")
                return SimpleNamespace(status="SUCCESS", price_bars=1, error_count=0)

            def fake_audit(*args, **kwargs):
                calls.append("data-audit")
                return SimpleNamespace(confidence_level="HIGH_CONFIDENCE")

            def fake_qlib(*args, **kwargs):
                calls.append("qlib-refresh")
                return SimpleNamespace(status="SUCCESS", version="v1", mode="incremental", row_count=1, error_message="")

            def fake_predict(*args, **kwargs):
                calls.append("model-predict")
                return SimpleNamespace(status="SUCCESS", run_id="p1", model_count=3, score_count=9, error_message="")

            def fake_daily(*args, **kwargs):
                calls.append("production-daily")
                self.assertFalse(kwargs["prepare_signals"])
                return SimpleNamespace(status="SUCCESS", report_path=str(Path(tmpdir) / "daily.md"), error_message="")

            with patch("alpha_ledger.pipeline_ops.data_update", side_effect=fake_data), \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage", side_effect=fake_audit), \
                 patch("alpha_ledger.pipeline_ops.qlib_refresh", side_effect=fake_qlib), \
                 patch("alpha_ledger.pipeline_ops.model_predict", side_effect=fake_predict), \
                 patch("alpha_ledger.pipeline_ops.screen_all", return_value=3), \
                 patch("alpha_ledger.pipeline_ops.refine_candidates_with_intraday", return_value=2), \
                 patch("alpha_ledger.pipeline_ops.production_daily", side_effect=fake_daily):
                result = production_run(db_path, "2026-06-03", output_root=Path(tmpdir) / "runs")

            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(
                calls,
                [
                    "data-update",
                    "data-audit",
                    "qlib-refresh",
                    "model-predict",
                    "data-update",
                    "production-daily",
                ],
            )
            self.assertTrue(Path(result.summary_path).exists())

    def test_production_run_uses_1m_intraday_for_signal_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
            data_calls: list[dict] = []

            def capture_data(*args, **kwargs):
                data_calls.append(kwargs)
                return SimpleNamespace(
                    status="SUCCESS", price_bars=1, intraday_bars=10,
                    error_count=0, corporate_events=0, financial_metrics=0,
                    money_flows=0,
                )

            with patch("alpha_ledger.pipeline_ops.data_update", side_effect=capture_data), \
                 patch("alpha_ledger.pipeline_ops.audit_data_coverage",
                       return_value=SimpleNamespace(confidence_level="HIGH_CONFIDENCE")), \
                 patch("alpha_ledger.pipeline_ops.qlib_refresh",
                       return_value=SimpleNamespace(status="SUCCESS", version="v1", mode="incremental", row_count=1, error_message="")), \
                 patch("alpha_ledger.pipeline_ops.model_predict",
                       return_value=SimpleNamespace(status="SUCCESS", run_id="p1", model_count=1, score_count=3, error_message="")), \
                 patch("alpha_ledger.pipeline_ops.screen_all", return_value=1), \
                 patch("alpha_ledger.pipeline_ops.refine_candidates_with_intraday", return_value=1), \
                 patch("alpha_ledger.pipeline_ops.production_daily",
                       return_value=SimpleNamespace(status="SUCCESS", report_path=str(Path(tmpdir) / "daily.md"), error_message="")):
                result = production_run(db_path, "2026-06-03", output_root=Path(tmpdir) / "runs")

            self.assertEqual(result.status, "SUCCESS")
            # Second data_update call is the signal-intraday-context step
            self.assertGreaterEqual(len(data_calls), 2)
            intraday_call = data_calls[1]
            self.assertTrue(intraday_call["fetch_intraday"])
            self.assertEqual(intraday_call["intraday_period"], "1")
            self.assertEqual(intraday_call["price_mode"], "none")


class ModelArenaTest(TestCase):
    def test_baseline18_dry_run_has_18_tasks_and_no_model_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
            result = model_arena(
                db_path,
                "2026-06-03",
                pool="baseline18",
                max_workers=1,
                dry_run=True,
                output_dir=Path(tmpdir) / "arena",
            )
            self.assertEqual(result.total_models, 18)
            self.assertEqual(result.status, "DRY_RUN")
            with connect(db_path) as conn:
                row = conn.execute("SELECT COUNT(*) AS c FROM model_scores").fetchone()
            self.assertEqual(row["c"], 0)

    def test_max_workers_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with self.assertRaises(ValueError):
                model_arena(db_path, "2026-06-03", max_workers=18, dry_run=True)


class ModelEvaluateTest(TestCase):
    def _write_pred(self, path: Path, dates: list[str]) -> None:
        index = pd.MultiIndex.from_tuples(
            [(pd.Timestamp(day), instrument) for day in dates for instrument in ("SH600519", "SZ000001")],
            names=["datetime", "instrument"],
        )
        scores = [1.0 if instrument == "SH600519" else 0.1 for _day in dates for instrument in ("SH600519", "SZ000001")]
        pd.DataFrame({"score": scores}, index=index).to_pickle(path)

    def _seed_model_evaluate_data(self, conn: sqlite3.Connection, pred_path: Path, version: str) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at, industry_sw_l1)
            VALUES ('CN_A', '600519.SS', '贵州茅台', 'test', 'sh600519', 1, '[]', datetime('now'), '食品饮料')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at, industry_sw_l1)
            VALUES ('CN_A', '000001.SZ', '平安银行', 'test', 'sz000001', 1, '[]', datetime('now'), '银行')
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', '000300.SS', '沪深300', 'test', 'sh000300', 1, '[]', datetime('now'))
            """
        )
        for idx, day in enumerate([f"2026-06-{i:02d}" for i in range(1, 16)]):
            conn.execute(
                """
                INSERT OR REPLACE INTO price_bars
                    (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                VALUES ('CN_A', '600519.SS', ?, ?, ?, ?, ?, 1000, 100000, 'RAW_FALLBACK')
                """,
                (day, 100 + idx, 102 + idx, 103 + idx, 99 + idx),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO price_bars
                    (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                VALUES ('CN_A', '000001.SZ', ?, ?, ?, ?, ?, 1000, 100000, 'RAW_FALLBACK')
                """,
                (day, 100 - idx * 0.5, 99 - idx * 0.5, 101 - idx * 0.5, 98 - idx * 0.5),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO price_bars
                    (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                VALUES ('CN_A', '000300.SS', ?, 100, 100.1, 101, 99, 1000, 100000, 'ADJUSTED')
                """,
                (day,),
            )
        for spec in baseline18_specs("2026-06-04"):
            conn.execute(
                """
                INSERT INTO model_registry (
                    model_name, model_version, model_family, feature_set, label_name,
                    label_expr, horizon_days, train_start, train_end, valid_start,
                    valid_end, test_start, test_end, status, artifact_path,
                    metrics_json, created_at, updated_at
                )
                VALUES (?, ?, 'qlib_lgbm', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'RESEARCH', ?, '{}', datetime('now'), datetime('now'))
                """,
                (
                    spec.model_name,
                    version,
                    spec.feature_set,
                    spec.label_name,
                    spec.label_expr,
                    spec.horizon_days,
                    spec.train_start,
                    spec.train_end,
                    spec.valid_start,
                    spec.valid_end,
                    spec.test_start,
                    spec.test_end,
                    str(pred_path),
                ),
            )
        conn.commit()

    def test_model_evaluate_writes_reports_metrics_and_keeps_scores_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            pred_path = Path(tmpdir) / "pred.pkl"
            self._write_pred(pred_path, [f"2026-06-{i:02d}" for i in range(1, 9)])
            with connect(db_path) as conn:
                init_db(conn)
                self._seed_model_evaluate_data(conn, pred_path, "baseline18_20260604")

            result = model_evaluate(
                db_path,
                pool="baseline18",
                model_version="baseline18_20260604",
                as_of="2026-06-15",
                mode="fixed-test",
                output_dir=Path(tmpdir) / "validation",
            )

            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.model_count, 18)
            self.assertTrue(Path(result.report_path).exists())
            self.assertTrue(Path(result.metrics_path).exists())
            text = Path(result.report_path).read_text(encoding="utf-8")
            self.assertIn("行业暴露", text)
            self.assertIn("指标说明", text)
            self.assertIn("食品饮料", text)
            with connect(db_path) as conn:
                score_count = conn.execute("SELECT COUNT(*) AS c FROM model_scores").fetchone()["c"]
                self.assertEqual(score_count, 0)
                run_count = conn.execute("SELECT COUNT(*) AS c FROM model_validation_runs").fetchone()["c"]
                self.assertEqual(run_count, 1)
                metric_count = conn.execute("SELECT COUNT(*) AS c FROM model_validation_metrics").fetchone()["c"]
                self.assertGreater(metric_count, 0)
                top1 = conn.execute(
                    """
                    SELECT sample_count, industry_exposure_json
                    FROM model_validation_metrics
                    WHERE segment='external_validation' AND bucket='TOP_1'
                    LIMIT 1
                    """
                ).fetchone()
                self.assertGreater(top1["sample_count"], 0)
                self.assertIn("食品饮料", top1["industry_exposure_json"])
                row = conn.execute(
                    """
                    SELECT status, metrics_json
                    FROM model_registry
                    WHERE model_name='arena_alpha360_2025_t2' AND model_version='baseline18_20260604'
                    """
                ).fetchone()
                self.assertEqual(row["status"], "RESEARCH")
                self.assertIn("latest_fixed_test_validation", row["metrics_json"])


class ProductionAsyncTest(TestCase):
    def test_production_async_records_partial_without_touching_daily(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
            result_obj = SimpleNamespace(
                status="PARTIAL_SUCCESS",
                corporate_events=1,
                financial_metrics=0,
                money_flows=0,
                intraday_bars=0,
                error_count=1,
            )
            with patch("alpha_ledger.pipeline_ops.data_update", return_value=result_obj) as mock_update:
                result = production_async(db_path, "2026-06-03", output_dir=Path(tmpdir) / "async")

            self.assertEqual(result.status, "PARTIAL_SUCCESS")
            self.assertTrue(Path(result.report_path).exists())
            kwargs = mock_update.call_args.kwargs
            self.assertTrue(kwargs["fetch_events"])
            self.assertFalse(kwargs["fetch_intraday"])
            self.assertEqual(kwargs["price_mode"], "essential")


class ModelValidationGovernanceTest(TestCase):
    def _seed_model_validation_data(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO model_registry (
                model_name, model_version, model_family, feature_set, label_name,
                label_expr, horizon_days, train_start, train_end, valid_start,
                valid_end, test_start, test_end, status, metrics_json,
                created_at, updated_at
            )
            VALUES (
                'arena_alpha158_2026_t2', 'baseline18_20260603', 'qlib_lgbm',
                'Alpha158', 'T+2', 'Ref($close, -2) / Ref($open, -1) - 1',
                2, '2026-01-01', '2026-05-20', '2026-05-21',
                '2026-05-25', '2026-05-26', '2026-06-03',
                'PRODUCTION', '{"top_return": 0.02}', datetime('now'), datetime('now')
            )
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', '600519.SS', '贵州茅台', 'test', 'sh600519', 1, '[]', datetime('now'))
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', '000001.SZ', '平安银行', 'test', 'sz000001', 1, '[]', datetime('now'))
            """
        )
        dates = ["2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03"]
        for idx, day in enumerate(dates):
            conn.execute(
                """
                INSERT OR REPLACE INTO price_bars
                    (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                VALUES ('CN_A', '600519.SS', ?, ?, ?, ?, ?, 1000, 100000, 'RAW_FALLBACK')
                """,
                (day, 100 + idx, 101 + idx, 102 + idx, 99 + idx),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO price_bars
                    (market, ticker, date, open, close, high, low, volume, amount, adjustment_status)
                VALUES ('CN_A', '000001.SZ', ?, ?, ?, ?, ?, 1000, 100000, 'RAW_FALLBACK')
                """,
                (day, 100 - idx, 99 - idx, 101 - idx, 98 - idx),
            )
        for day in ["2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03"]:
            conn.execute(
                """
                INSERT INTO model_scores
                    (model_name, model_version, market, ticker, score_date, score, percentile, created_at)
                VALUES ('arena_alpha158_2026_t2', 'baseline18_20260603', 'CN_A',
                        '600519.SS', ?, 1.0, 90.0, datetime('now'))
                """,
                (day,),
            )
            conn.execute(
                """
                INSERT INTO model_scores
                    (model_name, model_version, market, ticker, score_date, score, percentile, created_at)
                VALUES ('arena_alpha158_2026_t2', 'baseline18_20260603', 'CN_A',
                        '000001.SZ', ?, 0.1, 10.0, datetime('now'))
                """,
                (day,),
            )
        conn.commit()

    def test_model_validate_uses_mature_label_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
                self._seed_model_validation_data(conn)

            result = model_validate(db_path, "2026-06-03", output_dir=Path(tmpdir) / "validation")

            self.assertEqual(result.status, "SUCCESS")
            text = Path(result.report_path).read_text(encoding="utf-8")
            self.assertIn("2026-06-01", text)
            self.assertNotIn("2026-06-03 |", text)

    def test_model_governance_review_does_not_mutate_model_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
                self._seed_model_validation_data(conn)

            result = model_governance_review(db_path, "2026-06-03", output_dir=Path(tmpdir) / "governance")

            self.assertEqual(result.status, "SUCCESS")
            self.assertTrue(Path(result.report_path).exists())
            with connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT status
                    FROM model_registry
                    WHERE model_name='arena_alpha158_2026_t2'
                    """
                ).fetchone()
            self.assertEqual(row["status"], "PRODUCTION")


class QfqMaintenanceTest(TestCase):
    def test_qfq_maintenance_skips_before_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)
                conn.execute(
                    """
                    INSERT INTO data_fetch_runs
                        (run_type, market, start_date, end_date, status, started_at)
                    VALUES ('qfq-maintenance', 'CN_A', '2026-05-15', '2026-06-01', 'SUCCESS', datetime('now'))
                    """
                )
                conn.commit()

            with patch("alpha_ledger.cli.qfq_backfill") as mock_backfill:
                command_qfq_maintenance(
                    db_path,
                    "2026-06-05",
                    interval_days=14,
                    lookback_days=45,
                    source="auto",
                    throttle=0.0,
                    limit=None,
                    commit_every=50,
                    out_dir=str(Path(tmpdir) / "reports"),
                    force=False,
                    dry_run=False,
                )

            mock_backfill.assert_not_called()
            self.assertTrue((Path(tmpdir) / "reports" / "qfq_maintenance_20260605_SKIPPED.md").exists())

    def test_qfq_maintenance_records_due_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.sqlite")
            with connect(db_path) as conn:
                init_db(conn)

            result = SimpleNamespace(
                start="2026-04-21",
                end="2026-06-05",
                source="auto",
                total_tickers=2,
                skipped_benchmarks=0,
                skipped_errors=0,
                updated_rows=4,
                ticker_errors=[],
                benchmark_tickers=[],
                elapsed_seconds=0.1,
                dry_run=False,
                baostock_count=2,
                akshare_count=0,
                target_count=2,
            )
            with patch("alpha_ledger.cli.qfq_backfill", return_value=result) as mock_backfill, \
                 patch("alpha_ledger.cli.write_qfq_backfill_report") as mock_report:
                mock_report.return_value = (Path(tmpdir) / "qfq.md", Path(tmpdir) / "qfq.json")
                command_qfq_maintenance(
                    db_path,
                    "2026-06-05",
                    interval_days=14,
                    lookback_days=45,
                    source="auto",
                    throttle=0.0,
                    limit=2,
                    commit_every=50,
                    out_dir=str(Path(tmpdir) / "reports"),
                    force=False,
                    dry_run=False,
                )

            mock_backfill.assert_called_once()
            with connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT status, requested_symbols, price_bars, error_count
                    FROM data_fetch_runs
                    WHERE run_type='qfq-maintenance'
                    """
                ).fetchone()
            self.assertEqual(row["status"], "SUCCESS")
            self.assertEqual(row["requested_symbols"], 2)
            self.assertEqual(row["price_bars"], 4)
            self.assertEqual(row["error_count"], 0)
