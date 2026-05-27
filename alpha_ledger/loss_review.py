from __future__ import annotations

import sqlite3
from pathlib import Path


def _loss_tags(row: sqlite3.Row) -> list[str]:
    tags: list[str] = []
    trigger = str(row["trigger_condition"] or "")
    score = float(row["candidate_score"] or 0.0)
    rrr = float(row["reward_risk_ratio"] or 0.0)
    net = float(row["net_return_pct"] or 0.0)
    mae = float(row["max_drawdown_pct"] or 0.0)
    execution_type = str(row["execution_type"] or "")
    if "调研" in trigger or "投资者关系" in trigger:
        tags.append("弱事件/调研")
    if score >= 78 and net < 0:
        tags.append("高分亏损")
    if rrr >= 1.5 and net < 0:
        tags.append("高风报比亏损")
    if "STOP" in str(row["exit_type"] or ""):
        tags.append("止损触发")
    if "涨幅 8" in trigger or "涨幅 9" in trigger or "涨幅 10" in trigger or "涨幅 15" in trigger:
        tags.append("追高")
    if mae <= -10:
        tags.append("止损过远/回撤过大")
    if execution_type == "NEXT_OPEN_DAILY":
        tags.append("缺分时成交")
    if float(row["volume_ratio"] or 0.0) < 1.0:
        tags.append("缺少量价承接")
    return tags or ["待人工复盘"]


def loss_review_rows(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    limit: int = 20,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            WHERE through_date <= ?
            GROUP BY candidate_id
        ),
        eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l ON l.candidate_id = e.candidate_id AND l.through_date = e.through_date
        )
        SELECT
            c.as_of_date, c.ticker, c.name, c.market, c.strategy_id,
            c.candidate_score, c.reward_risk_ratio, c.trigger_condition,
            e.execution_type, e.exit_type, e.net_return_pct, e.excess_return_pct,
            e.max_gain_pct, e.max_drawdown_pct,
            CASE
                WHEN c.trigger_condition LIKE '%量比 %'
                THEN CAST(substr(c.trigger_condition, instr(c.trigger_condition, '量比 ') + 3, 4) AS REAL)
                ELSE NULL
            END AS volume_ratio
        FROM candidates c
        JOIN eval e ON e.candidate_id = c.id
        WHERE c.market = 'CN_A'
          AND c.as_of_date >= ?
          AND c.as_of_date <= ?
          AND e.net_return_pct < 0
        ORDER BY e.net_return_pct ASC
        LIMIT ?
        """,
        (through_date, start_date, end_date, limit),
    ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        result.append(
            {
                "as_of_date": row["as_of_date"],
                "ticker": row["ticker"],
                "name": row["name"],
                "market": row["market"],
                "strategy_id": row["strategy_id"],
                "candidate_score": row["candidate_score"],
                "reward_risk_ratio": row["reward_risk_ratio"],
                "execution_type": row["execution_type"],
                "exit_type": row["exit_type"],
                "net_return_pct": row["net_return_pct"],
                "excess_return_pct": row["excess_return_pct"],
                "max_gain_pct": row["max_gain_pct"],
                "max_drawdown_pct": row["max_drawdown_pct"],
                "tags": _loss_tags(row),
                "trigger_condition": row["trigger_condition"],
            }
        )
    return result


def render_loss_review(conn: sqlite3.Connection, start_date: str, end_date: str, through_date: str) -> str:
    rows = loss_review_rows(conn, start_date, end_date, through_date, limit=20)
    lines = [f"# Loss Review - {start_date} to {end_date}", ""]
    lines.append(f"- 验证截止日：`{through_date}`")
    lines.append("- 用途：把亏损样本转成后续硬过滤，不作为买入清单。")
    lines.append("")
    if not rows:
        lines.append("暂无亏损样本。")
        return "\n".join(lines).rstrip() + "\n"
    tag_counts: dict[str, int] = {}
    for row in rows:
        for tag in row["tags"]:
            tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1
    lines.append("## 高频亏损标签")
    lines.append("")
    for tag, count in sorted(tag_counts.items(), key=lambda item: item[1], reverse=True):
        lines.append(f"- {tag}: {count}")
    lines.append("")
    lines.append("## 最大亏损样本")
    lines.append("")
    lines.append("| 日期 | 股票 | 策略 | 分数 | 风报比 | 净收益 | 超额 | MFE | MAE | 退出 | 标签 | 触发摘要 |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|")
    for row in rows:
        trigger = str(row["trigger_condition"]).replace("|", "/")
        if len(trigger) > 80:
            trigger = trigger[:77] + "..."
        lines.append(
            "| "
            f"{row['as_of_date']} | {row['name']} `{row['ticker']}` | {row['strategy_id']} | "
            f"{float(row['candidate_score'] or 0):.1f} | {float(row['reward_risk_ratio'] or 0):.2f} | "
            f"{float(row['net_return_pct'] or 0):.2f}% | "
            f"{float(row['excess_return_pct'] or 0):.2f}% | "
            f"{float(row['max_gain_pct'] or 0):.2f}% | {float(row['max_drawdown_pct'] or 0):.2f}% | "
            f"{row['exit_type']} | {', '.join(str(tag) for tag in row['tags'])} | {trigger} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def write_loss_review(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    out_path: Path | str | None = None,
) -> Path:
    path = Path(out_path) if out_path else Path("reports") / f"loss_review_{start_date}_{end_date}_through_{through_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_loss_review(conn, start_date, end_date, through_date), encoding="utf-8")
    return path
