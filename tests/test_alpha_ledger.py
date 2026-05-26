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
    evaluate_candidate_horizons_for_date,
    evaluate_candidates,
    suggest_strategy_weight_adjustments,
    trade_cost_pct,
)
from alpha_ledger.portfolio_backtest import run_portfolio_backtest
from alpha_ledger.reporting import daily_action_plan
from alpha_ledger.screener import _candidate, _latest_financial_flags, confirm_candidates, screen_all
from alpha_ledger.seed import seed_all


class AlphaLedgerMvpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        init_db(self.conn)
        seed_all(self.conn)

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
        self.assertEqual(count, 4)
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
        self.assertEqual(plan[0]["plan_bucket"], "今日可买")

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


if __name__ == "__main__":
    unittest.main()
