from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .execution import intraday_exit_path, open_5min_vwap_entry
from .benchmarks import benchmark_for_asset
from .ledger import now_utc
from .metrics import (
    FORMAL_MARKETS,
    _long_trade_path,
    adjusted_bar_price,
    adjusted_price_from_raw,
    adjusted_trade_return_pct,
    benchmark_max_drawdown_pct,
    benchmark_return_pct,
    compute_sharpe_ratio,
    compute_sortino_ratio,
    slippage_pct,
    trade_cost_pct,
)
from .trading_rules import (
    is_one_price_limit_down_from_values,
    is_one_price_limit_up_from_values,
)


@dataclass(frozen=True)
class TradeRecord:
    ticker: str
    name: str
    market: str
    strategy_id: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_type: str
    return_pct: float
    net_return_pct: float
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    cost_pct: float
    slippage_pct: float
    pnl: float
    position_size: float


@dataclass(frozen=True)
class PortfolioResult:
    start_date: str
    end_date: str
    through_date: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate: float
    avg_pnl_per_trade: float
    trade_count: int
    max_concurrent_positions: int
    avg_net_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    turnover_pct: float
    cost_model: str
    skipped_order_count: int
    benchmark_ticker: str | None
    benchmark_return_pct: float | None
    active_return_pct: float | None
    benchmark_max_drawdown_pct: float | None
    win_vs_benchmark_rate: float | None
    require_intraday: bool
    market_scope: tuple[str, ...]
    sharpe_ratio: float
    sortino_ratio: float
    daily_equity: list[tuple[str, float]]
    trades: list[TradeRecord]


@dataclass
class _OpenPosition:
    ticker: str
    name: str
    market: str
    strategy_id: str
    entry_date: str
    entry_price: float
    entry_return_price: float
    stop_loss: float | None
    target_1: float | None
    target_2: float | None
    horizon_days: int
    position_size: float
    shares: float
    exit_date: str
    exit_price: float
    exit_type: str
    return_pct: float
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    cost: float
    slippage_cost: float = 0.0
    exit_deferred: bool = False


@dataclass(frozen=True)
class _PendingOrder:
    candidate_id: int
    signal_date: str
    ticker: str
    name: str
    market: str
    strategy_id: str
    candidate_score: float
    strategy_weight: float
    target_horizon_days: int
    stop_loss: float | None
    target_1: float | None
    target_2: float | None
    trailing_stop_pct: float | None = None
    trailing_activation_pct: float | None = None


