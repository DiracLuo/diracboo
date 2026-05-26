from __future__ import annotations

import sqlite3
from pathlib import Path

from .metrics import (
    FORMAL_MARKETS,
    candidate_action_leaderboard,
    candidate_horizon_strategy_leaderboard,
    candidate_market_leaderboard,
    candidate_strategy_leaderboard,
    score_calibration,
    suggest_strategy_weight_adjustments,
)


def fmt_pct(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def fmt_price(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def fmt_rate(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


MAX_ACTIONABLE_CANDIDATES = 5
FORMAL_MARKET_LABEL = ", ".join(FORMAL_MARKETS)


def daily_action_plan(conn: sqlite3.Connection, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH scored AS (
        SELECT
            c.*,
            st.name AS strategy_name,
            st.version AS strategy_version,
            st.weight,
            CASE
                WHEN COALESCE(c.confirmation_status, 'PENDING') = 'CONFIRMED'
                     AND c.confirmation_date = ?
                THEN 1
                WHEN c.data_date IS NULL OR c.data_date = ''
                THEN 0
                WHEN c.market = 'CN_A' AND c.data_date = ?
                THEN 1
                ELSE 0
            END AS data_is_fresh,
            CASE
                WHEN (c.action = 'BUY_CANDIDATE'
                       OR COALESCE(c.confirmation_status, 'PENDING') = 'CONFIRMED'
                       OR c.status = 'CONFIRMED')
                     AND c.candidate_score >= 78
                     AND COALESCE(c.confirmation_status, 'PENDING') != 'CANCELLED'
                     AND COALESCE(c.reward_risk_ratio, 0) >= 1.5
                     AND (
                         (COALESCE(c.confirmation_status, 'PENDING') = 'CONFIRMED' AND c.confirmation_date = ?)
                         OR (
                                 c.data_date IS NOT NULL AND c.data_date != ''
                             AND c.market = 'CN_A'
                             AND c.data_date = ?
                         )
                     )
                THEN '今日可买'
                WHEN c.candidate_score >= 82
                     AND COALESCE(c.confirmation_status, 'PENDING') != 'CANCELLED'
                     AND COALESCE(c.reward_risk_ratio, 0) >= 1.5
                THEN '重点等确认'
                WHEN c.action LIKE '%CONFIRM%'
                     AND COALESCE(c.confirmation_status, 'PENDING') != 'CANCELLED'
                THEN '等确认'
                ELSE '观察'
            END AS plan_bucket,
            COALESCE(c.reward_risk_ratio,
                CASE WHEN COALESCE(c.stop_loss, 0) > 0 AND c.entry_price > c.stop_loss
                THEN (c.target_1 - c.entry_price) / (c.entry_price - c.stop_loss)
                END, 0) AS reward_risk
        FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        WHERE (c.as_of_date = ? OR c.confirmation_date = ?)
          AND c.market = 'CN_A'
          AND st.status != 'RETIRED'
        ),
        planned AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY market, ticker
                    ORDER BY candidate_score * weight DESC, id ASC
                ) AS rn
            FROM scored
        )
        SELECT *
        FROM planned
        WHERE rn = 1
          AND reward_risk >= 1.0
        ORDER BY
            CASE plan_bucket
                WHEN '今日可买' THEN 1
                WHEN '重点等确认' THEN 2
                WHEN '等确认' THEN 3
                ELSE 4
            END,
            candidate_score * weight DESC,
            ticker
        """,
        (
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
        ),
    ).fetchall()


def render_daily_plan(conn: sqlite3.Connection, as_of_date: str) -> str:
    rows = daily_action_plan(conn, as_of_date)
    lines: list[str] = []
    lines.append(f"# Alpha Ledger Daily Plan - {as_of_date}")
    lines.append("")
    lines.append(f"- 正式交易范围：{FORMAL_MARKET_LABEL}。美股/港股暂为实验数据，不进入今日买入清单。")
    lines.append("")
    if not rows:
        lines.append("暂无可操作候选。")
        return "\n".join(lines).rstrip() + "\n"

    actionable = [r for r in rows if r["plan_bucket"] == "今日可买"]
    confirmation = [r for r in rows if r["plan_bucket"] in ("重点等确认", "等确认")]
    observation = [r for r in rows if r["plan_bucket"] == "观察"]

    if actionable:
        lines.append(f"## 可操作买入清单（最多 {MAX_ACTIONABLE_CANDIDATES} 只）")
        lines.append("")
        lines.append("| 股票 | 市场 | 策略 | 分数 | 入场 | 止损 | 目标1 | 风报比 | 数据日 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---|---|")
        for row in actionable[:MAX_ACTIONABLE_CANDIDATES]:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 80:
                trigger = trigger[:77] + "..."
            data_date = row["confirmation_date"] or row["data_date"] or "-"
            lines.append(
                "| "
                f"{row['name']} `{row['ticker']}` | {row['market']} | {row['strategy_name']} `{row['strategy_version']}` | "
                f"{float(row['candidate_score']):.1f} | {fmt_price(row['entry_price'])} | "
                f"{fmt_price(row['stop_loss'])} | {fmt_price(row['target_1'])} | "
                f"{float(row['reward_risk']):.2f} | {data_date} | {trigger} |"
            )
        lines.append("")

    if confirmation:
        lines.append("## 等确认候选")
        lines.append("")
        lines.append("| 股票 | 市场 | 策略 | 分数 | 入场 | 止损 | 目标1 | 风报比 | 数据日 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---|---|")
        for row in confirmation[:10]:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 80:
                trigger = trigger[:77] + "..."
            data_date = row["confirmation_date"] or row["data_date"] or "-"
            lines.append(
                "| "
                f"{row['name']} `{row['ticker']}` | {row['market']} | {row['strategy_name']} `{row['strategy_version']}` | "
                f"{float(row['candidate_score']):.1f} | {fmt_price(row['entry_price'])} | "
                f"{fmt_price(row['stop_loss'])} | {fmt_price(row['target_1'])} | "
                f"{float(row['reward_risk']):.2f} | {data_date} | {trigger} |"
            )
        lines.append("")

    if observation:
        lines.append(f"## 观察池（{len(observation)} 只）")
        lines.append("")
        lines.append("- 以下候选风报比不足或条件不满足，仅供研究参考，不建议直接买入。")
        lines.append("")

    lines.append("## 淘汰规则")
    lines.append("")
    lines.append("- 跌破止损或事件窗口低点，直接淘汰。")
    lines.append("- 等确认候选若次日不放量承接或收盘跌回触发位下方，降级观察。")
    lines.append("- 同一股票同日多策略重叠时，只按最高分策略处理，避免重复下注。")
    return "\n".join(lines).rstrip() + "\n"


def write_daily_plan(
    conn: sqlite3.Connection,
    as_of_date: str,
    out_path: Path | str | None = None,
) -> Path:
    path = Path(out_path) if out_path else Path("reports") / f"daily_plan_{as_of_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_daily_plan(conn, as_of_date), encoding="utf-8")
    return path


def replay_daily_summary(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            GROUP BY candidate_id
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        )
        SELECT
            c.as_of_date,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(e.id) AS evaluated_count,
            AVG(e.return_pct) AS avg_return_pct,
            AVG(e.net_return_pct) AS avg_net_return_pct,
            AVG(e.benchmark_return_pct) AS avg_benchmark_return_pct,
            AVG(e.excess_return_pct) AS avg_excess_return_pct,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate
        FROM candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        JOIN strategies st ON st.id = c.strategy_id
        WHERE c.as_of_date >= ? AND c.as_of_date <= ?
          AND c.market = 'CN_A'
          AND st.status != 'RETIRED'
        GROUP BY c.as_of_date
        ORDER BY c.as_of_date
        """,
        (start_date, end_date),
    ).fetchall()


def replay_samples(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    order: str,
    limit: int = 15,
) -> list[sqlite3.Row]:
    direction = "ASC" if order == "worst" else "DESC"
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            GROUP BY candidate_id
        )
        SELECT
            c.as_of_date,
            c.name,
            c.ticker,
            c.market,
            st.name AS strategy_name,
            st.version AS strategy_version,
            c.candidate_score,
            c.action,
            c.entry_price,
            c.stop_loss,
            c.target_1,
            c.trigger_condition,
            e.observed_days,
            e.execution_date,
            e.execution_price,
            e.execution_type,
            e.end_date,
            e.end_close,
            e.return_pct,
            e.net_return_pct,
            e.benchmark_return_pct,
            e.excess_return_pct,
            e.max_gain_pct,
            e.max_drawdown_pct,
            e.hit_stop,
            e.hit_target_1,
            e.hit_target_2,
            e.exit_type,
            e.exit_date,
            e.exit_price,
            e.exit_note
        FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        JOIN latest l ON l.candidate_id = c.id
        JOIN candidate_evaluations e
          ON e.candidate_id = c.id
         AND e.through_date = l.through_date
        WHERE c.as_of_date >= ? AND c.as_of_date <= ?
          AND c.market = 'CN_A'
          AND st.status != 'RETIRED'
        ORDER BY e.net_return_pct {direction}, c.candidate_score DESC
        LIMIT ?
        """,
        (start_date, end_date, limit),
    ).fetchall()


def replay_horizon_strategy_matrix(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    *,
    dedupe: bool = True,
) -> list[sqlite3.Row]:
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, horizon_days, MAX(through_date) AS through_date
            FROM candidate_horizon_evaluations
            WHERE through_date <= ?
            GROUP BY candidate_id, horizon_days
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_horizon_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.horizon_days = e.horizon_days
             AND l.through_date = e.through_date
        ),
        base_candidates AS (
            SELECT
                c.*,
                st.name AS strategy_name,
                st.version AS strategy_version,
                st.weight,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            WHERE c.as_of_date >= ?
              AND c.as_of_date <= ?
              AND c.market = 'CN_A'
              AND st.status != 'RETIRED'
        ),
        selected_candidates AS (
            SELECT *
            FROM base_candidates
            {dedupe_sql}
        )
        SELECT
            c.strategy_id,
            c.strategy_name,
            c.strategy_version,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_5d,
            COUNT(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.benchmark_return_pct END) AS avg_benchmark_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.excess_return_pct END) AS avg_excess_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate_10d,
            COUNT(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_20d,
            COUNT(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_60d
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name, c.strategy_version
        ORDER BY COALESCE(avg_net_return_10d, avg_net_return_5d, -999) DESC, completed_10d DESC, candidate_count DESC
        """,
        (through_date, start_date, end_date),
    ).fetchall()


