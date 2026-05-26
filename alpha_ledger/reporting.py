from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .audit import latest_audits
from .metrics import (
    candidate_action_leaderboard,
    candidate_horizon_strategy_leaderboard,
    candidate_market_leaderboard,
    candidate_strategy_leaderboard,
    strategy_leaderboard,
    suggest_strategy_weight_adjustments,
)
from .screener import latest_candidates


REPORT_CANDIDATE_LIMIT = 20
REPORT_EVALUATION_SIDE_LIMIT = 10
REPORT_DETAIL_TOP_SCORE_LIMIT = 10
REPORT_DETAIL_SIDE_LIMIT = 5


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


def safe_json_list(value: str | None) -> list[dict[str, object]]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def evidence_line(item: dict[str, object]) -> str:
    title = str(item.get("title") or item.get("summary") or item.get("type") or "证据")
    url = item.get("url")
    if url:
        return f"- 证据：[{title}]({url})"
    return f"- 证据：{title}"


def selected_candidate_evaluations(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    if len(rows) <= REPORT_EVALUATION_SIDE_LIMIT * 2:
        return rows
    selected: list[sqlite3.Row] = []
    seen: set[object] = set()
    for row in rows[:REPORT_EVALUATION_SIDE_LIMIT] + rows[-REPORT_EVALUATION_SIDE_LIMIT:]:
        key = row["candidate_id"] if "candidate_id" in row.keys() else (row["ticker"], row["strategy_name"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
    return selected


def selected_candidate_details(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    if len(rows) <= REPORT_DETAIL_TOP_SCORE_LIMIT + REPORT_DETAIL_SIDE_LIMIT * 2:
        return rows
    evaluated = [row for row in rows if row["return_pct"] is not None]
    best = sorted(evaluated, key=lambda row: float(row["return_pct"]), reverse=True)[:REPORT_DETAIL_SIDE_LIMIT]
    worst = sorted(evaluated, key=lambda row: float(row["return_pct"]))[:REPORT_DETAIL_SIDE_LIMIT]
    selected: list[sqlite3.Row] = []
    seen: set[int] = set()
    for row in rows[:REPORT_DETAIL_TOP_SCORE_LIMIT] + best + worst:
        key = int(row["id"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
    return selected


def latest_evaluations(conn: sqlite3.Connection, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            s.id,
            s.signal_date,
            s.ticker,
            s.name,
            s.market,
            st.name AS strategy_name,
            s.entry_price,
            s.stop_loss,
            s.target_1,
            s.target_2,
            s.confidence,
            s.status,
            e.horizon_days,
            e.observed_days,
            e.end_date,
            e.end_close,
            e.return_pct,
            e.max_gain_pct,
            e.max_drawdown_pct,
            e.hit_stop,
            e.hit_target_1,
            e.hit_target_2,
            e.exit_type,
            e.exit_date,
            e.exit_price,
            e.exit_note
        FROM signals s
        JOIN strategies st ON st.id = s.strategy_id
        LEFT JOIN evaluations e
          ON e.signal_id = s.id
         AND e.as_of_date = ?
        WHERE s.signal_date <= ?
          AND st.status != 'RETIRED'
        ORDER BY s.signal_date DESC, s.id, e.horizon_days
        """,
        (as_of_date, as_of_date),
    ).fetchall()


def latest_candidate_evaluations(conn: sqlite3.Connection, candidate_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            GROUP BY candidate_id
        )
        SELECT
            c.name,
            c.ticker,
            c.market,
            c.id AS candidate_id,
            st.name AS strategy_name,
            c.action,
            c.entry_price,
            c.stop_loss,
            c.target_1,
            c.target_2,
            e.through_date,
            e.observed_days,
            e.execution_date,
            e.execution_price,
            e.execution_type,
            e.end_date,
            e.end_close,
            e.return_pct,
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
        WHERE c.as_of_date = ?
          AND st.status != 'RETIRED'
        ORDER BY e.return_pct DESC
        """,
        (candidate_date,),
    ).fetchall()


def latest_candidate_details(conn: sqlite3.Connection, candidate_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            GROUP BY candidate_id
        )
        SELECT
            c.*,
            st.name AS strategy_name,
            e.through_date,
            e.observed_days,
            e.execution_date,
            e.execution_price,
            e.execution_type,
            e.end_date,
            e.end_close,
            e.return_pct,
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
        LEFT JOIN latest l ON l.candidate_id = c.id
        LEFT JOIN candidate_evaluations e
          ON e.candidate_id = c.id
         AND e.through_date = l.through_date
        WHERE c.as_of_date = ?
          AND st.status != 'RETIRED'
        ORDER BY c.candidate_score DESC, c.ticker
        """,
        (candidate_date,),
    ).fetchall()


def data_coverage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            market,
            COUNT(DISTINCT ticker) AS tickers,
            COUNT(*) AS bars,
            MIN(date) AS min_date,
            MAX(date) AS max_date
        FROM price_bars
        GROUP BY market
        ORDER BY market
        """
    ).fetchall()


def daily_action_plan(conn: sqlite3.Connection, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH scored AS (
        SELECT
            c.*,
            st.name AS strategy_name,
            st.weight,
            CASE
                WHEN c.action = 'BUY_CANDIDATE' AND c.candidate_score >= 78 THEN '今日可买'
                WHEN c.candidate_score >= 82 THEN '重点等确认'
                WHEN c.action LIKE '%CONFIRM%' THEN '等确认'
                ELSE '观察'
            END AS plan_bucket,
            CASE
                WHEN c.entry_price > c.stop_loss
                THEN (c.target_1 - c.entry_price) / (c.entry_price - c.stop_loss)
            END AS reward_risk
        FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        WHERE c.as_of_date = ?
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
        (as_of_date,),
    ).fetchall()


def render_daily_plan(conn: sqlite3.Connection, as_of_date: str) -> str:
    rows = daily_action_plan(conn, as_of_date)
    lines: list[str] = []
    lines.append(f"# Alpha Ledger Daily Plan - {as_of_date}")
    lines.append("")
    if not rows:
        lines.append("暂无候选。")
        return "\n".join(lines).rstrip() + "\n"
    for bucket in ("今日可买", "重点等确认", "等确认", "观察"):
        bucket_rows = [row for row in rows if row["plan_bucket"] == bucket]
        if not bucket_rows:
            continue
        lines.append(f"## {bucket}")
        lines.append("")
        lines.append("| 股票 | 市场 | 策略 | 分数 | 入场 | 止损 | 目标1 | 风报比 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---|")
        for row in bucket_rows[:20]:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 100:
                trigger = trigger[:97] + "..."
            lines.append(
                "| "
                f"{row['name']} `{row['ticker']}` | {row['market']} | {row['strategy_name']} | "
                f"{float(row['candidate_score']):.1f} | {fmt_price(row['entry_price'])} | "
                f"{fmt_price(row['stop_loss'])} | {fmt_price(row['target_1'])} | "
                f"{fmt_price(row['reward_risk'])} | {trigger} |"
            )
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


def render_report(conn: sqlite3.Connection, as_of_date: str) -> str:
    signals = conn.execute(
        """
        SELECT s.*, st.name AS strategy_name
        FROM signals s
        JOIN strategies st ON st.id = s.strategy_id
        WHERE s.signal_date <= ?
          AND st.status != 'RETIRED'
        ORDER BY s.signal_date DESC, s.id
        """,
        (as_of_date,),
    ).fetchall()
    evals = latest_evaluations(conn, as_of_date)
    candidate_evals = latest_candidate_evaluations(conn, as_of_date)
    candidate_details = latest_candidate_details(conn, as_of_date)
    candidate_details_display = selected_candidate_details(candidate_details)
    candidate_leaderboard = candidate_horizon_strategy_leaderboard(
        conn,
        as_of_date,
        as_of_date,
        horizon_days=10,
        dedupe=True,
    )
    leaderboard = strategy_leaderboard(conn, as_of_date)
    candidates = latest_candidates(conn, as_of_date)
    candidates_display = candidates[:REPORT_CANDIDATE_LIMIT]
    audits = latest_audits(conn, as_of_date)
    coverage = data_coverage(conn)
    action_plan = daily_action_plan(conn, as_of_date)

    lines: list[str] = []
    lines.append(f"# Alpha Ledger Report - {as_of_date}")
    lines.append("")
    lines.append("## 结论摘要")
    lines.append("")
    if signals:
        lines.append(f"- 当前账本共有 {len(signals)} 条事前预测信号。")
    else:
        lines.append("- 当前账本还没有预测信号。")
    completed_evals = [row for row in evals if row["return_pct"] is not None]
    if completed_evals:
        best = max(completed_evals, key=lambda row: row["return_pct"])
        lines.append(
            f"- 当前最好样本：{best['name']} {best['ticker']}，"
            f"{best['observed_days']} 个交易日收益 {fmt_pct(best['return_pct'])}。"
        )
    lines.append("- 账本原则：先记录预测，再验证结果；策略靠真实收益晋级。")
    lines.append("")

    lines.append("## 数据覆盖")
    lines.append("")
    if coverage:
        lines.append("| 市场 | 股票数 | K线数 | 起始日 | 最新日 |")
        lines.append("|---|---:|---:|---|---|")
        for row in coverage:
            lines.append(
                f"| {row['market']} | {row['tickers']} | {row['bars']} | "
                f"{row['min_date']} | {row['max_date']} |"
            )
    else:
        lines.append("暂无行情数据。")
    lines.append("")

    lines.append("## 今日执行清单")
    lines.append("")
    if action_plan:
        lines.append("| 分组 | 股票 | 市场 | 策略 | 分数 | 入场 | 止损 | 目标1 | 风报比 |")
        lines.append("|---|---|---|---|---:|---:|---:|---:|---:|")
        for row in action_plan[:REPORT_CANDIDATE_LIMIT]:
            lines.append(
                "| "
                f"{row['plan_bucket']} | {row['name']} `{row['ticker']}` | {row['market']} | "
                f"{row['strategy_name']} | {float(row['candidate_score']):.1f} | "
                f"{fmt_price(row['entry_price'])} | {fmt_price(row['stop_loss'])} | "
                f"{fmt_price(row['target_1'])} | {fmt_price(row['reward_risk'])} |"
            )
        lines.append("")
        lines.append(f"完整执行清单可用 `python -m alpha_ledger daily-plan --as-of {as_of_date}` 生成。")
    else:
        lines.append("暂无执行候选。")
    lines.append("")

    lines.append("## 今日重点候选")
    lines.append("")
    if candidates_display:
        lines.append("| 股票 | 市场 | 策略 | 分数 | 动作 | 入场价 | 止损 | 目标1 | 触发条件 |")
        lines.append("|---|---|---|---:|---|---:|---:|---:|---|")
        for candidate in candidates_display:
            lines.append(
                "| "
                f"{candidate['name']} `{candidate['ticker']}` | {candidate['market']} | "
                f"{candidate['strategy_name']} | {float(candidate['candidate_score']):.1f} | "
                f"{candidate['action']} | {fmt_price(candidate['entry_price'])} | "
                f"{fmt_price(candidate['stop_loss'])} | {fmt_price(candidate['target_1'])} | "
                f"{candidate['trigger_condition']} |"
            )
        if len(candidates) > len(candidates_display):
            lines.append("")
            lines.append(
                f"本节仅展示分数最高的 {len(candidates_display)} 个候选；"
                f"当日完整候选数为 {len(candidates)}，可用 `python -m alpha_ledger candidates --as-of {as_of_date}` 查看。"
            )
    else:
        lines.append("暂无新候选。没有候选比硬凑股票更有价值。")
    lines.append("")

    lines.append("## 信号跟踪")
    lines.append("")
    lines.append("| 日期 | 股票 | 市场 | 策略 | 入场价 | 止损 | 目标1 | 目标2 | 置信度 | 状态 |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---|---|")
    for signal in signals:
        lines.append(
            "| "
            f"{signal['signal_date']} | {signal['name']} `{signal['ticker']}` | {signal['market']} | "
            f"{signal['strategy_name']} | {fmt_price(signal['entry_price'])} | "
            f"{fmt_price(signal['stop_loss'])} | {fmt_price(signal['target_1'])} | "
            f"{fmt_price(signal['target_2'])} | {signal['confidence']} | {signal['status']} |"
        )
    lines.append("")

    lines.append("## 候选截止日后验验证")
    lines.append("")
    if candidate_evals:
        lines.append("| 股票 | 策略 | 动作 | 执行日 | 执行价 | 观察到 | 退出 | 退出价 | 收益 | 最大浮盈 | 最大回撤 |")
        lines.append("|---|---|---|---|---:|---:|---|---:|---:|---:|---:|")
        candidate_eval_display = selected_candidate_evaluations(candidate_evals)
        for row in candidate_eval_display:
            lines.append(
                "| "
                f"{row['name']} `{row['ticker']}` | {row['strategy_name']} | {row['action']} | "
                f"{row['execution_date']} | {fmt_price(row['execution_price'])} | "
                f"{row['observed_days']}日 | {row['exit_type']} {row['exit_date']} | "
                f"{fmt_price(row['exit_price'])} | {fmt_pct(row['return_pct'])} | "
                f"{fmt_pct(row['max_gain_pct'])} | {fmt_pct(row['max_drawdown_pct'])} |"
            )
        if len(candidate_evals) > len(candidate_eval_display):
            lines.append("")
            lines.append(
                f"本节展示收益最高和最低的代表样本共 {len(candidate_eval_display)} 个；"
                f"当日完整后验样本数为 {len(candidate_evals)}。"
            )
    else:
        lines.append("暂无候选后验验证。")
    lines.append("")

    lines.append("## 候选策略 T+10 固定周期回放（完整样本）")
    lines.append("")
    if candidate_leaderboard:
        lines.append("| 策略 | 候选数 | 已验证 | 平均收益 | 胜率 | 目标1率 | 止损率 | 平均最大浮盈 | 平均最大回撤 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in candidate_leaderboard:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}` | {row['candidate_count']} | "
                f"{row['evaluated_count']} | {fmt_pct(row['avg_return_pct'])} | "
                f"{fmt_rate(row['win_rate'])} | {fmt_rate(row['target_1_rate'])} | "
                f"{fmt_rate(row['stop_rate'])} | {fmt_pct(row['avg_max_gain_pct'])} | "
                f"{fmt_pct(row['avg_max_drawdown_pct'])} |"
            )
    else:
        lines.append("暂无候选策略回放数据。")
    lines.append("")

    lines.append("## 事后评估")
    lines.append("")
    completed_evals = [row for row in evals if row["horizon_days"] is not None]
    if completed_evals:
        lines.append("| 股票 | 周期 | 已观察 | 退出 | 退出价 | 收益 | 最大浮盈 | 最大回撤 |")
        lines.append("|---|---:|---:|---|---:|---:|---:|---:|")
        for row in completed_evals:
            lines.append(
                "| "
                f"{row['name']} `{row['ticker']}` | T+{row['horizon_days']} | {row['observed_days']} | "
                f"{row['exit_type']} {row['exit_date']} | {fmt_price(row['exit_price'])} | "
                f"{fmt_pct(row['return_pct'])} | {fmt_pct(row['max_gain_pct'])} | "
                f"{fmt_pct(row['max_drawdown_pct'])} |"
            )
    else:
        lines.append("暂无可评估数据。")
    lines.append("")

    lines.append("## 策略排行榜")
    lines.append("")
    if leaderboard:
        lines.append("| 策略 | 样本数 | 目标周期 | 权重 | T+5均值 | T+10均值 | T+20均值 | T+60均值 | T+10胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in leaderboard:
            win_rate = row["win_rate_10d"]
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}` | {row['signal_count']} | "
                f"T+{row['target_horizon_days']} | {float(row['weight']):.2f} | "
                f"{fmt_pct(row['avg_return_5d'])} | "
                f"{fmt_pct(row['avg_return_10d'])} | {fmt_pct(row['avg_return_20d'])} | "
                f"{fmt_pct(row['avg_return_60d'])} | "
                f"{'-' if win_rate is None else f'{float(win_rate) * 100:.2f}%'} |"
            )
    else:
        lines.append("暂无策略战绩。")
    lines.append("")

    lines.append("## 策略健康审计")
    lines.append("")
    if audits:
        lines.append("| 策略 | 状态 | Edge分 | 样本质量 | 拥挤风险 | 失效风险 | 说明 |")
        lines.append("|---|---|---:|---:|---:|---:|---|")
        for audit in audits:
            lines.append(
                "| "
                f"{audit['strategy_name']} `{audit['strategy_id']}` | {audit['health_status']} | "
                f"{float(audit['edge_score']):.1f} | {float(audit['sample_quality_score']):.2f} | "
                f"{float(audit['crowding_risk_score']):.2f} | {float(audit['decay_risk_score']):.2f} | "
                f"{audit['notes']} |"
            )
    else:
        lines.append("暂无策略健康审计。运行 `python -m alpha_ledger audit --as-of 日期` 后生成。")
    lines.append("")

    lines.append("## 候选样本细节")
    lines.append("")
    if candidate_details_display:
        for candidate in candidate_details_display:
            lines.append(
                f"### {candidate['name']} `{candidate['ticker']}` - {candidate['strategy_name']}"
            )
            lines.append("")
            lines.append(
                f"- 分数/动作：{float(candidate['candidate_score']):.1f} / {candidate['action']}"
            )
            lines.append(
                f"- 价格计划：入场 {fmt_price(candidate['entry_price'])}，"
                f"买区 {fmt_price(candidate['buy_zone_low'])}-{fmt_price(candidate['buy_zone_high'])}，"
                f"止损 {fmt_price(candidate['stop_loss'])}，目标 "
                f"{fmt_price(candidate['target_1'])}/{fmt_price(candidate['target_2'])}"
            )
            lines.append(f"- 买入逻辑：{candidate['thesis']}")
            lines.append(f"- 触发条件：{candidate['trigger_condition']}")
            lines.append(f"- 风险点：{candidate['risk_notes']}")
            if candidate["return_pct"] is not None:
                lines.append(
                    f"- 后验结果：观察到 {candidate['observed_days']} 日，"
                    f"截止 {candidate['end_date']} 收盘 {fmt_price(candidate['end_close'])}，"
                    f"收益 {fmt_pct(candidate['return_pct'])}，最大浮盈 "
                    f"{fmt_pct(candidate['max_gain_pct'])}，最大回撤 "
                    f"{fmt_pct(candidate['max_drawdown_pct'])}，"
                    f"止损 {'是' if candidate['hit_stop'] else '否'}，"
                    f"目标1 {'是' if candidate['hit_target_1'] else '否'}。"
                )
            for item in safe_json_list(candidate["evidence_json"]):
                lines.append(evidence_line(item))
            lines.append("")
        if len(candidate_details) > len(candidate_details_display):
            lines.append(
                f"本节展示高分、最好和最差的代表样本共 {len(candidate_details_display)} 个；"
                f"当日完整候选明细数为 {len(candidate_details)}，避免主报告被低优先级样本淹没。"
            )
            lines.append("")
    else:
        lines.append("暂无候选样本。")
        lines.append("")

    lines.append("## 正式信号样本细节")
    lines.append("")
    if not signals:
        lines.append("暂无正式信号样本；自动筛选样本见上一节。")
        lines.append("")
    for signal in signals:
        evidence = safe_json_list(signal["evidence_json"])
        lines.append(f"### {signal['name']} `{signal['ticker']}`")
        lines.append("")
        lines.append(f"- 策略：{signal['strategy_name']}")
        lines.append(f"- 买入逻辑：{signal['thesis']}")
        lines.append(f"- 触发条件：{signal['trigger_condition']}")
        lines.append(f"- 风险点：{signal['risk_notes']}")
        for item in evidence:
            lines.append(evidence_line(item))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(conn: sqlite3.Connection, as_of_date: str, out_path: Path | str | None = None) -> Path:
    path = Path(out_path) if out_path else Path("reports") / f"alpha_report_{as_of_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(conn, as_of_date), encoding="utf-8")
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
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate
        FROM candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        JOIN strategies st ON st.id = c.strategy_id
        WHERE c.as_of_date >= ? AND c.as_of_date <= ?
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
          AND st.status != 'RETIRED'
        ORDER BY e.return_pct {direction}, c.candidate_score DESC
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
                st.weight,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            WHERE c.as_of_date >= ?
              AND c.as_of_date <= ?
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
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_5d,
            COUNT(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_10d,
            COUNT(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_20d,
            COUNT(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_60d
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name
        ORDER BY COALESCE(avg_return_10d, avg_return_5d, -999) DESC, completed_10d DESC, candidate_count DESC
        """,
        (through_date, start_date, end_date),
    ).fetchall()


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
    daily = replay_daily_summary(conn, start_date, end_date)
    winners = replay_samples(conn, start_date, end_date, order="best")
    losers = replay_samples(conn, start_date, end_date, order="worst")
    total_candidates = sum(int(row["candidate_count"]) for row in daily)
    total_evaluated = sum(int(row["evaluated_count"]) for row in daily)

    lines: list[str] = []
    lines.append(f"# Alpha Ledger Replay - {start_date} to {end_date}")
    lines.append("")
    lines.append("## 结论摘要")
    lines.append("")
    lines.append(
        f"- 回放区间覆盖 {len(daily)} 个有候选日期，共 {total_candidates} 个候选，"
        f"其中 {total_evaluated} 个已用 {through_date} 后验验证。"
    )
    lines.append("- 回放只使用候选日当时可见的价格、事件日期和已披露财务数据；历史资金流若无可回放数据，不参与打分。")
    lines.append("- 候选日不假设可在收盘成交；后验收益默认按候选日后第一个交易日开盘价作为执行价。")
    lines.append("- 固定周期榜只统计完整走满 T+5/T+10/T+20/T+60 的样本；未走满的候选继续等待，不计入正式胜率。")
    lines.append("- 策略榜同时展示原始候选和去重候选；去重规则为同一日期同一股票只保留分数最高的策略。")
    lines.append("")

    lines.append("## 固定持有周期策略榜")
    lines.append("")
    if horizon_matrix:
        lines.append("| 策略 | 候选数 | T+5样本 | T+5均值 | T+5胜率 | T+10样本 | T+10均值 | T+10胜率 | T+20样本 | T+20均值 | T+20胜率 | T+60样本 | T+60均值 | T+60胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in horizon_matrix:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}` | {row['candidate_count']} | "
                f"{row['completed_5d']} | {fmt_pct(row['avg_return_5d'])} | {fmt_rate(row['win_rate_5d'])} | "
                f"{row['completed_10d']} | {fmt_pct(row['avg_return_10d'])} | {fmt_rate(row['win_rate_10d'])} | "
                f"{row['completed_20d']} | {fmt_pct(row['avg_return_20d'])} | {fmt_rate(row['win_rate_20d'])} | "
                f"{row['completed_60d']} | {fmt_pct(row['avg_return_60d'])} | {fmt_rate(row['win_rate_60d'])} |"
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
        lines.append("| 策略 | 候选数 | 已验证 | 均分 | 平均收益 | 胜率 | 目标1率 | 目标2率 | 止损率 | MFE平均最大浮盈 | MAE平均最大回撤 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}` | {row['candidate_count']} | "
                f"{row['evaluated_count']} | {fmt_price(row['avg_candidate_score'])} | "
                f"{fmt_pct(row['avg_return_pct'])} | {fmt_rate(row['win_rate'])} | "
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
        lines.append(f"| {segment_name} | 候选数 | 已验证 | 均分 | 平均收益 | 胜率 | 目标1率 | 止损率 | MFE | MAE |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                "| "
                f"{row['segment']} | {row['candidate_count']} | {row['evaluated_count']} | "
                f"{fmt_price(row['avg_candidate_score'])} | {fmt_pct(row['avg_return_pct'])} | "
                f"{fmt_rate(row['win_rate'])} | {fmt_rate(row['target_1_rate'])} | "
                f"{fmt_rate(row['stop_rate'])} | {fmt_pct(row['avg_max_gain_pct'])} | "
                f"{fmt_pct(row['avg_max_drawdown_pct'])} |"
            )
        lines.append("")

    append_segment_table("## 分市场表现", market_leaderboard, "市场")
    append_segment_table("## 触发类型表现", action_leaderboard, "触发类型")

    lines.append("## 策略权重建议（基于去重 T+10 固定周期）")
    lines.append("")
    if weight_suggestions:
        lines.append("| 策略 | 已验证 | 当前权重 | 建议权重 | 建议 | 止损率 | 胜率 | 平均收益 | 原因 |")
        lines.append("|---|---:|---:|---:|---|---:|---:|---:|---|")
        for item in weight_suggestions:
            lines.append(
                "| "
                f"{item['strategy_name']} `{item['strategy_id']}` | {item['evaluated_count']} | "
                f"{float(item['current_weight']):.2f} | {float(item['suggested_weight']):.2f} | "
                f"{item['recommendation']} | {fmt_rate(item['stop_rate'])} | "
                f"{fmt_rate(item['win_rate'])} | {fmt_pct(item['avg_return_pct'])} | "
                f"{item['reason']} |"
            )
    else:
        lines.append("暂无策略权重建议。")
    lines.append("")

    lines.append("## 每日回放概览")
    lines.append("")
    if daily:
        lines.append("| 日期 | 候选数 | 已验证 | 平均收益 | 胜率 | 目标1率 | 止损率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for row in daily:
            lines.append(
                "| "
                f"{row['as_of_date']} | {row['candidate_count']} | {row['evaluated_count']} | "
                f"{fmt_pct(row['avg_return_pct'])} | {fmt_rate(row['win_rate'])} | "
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
        lines.append("| 日期 | 股票 | 策略 | 分数 | 计划入场 | 执行价 | 退出 | 退出价 | 收益 | 最大浮盈 | 最大回撤 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---|")
        for row in rows:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 90:
                trigger = trigger[:87] + "..."
            lines.append(
                "| "
                f"{row['as_of_date']} | {row['name']} `{row['ticker']}` | {row['strategy_name']} | "
                f"{float(row['candidate_score']):.1f} | {fmt_price(row['entry_price'])} | "
                f"{fmt_price(row['execution_price'])} | {row['exit_type']} {row['exit_date']} | "
                f"{fmt_price(row['exit_price'])} | "
                f"{fmt_pct(row['return_pct'])} | "
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