def _get_trading_days(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM price_bars WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [str(row["date"]) for row in rows]


def _previous_close(conn: sqlite3.Connection, market: str, ticker: str, before_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date < ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (market, ticker, before_date),
    ).fetchone()
    return float(row["close"]) if row and row["close"] is not None else None


def _instrument_name(conn: sqlite3.Connection, market: str, ticker: str) -> str:
    row = conn.execute(
        "SELECT name FROM instruments WHERE market = ? AND ticker = ?",
        (market, ticker),
    ).fetchone()
    return str(row["name"]) if row and row["name"] is not None else ""


def _is_suspended_or_illiquid(bar: sqlite3.Row) -> bool:
    volume = float(bar["volume"] or 0.0)
    amount = bar["amount"]
    if volume <= 0:
        return True
    if amount is not None and float(amount) <= 0:
        return True
    return False


def _is_one_price_limit_up(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    bar: sqlite3.Row,
) -> bool:
    if market != "CN_A":
        return False
    previous = _previous_close(conn, market, ticker, str(bar["date"]))
    if previous is None or previous <= 0:
        return False
    open_price = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    name = _instrument_name(conn, market, ticker)
    return is_one_price_limit_up_from_values(previous, open_price, high, low, ticker, name)


def _is_one_price_limit_down(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    bar: sqlite3.Row,
) -> bool:
    if market != "CN_A":
        return False
    previous = _previous_close(conn, market, ticker, str(bar["date"]))
    if previous is None or previous <= 0:
        return False
    open_price = float(bar["open"])
    high = float(bar["high"])
    low = float(bar["low"])
    name = _instrument_name(conn, market, ticker)
    return is_one_price_limit_down_from_values(previous, open_price, high, low, ticker, name)


def _get_next_trading_day(
    conn: sqlite3.Connection,
    date: str,
    market: str | None = None,
    ticker: str | None = None,
) -> str | None:
    filters = ["date > ?"]
    params: list[object] = [date]
    if market is not None:
        filters.append("market = ?")
        params.append(market)
    if ticker is not None:
        filters.append("ticker = ?")
        params.append(ticker)
    row = conn.execute(
        f"SELECT MIN(date) AS d FROM price_bars WHERE {' AND '.join(filters)}",
        tuple(params),
    ).fetchone()
    return str(row["d"]) if row and row["d"] else None


def _atr_pct(conn: sqlite3.Connection, market: str, ticker: str, as_of_date: str, period: int = 14) -> float | None:
    rows = conn.execute(
        """
        SELECT high, low, close FROM price_bars
        WHERE market = ? AND ticker = ? AND date <= ?
        ORDER BY date DESC LIMIT ?
        """,
        (market, ticker, as_of_date, period + 1),
    ).fetchall()
    if len(rows) < 2:
        return None
    trs: list[float] = []
    for i in range(len(rows) - 1):
        h = float(rows[i]["high"])
        l = float(rows[i]["low"])
        prev_c = float(rows[i + 1]["close"])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if not trs:
        return None
    atr = sum(trs) / len(trs)
    close = float(rows[0]["close"])
    if close <= 0:
        return None
    return atr / close * 100.0


def _mark_to_market_equity(
    conn: sqlite3.Connection,
    positions: list[_OpenPosition],
    capital: float,
    day: str,
) -> float:
    equity = capital
    for pos in positions:
        bar = _get_price_bar(conn, pos.market, pos.ticker, day)
        if bar is None or pos.entry_return_price <= 0:
            equity += pos.position_size
            continue
        close = adjusted_bar_price(bar, "adj_close", "close")
        equity += pos.position_size * (close / pos.entry_return_price)
    return equity


def _risk_parity_position_size(
    *,
    capital: float,
    entry_price: float,
    stop_loss: float | None,
    remaining_slots: int,
    initial_capital: float,
    risk_budget_pct: float,
    max_position_pct: float,
) -> float:
    fallback = capital / remaining_slots if remaining_slots > 0 else 0.0
    max_position_cash = initial_capital * max_position_pct / 100.0
    if stop_loss is None or stop_loss <= 0 or stop_loss >= entry_price:
        return min(fallback, max_position_cash, capital)
    risk_per_cash = (entry_price - stop_loss) / entry_price
    if risk_per_cash <= 0:
        return min(fallback, max_position_cash, capital)
    risk_budget_cash = initial_capital * risk_budget_pct / 100.0
    return min(risk_budget_cash / risk_per_cash, max_position_cash, capital)


def _get_price_bar(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    date: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM price_bars WHERE market = ? AND ticker = ? AND date = ?",
        (market, ticker, date),
    ).fetchone()


def _select_actionable_candidates(
    conn: sqlite3.Connection,
    as_of_date: str,
    candidate_end_date: str,
    exclude_tickers: set[tuple[str, str]],
    max_picks: int,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[sqlite3.Row]:
    market_filter = ""
    market_params: list[object] = []
    if markets:
        market_filter = f"AND c.market IN ({', '.join('?' for _ in markets)})"
        market_params.extend(markets)
    rows = conn.execute(
        f"""
        SELECT
            c.*,
            s.weight AS strategy_weight,
            s.target_horizon_days,
            CASE
                WHEN c.stop_loss IS NOT NULL
                     AND c.target_1 IS NOT NULL
                     AND c.stop_loss > 0
                     AND c.entry_price > c.stop_loss
                THEN (c.target_1 - c.entry_price) / (c.entry_price - c.stop_loss)
                ELSE NULL
            END AS computed_rrr
        FROM candidates c
        JOIN strategies s ON s.id = c.strategy_id
        WHERE (
                (c.as_of_date = ? AND c.as_of_date <= ? AND UPPER(c.action) = 'BUY_CANDIDATE')
             OR (
                    c.confirmation_date = ?
                AND c.as_of_date <= ?
                AND (
                        UPPER(c.status) = 'CONFIRMED'
                     OR UPPER(COALESCE(c.confirmation_status, 'PENDING')) = 'CONFIRMED'
                )
             )
          )
          AND s.status != 'RETIRED'
          {market_filter}
        ORDER BY c.candidate_score * s.weight DESC
        """,
        tuple([as_of_date, candidate_end_date, as_of_date, candidate_end_date] + market_params),
    ).fetchall()
    result: list[sqlite3.Row] = []
    for row in rows:
        key = (row["market"], row["ticker"])
        if key in exclude_tickers:
            continue
        rrr = row["computed_rrr"]
        if rrr is not None and float(rrr) < 1.0:
            continue
        if row["confirmation_date"] is None and rrr is not None and float(rrr) < 1.5:
            continue
        result.append(row)
        if len(result) >= max_picks:
            break
    return result


def _pending_order_from_candidate(row: sqlite3.Row) -> _PendingOrder:
    return _PendingOrder(
        candidate_id=int(row["id"]),
        signal_date=str(row["as_of_date"]),
        ticker=str(row["ticker"]),
        name=str(row["name"]),
        market=str(row["market"]),
        strategy_id=str(row["strategy_id"]),
        candidate_score=float(row["candidate_score"] or 0.0),
        strategy_weight=float(row["strategy_weight"] or 1.0),
        target_horizon_days=int(row["target_horizon_days"] or 10),
        stop_loss=float(row["stop_loss"]) if row["stop_loss"] is not None else None,
        target_1=float(row["target_1"]) if row["target_1"] is not None else None,
        target_2=float(row["target_2"]) if row["target_2"] is not None else None,
        trailing_stop_pct=float(row["trailing_stop_pct"]) if row["trailing_stop_pct"] is not None else None,
        trailing_activation_pct=float(row["trailing_activation_pct"]) if row["trailing_activation_pct"] is not None else None,
    )


def _compute_exit(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    entry_date: str,
    entry_price: float,
    stop_loss: float | None,
    target_1: float | None,
    target_2: float | None,
    horizon_days: int,
    through_date: str,
    execution_mode: str = "intraday",
    trailing_stop_pct: float | None = None,
    trailing_activation_pct: float | None = None,
) -> tuple[str, str, float] | None:
    start_operator = ">" if market == "CN_A" else ">="
    bars = conn.execute(
        f"""
        SELECT * FROM price_bars
        WHERE market = ? AND ticker = ? AND date {start_operator} ? AND date <= ?
        ORDER BY date
        """,
        (market, ticker, entry_date, through_date),
    ).fetchall()
    if not bars:
        return None
    truncated = bars[:horizon_days]
    if execution_mode == "intraday":
        intraday_through = str(truncated[-1]["date"])
        intraday_exit = intraday_exit_path(
            conn,
            market,
            ticker,
            entry_date,
            entry_price,
            stop_loss,
            target_1,
            target_2,
            intraday_through,
            trailing_stop_pct=trailing_stop_pct,
            trailing_activation_pct=trailing_activation_pct,
        )
        if intraday_exit is not None:
            exit_type = intraday_exit.exit_type
            if exit_type == "HOLD":
                exit_type = "HORIZON"
            return intraday_exit.date, exit_type, intraday_exit.price
    path = _long_trade_path(
        truncated, entry_price, stop_loss, target_1, target_2, market, ticker=ticker,
        trailing_stop_pct=trailing_stop_pct, trailing_activation_pct=trailing_activation_pct,
    )
    exit_type = path["exit_type"]
    if exit_type == "HOLD":
        exit_type = "HORIZON"
    return str(path["exit_date"]), exit_type, float(path["exit_price"])


def _close_due_positions(
    conn: sqlite3.Connection,
    positions: list[_OpenPosition],
    day: str,
    capital: float,
    closed_trades: list[TradeRecord],
    cooldown_days: int,
    benchmark_ticker: str | None,
) -> tuple[list[_OpenPosition], float, dict[tuple[str, str], int]]:
    cooldowns: dict[tuple[str, str], int] = {}
    still_open: list[_OpenPosition] = []
    for pos in positions:
        if pos.exit_date <= day:
            bar = _get_price_bar(conn, pos.market, pos.ticker, day)
            if bar and _is_one_price_limit_down(conn, pos.market, pos.ticker, bar):
                next_day = _get_next_trading_day(conn, day, pos.market, pos.ticker)
                if next_day:
                    pos.exit_date = next_day
                    pos.exit_deferred = True
                    still_open.append(pos)
                    continue
            if pos.exit_deferred and bar is not None:
                exit_price = float(bar["open"])
                gross_return_pct = adjusted_trade_return_pct(
                    conn,
                    pos.market,
                    pos.ticker,
                    pos.entry_date,
                    pos.entry_price,
                    day,
                    exit_price,
                )
                trade_benchmark_return = benchmark_return_pct(
                    conn,
                    pos.market,
                    pos.entry_date,
                    day,
                    benchmark_ticker=benchmark_for_asset(pos.market, pos.ticker, benchmark_ticker),
                )
                cost_pct = pos.cost / pos.position_size * 100.0 if pos.position_size else 0.0
                pos.exit_date = day
                pos.exit_price = exit_price
                pos.exit_type = f"{pos.exit_type}_LIMIT_DOWN_DEFERRED"
                pos.return_pct = gross_return_pct
                pos.benchmark_return_pct = trade_benchmark_return
                pos.excess_return_pct = (
                    gross_return_pct - cost_pct - trade_benchmark_return
                    if trade_benchmark_return is not None
                    else None
                )
            gross_value = pos.position_size * (1.0 + pos.return_pct / 100.0)
            capital += gross_value - pos.cost - pos.slippage_cost
            pnl = gross_value - pos.cost - pos.slippage_cost - pos.position_size
            net_return_pct = pnl / pos.position_size * 100.0 if pos.position_size else 0.0
            cost_pct = pos.cost / pos.position_size * 100.0 if pos.position_size else 0.0
            slip_pct = pos.slippage_cost / pos.position_size * 100.0 if pos.position_size else 0.0
            closed_trades.append(
                TradeRecord(
                    ticker=pos.ticker,
                    name=pos.name,
                    market=pos.market,
                    strategy_id=pos.strategy_id,
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    exit_date=pos.exit_date,
                    exit_price=pos.exit_price,
                    exit_type=pos.exit_type,
                    return_pct=pos.return_pct,
                    net_return_pct=net_return_pct,
                    benchmark_return_pct=pos.benchmark_return_pct,
                    excess_return_pct=pos.excess_return_pct,
                    cost_pct=cost_pct,
                    slippage_pct=slip_pct,
                    pnl=pnl,
                    position_size=pos.position_size,
                )
            )
            cooldowns[(pos.market, pos.ticker)] = cooldown_days
        else:
            still_open.append(pos)
    return still_open, capital, cooldowns


def run_portfolio_backtest(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    *,
    max_positions: int = 5,
    initial_capital: float = 1_000_000.0,
    cost_bps: int | None = None,
    cooldown_days: int = 10,
    execution_mode: str = "intraday",
    markets: tuple[str, ...] = FORMAL_MARKETS,
    benchmark_ticker: str | None = "000300.SS",
    require_intraday: bool = False,
    drawdown_reduce_threshold: float = 10.0,
    drawdown_halt_threshold: float = 20.0,
    max_per_sector: int = 2,
    sizing_mode: str = "equal",
    risk_budget_pct: float = 2.0,
    max_position_pct: float = 25.0,
) -> PortfolioResult:
    trading_days = _get_trading_days(conn, start_date, through_date)
    if not trading_days:
        return PortfolioResult(
            start_date=start_date,
            end_date=end_date,
            through_date=through_date,
            initial_capital=initial_capital,
            final_capital=initial_capital,
            total_return_pct=0.0,
            max_drawdown_pct=0.0,
            win_rate=0.0,
            avg_pnl_per_trade=0.0,
            trade_count=0,
            max_concurrent_positions=0,
            avg_net_return_pct=0.0,
            avg_win_pct=0.0,
            avg_loss_pct=0.0,
            profit_factor=0.0,
            turnover_pct=0.0,
            cost_model="custom" if cost_bps is not None else "market",
            skipped_order_count=0,
            benchmark_ticker=benchmark_ticker,
            benchmark_return_pct=None,
            active_return_pct=None,
            benchmark_max_drawdown_pct=None,
            win_vs_benchmark_rate=None,
            require_intraday=require_intraday,
            market_scope=markets,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            daily_equity=[],
            trades=[],
        )

    capital = initial_capital
    open_positions: list[_OpenPosition] = []
    closed_trades: list[TradeRecord] = []
    daily_equity: list[tuple[str, float]] = []
    exclude_tickers: dict[tuple[str, str], int] = {}
    pending_orders: dict[str, list[_PendingOrder]] = {}
    cost_model = f"custom:{cost_bps}bps" if cost_bps is not None else "market-cost-by-symbol"
    if sizing_mode != "equal":
        cost_model += f"+{sizing_mode}"
    max_concurrent = 0
    skipped_orders = 0
    peak_equity = initial_capital
    consecutive_losses = 0
    consecutive_loss_cooldown_until: str | None = None

    for day in trading_days:
        prev_trade_count = len(closed_trades)
        open_positions, capital, new_cooldowns = _close_due_positions(
            conn, open_positions, day, capital, closed_trades, cooldown_days, benchmark_ticker
        )
        for trade in closed_trades[prev_trade_count:]:
            if trade.net_return_pct < 0:
                consecutive_losses += 1
                if consecutive_losses >= 5:
                    next_day = _get_next_trading_day(conn, day)
                    if next_day:
                        consecutive_loss_cooldown_until = next_day
            else:
                consecutive_losses = 0
                consecutive_loss_cooldown_until = None

        total_equity = _mark_to_market_equity(conn, open_positions, capital, day)
        if total_equity > peak_equity:
            peak_equity = total_equity

        to_remove: list[tuple[str, str]] = []
        for k in exclude_tickers:
            exclude_tickers[k] -= 1
            if exclude_tickers[k] <= 0:
                to_remove.append(k)
        for k in to_remove:
            del exclude_tickers[k]

        exclude_tickers.update(new_cooldowns)

        todays_orders = pending_orders.pop(day, [])
        todays_orders.sort(key=lambda order: order.candidate_score * order.strategy_weight, reverse=True)
        for order in todays_orders:
            effective_max = max_positions
            if consecutive_loss_cooldown_until and day <= consecutive_loss_cooldown_until:
                effective_max = 0
            current_equity = _mark_to_market_equity(conn, open_positions, capital, day)
            current_drawdown = (peak_equity - current_equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
            if current_drawdown >= drawdown_halt_threshold:
                effective_max = 0
            elif current_drawdown >= drawdown_reduce_threshold:
                effective_max = max(1, max_positions - 1)
            if len(open_positions) >= effective_max:
                break
            key = (order.market, order.ticker)
            if key in exclude_tickers or any((pos.market, pos.ticker) == key for pos in open_positions):
                continue
            sector_key = order.ticker[:3] if order.market == "CN_A" and len(order.ticker) >= 3 else order.ticker
            sector_count = sum(1 for p in open_positions if (p.ticker[:3] if p.market == "CN_A" and len(p.ticker) >= 3 else p.ticker) == sector_key)
            if sector_count >= max_per_sector:
                skipped_orders += 1
                continue
            bar = _get_price_bar(conn, order.market, order.ticker, day)
            if not bar:
                skipped_orders += 1
                continue
            if _is_suspended_or_illiquid(bar):
                skipped_orders += 1
                continue
            if _is_one_price_limit_up(conn, order.market, order.ticker, bar):
                skipped_orders += 1
                continue
            daily_open = float(bar["open"])
            if execution_mode == "intraday":
                entry = open_5min_vwap_entry(conn, order.market, order.ticker, day, daily_open)
                entry_price = entry.price
                if require_intraday and entry.execution_type == "NEXT_OPEN_DAILY":
                    skipped_orders += 1
                    continue
            else:
                entry_price = daily_open
            if entry_price <= 0:
                continue
            entry_return_price = adjusted_price_from_raw(bar, entry_price)
            remaining_slots = max_positions - len(open_positions)
            if sizing_mode == "risk_parity" and remaining_slots > 0:
                position_size = _risk_parity_position_size(
                    capital=capital,
                    entry_price=entry_price,
                    stop_loss=order.stop_loss,
                    remaining_slots=remaining_slots,
                    initial_capital=initial_capital,
                    risk_budget_pct=risk_budget_pct,
                    max_position_pct=max_position_pct,
                )
            else:
                equal_size = capital / remaining_slots if remaining_slots > 0 else 0.0
                position_size = min(equal_size, initial_capital * max_position_pct / 100.0)
            if position_size <= 0:
                break
            shares = position_size / entry_price
            exit_result = _compute_exit(
                conn,
                order.market,
                order.ticker,
                day,
                entry_price,
                order.stop_loss,
                order.target_1,
                order.target_2,
                order.target_horizon_days,
                through_date,
                execution_mode,
                trailing_stop_pct=order.trailing_stop_pct,
                trailing_activation_pct=order.trailing_activation_pct,
            )
            if exit_result is None:
                skipped_orders += 1
                continue
            exit_date, exit_type, exit_price = exit_result
            gross_return_pct = adjusted_trade_return_pct(
                conn,
                order.market,
                order.ticker,
                day,
                entry_price,
                exit_date,
                exit_price,
            )
            trade_benchmark_return = benchmark_return_pct(
                conn,
                order.market,
                day,
                exit_date,
                benchmark_ticker=benchmark_for_asset(order.market, order.ticker, benchmark_ticker),
            )
            cost_pct = (cost_bps / 100.0) if cost_bps is not None else trade_cost_pct(order.market)
            slip_rate = slippage_pct(order.market)
            cost_rate = cost_pct / 100.0
            round_trip_cost = position_size * cost_rate
            slip_cost = position_size * slip_rate
            net_return_pct = gross_return_pct - cost_pct - slip_rate * 100.0
            excess_return_pct = (
                net_return_pct - trade_benchmark_return
                if trade_benchmark_return is not None
                else None
            )
            capital -= position_size
            open_positions.append(
                _OpenPosition(
                    ticker=order.ticker,
                    name=order.name,
                    market=order.market,
                    strategy_id=order.strategy_id,
                    entry_date=day,
                    entry_price=entry_price,
                    entry_return_price=entry_return_price,
                    stop_loss=order.stop_loss,
                    target_1=order.target_1,
                    target_2=order.target_2,
                    horizon_days=order.target_horizon_days,
                    position_size=position_size,
                    shares=shares,
                    exit_date=exit_date,
                    exit_price=exit_price,
                    exit_type=exit_type,
                    return_pct=gross_return_pct,
                    benchmark_return_pct=trade_benchmark_return,
                    excess_return_pct=excess_return_pct,
                    cost=round_trip_cost,
                    slippage_cost=slip_cost,
                )
            )

        open_positions, capital, same_day_cooldowns = _close_due_positions(
            conn, open_positions, day, capital, closed_trades, cooldown_days, benchmark_ticker
        )
        exclude_tickers.update(same_day_cooldowns)

        available_slots = max_positions - len(open_positions)
        if available_slots > 0:
            pending_keys = {
                (order.market, order.ticker)
                for orders in pending_orders.values()
                for order in orders
            }
            excluded = set(exclude_tickers.keys()) | pending_keys | {
                (pos.market, pos.ticker) for pos in open_positions
            }
            candidates = _select_actionable_candidates(
                conn, day, end_date, excluded, available_slots, markets=markets,
            )
            for cand in candidates:
                next_day = _get_next_trading_day(conn, day, str(cand["market"]), str(cand["ticker"]))
                if not next_day or next_day > through_date:
                    continue
                order = _pending_order_from_candidate(cand)
                pending_orders.setdefault(next_day, []).append(order)
                pending_keys.add((order.market, order.ticker))

        max_concurrent = max(max_concurrent, len(open_positions))

        bars_today = conn.execute(
            "SELECT * FROM price_bars WHERE date = ?",
            (day,),
        ).fetchall()
        close_map: dict[tuple[str, str], sqlite3.Row] = {
            (b["market"], b["ticker"]): b for b in bars_today
        }
        equity = capital
        for pos in open_positions:
            bar = close_map.get((pos.market, pos.ticker))
            if bar is None or pos.entry_return_price <= 0:
                equity += pos.position_size
                continue
            close = adjusted_bar_price(bar, "adj_close", "close")
            equity += pos.position_size * (close / pos.entry_return_price)
        daily_equity.append((day, equity))

    for pos in open_positions:
        gross_value = pos.position_size * (1.0 + pos.return_pct / 100.0)
        capital += gross_value - pos.cost - pos.slippage_cost
        pnl = gross_value - pos.cost - pos.slippage_cost - pos.position_size
        net_return_pct = pnl / pos.position_size * 100.0 if pos.position_size else 0.0
        cost_pct = pos.cost / pos.position_size * 100.0 if pos.position_size else 0.0
        slip_pct = pos.slippage_cost / pos.position_size * 100.0 if pos.position_size else 0.0
        closed_trades.append(TradeRecord(
            ticker=pos.ticker,
            name=pos.name,
            market=pos.market,
            strategy_id=pos.strategy_id,
            entry_date=pos.entry_date,
            entry_price=pos.entry_price,
            exit_date=pos.exit_date,
            exit_price=pos.exit_price,
            exit_type=pos.exit_type,
            return_pct=pos.return_pct,
            net_return_pct=net_return_pct,
            benchmark_return_pct=pos.benchmark_return_pct,
            excess_return_pct=pos.excess_return_pct,
            cost_pct=cost_pct,
            slippage_pct=slip_pct,
            pnl=pnl,
            position_size=pos.position_size,
        ))

    final_capital = capital
    total_return_pct = (final_capital / initial_capital - 1.0) * 100.0 if initial_capital else 0.0

    peak = initial_capital
    max_dd = 0.0
    for _, eq in daily_equity:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    trade_count = len(closed_trades)
    wins = sum(1 for t in closed_trades if t.pnl > 0)
    win_rate = wins / trade_count * 100.0 if trade_count > 0 else 0.0
    avg_pnl = sum(t.pnl for t in closed_trades) / trade_count if trade_count > 0 else 0.0
    avg_net_return_pct = sum(t.net_return_pct for t in closed_trades) / trade_count if trade_count > 0 else 0.0
    winning_returns = [t.net_return_pct for t in closed_trades if t.net_return_pct > 0]
    losing_returns = [t.net_return_pct for t in closed_trades if t.net_return_pct <= 0]
    avg_win_pct = sum(winning_returns) / len(winning_returns) if winning_returns else 0.0
    avg_loss_pct = sum(losing_returns) / len(losing_returns) if losing_returns else 0.0
    gross_profit = sum(t.pnl for t in closed_trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in closed_trades if t.pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
    turnover_pct = sum(t.position_size for t in closed_trades) / initial_capital * 100.0 if initial_capital else 0.0
    portfolio_benchmark_return = None
    portfolio_benchmark_dd = None
    active_return = None
    if "CN_A" in markets:
        portfolio_benchmark_return = benchmark_return_pct(
            conn,
            "CN_A",
            start_date,
            through_date,
            benchmark_ticker=benchmark_ticker,
        )
        portfolio_benchmark_dd = benchmark_max_drawdown_pct(
            conn,
            "CN_A",
            start_date,
            through_date,
            benchmark_ticker=benchmark_ticker,
        )
        if portfolio_benchmark_return is not None:
            active_return = total_return_pct - portfolio_benchmark_return
    comparable_trades = [t for t in closed_trades if t.benchmark_return_pct is not None]
    win_vs_benchmark_rate = (
        sum(1 for t in comparable_trades if t.net_return_pct > float(t.benchmark_return_pct)) / len(comparable_trades) * 100.0
        if comparable_trades
        else None
    )

    daily_returns = []
    for i in range(1, len(daily_equity)):
        prev_eq = daily_equity[i - 1][1]
        curr_eq = daily_equity[i][1]
        if prev_eq > 0:
            daily_returns.append((curr_eq - prev_eq) / prev_eq)
    sharpe = compute_sharpe_ratio(daily_returns) if len(daily_returns) >= 2 else 0.0
    sortino = compute_sortino_ratio(daily_returns) if len(daily_returns) >= 2 else 0.0

    return PortfolioResult(
        start_date=start_date,
        end_date=end_date,
        through_date=through_date,
        initial_capital=initial_capital,
        final_capital=final_capital,
        total_return_pct=total_return_pct,
        max_drawdown_pct=max_dd,
        win_rate=win_rate,
        avg_pnl_per_trade=avg_pnl,
        trade_count=trade_count,
        max_concurrent_positions=max_concurrent,
        avg_net_return_pct=avg_net_return_pct,
        avg_win_pct=avg_win_pct,
        avg_loss_pct=avg_loss_pct,
        profit_factor=profit_factor,
        turnover_pct=turnover_pct,
        cost_model=cost_model,
        skipped_order_count=skipped_orders,
        benchmark_ticker=benchmark_ticker if "CN_A" in markets else None,
        benchmark_return_pct=portfolio_benchmark_return,
        active_return_pct=active_return,
        benchmark_max_drawdown_pct=portfolio_benchmark_dd,
        win_vs_benchmark_rate=win_vs_benchmark_rate,
        require_intraday=require_intraday,
        market_scope=markets,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        daily_equity=daily_equity,
        trades=closed_trades,
    )


def render_portfolio_report(result: PortfolioResult) -> str:
    lines: list[str] = []
    lines.append("# Portfolio Backtest Report")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Period: {result.start_date} to {result.through_date}")
    lines.append(f"- Initial Capital: {result.initial_capital:,.2f}")
    lines.append(f"- Final Capital: {result.final_capital:,.2f}")
    lines.append(f"- Total Return: {result.total_return_pct:.2f}%")
    lines.append(f"- Sharpe Ratio: {result.sharpe_ratio:.2f}")
    lines.append(f"- Sortino Ratio: {result.sortino_ratio:.2f}")
    if result.benchmark_ticker and result.benchmark_return_pct is not None:
        lines.append(f"- Benchmark ({result.benchmark_ticker}) Return: {result.benchmark_return_pct:.2f}%")
        lines.append(f"- Active Return: {result.active_return_pct:.2f}%" if result.active_return_pct is not None else "- Active Return: -")
        lines.append(f"- Benchmark Max Drawdown: {result.benchmark_max_drawdown_pct:.2f}%" if result.benchmark_max_drawdown_pct is not None else "- Benchmark Max Drawdown: -")
        lines.append(f"- Win Rate vs Benchmark: {result.win_vs_benchmark_rate:.1f}%" if result.win_vs_benchmark_rate is not None else "- Win Rate vs Benchmark: -")
    elif result.benchmark_ticker:
        lines.append(f"- Benchmark ({result.benchmark_ticker}) Return: missing; cannot judge alpha.")
    lines.append(f"- Max Drawdown: {result.max_drawdown_pct:.2f}%")
    lines.append(f"- Win Rate: {result.win_rate:.1f}%")
    lines.append(f"- Avg Net Return per Trade: {result.avg_net_return_pct:.2f}%")
    lines.append(f"- Avg Win / Avg Loss: {result.avg_win_pct:.2f}% / {result.avg_loss_pct:.2f}%")
    lines.append(f"- Profit Factor: {result.profit_factor:.2f}")
    lines.append(f"- Turnover: {result.turnover_pct:.2f}%")
    lines.append(f"- Avg PnL per Trade: {result.avg_pnl_per_trade:,.2f}")
    lines.append(f"- Trade Count: {result.trade_count}")
    lines.append(f"- Max Concurrent Positions: {result.max_concurrent_positions}")
    lines.append(f"- Market Scope: {', '.join(result.market_scope) if result.market_scope else 'ALL'}")
    lines.append(f"- Cost Model: {result.cost_model}")
    lines.append(f"- Require Intraday: {'yes' if result.require_intraday else 'no'}")
    lines.append(f"- Skipped Orders: {result.skipped_order_count}")
    lines.append("- A-share execution guards: T+1 exit, suspended/zero-volume skip, one-price limit-up buy skip, intraday VWAP when available.")
    lines.append("")

    lines.append("## Daily Equity Curve")
    lines.append("")
    lines.append("| Date | Equity |")
    lines.append("|------|--------|")
    for date, equity in result.daily_equity:
        lines.append(f"| {date} | {equity:,.2f} |")
    lines.append("")

    lines.append("## Trade Details")
    lines.append("")
    if result.trades:
        lines.append(
            "| Ticker | Name | Market | Strategy | Entry Date | Entry Price "
            "| Exit Date | Exit Price | Exit Type | Return % | Net Return % "
            "| Benchmark % | Excess % | Cost % | PnL | Position Size |"
        )
        lines.append(
            "|--------|------|--------|----------|------------|-------------"
            "|-----------|------------|-----------|----------|--------------"
            "|-------------|----------|--------|-----|---------------|"
        )
        for t in result.trades:
            benchmark = "-" if t.benchmark_return_pct is None else f"{t.benchmark_return_pct:.2f}"
            excess = "-" if t.excess_return_pct is None else f"{t.excess_return_pct:.2f}"
            lines.append(
                f"| {t.ticker} | {t.name} | {t.market} | {t.strategy_id} "
                f"| {t.entry_date} | {t.entry_price:.2f} "
                f"| {t.exit_date} | {t.exit_price:.2f} | {t.exit_type} "
                f"| {t.return_pct:.2f} | {t.net_return_pct:.2f} | {benchmark} | {excess} | {t.cost_pct:.2f} "
                f"| {t.pnl:,.2f} | {t.position_size:,.2f} |"
            )
    else:
        lines.append("No trades executed.")
    lines.append("")
    return "\n".join(lines)


def write_portfolio_report(result: PortfolioResult, out_path: Path | None = None) -> Path:
    path = out_path or Path("reports") / f"portfolio_backtest_{result.start_date}_{result.end_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_portfolio_report(result), encoding="utf-8")
    return path