def replay_data_quality_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    price_end_date: str | None = None,
) -> sqlite3.Row:
    price_end = price_end_date or end_date
    return conn.execute(
        """
        WITH formal_candidates AS (
            SELECT id
            FROM candidates
            WHERE as_of_date >= ?
              AND as_of_date <= ?
              AND market = 'CN_A'
        )
        SELECT
            (SELECT COUNT(*) FROM formal_candidates) AS candidate_count,
            (
                SELECT COUNT(*)
                FROM candidate_evaluations e
                JOIN formal_candidates c ON c.id = e.candidate_id
                WHERE e.execution_type = 'NEXT_OPEN_DAILY'
            ) AS daily_fallback_count,
            (
                SELECT COUNT(*)
                FROM price_bars p
                JOIN candidates c
                  ON c.market = p.market
                 AND c.ticker = p.ticker
                 AND c.as_of_date = p.date
                WHERE c.id IN (SELECT id FROM formal_candidates)
                  AND COALESCE(p.volume, 0) <= 0
            ) AS zero_volume_signal_day_count,
            (
                SELECT COUNT(*)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker != '000300.SS'
                  AND date >= ?
                  AND date <= ?
            ) AS price_bar_count,
            (
                SELECT COUNT(*)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker != '000300.SS'
                  AND date >= ?
                  AND date <= ?
                  AND adjustment_status = 'ADJUSTED'
            ) AS adjusted_price_bar_count,
            (
                SELECT COUNT(*)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker != '000300.SS'
                  AND date >= ?
                  AND date <= ?
                  AND adjustment_status = 'RAW_FALLBACK'
            ) AS raw_fallback_price_bar_count,
            (
                SELECT COUNT(DISTINCT date)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker = '000300.SS'
                  AND date >= ?
                  AND date <= ?
            ) AS benchmark_day_count,
            (
                SELECT COUNT(DISTINCT date)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND date >= ?
                  AND date <= ?
            ) AS trading_day_count
        """,
        (
            start_date,
            end_date,
            start_date,
            price_end,
            start_date,
            price_end,
            start_date,
            price_end,
            start_date,
            price_end,
            start_date,
            price_end,
        ),
    ).fetchone()


