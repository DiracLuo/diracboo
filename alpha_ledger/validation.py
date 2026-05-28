from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from math import sqrt
from pathlib import Path

from .metrics import compute_sharpe_ratio, compute_sortino_ratio
from .portfolio_backtest import PortfolioResult


@dataclass(frozen=True)
class MonteCarloResult:
    observed_sharpe: float
    observed_max_drawdown_pct: float
    permutations: int
    sharpe_p_value: float
    drawdown_p_value: float
    random_sharpe_median: float
    random_sharpe_5th: float
    random_sharpe_95th: float
    random_drawdown_median: float


@dataclass(frozen=True)
class BootstrapResult:
    observed_sharpe: float
    median_sharpe: float
    ci_lower: float
    ci_upper: float
    prob_positive_sharpe: float
    samples: int


@dataclass(frozen=True)
class WalkForwardWindow:
    start_date: str
    end_date: str
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    trade_count: int
    win_rate: float


@dataclass(frozen=True)
class WalkForwardValidation:
    windows: tuple[WalkForwardWindow, ...]
    consistency_rate: float
    mean_return_pct: float
    std_return_pct: float
    mean_sharpe: float
    std_sharpe: float


def _daily_returns_from_equity(daily_equity: list[tuple[str, float]]) -> list[float]:
    if len(daily_equity) < 2:
        return []
    returns = []
    for i in range(1, len(daily_equity)):
        prev = daily_equity[i - 1][1]
        curr = daily_equity[i][1]
        if prev > 0:
            returns.append((curr - prev) / prev)
    return returns


