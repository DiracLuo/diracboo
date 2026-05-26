from __future__ import annotations

import sqlite3
import tempfile
import unittest

from alpha_ledger.audit import audit_all
from alpha_ledger.db import init_db
from alpha_ledger.event_data import import_events_csv
from alpha_ledger.ledger import verify_signals
from alpha_ledger.metrics import (
    candidate_action_leaderboard,
    candidate_horizon_strategy_leaderboard,
    candidate_market_leaderboard,
    candidate_strategy_leaderboard,
    evaluate_all,
    evaluate_candidate_horizons_for_date,
    evaluate_candidates,
)
from alpha_ledger.screener import _latest_financial_flags, screen_all
from alpha_ledger.seed import seed_all


class AlphaLedgerMvpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        seed_all(self.conn)

    def test_seeded_signal_hashes_are_valid(self) -> None:
        self.assertEqual(verify_signals(self.conn), [])

    def test_xingye_t5_evaluation(self) -> None:
        count = evaluate_all(self.conn, "2026-05-25")
        self.assertEqual(count, 4)
        row = self.conn.execute(
            """
            SELECT return_pct, hit_target_1, hit_target_2
            FROM evaluations
            WHERE horizon_days = 5
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["return_pct"], 7.0648, places=3)
        self.assertEqual(row["hit_target_1"], 1)
        self.assertEqual(row["hit_target_2"], 0)

    def test_xingye_can_be_screened_on_signal_date(self) -> None:
        count = screen_all(self.conn, "2026-05-13")
        self.assertGreaterEqual(count, 1)
        row = self.conn.execute(
            """
            SELECT ticker, strategy_id, candidate_score
            FROM candidates
            WHERE as_of_date = '2026-05-13'
              AND strategy_id = 'xingye_style_prepositioning'
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ticker"], "002674.SZ")
        self.assertEqual(row["strategy_id"], "xingye_style_prepositioning")
        self.assertAlmostEqual(row["candidate_score"], 95.6, places=1)

    def test_strategy_audit_keeps_tiny_samples_in_observe_mode(self) -> None:
        evaluate_all(self.conn, "2026-05-25")
        count = audit_all(self.conn, "2026-05-25")
        self.assertEqual(count, 13)
        row = self.conn.execute(
            """
            SELECT health_status
            FROM strategy_audits
            WHERE strategy_id = 'xingye_style_prepositioning'
            """
        ).fetchone()
        self.assertEqual(row["health_status"], "INSUFFICIENT_SAMPLE")

    def test_replay_leaderboards_include_deduped_market_and_action_views(self) -> None:
        screen_all(self.conn, "2026-05-13")
        evaluate_candidates(self.conn, "2026-05-13", "2026-05-25")
        horizon_count = evaluate_candidate_horizons_for_date(self.conn, "2026-05-13", "2026-05-25")
        self.assertGreaterEqual(horizon_count, 4)

        eval_row = self.conn.execute(
            """
            SELECT execution_date, execution_price, return_pct, exit_type, exit_date
            FROM candidate_evaluations
            ORDER BY return_pct DESC
            LIMIT 1
            """
        ).fetchone()
        self.assertEqual(eval_row["execution_date"], "2026-05-14")
        self.assertAlmostEqual(eval_row["execution_price"], 13.72, places=2)
        self.assertAlmostEqual(eval_row["return_pct"], 5.6122, places=3)
        self.assertEqual(eval_row["exit_type"], "TARGET_1")
        self.assertEqual(eval_row["exit_date"], "2026-05-20")

        horizon_row = self.conn.execute(
            """
            SELECT horizon_days, observed_days, execution_date, execution_price, return_pct
            FROM candidate_horizon_evaluations
            JOIN candidates c ON c.id = candidate_horizon_evaluations.candidate_id
            WHERE horizon_days = 5
              AND c.strategy_id = 'xingye_style_prepositioning'
            LIMIT 1
            """
        ).fetchone()
        self.assertEqual(horizon_row["observed_days"], 5)
        self.assertEqual(horizon_row["execution_date"], "2026-05-14")
        self.assertAlmostEqual(horizon_row["execution_price"], 13.72, places=2)
        self.assertAlmostEqual(horizon_row["return_pct"], 5.6122, places=3)

        strategy_rows = candidate_strategy_leaderboard(
            self.conn,
            "2026-05-13",
            "2026-05-13",
            "2026-05-25",
            dedupe=True,
        )
        self.assertEqual(strategy_rows[0]["strategy_id"], "xingye_style_prepositioning")
        self.assertIsNotNone(strategy_rows[0]["avg_max_gain_pct"])

        horizon_strategy_rows = candidate_horizon_strategy_leaderboard(
            self.conn,
            "2026-05-13",
            "2026-05-13",
            "2026-05-25",
            horizon_days=5,
            dedupe=True,
        )
        self.assertEqual(horizon_strategy_rows[0]["strategy_id"], "xingye_style_prepositioning")
        self.assertEqual(horizon_strategy_rows[0]["evaluated_count"], 1)

        market_rows = candidate_market_leaderboard(self.conn, "2026-05-13", "2026-05-13", "2026-05-25")
        self.assertEqual(market_rows[0]["segment"], "CN_A")

        action_rows = candidate_action_leaderboard(self.conn, "2026-05-13", "2026-05-13", "2026-05-25")
        self.assertEqual(action_rows[0]["segment"], "次日确认/确认后触发")

    def test_financial_metrics_are_visible_only_after_disclosure_date(self) -> None:
        self.conn.execute(
            """
            INSERT INTO financial_metrics (
                market, ticker, report_date, published_date, metric_name,
                metric_value, unit, source, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "CN_A",
                "002674.SZ",
                "2026-03-31",
                "2026-04-30",
                "净利润增长率(%)",
                25.0,
                "%",
                "test",
                "2026-05-01T00:00:00Z",
            ),
        )
        self.conn.commit()

        self.assertEqual(_latest_financial_flags(self.conn, "CN_A", "002674.SZ", "2026-04-15"), [])
        self.assertEqual(
            _latest_financial_flags(self.conn, "CN_A", "002674.SZ", "2026-05-01"),
            ["净利润增长25.0%"],
        )

    def test_import_events_csv_supports_us_and_hk_events(self) -> None:
        csv_text = (
            "market,ticker,name,event_date,event_type,title,source,importance_score,summary\n"
            "US,NVDA,NVIDIA,2026-05-10,earnings,AI demand beat,manual,0.82,earnings beat\n"
            "HK,700,腾讯控股,2026-05-11,buyback,回购金额扩大,manual,0.76,回购\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", suffix=".csv") as handle:
            handle.write(csv_text)
            handle.flush()
            result = import_events_csv(self.conn, handle.name)

        self.assertEqual(result.errors, ())
        self.assertEqual(result.corporate_events, 2)
        rows = self.conn.execute(
            """
            SELECT market, ticker, event_type
            FROM corporate_events
            WHERE market IN ('US', 'HK')
            ORDER BY market, ticker
            """
        ).fetchall()
        self.assertEqual([(row["market"], row["ticker"], row["event_type"]) for row in rows], [
            ("HK", "0700.HK", "buyback"),
            ("US", "NVDA", "earnings"),
        ])


if __name__ == "__main__":
    unittest.main()
