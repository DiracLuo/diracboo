from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .metrics import FORMAL_MARKETS
from .portfolio_backtest import run_portfolio_backtest


@dataclass(frozen=True)
class WalkForwardResult:
    start_date: str
    end_date: str
    status: str
    trading_days: int
    formal_candidates: int
    portfolio_trades: int
    adjusted_coverage_pct: float
    benchmark_coverage_pct: float
    min_trading_days: int
    min_candidates: int
    min_trades: int
    notes: tuple[str, ...]


def _pct(numerator: int, denominator: int) -> float:
    return numerator / denominator * 100.0 if denominator else 0.0


def _adjusted_coverage(conn: sqlite3.Connection, start_date: str, end_date: str) -> float:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN adjustment_status = 'ADJUSTED' THEN 1 ELSE 0 END) AS adjusted
        FROM price_bars
        WHERE market = 'CN_A'
          AND ticker != '000300.SS'
          AND date >= ?
          AND date <= ?
        """,
        (start_date, end_date),
    ).fetchone()
    return _pct(int(row["adjusted"] or 0), int(row["total"] or 0)) if row else 0.0


def _benchmark_coverage(conn: sqlite3.Connection, start_date: str, end_date: str, benchmark: str = "000300.SS") -> float:
    trading_days = conn.execute(
        """
        SELECT COUNT(DISTINCT date) AS count
        FROM price_bars
        WHERE market = 'CN_A'
          AND date >= ?
          AND date <= ?
        """,
        (start_date, end_date),
    ).fetchone()
    benchmark_days = conn.execute(
        """
        SELECT COUNT(DISTINCT date) AS count
        FROM price_bars
        WHERE market = 'CN_A'
          AND ticker = ?
          AND date >= ?
          AND date <= ?
        """,
        (benchmark, start_date, end_date),
    ).fetchone()
    return _pct(int(benchmark_days["count"] or 0), int(trading_days["count"] or 0))


def run_walk_forward(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    min_trading_days: int = 120,
    min_candidates: int = 500,
    min_trades: int = 50,
    min_coverage_pct: float = 95.0,
) -> WalkForwardResult:
    markets = FORMAL_MARKETS
    trading_days = conn.execute(
        """
        SELECT COUNT(DISTINCT date) AS count
        FROM price_bars
        WHERE market = 'CN_A'
          AND date >= ?
          AND date <= ?
        """,
        (start_date, end_date),
    ).fetchone()
    candidate_count = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM candidates
        WHERE market IN ({', '.join('?' for _ in markets)})
          AND as_of_date >= ?
          AND as_of_date <= ?
        """,
        tuple(markets) + (start_date, end_date),
    ).fetchone()
    day_count = int(trading_days["count"] or 0)
    formal_candidates = int(candidate_count["count"] or 0)
    portfolio_result = run_portfolio_backtest(
        conn,
        start_date,
        end_date,
        end_date,
        markets=markets,
    )
    portfolio_trades = portfolio_result.trade_count
    adjusted_coverage = _adjusted_coverage(conn, start_date, end_date)
    benchmark_coverage = _benchmark_coverage(conn, start_date, end_date)

    notes: list[str] = []
    if day_count < min_trading_days:
        notes.append(f"交易日不足：{day_count}/{min_trading_days}。")
    if formal_candidates < min_candidates:
        notes.append(f"正式候选不足：{formal_candidates}/{min_candidates}。")
    if portfolio_trades < min_trades:
        notes.append(f"已验证交易不足：{portfolio_trades}/{min_trades}。")
    if adjusted_coverage < min_coverage_pct:
        notes.append(f"复权覆盖不足：{adjusted_coverage:.1f}%/{min_coverage_pct:.1f}%。")
    if benchmark_coverage < min_coverage_pct:
        notes.append(f"基准覆盖不足：{benchmark_coverage:.1f}%/{min_coverage_pct:.1f}%。")

    status = "READY" if not notes else "INSUFFICIENT_HISTORY"
    if status == "READY":
        notes.append("满足最低样本条件，可以进入正式 walk-forward 参数验证。")
    else:
        notes.append("当前只允许做时间切分 sanity check，不允许自动调参或升权。")

    return WalkForwardResult(
        start_date=start_date,
        end_date=end_date,
        status=status,
        trading_days=day_count,
        formal_candidates=formal_candidates,
        portfolio_trades=portfolio_trades,
        adjusted_coverage_pct=adjusted_coverage,
        benchmark_coverage_pct=benchmark_coverage,
        min_trading_days=min_trading_days,
        min_candidates=min_candidates,
        min_trades=min_trades,
        notes=tuple(notes),
    )


def render_walk_forward_report(result: WalkForwardResult) -> str:
    lines: list[str] = []
    lines.append(f"# Walk-forward 数据充分性检查 - {result.start_date} to {result.end_date}")
    lines.append("")
    lines.append("> 注意：本报告仅检查数据量是否满足正式 walk-forward 参数验证的前提条件。")
    lines.append("> 它不是 walk-forward 参数优化结果，不包含样本外收益或参数拟合。")
    lines.append("")
    lines.append(f"- Status: `{result.status}`")
    lines.append(f"- Trading days: {result.trading_days}/{result.min_trading_days}")
    lines.append(f"- Formal candidates: {result.formal_candidates}/{result.min_candidates}")
    lines.append(f"- Verified trades: {result.portfolio_trades}/{result.min_trades}")
    lines.append(f"- Adjusted price coverage: {result.adjusted_coverage_pct:.1f}%")
    lines.append(f"- Benchmark coverage: {result.benchmark_coverage_pct:.1f}%")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for note in result.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def write_walk_forward_report(result: WalkForwardResult, out_path: Path | None = None) -> Path:
    path = out_path or Path("reports") / f"walk_forward_{result.start_date}_{result.end_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_walk_forward_report(result), encoding="utf-8")
    return path