def _max_drawdown_from_equity(daily_equity: list[tuple[str, float]]) -> float:
    if not daily_equity:
        return 0.0
    peak = daily_equity[0][1]
    max_dd = 0.0
    for _, equity in daily_equity:
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _shuffled_max_drawdown(pnl_sequence: list[float]) -> float:
    capital = 1_000_000.0
    peak = capital
    max_dd = 0.0
    for pnl in pnl_sequence:
        capital += pnl
        if capital > peak:
            peak = capital
        if peak > 0:
            dd = (peak - capital) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _recompute_sharpe_from_trades(
    pnl_list: list[float],
    daily_equity: list[tuple[str, float]],
    initial_capital: float,
) -> float:
    """Rebuild equity curve from shuffled trade PnLs and compute Sharpe.

    Distributes each trade's PnL evenly across its original holding-period
    days so that the daily-return series has the correct length.
    """
    if not daily_equity or len(daily_equity) < 2:
        return 0.0
    n_days = len(daily_equity)
    daily_pnl = [0.0] * n_days
    per_trade_days = max(1, n_days // max(1, len(pnl_list)))
    for i, pnl in enumerate(pnl_list):
        start = min(i * per_trade_days, n_days - 1)
        end = min(start + per_trade_days, n_days)
        share = pnl / max(1, end - start)
        for d in range(start, end):
            daily_pnl[d] += share
    capital = initial_capital
    returns = []
    for pnl in daily_pnl:
        prev = capital
        capital += pnl
        if prev > 0:
            returns.append((capital - prev) / prev)
    return compute_sharpe_ratio(returns)


def monte_carlo_permutation(
    result: PortfolioResult,
    *,
    permutations: int = 1000,
    seed: int | None = None,
) -> MonteCarloResult:
    if result.trade_count < 5:
        return MonteCarloResult(
            observed_sharpe=result.sharpe_ratio,
            observed_max_drawdown_pct=result.max_drawdown_pct,
            permutations=0,
            sharpe_p_value=1.0,
            drawdown_p_value=1.0,
            random_sharpe_median=0.0,
            random_sharpe_5th=0.0,
            random_sharpe_95th=0.0,
            random_drawdown_median=0.0,
        )

    rng = random.Random(seed)
    pnl_list = [t.pnl for t in result.trades]
    observed_sharpe = result.sharpe_ratio
    observed_dd = result.max_drawdown_pct

    random_sharpes: list[float] = []
    random_drawdowns: list[float] = []

    for _ in range(permutations):
        shuffled = pnl_list[:]
        rng.shuffle(shuffled)
        random_drawdowns.append(_shuffled_max_drawdown(shuffled))
        random_sharpes.append(
            _recompute_sharpe_from_trades(shuffled, result.daily_equity, result.initial_capital)
        )

    sharpe_better = sum(1 for s in random_sharpes if s >= observed_sharpe)
    dd_better = sum(1 for d in random_drawdowns if d <= observed_dd)

    random_sharpes_sorted = sorted(random_sharpes)
    idx_5 = max(0, int(permutations * 0.05) - 1)
    idx_95 = min(permutations - 1, int(permutations * 0.95) - 1)
    idx_med = permutations // 2

    return MonteCarloResult(
        observed_sharpe=observed_sharpe,
        observed_max_drawdown_pct=observed_dd,
        permutations=permutations,
        sharpe_p_value=sharpe_better / permutations,
        drawdown_p_value=dd_better / permutations,
        random_sharpe_median=random_sharpes_sorted[idx_med],
        random_sharpe_5th=random_sharpes_sorted[idx_5],
        random_sharpe_95th=random_sharpes_sorted[idx_95],
        random_drawdown_median=sorted(random_drawdowns)[idx_med],
    )


def bootstrap_sharpe_ci(
    result: PortfolioResult,
    *,
    samples: int = 1000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> BootstrapResult:
    returns = _daily_returns_from_equity(result.daily_equity)
    if len(returns) < 10:
        return BootstrapResult(
            observed_sharpe=result.sharpe_ratio,
            median_sharpe=result.sharpe_ratio,
            ci_lower=result.sharpe_ratio,
            ci_upper=result.sharpe_ratio,
            prob_positive_sharpe=1.0 if result.sharpe_ratio > 0 else 0.0,
            samples=0,
        )

    rng = random.Random(seed)
    observed_sharpe = compute_sharpe_ratio(returns)
    boot_sharpes: list[float] = []
    n = len(returns)

    for _ in range(samples):
        resampled = [returns[rng.randint(0, n - 1)] for _ in range(n)]
        boot_sharpes.append(compute_sharpe_ratio(resampled))

    boot_sharpes.sort()
    alpha = (1.0 - confidence) / 2.0
    idx_lower = max(0, int(samples * alpha) - 1)
    idx_upper = min(samples - 1, int(samples * (1.0 - alpha)) - 1)
    idx_med = samples // 2

    prob_positive = sum(1 for s in boot_sharpes if s > 0) / samples

    return BootstrapResult(
        observed_sharpe=observed_sharpe,
        median_sharpe=boot_sharpes[idx_med],
        ci_lower=boot_sharpes[idx_lower],
        ci_upper=boot_sharpes[idx_upper],
        prob_positive_sharpe=prob_positive,
        samples=samples,
    )


def walk_forward_windows(
    result: PortfolioResult,
    *,
    n_windows: int = 5,
) -> WalkForwardValidation:
    equity = result.daily_equity
    if len(equity) < n_windows * 5:
        return WalkForwardValidation(
            windows=(),
            consistency_rate=0.0,
            mean_return_pct=0.0,
            std_return_pct=0.0,
            mean_sharpe=0.0,
            std_sharpe=0.0,
        )

    window_size = len(equity) // n_windows
    windows: list[WalkForwardWindow] = []

    for i in range(n_windows):
        start_idx = i * window_size
        end_idx = start_idx + window_size if i < n_windows - 1 else len(equity)
        window_equity = equity[start_idx:end_idx]

        if len(window_equity) < 2:
            continue

        start_date = window_equity[0][0]
        end_date = window_equity[-1][0]
        start_val = window_equity[0][1]
        end_val = window_equity[-1][1]
        ret = (end_val - start_val) / start_val * 100.0 if start_val > 0 else 0.0
        dd = _max_drawdown_from_equity(window_equity)
        win_returns = _daily_returns_from_equity(window_equity)
        sharpe = compute_sharpe_ratio(win_returns) if win_returns else 0.0

        window_trades = [
            t for t in result.trades
            if start_date <= t.entry_date <= end_date
        ]
        trade_count = len(window_trades)
        wins = sum(1 for t in window_trades if t.net_return_pct > 0)
        win_rate = wins / trade_count * 100.0 if trade_count > 0 else 0.0

        windows.append(WalkForwardWindow(
            start_date=start_date,
            end_date=end_date,
            total_return_pct=ret,
            sharpe=sharpe,
            max_drawdown_pct=dd,
            trade_count=trade_count,
            win_rate=win_rate,
        ))

    if not windows:
        return WalkForwardValidation(
            windows=(),
            consistency_rate=0.0,
            mean_return_pct=0.0,
            std_return_pct=0.0,
            mean_sharpe=0.0,
            std_sharpe=0.0,
        )

    profitable = sum(1 for w in windows if w.total_return_pct > 0)
    consistency = profitable / len(windows)
    rets = [w.total_return_pct for w in windows]
    sharpes = [w.sharpe for w in windows]
    mean_ret = sum(rets) / len(rets)
    mean_sharpe = sum(sharpes) / len(sharpes)
    std_ret = sqrt(sum((r - mean_ret) ** 2 for r in rets) / len(rets)) if len(rets) > 1 else 0.0
    std_sharpe = sqrt(sum((s - mean_sharpe) ** 2 for s in sharpes) / len(sharpes)) if len(sharpes) > 1 else 0.0

    return WalkForwardValidation(
        windows=tuple(windows),
        consistency_rate=consistency,
        mean_return_pct=mean_ret,
        std_return_pct=std_ret,
        mean_sharpe=mean_sharpe,
        std_sharpe=std_sharpe,
    )


def render_validation_report(
    result: PortfolioResult,
    mc: MonteCarloResult,
    bootstrap: BootstrapResult,
    wf: WalkForwardValidation,
) -> str:
    lines: list[str] = []
    lines.append("# 统计验证报告")
    lines.append("")
    lines.append(f"- 回测区间：{result.start_date} ~ {result.end_date}")
    lines.append(f"- 总收益：{result.total_return_pct:.2f}%")
    lines.append(f"- 最大回撤：{result.max_drawdown_pct:.2f}%")
    lines.append(f"- 交易笔数：{result.trade_count}")
    lines.append(f"- Sharpe：{result.sharpe_ratio:.4f}")
    lines.append("")

    lines.append("## Monte Carlo 置换检验")
    lines.append("")
    lines.append(f"- 置换次数：{mc.permutations}")
    lines.append(f"- 观测 Sharpe：{mc.observed_sharpe:.4f}")
    lines.append(f"- 随机 Sharpe 中位数：{mc.random_sharpe_median:.4f}（5th={mc.random_sharpe_5th:.4f}, 95th={mc.random_sharpe_95th:.4f}）")
    lines.append(f"- Sharpe p-value：{mc.sharpe_p_value:.4f} {'(显著)' if mc.sharpe_p_value < 0.05 else '(不显著)'}")
    lines.append(f"- 观测最大回撤：{mc.observed_max_drawdown_pct:.2f}%")
    lines.append(f"- 随机回撤中位数：{mc.random_drawdown_median:.2f}%")
    lines.append(f"- 回撤 p-value：{mc.drawdown_p_value:.4f} {'(显著)' if mc.drawdown_p_value < 0.05 else '(不显著)'}")
    lines.append("")

    lines.append("## Bootstrap Sharpe 置信区间")
    lines.append("")
    lines.append(f"- 观测 Sharpe：{bootstrap.observed_sharpe:.4f}")
    lines.append(f"- Bootstrap 中位数：{bootstrap.median_sharpe:.4f}")
    lines.append(f"- 95% 置信区间：[{bootstrap.ci_lower:.4f}, {bootstrap.ci_upper:.4f}]")
    lines.append(f"- Sharpe > 0 概率：{bootstrap.prob_positive_sharpe:.2%}")
    lines.append("")

    lines.append("## Walk-Forward 窗口分析")
    lines.append("")
    lines.append(f"- 窗口数：{len(wf.windows)}")
    lines.append(f"- 盈利窗口一致性：{wf.consistency_rate:.2%}")
    lines.append(f"- 窗口平均收益：{wf.mean_return_pct:.2f}% (std={wf.std_return_pct:.2f}%)")
    lines.append(f"- 窗口平均 Sharpe：{wf.mean_sharpe:.4f} (std={wf.std_sharpe:.4f})")
    lines.append("")
    if wf.windows:
        lines.append("| 窗口 | 区间 | 收益 | Sharpe | 回撤 | 交易数 | 胜率 |")
        lines.append("|------|------|------|--------|------|--------|------|")
        for i, w in enumerate(wf.windows, 1):
            lines.append(f"| {i} | {w.start_date}~{w.end_date} | {w.total_return_pct:.2f}% | {w.sharpe:.4f} | {w.max_drawdown_pct:.2f}% | {w.trade_count} | {w.win_rate:.1f}% |")
        lines.append("")

    lines.append("## 结论")
    lines.append("")
    if mc.sharpe_p_value < 0.05:
        lines.append("- Monte Carlo：Sharpe 显著优于随机排列（p < 0.05）")
    else:
        lines.append("- Monte Carlo：Sharpe 未显著优于随机排列（p >= 0.05），策略收益可能来自运气")
    if bootstrap.ci_lower > 0:
        lines.append("- Bootstrap：Sharpe 95% CI 下界 > 0，策略有正期望")
    elif bootstrap.ci_upper < 0:
        lines.append("- Bootstrap：Sharpe 95% CI 上界 < 0，策略有负期望")
    else:
        lines.append("- Bootstrap：Sharpe 95% CI 跨越 0，无法确认策略有正期望")
    if wf.consistency_rate >= 0.6:
        lines.append(f"- Walk-Forward：{wf.consistency_rate:.0%} 窗口盈利，策略在不同时间段有一定稳定性")
    else:
        lines.append(f"- Walk-Forward：仅 {wf.consistency_rate:.0%} 窗口盈利，策略稳定性不足")
    lines.append("")
    return "\n".join(lines)


def write_validation_report(
    result: PortfolioResult,
    out_path: Path | None = None,
) -> Path:
    mc = monte_carlo_permutation(result)
    bootstrap = bootstrap_sharpe_ci(result)
    wf = walk_forward_windows(result)
    report = render_validation_report(result, mc, bootstrap, wf)
    path = out_path or Path("reports") / f"validation_{result.start_date}_{result.end_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    return path