def render_replay_report(conn: sqlite3.Connection, start_date: str, end_date: str, through_date: str) -> str:
    leaderboard = candidate_strategy_leaderboard(conn, start_date, end_date, through_date)
    deduped_leaderboard = candidate_strategy_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        dedupe=True,
    )
    horizon_matrix = replay_horizon_strategy_matrix(conn, start_date, end_date, through_date, dedupe=True)
    market_leaderboard = candidate_market_leaderboard(conn, start_date, end_date, through_date)
    action_leaderboard = candidate_action_leaderboard(conn, start_date, end_date, through_date)
    weight_suggestions = suggest_strategy_weight_adjustments(conn, start_date, end_date, through_date)
    calibration = score_calibration(conn, start_date, end_date, through_date, 10)
    data_quality = replay_data_quality_summary(conn, start_date, end_date, through_date)
    daily = replay_daily_summary(conn, start_date, end_date)
    winners = replay_samples(conn, start_date, end_date, order="best")
    losers = replay_samples(conn, start_date, end_date, order="worst")
    total_candidates = sum(int(row["candidate_count"]) for row in daily)
    total_evaluated = sum(int(row["evaluated_count"]) for row in daily)
    price_bar_count = int(data_quality["price_bar_count"] or 0)
    adjusted_price_bar_count = int(data_quality["adjusted_price_bar_count"] or 0)
    raw_fallback_price_bar_count = int(data_quality["raw_fallback_price_bar_count"] or 0)
    adjusted_coverage = adjusted_price_bar_count / price_bar_count * 100.0 if price_bar_count else 0.0
    benchmark_day_count = int(data_quality["benchmark_day_count"] or 0)
    trading_day_count = int(data_quality["trading_day_count"] or 0)
    benchmark_coverage = benchmark_day_count / trading_day_count * 100.0 if trading_day_count else 0.0

    lines: list[str] = []
    lines.append(f"# Alpha Ledger Replay - {start_date} to {end_date}")
    lines.append("")
    lines.append("## 结论摘要")
    lines.append("")
    lines.append(
        f"- 回放区间覆盖 {len(daily)} 个有候选日期，共 {total_candidates} 个候选，"
        f"其中 {total_evaluated} 个已用 {through_date} 后验验证。"
    )
    lines.append(f"- 正式统计范围：{FORMAL_MARKET_LABEL}。美股/港股保留为实验能力，但不进入本报告正式收益结论。")
    lines.append("- 回放只使用候选日当时可见的价格、事件日期和已披露财务数据；历史资金流若无可回放数据，不参与打分。")
    lines.append("- 候选日不假设可在收盘成交；有分时数据时按次一交易日开盘前5根K线VWAP估算入场，缺分时才回退日线开盘价。")
    lines.append("- 收益统计默认使用复权价格；成交价仍展示原始价格。")
    if adjusted_coverage < 95.0:
        lines.append(f"- WARNING: 前复权覆盖率仅 {adjusted_coverage:.1f}%，当前回测收益不属于高置信正式结论。")
    if benchmark_coverage < 95.0:
        lines.append(f"- WARNING: 沪深300基准覆盖率仅 {benchmark_coverage:.1f}%，不能可靠判断 alpha。")
    lines.append("- 固定周期榜只统计完整走满 T+5/T+10/T+20/T+60 的样本；未走满的候选继续等待，不计入正式胜率。")
    lines.append("- 策略榜同时展示原始候选和去重候选；去重规则为同一日期同一股票只保留分数最高的策略。")
    lines.append("")

    lines.append("## 固定持有周期策略榜")
    lines.append("")
    if horizon_matrix:
        lines.append("| 策略 | 候选数 | T+5样本 | T+5净均值 | T+5净胜率 | T+10样本 | T+10净均值 | T+10基准 | T+10超额 | T+10超额胜率 | T+20样本 | T+20净均值 | T+20净胜率 | T+60样本 | T+60净均值 | T+60净胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in horizon_matrix:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}@{row['strategy_version']}` | {row['candidate_count']} | "
                f"{row['completed_5d']} | {fmt_pct(row['avg_net_return_5d'])} | {fmt_rate(row['net_win_rate_5d'])} | "
                f"{row['completed_10d']} | {fmt_pct(row['avg_net_return_10d'])} | {fmt_pct(row['avg_benchmark_return_10d'])} | "
                f"{fmt_pct(row['avg_excess_return_10d'])} | {fmt_rate(row['excess_win_rate_10d'])} | "
                f"{row['completed_20d']} | {fmt_pct(row['avg_net_return_20d'])} | {fmt_rate(row['net_win_rate_20d'])} | "
                f"{row['completed_60d']} | {fmt_pct(row['avg_net_return_60d'])} | {fmt_rate(row['net_win_rate_60d'])} |"
            )
    else:
        lines.append("暂无固定周期候选回放数据。")
    lines.append("")

    def append_strategy_table(title: str, rows: list[sqlite3.Row]) -> None:
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("暂无策略回放数据。")
            lines.append("")
            return
        lines.append("| 策略 | 候选数 | 已验证 | 均分 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 目标1率 | 目标2率 | 止损率 | MFE | MAE |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}@{row['strategy_version']}` | {row['candidate_count']} | "
                f"{row['evaluated_count']} | {fmt_price(row['avg_candidate_score'])} | "
                f"{fmt_pct(row['avg_net_return_pct'])} | {fmt_pct(row['avg_benchmark_return_pct'])} | "
                f"{fmt_pct(row['avg_excess_return_pct'])} | {fmt_rate(row['excess_win_rate'])} | "
                f"{fmt_rate(row['net_win_rate'])} | "
                f"{fmt_rate(row['target_1_rate'])} | {fmt_rate(row['target_2_rate'])} | "
                f"{fmt_rate(row['stop_rate'])} | {fmt_pct(row['avg_max_gain_pct'])} | "
                f"{fmt_pct(row['avg_max_drawdown_pct'])} |"
            )
        lines.append("")

    append_strategy_table("## 截止日候选策略胜率", leaderboard)
    append_strategy_table("## 截止日去重后策略胜率", deduped_leaderboard)

    def append_segment_table(title: str, rows: list[sqlite3.Row], segment_name: str) -> None:
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("暂无分组回放数据。")
            lines.append("")
            return
        lines.append(f"| {segment_name} | 候选数 | 已验证 | 均分 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 目标1率 | 止损率 | MFE | MAE |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                "| "
                f"{row['segment']} | {row['candidate_count']} | {row['evaluated_count']} | "
                f"{fmt_price(row['avg_candidate_score'])} | {fmt_pct(row['avg_net_return_pct'])} | "
                f"{fmt_pct(row['avg_benchmark_return_pct'])} | {fmt_pct(row['avg_excess_return_pct'])} | "
                f"{fmt_rate(row['excess_win_rate'])} | {fmt_rate(row['net_win_rate'])} | {fmt_rate(row['target_1_rate'])} | "
                f"{fmt_rate(row['stop_rate'])} | {fmt_pct(row['avg_max_gain_pct'])} | "
                f"{fmt_pct(row['avg_max_drawdown_pct'])} |"
            )
        lines.append("")

    append_segment_table("## 分市场表现", market_leaderboard, "市场")
    append_segment_table("## 触发类型表现", action_leaderboard, "触发类型")

    lines.append("## 分数校准（T+10净收益）")
    lines.append("")
    if calibration:
        lines.append("| 分数桶 | 样本 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 止损率 | 目标率 | 最差收益 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in calibration:
            lines.append(
                "| "
                f"{row['score_bucket']} | {row['sample_count']} | {fmt_pct(row['avg_net_return'])} | "
                f"{fmt_pct(row['avg_benchmark_return'])} | {fmt_pct(row['avg_excess_return'])} | "
                f"{fmt_rate(row['excess_win_rate'])} | {fmt_rate(row['net_win_rate'])} | {fmt_rate(row['stop_rate'])} | "
                f"{fmt_rate(row['target_rate'])} | {fmt_pct(row['worst_return'])} |"
            )
        high = calibration[0]
        low = calibration[-1]
        high_metric = high["avg_excess_return"] if high["avg_excess_return"] is not None else high["avg_net_return"]
        low_metric = low["avg_excess_return"] if low["avg_excess_return"] is not None else low["avg_net_return"]
        if high_metric is not None and low_metric is not None and float(high_metric) <= float(low_metric):
            lines.append("")
            lines.append("- WARNING: 高分桶超额收益不优于低分桶，当前分数不应单独作为买入排序依据。")
    else:
        lines.append("暂无完整 T+10 分数校准样本。")
    lines.append("")

    lines.append("## 数据质量审计")
    lines.append("")
    lines.append(
        f"- 正式候选数：{int(data_quality['candidate_count'] or 0)}；"
        f"缺分时而回退日线开盘的评估：{int(data_quality['daily_fallback_count'] or 0)}；"
        f"信号日零成交候选：{int(data_quality['zero_volume_signal_day_count'] or 0)}。"
    )
    lines.append(
        f"- 前复权覆盖：{adjusted_price_bar_count}/{price_bar_count} ({adjusted_coverage:.1f}%)；"
        f"原始价格回退：{raw_fallback_price_bar_count}。"
    )
    lines.append(
        f"- 基准覆盖：沪深300 {benchmark_day_count}/{trading_day_count} 个交易日 ({benchmark_coverage:.1f}%)。"
    )
    lines.append("- 回退日线开盘或零成交样本会降低执行可信度，正式调参时应单独复核。")
    lines.append("")

    lines.append("## 策略权重建议（基于各策略目标周期）")
    lines.append("")
    if weight_suggestions:
        lines.append("| 策略 | 目标周期 | 已验证 | 当前权重 | 建议权重 | 建议 | 止损率 | 超额胜率 | 平均超额 | 平均净收益 | 原因 |")
        lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|")
        for item in weight_suggestions:
            lines.append(
                "| "
                f"{item['strategy_name']} `{item['strategy_id']}@{item.get('strategy_version', 'v1')}` | T+{item.get('target_horizon_days', 10)} | "
                f"{item['evaluated_count']} | "
                f"{float(item['current_weight']):.2f} | {float(item['suggested_weight']):.2f} | "
                f"{item['recommendation']} | {fmt_rate(item['stop_rate'])} | "
                f"{fmt_rate(item.get('excess_win_rate'))} | {fmt_pct(item.get('avg_excess_return_pct'))} | "
                f"{fmt_pct(item['avg_return_pct'])} | "
                f"{item['reason']} |"
            )
    else:
        lines.append("暂无策略权重建议。")
    lines.append("")

    lines.append("## 每日回放概览")
    lines.append("")
    if daily:
        lines.append("| 日期 | 候选数 | 已验证 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 目标1率 | 止损率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in daily:
            lines.append(
                "| "
                f"{row['as_of_date']} | {row['candidate_count']} | {row['evaluated_count']} | "
                f"{fmt_pct(row['avg_net_return_pct'])} | {fmt_pct(row['avg_benchmark_return_pct'])} | "
                f"{fmt_pct(row['avg_excess_return_pct'])} | {fmt_rate(row['excess_win_rate'])} | "
                f"{fmt_rate(row['net_win_rate'])} | "
                f"{fmt_rate(row['target_1_rate'])} | {fmt_rate(row['stop_rate'])} |"
            )
    else:
        lines.append("暂无每日候选。")
    lines.append("")

    def append_sample_table(title: str, rows: list[sqlite3.Row]) -> None:
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("暂无样本。")
            lines.append("")
            return
        lines.append("| 日期 | 股票 | 策略 | 分数 | 计划入场 | 执行价 | 退出 | 退出价 | 净收益 | 基准 | 超额 | 最大浮盈 | 最大回撤 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---|")
        for row in rows:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 90:
                trigger = trigger[:87] + "..."
            lines.append(
                "| "
                f"{row['as_of_date']} | {row['name']} `{row['ticker']}` | {row['strategy_name']} `{row['strategy_version']}` | "
                f"{float(row['candidate_score']):.1f} | {fmt_price(row['entry_price'])} | "
                f"{fmt_price(row['execution_price'])} | {row['exit_type']} {row['exit_date']} | "
                f"{fmt_price(row['exit_price'])} | "
                f"{fmt_pct(row['net_return_pct'])} | {fmt_pct(row['benchmark_return_pct'])} | "
                f"{fmt_pct(row['excess_return_pct'])} | "
                f"{fmt_pct(row['max_gain_pct'])} | {fmt_pct(row['max_drawdown_pct'])} | "
                f"{trigger} |"
            )
        lines.append("")

    append_sample_table("## 最强样本", winners)
    append_sample_table("## 最弱样本", losers)

    return "\n".join(lines).rstrip() + "\n"


def write_replay_report(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    out_path: Path | str | None = None,
) -> Path:
    path = (
        Path(out_path)
        if out_path
        else Path("reports") / f"replay_{start_date}_{end_date}_through_{through_date}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_replay_report(conn, start_date, end_date, through_date), encoding="utf-8")
    return path
