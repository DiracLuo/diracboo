from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch, MagicMock

from alpha_ledger.audit import audit_all
from alpha_ledger.benchmarks import benchmark_for_asset
from alpha_ledger.cli import command_data_backfill
from alpha_ledger.data_ops import CONFIDENCE_HIGH, audit_data_coverage, data_update, probe_adjustment_sources
from alpha_ledger.db import init_db
from alpha_ledger.event_data import import_events_csv
from alpha_ledger.ledger import verify_signals
from alpha_ledger.loss_review import render_loss_review
from alpha_ledger.metrics import (
    candidate_action_leaderboard,
    candidate_horizon_strategy_leaderboard,
    candidate_market_leaderboard,
    candidate_strategy_leaderboard,
    evaluate_candidate_horizons_for_date,
    evaluate_candidates,
    suggest_strategy_weight_adjustments,
    trade_cost_pct,
)
from alpha_ledger.portfolio_backtest import _risk_parity_position_size, run_portfolio_backtest
from alpha_ledger.reporting import daily_action_plan, render_daily_plan
from alpha_ledger.screener import (
    _candidate,
    _latest_financial_flags,
    confirm_candidates,
    screen_all,
    screen_cn_a_pead_quality_surprise,
    screen_event_catalyst,
)
from alpha_ledger.seed import seed_all
from alpha_ledger.trading_rules import cn_a_limit_pct


class AlphaLedgerMvpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        seed_all(self.conn)

    def _insert_event_test_stock(self, ticker: str, name: str = "测试股份") -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', ?, ?, 'test', ?, 1, '[]', 'now')
            """,
            (ticker, name, ticker),
        )
        dates = [
            "2026-05-01",
            "2026-05-04",
            "2026-05-05",
            "2026-05-06",
            "2026-05-07",
            "2026-05-08",
            "2026-05-11",
            "2026-05-12",
            "2026-05-13",
            "2026-05-14",
            "2026-05-15",
        ]
        for idx, date_value in enumerate(dates):
            close = 10.0 + idx * 0.02
            volume = 100_000 if idx < 10 else 260_000
            change_pct = 0.2 if idx < 10 else 2.0
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "CN_A",
                    ticker,
                    date_value,
                    close - 0.05,
                    close,
                    close + 0.30,
                    close - 0.30,
                    volume,
                    8_000_000,
                    change_pct,
                    close - 0.05,
                    close,
                    close + 0.30,
                    close - 0.30,
                    1.0,
                    "ADJUSTED",
                ),
            )

    def test_default_seed_has_no_manual_signals(self) -> None:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM signals").fetchone()
        self.assertEqual(row["count"], 0)
        self.assertEqual(verify_signals(self.conn), [])

    def test_xingye_t5_candidate_evaluation(self) -> None:
        screen_all(self.conn, "2026-05-13")
        count = evaluate_candidates(self.conn, "2026-05-13", "2026-05-25")
        self.assertGreaterEqual(count, 1)
        row = self.conn.execute(
            """
            SELECT e.return_pct, e.hit_target_1, e.hit_target_2
            FROM candidate_evaluations e
            JOIN candidates c ON c.id = e.candidate_id
            WHERE c.strategy_id = 'xingye_style_prepositioning'
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["return_pct"], 5.6122, places=3)
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
        self.assertGreaterEqual(row["candidate_score"], 80.0)

    def test_strategy_audit_keeps_tiny_samples_in_observe_mode(self) -> None:
        screen_all(self.conn, "2026-05-13")
        evaluate_candidate_horizons_for_date(self.conn, "2026-05-13", "2026-05-25")
        count = audit_all(self.conn, "2026-05-25")
        self.assertEqual(count, 5)
        row = self.conn.execute(
            """
            SELECT signal_count, health_status
            FROM strategy_audits
            WHERE strategy_id = 'xingye_style_prepositioning'
            """
        ).fetchone()
        self.assertEqual(row["signal_count"], 1)
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

    def test_candidate_reward_risk_requires_positive_stop(self) -> None:
        zero_stop = _candidate(
            as_of_date="2026-01-01",
            market="CN_A",
            ticker="000001.SZ",
            name="零止损测试",
            strategy_id="trend_breakout",
            score=80,
            action="BUY_CANDIDATE",
            close=10.0,
            stop_loss=0.0,
            target_1=12.0,
            target_2=14.0,
            thesis="test",
            trigger_condition="test",
            risk_notes="test",
            evidence=[],
            data_date="2026-01-01",
        )
        invalid_stop = _candidate(
            as_of_date="2026-01-01",
            market="CN_A",
            ticker="000002.SZ",
            name="高止损测试",
            strategy_id="trend_breakout",
            score=80,
            action="BUY_CANDIDATE",
            close=10.0,
            stop_loss=10.0,
            target_1=12.0,
            target_2=14.0,
            thesis="test",
            trigger_condition="test",
            risk_notes="test",
            evidence=[],
            data_date="2026-01-01",
        )
        self.assertEqual(zero_stop["reward_risk_ratio"], 0.0)
        self.assertEqual(invalid_stop["reward_risk_ratio"], 0.0)

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

    def test_portfolio_backtest_respects_a_share_t1_and_round_trip_cost(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('CN_A', '000001.SZ', '测试银行', 'test', '000001', '2026-01-01T00:00:00Z')
            """
        )
        for date, open_price, close, high, low in (
            ("2026-01-01", 10.0, 10.0, 10.2, 9.8),
            ("2026-01-02", 10.0, 14.0, 14.0, 9.8),
            ("2026-01-05", 10.5, 9.0, 11.0, 8.5),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct
                )
                VALUES ('CN_A', '000001.SZ', ?, ?, ?, ?, ?, 1000000, NULL, NULL, NULL, NULL)
                """,
                (date, open_price, close, high, low),
            )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-01-01', 'CN_A', '000001.SZ', '测试银行', 'trend_breakout', 95,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                13, 15, 3.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-01-01', '2026-01-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        result = run_portfolio_backtest(
            self.conn,
            "2026-01-01",
            "2026-01-01",
            "2026-01-05",
            max_positions=1,
            initial_capital=100000,
            cost_bps=18,
            cooldown_days=10,
        )

        self.assertEqual(result.trade_count, 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_date, "2026-01-02")
        self.assertEqual(trade.exit_date, "2026-01-05")
        self.assertEqual(trade.exit_type, "STOP_LOSS")
        self.assertNotEqual(trade.entry_date, trade.exit_date)
        self.assertAlmostEqual(trade.cost_pct, 0.18, places=3)
        self.assertAlmostEqual(trade.cost_pct, trade_cost_pct("CN_A"), places=3)
        self.assertEqual(result.daily_equity[0], ("2026-01-01", 100000))

    def test_portfolio_backtest_uses_market_cost_when_not_overridden(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('US', 'TEST', 'US Test', 'test', 'TEST', '2026-01-01T00:00:00Z')
            """
        )
        for date, open_price, close, high, low in (
            ("2026-01-01", 10.0, 10.0, 10.2, 9.8),
            ("2026-01-02", 10.0, 11.0, 12.0, 9.8),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct
                )
                VALUES ('US', 'TEST', ?, ?, ?, ?, ?, 1000000, NULL, NULL, NULL, NULL)
                """,
                (date, open_price, close, high, low),
            )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-01-01', 'US', 'TEST', 'US Test', 'trend_breakout', 95,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                12.5, 14, 2.5, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-01-01', '2026-01-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        result = run_portfolio_backtest(
            self.conn,
            "2026-01-01",
            "2026-01-01",
            "2026-01-02",
            max_positions=1,
            initial_capital=100000,
            markets=("US",),
        )

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.cost_model, "market-cost-by-symbol")
        self.assertAlmostEqual(result.trades[0].cost_pct, trade_cost_pct("US"), places=3)

    def test_portfolio_backtest_uses_intraday_entry_and_exit_when_available(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('CN_A', '000005.SZ', '分时测试', 'test', '000005', '2026-04-01T00:00:00Z')
            """
        )
        for date, open_price, close, high, low in (
            ("2026-04-01", 10.0, 10.0, 10.1, 9.9),
            ("2026-04-02", 10.0, 11.0, 13.0, 9.9),
            ("2026-04-03", 11.0, 12.2, 12.5, 10.8),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct
                )
                VALUES ('CN_A', '000005.SZ', ?, ?, ?, ?, ?, 1000000, NULL, NULL, NULL, NULL)
                """,
                (date, open_price, close, high, low),
            )
        for index, (date, time, open_price, close, high, low, volume, amount) in enumerate((
            ("2026-04-02", "09:30", 10.0, 10.5, 13.0, 10.0, 1000, 10500),
            ("2026-04-02", "09:31", 10.5, 10.5, 13.0, 10.4, 1000, 10500),
            ("2026-04-03", "09:30", 11.0, 12.0, 12.1, 10.9, 1000, 12000),
        )):
            self.conn.execute(
                """
                INSERT INTO intraday_bars (
                    market, ticker, datetime, date, time, open, close, high, low, volume, amount
                )
                VALUES ('CN_A', '000005.SZ', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"{date}T{time}:00", date, time, open_price, close, high, low, volume, amount),
            )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-04-01', 'CN_A', '000005.SZ', '分时测试', 'trend_breakout', 95,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                12, 13, 2.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-04-01', '2026-04-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        result = run_portfolio_backtest(
            self.conn,
            "2026-04-01",
            "2026-04-01",
            "2026-04-03",
            max_positions=1,
            initial_capital=100000,
            execution_mode="intraday",
        )

        self.assertEqual(result.trade_count, 1)
        trade = result.trades[0]
        self.assertAlmostEqual(trade.entry_price, 10.5, places=2)
        self.assertEqual(trade.exit_date, "2026-04-03")
        self.assertEqual(trade.exit_type, "TARGET_1")

    def test_candidate_evaluation_uses_intraday_and_a_share_t1(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('CN_A', '000006.SZ', '候选分时测试', 'test', '000006', '2026-04-01T00:00:00Z')
            """
        )
        for date, open_price, close, high, low in (
            ("2026-04-01", 10.0, 10.0, 10.1, 9.9),
            ("2026-04-02", 10.0, 11.0, 13.0, 9.9),
            ("2026-04-03", 11.0, 12.2, 12.5, 10.8),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct
                )
                VALUES ('CN_A', '000006.SZ', ?, ?, ?, ?, ?, 1000000, NULL, NULL, NULL, NULL)
                """,
                (date, open_price, close, high, low),
            )
        for date, time, open_price, close, high, low, volume, amount in (
            ("2026-04-02", "09:30:00", 10.0, 10.5, 13.0, 10.0, 1000, 10500),
            ("2026-04-02", "09:35:00", 10.5, 10.5, 13.0, 10.4, 1000, 10500),
            ("2026-04-03", "09:30:00", 11.0, 12.0, 12.1, 10.9, 1000, 12000),
        ):
            self.conn.execute(
                """
                INSERT INTO intraday_bars (
                    market, ticker, datetime, date, time, open, close, high, low, volume, amount
                )
                VALUES ('CN_A', '000006.SZ', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"{date} {time}", date, time, open_price, close, high, low, volume, amount),
            )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-04-01', 'CN_A', '000006.SZ', '候选分时测试', 'trend_breakout', 95,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                12, 13, 2.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-04-01', '2026-04-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        count = evaluate_candidates(self.conn, "2026-04-01", "2026-04-03")
        self.assertEqual(count, 1)
        row = self.conn.execute(
            """
            SELECT execution_date, execution_price, execution_type, exit_date, exit_type
            FROM candidate_evaluations
            JOIN candidates c ON c.id = candidate_evaluations.candidate_id
            WHERE c.ticker = '000006.SZ'
            """
        ).fetchone()
        self.assertEqual(row["execution_date"], "2026-04-02")
        self.assertAlmostEqual(row["execution_price"], 10.5, places=2)
        self.assertEqual(row["execution_type"], "OPEN_5MIN_VWAP")
        self.assertEqual(row["exit_date"], "2026-04-03")
        self.assertEqual(row["exit_type"], "TARGET_1")

    def test_candidate_evaluation_uses_adjusted_returns_and_benchmark_alpha(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('CN_A', '000007.SZ', '复权测试', 'test', '000007', '2026-04-01T00:00:00Z')
            """
        )
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at, tags_json)
            VALUES ('CN_A', '000300.SS', '沪深300', 'test', 'sh000300', '2026-04-01T00:00:00Z', '["benchmark"]')
            """
        )
        for date, open_price, close, high, low, adj_open, adj_close, adj_high, adj_low in (
            ("2026-04-01", 100.0, 100.0, 100.0, 100.0, 50.0, 50.0, 50.0, 50.0),
            ("2026-04-02", 100.0, 100.0, 100.0, 100.0, 50.0, 50.0, 50.0, 50.0),
            ("2026-04-03", 90.0, 90.0, 90.0, 90.0, 60.0, 60.0, 60.0, 60.0),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                )
                VALUES ('CN_A', '000007.SZ', ?, ?, ?, ?, ?, 1000000, NULL,
                        NULL, NULL, NULL, ?, ?, ?, ?, ?, 'ADJUSTED')
                """,
                (date, open_price, close, high, low, adj_open, adj_close, adj_high, adj_low, adj_close / close),
            )
        for date, close in (("2026-04-02", 1000.0), ("2026-04-03", 1050.0)):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                )
                VALUES ('CN_A', '000300.SS', ?, ?, ?, ?, ?, 1000000, NULL,
                        NULL, NULL, NULL, ?, ?, ?, ?, 1.0, 'ADJUSTED')
                """,
                (date, close, close, close, close, close, close, close, close),
            )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-04-01', 'CN_A', '000007.SZ', '复权测试', 'trend_breakout', 95,
                'BUY_CANDIDATE', 100, 99, 101, 80,
                200, 220, 5.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-04-01', '2026-04-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        count = evaluate_candidates(self.conn, "2026-04-01", "2026-04-03", benchmark_ticker="000300.SS")
        self.assertEqual(count, 1)
        row = self.conn.execute(
            """
            SELECT return_pct, net_return_pct, benchmark_return_pct, excess_return_pct
            FROM candidate_evaluations
            JOIN candidates c ON c.id = candidate_evaluations.candidate_id
            WHERE c.ticker = '000007.SZ'
            """
        ).fetchone()
        self.assertAlmostEqual(row["return_pct"], 20.0, places=3)
        self.assertAlmostEqual(row["net_return_pct"], 19.82, places=3)
        self.assertAlmostEqual(row["benchmark_return_pct"], 5.0, places=3)
        self.assertAlmostEqual(row["excess_return_pct"], 14.82, places=3)

    def test_require_adjusted_excludes_raw_fallback_samples(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('CN_A', '000008.SZ', '未复权测试', 'test', '000008', '2026-04-01T00:00:00Z')
            """
        )
        for date, close in (("2026-04-01", 10.0), ("2026-04-02", 10.0), ("2026-04-03", 11.0)):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                )
                VALUES ('CN_A', '000008.SZ', ?, ?, ?, ?, ?, 1000000, NULL,
                        NULL, NULL, NULL, ?, ?, ?, ?, 1.0, 'RAW_FALLBACK')
                """,
                (date, close, close, close, close, close, close, close, close),
            )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-04-01', 'CN_A', '000008.SZ', '未复权测试', 'trend_breakout', 95,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                12, 13, 2.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-04-01', '2026-04-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        count = evaluate_candidates(
            self.conn,
            "2026-04-01",
            "2026-04-03",
            require_adjusted=True,
        )
        self.assertEqual(count, 0)

    def test_confirm_candidates_uses_candidate_own_next_bar(self) -> None:
        self.conn.execute(
            """
            INSERT INTO instruments (market, ticker, name, source, source_symbol, created_at)
            VALUES ('CN_A', '000002.SZ', '确认测试', 'test', '000002', '2026-02-01T00:00:00Z')
            """
        )
        for date, close, low, volume in (
            ("2026-02-01", 10.0, 9.8, 1000),
            ("2026-02-03", 10.5, 9.7, 900),
            ("2026-02-04", 10.6, 10.2, 950),
            ("2026-02-05", 12.2, 10.8, 1200),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount,
                    amplitude_pct, change_pct, turnover_pct
                )
                VALUES ('CN_A', '000002.SZ', ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)
                """,
                (date, close, close, close + 0.2, low, volume),
            )
        self.conn.execute(
            """
            INSERT INTO price_bars (
                market, ticker, date, open, close, high, low, volume, amount,
                amplitude_pct, change_pct, turnover_pct
            )
            VALUES ('CN_A', '000003.SZ', '2026-02-02', 1, 1, 1, 1, 1, NULL, NULL, NULL, NULL)
            """
        )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-02-01', 'CN_A', '000002.SZ', '确认测试', 'trend_breakout', 90,
                'WATCH_CONFIRMATION', 10, 9.9, 10.1, 9.5,
                12, 13, 2.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-02-01', '2026-02-01T00:00:00Z'
            )
            """
        )
        self.conn.commit()

        self.assertEqual(confirm_candidates(self.conn, "2026-02-02"), (0, 0))
        row = self.conn.execute("SELECT confirmation_status FROM candidates WHERE ticker = '000002.SZ'").fetchone()
        self.assertEqual(row["confirmation_status"], "PENDING")

        self.assertEqual(confirm_candidates(self.conn, "2026-02-03"), (1, 0))
        plan = daily_action_plan(self.conn, "2026-02-03")
        self.assertEqual(plan[0]["ticker"], "000002.SZ")
        self.assertEqual(plan[0]["plan_bucket"], "今日确认")

        after_confirmation = run_portfolio_backtest(
            self.conn,
            "2026-02-01",
            "2026-02-01",
            "2026-02-05",
            max_positions=1,
            initial_capital=100000,
        )
        self.assertEqual(after_confirmation.trade_count, 1)
        self.assertEqual(after_confirmation.trades[0].entry_date, "2026-02-04")
        pre_entry_equity = [
            equity for date, equity in after_confirmation.daily_equity
            if date <= "2026-02-03"
        ]
        self.assertTrue(pre_entry_equity)
        self.assertTrue(all(equity == 100000 for equity in pre_entry_equity))

    def test_weight_adjustment_uses_strategy_target_horizon(self) -> None:
        self.conn.execute(
            """
            INSERT INTO strategies (
                id, name, market_scope, thesis, entry_rules_json, exit_rules_json,
                target_horizon_days, status, weight, created_at
            )
            VALUES ('unit_target_horizon', '目标周期测试', 'CN_A', 'test', '{}', '{}', 5, 'ACTIVE', 1.0, '2026-03-01T00:00:00Z')
            """
        )
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            )
            VALUES (
                '2026-03-01', 'CN_A', '000004.SZ', '周期测试', 'unit_target_horizon', 88,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                12, 13, 2.0, 'test', 'test', 'test', '[]',
                'WATCHLIST', 'PENDING', '2026-03-01', '2026-03-01T00:00:00Z'
            )
            """
        )
        candidate_id = self.conn.execute(
            "SELECT id FROM candidates WHERE strategy_id = 'unit_target_horizon'"
        ).fetchone()["id"]
        for horizon, net_return, net_win in ((5, -3.0, 0), (10, 8.0, 1)):
            self.conn.execute(
                """
                INSERT INTO candidate_horizon_evaluations (
                    candidate_id, horizon_days, through_date, observed_days,
                    reference_date, reference_close, execution_date, execution_price,
                    execution_type, execution_note, end_date, end_close, return_pct,
                    gross_return_pct, cost_pct, net_return_pct, net_win,
                    max_gain_pct, max_drawdown_pct, hit_stop, hit_target_1,
                    hit_target_2, exit_type, exit_date, exit_price, exit_note, created_at
                )
                VALUES (?, ?, '2026-03-20', ?, '2026-03-02', 10, '2026-03-02', 10,
                        'NEXT_OPEN', 'test', '2026-03-10', ?, ?, ?, 0.18, ?, ?,
                        5, -4, 0, 0, 0, 'HOLD', '2026-03-10', ?, 'test', '2026-03-20T00:00:00Z')
                """,
                (
                    candidate_id,
                    horizon,
                    horizon,
                    10 * (1 + net_return / 100),
                    net_return,
                    net_return,
                    net_return,
                    net_win,
                    10 * (1 + net_return / 100),
                ),
            )
        self.conn.commit()

        suggestions = suggest_strategy_weight_adjustments(
            self.conn,
            "2026-03-01",
            "2026-03-01",
            "2026-03-20",
            min_samples=1,
        )
        item = next(row for row in suggestions if row["strategy_id"] == "unit_target_horizon")
        self.assertEqual(item["target_horizon_days"], 5)
        self.assertEqual(item["recommendation"], "DOWN_WEIGHT")


    def test_score_penalty_for_low_reward_risk(self) -> None:
        """Candidates with reward_risk < 1.0 should be capped at 70."""
        c = _candidate(
            as_of_date="2026-05-13",
            market="CN_A",
            ticker="000001.SZ",
            name="Test",
            strategy_id="trend_breakout",
            score=90.0,
            action="BUY_CANDIDATE",
            close=10.0,
            stop_loss=9.5,
            target_1=10.2,
            target_2=10.5,
            thesis="test",
            trigger_condition="test",
            risk_notes="test",
            evidence=[],
        )
        reward_risk = c["reward_risk_ratio"]
        self.assertLess(reward_risk, 1.0)
        self.assertLessEqual(c["candidate_score"], 70.0)

    def test_score_no_penalty_for_good_reward_risk(self) -> None:
        """Candidates with reward_risk >= 1.0 should not be capped."""
        c = _candidate(
            as_of_date="2026-05-13",
            market="CN_A",
            ticker="000001.SZ",
            name="Test",
            strategy_id="trend_breakout",
            score=90.0,
            action="BUY_CANDIDATE",
            close=10.0,
            stop_loss=9.0,
            target_1=11.5,
            target_2=12.0,
            thesis="test",
            trigger_condition="test",
            risk_notes="test",
            evidence=[],
        )
        self.assertGreaterEqual(c["reward_risk_ratio"], 1.0)
        self.assertGreater(c["candidate_score"], 70.0)

    def test_limit_down_defers_exit(self) -> None:
        """A position whose exit day is limit-down should be deferred."""
        from alpha_ledger.portfolio_backtest import _is_one_price_limit_down, _get_price_bar
        conn = self.conn
        conn.execute(
            "INSERT INTO price_bars (market, ticker, date, open, close, high, low, volume, amount, change_pct, adjustment_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "999999.SZ", "2026-05-01", 10.0, 10.0, 10.0, 10.0, 100000, 1000000, 0.0, "RAW_FALLBACK"),
        )
        conn.execute(
            "INSERT INTO price_bars (market, ticker, date, open, close, high, low, volume, amount, change_pct, adjustment_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("CN_A", "999999.SZ", "2026-05-02", 9.05, 9.05, 9.05, 9.05, 1000, 9050, -9.5, "RAW_FALLBACK"),
        )
        conn.commit()
        bar = _get_price_bar(conn, "CN_A", "999999.SZ", "2026-05-02")
        self.assertIsNotNone(bar)
        self.assertTrue(_is_one_price_limit_down(conn, "CN_A", "999999.SZ", bar))

    def test_limit_down_deferred_exit_reprices_on_next_sellable_day(self) -> None:
        from alpha_ledger.portfolio_backtest import _OpenPosition, _close_due_positions

        for row in (
            ("2026-05-01", 10.0, 10.0, 10.0, 10.0),
            ("2026-05-02", 9.05, 9.05, 9.05, 9.05),
            ("2026-05-03", 8.80, 9.00, 9.10, 8.70),
        ):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES ('CN_A', '999998.SZ', ?, ?, ?, ?, ?, 100000, 1000000, 0,
                          ?, ?, ?, ?, 1, 'ADJUSTED')
                """,
                (row[0], row[1], row[2], row[3], row[4], row[1], row[2], row[3], row[4]),
            )
        position = _OpenPosition(
            ticker="999998.SZ",
            name="跌停测试",
            market="CN_A",
            strategy_id="trend_breakout",
            entry_date="2026-05-01",
            entry_price=10.0,
            entry_return_price=10.0,
            stop_loss=9.5,
            target_1=None,
            target_2=None,
            horizon_days=5,
            position_size=10000.0,
            shares=1000.0,
            exit_date="2026-05-02",
            exit_price=9.5,
            exit_type="STOP_LOSS",
            return_pct=-5.0,
            benchmark_return_pct=None,
            excess_return_pct=None,
            cost=18.0,
        )
        open_positions, capital, _ = _close_due_positions(
            self.conn, [position], "2026-05-02", 0.0, [], 0, None
        )
        self.assertEqual(len(open_positions), 1)
        closed: list[object] = []
        open_positions, capital, _ = _close_due_positions(
            self.conn, open_positions, "2026-05-03", capital, closed, 0, None
        )
        self.assertEqual(open_positions, [])
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].exit_price, 8.80, places=2)
        self.assertIn("LIMIT_DOWN_DEFERRED", closed[0].exit_type)
        self.assertLess(closed[0].net_return_pct, -10.0)

    def test_cn_a_board_specific_limit_ratios(self) -> None:
        self.assertAlmostEqual(cn_a_limit_pct("600000.SS", "浦发银行"), 0.10)
        self.assertAlmostEqual(cn_a_limit_pct("300001.SZ", "创业板股"), 0.20)
        self.assertAlmostEqual(cn_a_limit_pct("688001.SS", "科创板股"), 0.20)
        self.assertAlmostEqual(cn_a_limit_pct("430001.BJ", "北交所股"), 0.30)
        self.assertAlmostEqual(cn_a_limit_pct("600001.SS", "ST测试"), 0.05)

    def test_event_catalyst_filters_plain_research_activity(self) -> None:
        self._insert_event_test_stock("301111.SZ")
        self.conn.execute(
            """
            INSERT INTO corporate_events (
                market, ticker, name, event_date, event_type, title, source, source_url,
                importance_score, summary, created_at
            ) VALUES ('CN_A', '301111.SZ', '测试股份', '2026-05-15', '投资者关系活动记录表',
                      '投资者关系调研活动：公司介绍产品客户情况', 'test', '', 0.95, '', 'now')
            """
        )
        rows = screen_event_catalyst(self.conn, "2026-05-15")
        self.assertFalse(any(row["ticker"] == "301111.SZ" for row in rows))

    def test_event_catalyst_keeps_hard_contract_event(self) -> None:
        self._insert_event_test_stock("301112.SZ")
        self.conn.execute(
            """
            INSERT INTO corporate_events (
                market, ticker, name, event_date, event_type, title, source, source_url,
                importance_score, summary, created_at
            ) VALUES ('CN_A', '301112.SZ', '测试股份', '2026-05-15', '重大合同',
                      '公司签订重大合同订单并带来收入增长', 'test', '', 0.80, '', 'now')
            """
        )
        rows = screen_event_catalyst(self.conn, "2026-05-15")
        row = next((candidate for candidate in rows if candidate["ticker"] == "301112.SZ"), None)
        self.assertIsNotNone(row)
        self.assertGreaterEqual(float(row["reward_risk_ratio"]), 1.0)
        stop_distance = (float(row["entry_price"]) - float(row["stop_loss"])) / float(row["entry_price"])
        self.assertGreaterEqual(stop_distance, 0.03)
        self.assertLessEqual(stop_distance, 0.10)

    def test_cn_a_pead_quality_surprise_requires_event_price_and_m2_m3(self) -> None:
        ticker = "301113.SZ"
        name = "财报测试"
        self.conn.execute(
            """
            INSERT OR REPLACE INTO instruments
                (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', ?, ?, 'test', ?, 1, '[]', 'now')
            """,
            (ticker, name, ticker),
        )
        previous_close = 10.0
        for day in range(1, 30):
            date_value = f"2026-05-{day:02d}"
            if day == 28:
                close = previous_close * 1.03
                volume = 1_700_000
                change_pct = 3.0
                high = close * 1.03
                low = close * 0.96
            elif day == 29:
                close = previous_close * 1.01
                volume = 1_200_000
                change_pct = 1.0
                high = close * 1.02
                low = close * 0.98
            else:
                close = 10.0 + day * 0.03
                volume = 1_000_000
                change_pct = 0.3
                high = close * 1.02
                low = close * 0.98
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "CN_A",
                    ticker,
                    date_value,
                    close * 0.99,
                    close,
                    high,
                    low,
                    volume,
                    65_000_000,
                    change_pct,
                    close * 0.99,
                    close,
                    high,
                    low,
                    1.0,
                    "ADJUSTED",
                ),
            )
            previous_close = close

        self.conn.execute(
            """
            INSERT INTO corporate_events (
                market, ticker, name, event_date, event_type, title, source, source_url,
                importance_score, summary, created_at
            ) VALUES ('CN_A', ?, ?, '2026-05-28', 'EARNINGS_REPORT',
                      '2026年一季报净利润超预期增长', 'test', '', 0.90, '', 'now')
            """,
            (ticker, name),
        )
        for metric_name, metric_value in [
            ("净利润增长率(%)", 40.0),
            ("主营业务收入增长率(%)", 18.0),
            ("加权净资产收益率(%)", 12.0),
        ]:
            self.conn.execute(
                """
                INSERT INTO financial_metrics (
                    market, ticker, report_date, published_date, metric_name,
                    metric_value, unit, source, created_at
                ) VALUES ('CN_A', ?, '2026-03-31', '2026-05-28', ?, ?, '%', 'test', 'now')
                """,
                (ticker, metric_name, metric_value),
            )
        for model_name, model_version, percentile in [
            ("qlib_alpha158_20250101", "t10_v2", 0.62),
            ("qlib_alpha158_20260101", "t10_v2", 0.41),
        ]:
            self.conn.execute(
                """
                INSERT INTO model_scores (
                    model_name, model_version, market, ticker, score_date, score,
                    rank, percentile, source_artifact, created_at
                ) VALUES (?, ?, 'CN_A', ?, '2026-05-29', 0.1, 1, ?, 'test', 'now')
                """,
                (model_name, model_version, ticker, percentile),
            )

        rows = screen_cn_a_pead_quality_surprise(self.conn, "2026-05-29")
        row = next((candidate for candidate in rows if candidate["ticker"] == ticker), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["strategy_id"], "cn_a_pead_quality_surprise")
        self.assertGreaterEqual(float(row["candidate_score"]), 75.0)

    def test_risk_parity_sizes_high_risk_positions_smaller(self) -> None:
        low_risk = _risk_parity_position_size(
            capital=1_000_000,
            entry_price=10.0,
            stop_loss=9.5,
            remaining_slots=5,
            initial_capital=1_000_000,
            risk_budget_pct=2.0,
            max_position_pct=50.0,
        )
        high_risk = _risk_parity_position_size(
            capital=1_000_000,
            entry_price=10.0,
            stop_loss=8.0,
            remaining_slots=5,
            initial_capital=1_000_000,
            risk_budget_pct=2.0,
            max_position_pct=50.0,
        )
        self.assertGreater(low_risk, high_risk)

    def test_daily_plan_marks_stale_date_and_hides_buy_list(self) -> None:
        report = render_daily_plan(self.conn, "2026-05-27")
        self.assertIn("data_status: `STALE_DATA`", report)
        self.assertNotIn("## 可操作买入清单", report)

    def test_drawdown_circuit_breaker_reduces_positions(self) -> None:
        """When drawdown exceeds threshold, max effective positions should decrease."""
        result = run_portfolio_backtest(
            self.conn,
            "2026-04-01",
            "2026-05-15",
            "2026-05-25",
            max_positions=5,
            drawdown_reduce_threshold=5.0,
            drawdown_halt_threshold=10.0,
        )
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.max_drawdown_pct, 0.0)

    def test_sector_diversification_limits_same_sector(self) -> None:
        """Portfolio should not hold more than max_per_sector positions in the same sector."""
        result = run_portfolio_backtest(
            self.conn,
            "2026-04-01",
            "2026-05-15",
            "2026-05-25",
            max_positions=5,
            max_per_sector=2,
        )
        self.assertIsNotNone(result)
        if result.trades:
            from collections import Counter
            sector_counts: Counter[str] = Counter()
            for trade in result.trades:
                sector_key = trade.ticker[:3] if trade.market == "CN_A" and len(trade.ticker) >= 3 else trade.ticker
                sector_counts[sector_key] += 1
            for count in sector_counts.values():
                self.assertLessEqual(count, 2)

    def test_compute_sharpe_ratio(self) -> None:
        """Sharpe ratio should be positive for mostly positive returns."""
        from alpha_ledger.metrics import compute_sharpe_ratio, compute_sortino_ratio
        returns = [2.0, -1.0, 3.0, -0.5, 2.5, 1.0]
        sharpe = compute_sharpe_ratio(returns)
        sortino = compute_sortino_ratio(returns)
        self.assertGreater(sharpe, 0.0)
        self.assertGreater(sortino, 0.0)

    def test_compute_sharpe_ratio_empty(self) -> None:
        """Sharpe ratio should be 0 for empty or single-element lists."""
        from alpha_ledger.metrics import compute_sharpe_ratio
        self.assertEqual(compute_sharpe_ratio([]), 0.0)
        self.assertEqual(compute_sharpe_ratio([1.0]), 0.0)

    def test_score_calibration_monotonicity(self) -> None:
        """After screening and evaluation, higher score buckets should have higher returns."""
        from alpha_ledger.metrics import score_calibration
        screen_all(self.conn, "2026-05-13")
        evaluate_candidates(self.conn, "2026-05-13", "2026-05-25")
        calibration = score_calibration(self.conn, "2026-04-01", "2026-05-15", "2026-05-25", 10)
        non_empty = [b for b in calibration if int(b["sample_count"]) > 0]
        if len(non_empty) >= 2:
            for i in range(len(non_empty) - 1):
                curr_ret = float(non_empty[i]["avg_net_return"] or 0)
                next_ret = float(non_empty[i + 1]["avg_net_return"] or 0)
                self.assertGreaterEqual(curr_ret, next_ret)

    def test_data_update_only_fetches_missing_price_dates(self) -> None:
        import alpha_ledger.data_ops as data_ops_module

        calls: list[tuple[str, str]] = []
        original_fetch_bars = data_ops_module.fetch_bars

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            calls.append((start.isoformat(), end.isoformat()))
            rows = []
            for instrument in instruments[:3]:
                rows.append(
                    {
                        "market": instrument.market,
                        "ticker": instrument.ticker,
                        "date": end.isoformat(),
                        "open": 10.0,
                        "close": 10.5,
                        "high": 10.8,
                        "low": 9.9,
                        "volume": 100000,
                        "amount": 1000000,
                        "change_pct": 1.0,
                        "adjustment_status": "ADJUSTED",
                    }
                )
            return rows, []

        data_ops_module.fetch_bars = fake_fetch_bars
        try:
            first = data_update(self.conn, "2026-05-26", fetch_events=False, fetch_intraday=False)
            second = data_update(self.conn, "2026-05-26", fetch_events=False, fetch_intraday=False)
        finally:
            data_ops_module.fetch_bars = original_fetch_bars

        self.assertEqual(calls, [("2026-05-26", "2026-05-26")])
        self.assertGreater(first.price_bars, 0)
        self.assertEqual(second.price_bars, 0)

    def test_data_update_records_fetch_errors(self) -> None:
        import alpha_ledger.data_ops as data_ops_module

        original_fetch_bars = data_ops_module.fetch_bars

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            return [], ["000001.SZ sina_cn: boom"]

        data_ops_module.fetch_bars = fake_fetch_bars
        try:
            result = data_update(self.conn, "2026-05-26", fetch_events=False, fetch_intraday=False)
        finally:
            data_ops_module.fetch_bars = original_fetch_bars

        self.assertEqual(result.status, "FAILED")
        row = self.conn.execute("SELECT COUNT(*) AS c FROM data_fetch_errors").fetchone()
        self.assertEqual(row["c"], 1)

    def test_data_update_adjust_none_passes_no_adjustment(self) -> None:
        import alpha_ledger.data_ops as data_ops_module

        calls: list[object] = []
        original_fetch_bars = data_ops_module.fetch_bars

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            calls.append(adjust)
            return [], []

        data_ops_module.fetch_bars = fake_fetch_bars
        try:
            data_update(self.conn, "2026-05-26", fetch_events=False, fetch_intraday=False, adjust=None)
        finally:
            data_ops_module.fetch_bars = original_fetch_bars

        self.assertEqual(calls, [None])

    def test_data_backfill_adjust_none_passes_no_adjustment(self) -> None:
        import alpha_ledger.cli as cli_module

        calls: list[object] = []
        original_fetch_bars = cli_module.fetch_bars
        original_read_universe = cli_module.read_universe

        def fake_read_universe(path, markets=None, symbols=None):
            from alpha_ledger.market_data import Instrument
            return [Instrument("CN_A", "000001.SZ", "平安银行", "test", "sz000001", True, ())]

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            calls.append(adjust)
            return [
                {
                    "market": "CN_A",
                    "ticker": "000001.SZ",
                    "date": start.isoformat(),
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "volume": 1000,
                    "amount": 10000,
                    "change_pct": 0.0,
                    "adjustment_status": "RAW_FALLBACK",
                }
            ], []

        cli_module.fetch_bars = fake_fetch_bars
        cli_module.read_universe = fake_read_universe
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite") as db_file:
                command_data_backfill(
                    db_file.name,
                    "2026-05-25",
                    "2026-05-26",
                    "CN_A",
                    batch_days=10,
                    throttle=0.0,
                    adjust="none",
                )
        finally:
            cli_module.fetch_bars = original_fetch_bars
            cli_module.read_universe = original_read_universe

        self.assertEqual(calls, [None])

    def test_data_update_repair_coverage_defaults_to_benchmarks_only(self) -> None:
        import alpha_ledger.data_ops as data_ops_module

        self.conn.execute(
            """
            INSERT OR REPLACE INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', '000001.SZ', '平安银行', 'test', 'sz000001', 1, '[]', 'now')
            """
        )
        for ticker, status in (("000300.SS", "ADJUSTED"), ("000001.SZ", "RAW_FALLBACK")):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES ('CN_A', ?, '2026-05-26', 10, 10, 10, 10, 1000, 10000, 0,
                          10, 10, 10, 10, 1, ?)
                """,
                (ticker, status),
            )

        calls: list[list[str]] = []
        original_fetch_bars = data_ops_module.fetch_bars

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            calls.append([item.ticker for item in instruments])
            rows = []
            for instrument in instruments:
                rows.append(
                    {
                        "market": instrument.market,
                        "ticker": instrument.ticker,
                        "date": end.isoformat(),
                        "open": 10.0,
                        "close": 10.0,
                        "high": 10.0,
                        "low": 10.0,
                        "volume": 1000,
                        "amount": 10000,
                        "change_pct": 0.0,
                        "adj_open": 10.0,
                        "adj_close": 10.0,
                        "adj_high": 10.0,
                        "adj_low": 10.0,
                        "adj_factor": 1.0,
                        "adjustment_status": "ADJUSTED",
                    }
                )
            return rows, []

        data_ops_module.fetch_bars = fake_fetch_bars
        try:
            result = data_update(
                self.conn,
                "2026-05-26",
                fetch_events=False,
                fetch_intraday=False,
                repair_coverage=True,
            )
        finally:
            data_ops_module.fetch_bars = original_fetch_bars

        fetched = {ticker for call in calls for ticker in call}
        self.assertNotIn("000001.SZ", fetched)
        self.assertIn("000905.SS", fetched)
        self.assertGreater(result.price_bars, 0)

    def test_data_update_repair_coverage_all_includes_raw_fallback(self) -> None:
        import alpha_ledger.data_ops as data_ops_module

        self.conn.execute(
            """
            INSERT OR REPLACE INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at)
            VALUES ('CN_A', '000001.SZ', '平安银行', 'test', 'sz000001', 1, '[]', 'now')
            """
        )
        for ticker, status in (("000300.SS", "ADJUSTED"), ("000001.SZ", "RAW_FALLBACK")):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES ('CN_A', ?, '2026-05-26', 10, 10, 10, 10, 1000, 10000, 0,
                          10, 10, 10, 10, 1, ?)
                """,
                (ticker, status),
            )

        calls: list[list[str]] = []
        original_fetch_bars = data_ops_module.fetch_bars

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            calls.append([item.ticker for item in instruments])
            return [
                {
                    "market": instrument.market,
                    "ticker": instrument.ticker,
                    "date": end.isoformat(),
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "volume": 1000,
                    "amount": 10000,
                    "change_pct": 0.0,
                    "adjustment_status": "ADJUSTED",
                }
                for instrument in instruments
            ], []

        data_ops_module.fetch_bars = fake_fetch_bars
        try:
            data_update(
                self.conn,
                "2026-05-26",
                fetch_events=False,
                fetch_intraday=False,
                repair_coverage=True,
                repair_scope="all",
            )
        finally:
            data_ops_module.fetch_bars = original_fetch_bars

        fetched = {ticker for call in calls for ticker in call}
        self.assertIn("000001.SZ", fetched)
        self.assertIn("000905.SS", fetched)

    def test_data_audit_downgrades_when_adjusted_and_intraday_missing(self) -> None:
        result = audit_data_coverage(self.conn, "2026-05-13", "2026-05-13", "CN_A", write=True)
        self.assertNotEqual(result.confidence_level, CONFIDENCE_HIGH)
        self.assertFalse(result.allow_formal_daily)

    def test_short_term_adjustment_ignore_caps_confidence_at_medium(self) -> None:
        for ticker in ("000300.SS", "000905.SS", "000852.SS", "399006.SZ", "000688.SS", "899050.BJ"):
            self.conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES ('CN_A', ?, '2026-05-26', 10, 10, 10, 10, 1000, 10000, 0,
                          10, 10, 10, 10, 1, 'ADJUSTED')
                """,
                (ticker,),
            )
        self.conn.execute(
            """
            INSERT INTO price_bars (
                market, ticker, date, open, close, high, low, volume, amount, change_pct,
                adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
            ) VALUES ('CN_A', '000001.SZ', '2026-05-26', 10, 10, 10, 10, 1000, 10000, 0,
                      10, 10, 10, 10, 1, 'RAW_FALLBACK')
            """
        )
        strict = audit_data_coverage(self.conn, "2026-05-26", "2026-05-26", "CN_A", write=False)
        short_term = audit_data_coverage(
            self.conn,
            "2026-05-26",
            "2026-05-26",
            "CN_A",
            write=False,
            ignore_adjustment_for_short_term=True,
        )
        self.assertEqual(strict.confidence_level, "LOW_CONFIDENCE")
        self.assertEqual(short_term.confidence_level, "MEDIUM_CONFIDENCE")
        self.assertFalse(short_term.allow_formal_daily)

    def test_data_audit_uses_benchmark_calendar_instead_of_weekdays(self) -> None:
        self.conn.execute(
            """
            INSERT INTO price_bars (
                market, ticker, date, open, close, high, low, volume, amount, change_pct,
                adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
            ) VALUES ('CN_A', '000300.SS', '2026-05-06', 10, 10, 10, 10, 1000, 10000, 0,
                      10, 10, 10, 10, 1, 'ADJUSTED')
            """
        )
        result = audit_data_coverage(self.conn, "2026-05-01", "2026-05-06", "CN_A", write=False)
        self.assertNotIn("2026-05-01", result.missing_dates)
        self.assertNotIn("2026-05-04", result.missing_dates)
        self.assertNotIn("2026-05-05", result.missing_dates)

    def test_data_audit_marks_no_trade_intraday_universe_as_fully_covered(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        try:
            for ticker in ("000300.SS", "000905.SS", "000852.SS", "399006.SZ", "000688.SS", "899050.BJ"):
                conn.execute(
                    """
                    INSERT INTO price_bars (
                        market, ticker, date, open, close, high, low, volume, amount, change_pct,
                        adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                    ) VALUES ('CN_A', ?, '2026-05-13', 10, 10, 10, 10, 0, 0, 0,
                              10, 10, 10, 10, 1, 'ADJUSTED')
                    """,
                    (ticker,),
                )
            conn.execute(
                """
                INSERT INTO price_bars (
                    market, ticker, date, open, close, high, low, volume, amount, change_pct,
                    adj_open, adj_close, adj_high, adj_low, adj_factor, adjustment_status
                ) VALUES ('CN_A', '000001.SZ', '2026-05-13', 10, 10, 10, 10, 0, 0, 0,
                          10, 10, 10, 10, 1, 'ADJUSTED')
                """
            )

            result = audit_data_coverage(conn, "2026-05-13", "2026-05-13", "CN_A", write=True)
            self.assertEqual(result.intraday_tradable_target_count, 0)
            self.assertEqual(result.intraday_tradable_symbol_count, 0)
            self.assertEqual(result.intraday_tradable_missing_count, 0)
            self.assertEqual(result.intraday_no_trade_symbol_count, 7)
            self.assertEqual(result.intraday_coverage_pct, 100.0)
            self.assertEqual(result.confidence_level, CONFIDENCE_HIGH)
            self.assertTrue(result.allow_formal_daily)
            self.assertTrue(any("无交易标的不计为待补缺口" in note for note in result.notes))

            row = conn.execute(
                """
                SELECT intraday_tradable_target_count, intraday_tradable_symbol_count,
                       intraday_tradable_missing_count, intraday_no_trade_symbol_count
                FROM data_coverage_daily
                WHERE market = 'CN_A' AND date = '2026-05-13'
                """
            ).fetchone()
            self.assertEqual(row["intraday_tradable_target_count"], 0)
            self.assertEqual(row["intraday_tradable_symbol_count"], 0)
            self.assertEqual(row["intraday_tradable_missing_count"], 0)
            self.assertEqual(row["intraday_no_trade_symbol_count"], 7)
        finally:
            conn.close()

    def test_adjustment_probe_reports_success_partial_and_failed(self) -> None:
        import alpha_ledger.data_ops as data_ops_module

        for ticker in ("600000.SS", "000001.SZ", "002674.SZ"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at)
                VALUES ('CN_A', ?, ?, 'test', ?, 1, '[]', 'now')
                """,
                (ticker, ticker, ticker),
            )

        original_fetch_bars = data_ops_module.fetch_bars

        def fake_fetch_bars(instruments, start, end, throttle_seconds=0.0, adjust="qfq"):
            ticker = instruments[0].ticker
            base = {
                "market": "CN_A",
                "ticker": ticker,
                "date": end.isoformat(),
                "open": 10.0,
                "close": 10.0,
                "high": 10.0,
                "low": 10.0,
                "volume": 1000,
                "amount": 10000,
                "change_pct": 0.0,
            }
            if ticker == "600000.SS":
                return [{**base, "adjustment_status": "ADJUSTED"}], []
            if ticker == "000001.SZ":
                return [{**base, "adjustment_status": "ADJUSTED"}, {**base, "date": start.isoformat(), "adjustment_status": "RAW_FALLBACK"}], []
            return [], [f"{ticker} adjusted source returned no rows"]

        data_ops_module.fetch_bars = fake_fetch_bars
        try:
            result = probe_adjustment_sources(self.conn, "2026-05-25", "2026-05-26", sample_size=3, throttle_seconds=0.0)
        finally:
            data_ops_module.fetch_bars = original_fetch_bars

        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.partial_count, 1)
        self.assertEqual(result.failed_count, 1)

    def test_layered_benchmark_mapping(self) -> None:
        self.assertEqual(benchmark_for_asset("CN_A", "300001.SZ", "auto"), "399006.SZ")
        self.assertEqual(benchmark_for_asset("CN_A", "688001.SS", "auto"), "000688.SS")
        self.assertEqual(benchmark_for_asset("CN_A", "920001.BJ", "auto"), "899050.BJ")
        self.assertEqual(benchmark_for_asset("CN_A", "600000.SS", "auto"), "000300.SS")
        self.assertEqual(benchmark_for_asset("CN_A", "000001.SZ", "000300.SS"), "000300.SS")

    def test_loss_review_tags_high_score_research_losses(self) -> None:
        self.conn.execute(
            """
            INSERT INTO candidates (
                as_of_date, market, ticker, name, strategy_id, candidate_score,
                action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                target_1, target_2, reward_risk_ratio, thesis, trigger_condition,
                risk_notes, evidence_json, status, confirmation_status, data_date, created_at
            ) VALUES (
                '2026-05-13', 'CN_A', '000777.SZ', '亏损测试', 'trend_breakout', 82,
                'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                12, 13, 2.0, 'test', '2026-05-13 调研活动 涨幅 10.00%，量比 0.80',
                'test', '[]', 'WATCHLIST', 'PENDING', '2026-05-13', 'now'
            )
            """
        )
        candidate_id = self.conn.execute("SELECT id FROM candidates WHERE ticker = '000777.SZ'").fetchone()["id"]
        self.conn.execute(
            """
            INSERT INTO candidate_evaluations (
                candidate_id, through_date, observed_days, reference_date, reference_close,
                execution_date, execution_price, execution_type, execution_note,
                end_date, end_close, return_pct, gross_return_pct, cost_pct,
                net_return_pct, net_win, benchmark_return_pct, excess_return_pct,
                max_gain_pct, max_drawdown_pct, hit_stop, hit_target_1, hit_target_2,
                exit_type, exit_date, exit_price, exit_note, created_at
            ) VALUES (?, '2026-05-25', 5, '2026-05-14', 10,
                      '2026-05-14', 10, 'NEXT_OPEN_DAILY', 'daily',
                      '2026-05-20', 8.5, -15, -15, 0.18,
                      -15.18, 0, -1, -14.18,
                      1, -16, 1, 0, 0,
                      'STOP_LOSS', '2026-05-20', 8.5, 'stop', 'now')
            """,
            (candidate_id,),
        )
        report = render_loss_review(self.conn, "2026-05-13", "2026-05-13", "2026-05-25")
        self.assertIn("Loss Review", report)
        self.assertTrue("高分亏损" in report or "弱事件/调研" in report or "止损触发" in report)

    def test_expected_value_score_sorts_daily_plan_before_raw_score(self) -> None:
        for ticker, score, ev in (("000101.SZ", 90.0, 1.0), ("000102.SZ", 78.0, 8.0)):
            self.conn.execute(
                """
                INSERT INTO instruments (market, ticker, name, source, source_symbol, active, tags_json, created_at)
                VALUES ('CN_A', ?, ?, 'test', ?, 1, '[]', 'now')
                """,
                (ticker, ticker, ticker),
            )
            self.conn.execute(
                """
                INSERT INTO candidates (
                    as_of_date, market, ticker, name, strategy_id, candidate_score,
                    action, entry_price, buy_zone_low, buy_zone_high, stop_loss,
                    target_1, target_2, reward_risk_ratio, expected_value_score,
                    thesis, trigger_condition, risk_notes, evidence_json, status,
                    confirmation_status, data_date, created_at
                ) VALUES (
                    '2026-02-10', 'CN_A', ?, ?, 'trend_breakout', ?,
                    'BUY_CANDIDATE', 10, 9.9, 10.1, 9,
                    12, 13, 2.0, ?, 'test', 'test', 'test', '[]',
                    'WATCHLIST', 'PENDING', '2026-02-10', 'now'
                )
                """,
                (ticker, ticker, score, ev),
            )
        rows = daily_action_plan(self.conn, "2026-02-10")
        self.assertEqual(rows[0]["ticker"], "000102.SZ")

    def test_baostock_symbol_conversion_sz(self) -> None:
        from alpha_ledger.market_data import _cn_a_to_baostock_symbol
        self.assertEqual(_cn_a_to_baostock_symbol("002674.SZ"), "sz.002674")
        self.assertEqual(_cn_a_to_baostock_symbol("300750.SZ"), "sz.300750")

    def test_baostock_symbol_conversion_ss(self) -> None:
        from alpha_ledger.market_data import _cn_a_to_baostock_symbol
        self.assertEqual(_cn_a_to_baostock_symbol("600519.SS"), "sh.600519")
        self.assertEqual(_cn_a_to_baostock_symbol("600519.SH"), "sh.600519")
        self.assertEqual(_cn_a_to_baostock_symbol("601318.SS"), "sh.601318")

    def test_instrument_canonicalizes_cn_a_ticker_but_preserves_source_symbol(self) -> None:
        from alpha_ledger.market_data import Instrument
        instrument = Instrument("CN_A", "600519.SH", "贵州茅台", "sina_cn", "sh600519", True, ())
        self.assertEqual(instrument.ticker, "600519.SS")
        self.assertEqual(instrument.source_symbol, "sh600519")
        self.assertEqual(instrument.as_row()["ticker"], "600519.SS")
        self.assertEqual(instrument.as_row()["source_symbol"], "sh600519")

    def test_baostock_symbol_conversion_bj_raises(self) -> None:
        from alpha_ledger.market_data import _cn_a_to_baostock_symbol, MarketDataError
        with self.assertRaises(MarketDataError):
            _cn_a_to_baostock_symbol("430047.BJ")

    def test_baostock_adjustment_returns_adjusted_map(self) -> None:
        from alpha_ledger.market_data import fetch_baostock_cn_adjusted_daily_map, Instrument
        mock_bs = MagicMock()
        mock_rs = MagicMock()
        mock_rs.error_code = "0"
        mock_rs.next.side_effect = [True, True, False]
        mock_rs.get_row_data.side_effect = [
            ["2026-05-20", "100.0", "105.0", "99.0", "103.0"],
            ["2026-05-21", "103.0", "108.0", "102.0", "107.0"],
        ]
        mock_bs.query_history_k_data_plus.return_value = mock_rs
        mock_bs.login.return_value = MagicMock(error_code="0")
        instrument = Instrument("CN_A", "600519.SS", "贵州茅台", "sina_cn", "sh600519", True, ())
        with patch("alpha_ledger.market_data._baostock_logged_in", True), \
             patch.dict("sys.modules", {"baostock": mock_bs}):
            result = fetch_baostock_cn_adjusted_daily_map(instrument, date(2026, 5, 20), date(2026, 5, 21))
        self.assertEqual(len(result), 2)
        self.assertIn("2026-05-20", result)
        self.assertAlmostEqual(result["2026-05-20"]["adj_close"], 103.0)
        self.assertAlmostEqual(result["2026-05-21"]["adj_close"], 107.0)

    def test_daily_run_fast_skips_events_but_fetches_full_prices(self) -> None:
        from alpha_ledger.cli import command_daily_run
        calls = {}
        import alpha_ledger.cli as cli_module
        def capture_data_update(conn, as_of, markets, **kwargs):
            calls.update(kwargs)
            return MagicMock(status="SUCCESS")
        with patch.object(cli_module, "data_update", capture_data_update), \
             patch.object(cli_module, "audit_data_coverage", return_value=MagicMock(confidence_level="MEDIUM")), \
             patch.object(cli_module, "screen_all", return_value=0), \
             patch.object(cli_module, "confirm_candidates", return_value=(0, 0)), \
             patch.object(cli_module, "write_daily_plan", return_value="test.md"), \
             patch.object(cli_module, "run_portfolio_backtest", return_value=MagicMock()), \
             patch.object(cli_module, "write_portfolio_report", return_value="test.md"):
            command_daily_run(":memory:", "2026-05-27", 0.0, fast=True)
        self.assertFalse(calls.get("fetch_events", True))
        self.assertFalse(calls.get("fetch_intraday", True))
        self.assertEqual(calls.get("price_mode"), "full")
        self.assertIsNone(calls.get("adjust"))

    def test_daily_run_full_keeps_events(self) -> None:
        from alpha_ledger.cli import command_daily_run
        calls = {}
        import alpha_ledger.cli as cli_module
        def capture_data_update(conn, as_of, markets, **kwargs):
            calls.update(kwargs)
            return MagicMock(status="SUCCESS")
        with patch.object(cli_module, "data_update", capture_data_update), \
             patch.object(cli_module, "audit_data_coverage", return_value=MagicMock(confidence_level="MEDIUM")), \
             patch.object(cli_module, "screen_all", return_value=0), \
             patch.object(cli_module, "confirm_candidates", return_value=(0, 0)), \
             patch.object(cli_module, "write_daily_plan", return_value="test.md"), \
             patch.object(cli_module, "run_portfolio_backtest", return_value=MagicMock()), \
             patch.object(cli_module, "write_portfolio_report", return_value="test.md"):
            command_daily_run(":memory:", "2026-05-27", 0.0, fast=False)
        self.assertTrue(calls.get("fetch_events", False))
        self.assertTrue(calls.get("fetch_intraday", False))
        self.assertEqual(calls.get("price_mode"), "full")

    def test_slippage_deducts_from_net_return(self) -> None:
        from alpha_ledger.metrics import slippage_pct, trade_cost_pct
        self.assertAlmostEqual(slippage_pct("CN_A"), 0.05, places=2)
        self.assertAlmostEqual(slippage_pct("US"), 0.03, places=2)
        self.assertAlmostEqual(slippage_pct("HK"), 0.05, places=2)

    def _make_bars(self, prices):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL, adj_close REAL)")
        bars = []
        for d, o, h, l, c in prices:
            conn.execute("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)", (d, o, h, l, c, 1000000, 10000000, c))
            bars.append(conn.execute("SELECT * FROM t WHERE date=?", (d,)).fetchone())
        return bars

    def test_trailing_stop_triggers_in_trend(self) -> None:
        from alpha_ledger.metrics import _long_trade_path
        prices = [
            ("2026-05-01", 100, 105, 99, 104),
            ("2026-05-02", 104, 112, 103, 111),
            ("2026-05-03", 111, 118, 110, 117),
            ("2026-05-04", 117, 120, 108, 109),
        ]
        bars = self._make_bars(prices)
        path = _long_trade_path(
            bars, entry_price=100.0, stop_loss=90.0,
            target_1=130.0, target_2=150.0, market="CN_A",
            trailing_stop_pct=3.0, trailing_activation_pct=8.0,
        )
        self.assertEqual(path["exit_type"], "TRAILING_STOP")
        self.assertGreater(path["exit_price"], 100.0)

    def test_trailing_stop_not_activated_below_threshold(self) -> None:
        from alpha_ledger.metrics import _long_trade_path
        prices = [
            ("2026-05-01", 100, 105, 99, 104),
            ("2026-05-02", 104, 107, 103, 106),
            ("2026-05-03", 106, 108, 105, 107),
        ]
        bars = self._make_bars(prices)
        path = _long_trade_path(
            bars, entry_price=100.0, stop_loss=90.0,
            target_1=130.0, target_2=150.0, market="CN_A",
            trailing_stop_pct=3.0, trailing_activation_pct=8.0,
        )
        self.assertNotEqual(path["exit_type"], "TRAILING_STOP")

    def test_normalize_cn_code_accepts_sh_suffix(self) -> None:
        """Test that normalize_cn_code accepts .SH and converts to .SS."""
        from alpha_ledger.event_data import normalize_cn_code
        ticker, source_symbol, prefix = normalize_cn_code("600519.SH")
        self.assertEqual(ticker, "600519.SS")
        self.assertEqual(source_symbol, "sh600519")
        self.assertEqual(prefix, "sh")

    def test_normalize_cn_code_accepts_ss_suffix(self) -> None:
        """Test that normalize_cn_code accepts .SS (canonical)."""
        from alpha_ledger.event_data import normalize_cn_code
        ticker, source_symbol, prefix = normalize_cn_code("600519.SS")
        self.assertEqual(ticker, "600519.SS")
        self.assertEqual(source_symbol, "sh600519")
        self.assertEqual(prefix, "sh")

    def test_normalize_cn_code_accepts_sz_suffix(self) -> None:
        """Test that normalize_cn_code accepts .SZ."""
        from alpha_ledger.event_data import normalize_cn_code
        ticker, source_symbol, prefix = normalize_cn_code("002674.SZ")
        self.assertEqual(ticker, "002674.SZ")
        self.assertEqual(source_symbol, "sz002674")
        self.assertEqual(prefix, "sz")

    def test_normalize_cn_code_accepts_bare_code(self) -> None:
        """Test that normalize_cn_code accepts bare numeric code."""
        from alpha_ledger.event_data import normalize_cn_code
        ticker, source_symbol, prefix = normalize_cn_code("600519")
        self.assertEqual(ticker, "600519.SS")
        self.assertEqual(source_symbol, "sh600519")
        self.assertEqual(prefix, "sh")

    def test_normalize_cn_code_sh_case_insensitive(self) -> None:
        """Test that .SH is case-insensitive."""
        from alpha_ledger.event_data import normalize_cn_code
        ticker1, _, _ = normalize_cn_code("600519.SH")
        ticker2, _, _ = normalize_cn_code("600519.sh")
        self.assertEqual(ticker1, "600519.SS")
        self.assertEqual(ticker2, "600519.SS")


if __name__ == "__main__":
    unittest.main()
