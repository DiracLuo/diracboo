from __future__ import annotations

import math
import sqlite3

from .benchmarks import benchmark_for_asset
from .trading_rules import is_one_price_limit_down_from_values

from .execution import intraday_exit_path, open_5min_vwap_entry
from .ledger import now_utc


HORIZONS = (5, 10, 20, 60)
CANDIDATE_HORIZONS = HORIZONS
MIN_WEIGHT_SUGGESTION_SAMPLES = 5
FORMAL_MARKETS = ("CN_A",)
MARKET_TRADE_COST_PCT = {
    "CN_A": 0.18,
    "US": 0.10,
    "HK": 0.15,
}
DEFAULT_TRADE_COST_PCT = 0.18
MARKET_SLIPPAGE_BPS = {
    "CN_A": 5,
    "US": 3,
    "HK": 5,
}
DEFAULT_SLIPPAGE_BPS = 5
DEFAULT_BENCHMARKS = {
    "CN_A": "000300.SS",
}


def trade_cost_pct(market: str) -> float:
    return MARKET_TRADE_COST_PCT.get(market, DEFAULT_TRADE_COST_PCT)


def slippage_pct(market: str) -> float:
    return MARKET_SLIPPAGE_BPS.get(market, DEFAULT_SLIPPAGE_BPS) / 100.0


def compute_sharpe_ratio(returns: list[float], risk_free_rate: float = 0.0, annualize_factor: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    return (mean - risk_free_rate) / std * math.sqrt(annualize_factor)


def compute_sortino_ratio(returns: list[float], risk_free_rate: float = 0.0, annualize_factor: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    downside_returns = [r for r in returns if r < risk_free_rate]
    if len(downside_returns) < 2:
        return 0.0
    downside_variance = sum((r - risk_free_rate) ** 2 for r in downside_returns) / (len(downside_returns) - 1)
    downside_std = math.sqrt(downside_variance) if downside_variance > 0 else 0.0
    if downside_std == 0:
        return 0.0
    return (mean - risk_free_rate) / downside_std * math.sqrt(annualize_factor)


def benchmark_ticker_for_market(market: str, benchmark_ticker: str | None = None) -> str | None:
    if benchmark_ticker == "auto":
        return DEFAULT_BENCHMARKS.get(market)
    return benchmark_ticker or DEFAULT_BENCHMARKS.get(market)


def benchmark_ticker_for_asset(market: str, ticker: str, benchmark_ticker: str | None = None) -> str | None:
    return benchmark_for_asset(market, ticker, benchmark_ticker) or benchmark_ticker_for_market(market, benchmark_ticker)


def pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _row_has(row: sqlite3.Row, key: str) -> bool:
    return key in row.keys()


def _row_float(row: sqlite3.Row, key: str, fallback_key: str | None = None) -> float | None:
    if _row_has(row, key) and row[key] is not None:
        return float(row[key])
    if fallback_key and _row_has(row, fallback_key) and row[fallback_key] is not None:
        return float(row[fallback_key])
    return None


def adjusted_price_from_raw(bar: sqlite3.Row, raw_price: float) -> float:
    close = _row_float(bar, "close")
    adj_close = _row_float(bar, "adj_close", "close")
    if close is None or close <= 0 or adj_close is None:
        return raw_price
    return raw_price * (adj_close / close)


def adjusted_bar_price(bar: sqlite3.Row, adj_key: str, raw_key: str) -> float:
    value = _row_float(bar, adj_key, raw_key)
    if value is not None:
        return value
    return float(bar[raw_key])


def adjustment_status(bar: sqlite3.Row) -> str:
    if _row_has(bar, "adjustment_status") and bar["adjustment_status"]:
        return str(bar["adjustment_status"])
    return "UNKNOWN"


def benchmark_return_pct(
    conn: sqlite3.Connection,
    market: str,
    start_date: str,
    end_date: str,
    benchmark_ticker: str | None = None,
) -> float | None:
    ticker = benchmark_ticker_for_market(market, benchmark_ticker)
    if ticker is None:
        return None
    start_bar = conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date >= ? AND date <= ?
        ORDER BY date
        LIMIT 1
        """,
        (market, ticker, start_date, end_date),
    ).fetchone()
    end_bar = conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date >= ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (market, ticker, start_date, end_date),
    ).fetchone()
    if start_bar is None or end_bar is None:
        return None
    start_price = adjusted_bar_price(start_bar, "adj_close", "close")
    end_price = adjusted_bar_price(end_bar, "adj_close", "close")
    return pct_change(start_price, end_price)


def benchmark_max_drawdown_pct(
    conn: sqlite3.Connection,
    market: str,
    start_date: str,
    end_date: str,
    benchmark_ticker: str | None = None,
) -> float | None:
    ticker = benchmark_ticker_for_market(market, benchmark_ticker)
    if ticker is None:
        return None
    rows = conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (market, ticker, start_date, end_date),
    ).fetchall()
    if not rows:
        return None
    peak = adjusted_bar_price(rows[0], "adj_close", "close")
    max_dd = 0.0
    for row in rows:
        close = adjusted_bar_price(row, "adj_close", "close")
        peak = max(peak, close)
        if peak > 0:
            max_dd = max(max_dd, (peak - close) / peak * 100.0)
    return max_dd


def adjusted_trade_return_pct(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    entry_date: str,
    entry_price: float,
    exit_date: str,
    exit_price: float,
) -> float:
    entry_bar = conn.execute(
        "SELECT * FROM price_bars WHERE market = ? AND ticker = ? AND date = ?",
        (market, ticker, entry_date),
    ).fetchone()
    exit_bar = conn.execute(
        "SELECT * FROM price_bars WHERE market = ? AND ticker = ? AND date = ?",
        (market, ticker, exit_date),
    ).fetchone()
    if entry_bar is None or exit_bar is None:
        return pct_change(entry_price, exit_price)
    entry_return_price = adjusted_price_from_raw(entry_bar, entry_price)
    exit_return_price = adjusted_price_from_raw(exit_bar, exit_price)
    return pct_change(entry_return_price, exit_return_price)


def _exit_price_for_stop(open_price: float, stop_loss: float) -> float:
    return open_price if open_price <= stop_loss else stop_loss


def _exit_price_for_target(open_price: float, target: float) -> float:
    return open_price if open_price >= target else target


def _long_trade_path(
    bars: list[sqlite3.Row],
    entry_price: float,
    stop_loss: float | None,
    target_1: float | None,
    target_2: float | None,
    market: str = "",
    entry_return_price: float | None = None,
    ticker: str = "",
    name: str = "",
    trailing_stop_pct: float | None = None,
    trailing_activation_pct: float | None = None,
) -> dict[str, object]:
    stop = stop_loss
    first_target = target_1
    second_target = target_2
    observed_bars: list[sqlite3.Row] = []
    exit_type = "HOLD"
    exit_date = None
    exit_price = float(bars[-1]["close"])
    exit_bar = bars[-1]
    exit_note = "未触发止损/目标，按观察窗口最后收盘价计。"
    trailing_active = False
    trailing_stop_price = 0.0
    highest_since_entry = entry_price

    for bar in bars:
        observed_bars.append(bar)
        open_price = float(bar["open"])
        low = float(bar["low"])
        high = float(bar["high"])

        if market == "CN_A" and len(observed_bars) >= 2:
            prev_close = float(observed_bars[-2]["close"])
            if is_one_price_limit_down_from_values(prev_close, open_price, high, low, ticker, name):
                continue

        if stop is not None and open_price <= stop:
            exit_type = "STOP_LOSS"
            exit_date = bar["date"]
            exit_price = open_price
            exit_bar = bar
            exit_note = "开盘低于或等于止损价，按开盘价止损。"
            break
        if first_target is not None and open_price >= first_target:
            exit_type = "TARGET_1"
            exit_date = bar["date"]
            exit_price = open_price
            exit_bar = bar
            exit_note = "开盘高于或等于目标1，按开盘价止盈。"
            break
        if second_target is not None and open_price >= second_target:
            exit_type = "TARGET_2"
            exit_date = bar["date"]
            exit_price = open_price
            exit_bar = bar
            exit_note = "开盘高于或等于目标2，按开盘价止盈。"
            break

        stop_hit = stop is not None and low <= stop
        target_2_hit = second_target is not None and high >= second_target
        target_1_hit = first_target is not None and high >= first_target
        if stop_hit and (target_1_hit or target_2_hit):
            exit_type = "STOP_LOSS"
            exit_date = bar["date"]
            exit_price = _exit_price_for_stop(open_price, float(stop))
            exit_bar = bar
            exit_note = "日K同时触及止损和目标，因无日内顺序数据，采用保守假设：止损先发生。"
            break
        if stop_hit:
            exit_type = "STOP_LOSS"
            exit_date = bar["date"]
            exit_price = _exit_price_for_stop(open_price, float(stop))
            exit_bar = bar
            exit_note = "盘中触及止损价。"
            break
        if target_1_hit:
            exit_type = "TARGET_1"
            exit_date = bar["date"]
            exit_price = _exit_price_for_target(open_price, float(first_target))
            exit_bar = bar
            exit_note = "盘中触及目标1。"
            break
        if target_2_hit:
            exit_type = "TARGET_2"
            exit_date = bar["date"]
            exit_price = _exit_price_for_target(open_price, float(second_target))
            exit_bar = bar
            exit_note = "盘中触及目标2。"
            break

        if trailing_stop_pct is not None and trailing_activation_pct is not None:
            if high > highest_since_entry:
                highest_since_entry = high
            activation_price = entry_price * (1.0 + trailing_activation_pct / 100.0)
            if not trailing_active and highest_since_entry >= activation_price:
                trailing_active = True
                trailing_stop_price = highest_since_entry * (1.0 - trailing_stop_pct / 100.0)
            if trailing_active:
                new_trailing = highest_since_entry * (1.0 - trailing_stop_pct / 100.0)
                if new_trailing > trailing_stop_price:
                    trailing_stop_price = new_trailing
                if open_price <= trailing_stop_price:
                    exit_type = "TRAILING_STOP"
                    exit_date = bar["date"]
                    exit_price = open_price
                    exit_bar = bar
                    exit_note = f"追踪止损触发：最高价 {highest_since_entry:.2f}，止损线 {trailing_stop_price:.2f}，开盘跌破。"
                    break
                if low <= trailing_stop_price:
                    exit_type = "TRAILING_STOP"
                    exit_date = bar["date"]
                    exit_price = trailing_stop_price
                    exit_bar = bar
                    exit_note = f"追踪止损触发：最高价 {highest_since_entry:.2f}，止损线 {trailing_stop_price:.2f}，盘中触及。"
                    break

    entry_return = entry_return_price if entry_return_price is not None else entry_price
    exit_return = adjusted_price_from_raw(exit_bar, exit_price)
    lows = [adjusted_bar_price(row, "adj_low", "low") for row in observed_bars]
    highs = [adjusted_bar_price(row, "adj_high", "high") for row in observed_bars]
    end_bar = observed_bars[-1]
    if exit_date is None:
        exit_date = end_bar["date"]
    hit_stop = int(exit_type == "STOP_LOSS")
    hit_target_1 = int(exit_type in {"TARGET_1", "TARGET_2"})
    hit_target_2 = int(exit_type == "TARGET_2")
    cost = trade_cost_pct(market)
    gross = pct_change(entry_return, exit_return)
    net = gross - cost
    return {
        "end_date": exit_date,
        "end_close": exit_price,
        "return_pct": gross,
        "gross_return_pct": gross,
        "cost_pct": cost,
        "net_return_pct": net,
        "net_win": int(net > 0),
        "max_gain_pct": pct_change(entry_return, max(highs)),
        "max_drawdown_pct": pct_change(entry_return, min(lows)),
        "hit_stop": hit_stop,
        "hit_target_1": hit_target_1,
        "hit_target_2": hit_target_2,
        "exit_type": exit_type,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_note": exit_note,
    }


def _path_from_intraday_exit(
    market: str,
    entry_price: float,
    exit_date: str,
    exit_price: float,
    exit_type: str,
    exit_note: str,
    observed_bars: list[sqlite3.Row],
    entry_return_price: float | None = None,
) -> dict[str, object]:
    hit_stop = int(exit_type == "STOP_LOSS")
    hit_target_1 = int(exit_type in {"TARGET_1", "TARGET_2"})
    hit_target_2 = int(exit_type == "TARGET_2")
    cost = trade_cost_pct(market)
    entry_return = entry_return_price if entry_return_price is not None else entry_price
    exit_bar = next((row for row in observed_bars if str(row["date"]) == str(exit_date)), observed_bars[-1])
    exit_return = adjusted_price_from_raw(exit_bar, exit_price)
    gross = pct_change(entry_return, exit_return)
    lows = [adjusted_bar_price(row, "adj_low", "low") for row in observed_bars]
    highs = [adjusted_bar_price(row, "adj_high", "high") for row in observed_bars]
    return {
        "end_date": exit_date,
        "end_close": exit_price,
        "return_pct": gross,
        "gross_return_pct": gross,
        "cost_pct": cost,
        "net_return_pct": gross - cost,
        "net_win": int(gross - cost > 0),
        "max_gain_pct": pct_change(entry_return, max(highs)),
        "max_drawdown_pct": pct_change(entry_return, min(lows)),
        "hit_stop": hit_stop,
        "hit_target_1": hit_target_1,
        "hit_target_2": hit_target_2,
        "exit_type": exit_type,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_note": exit_note,
    }


def _override_risk_with_observed_bars(
    path: dict[str, object],
    entry_price: float,
    observed_bars: list[sqlite3.Row],
    entry_return_price: float | None = None,
) -> dict[str, object]:
    exit_date = str(path["exit_date"] or path["end_date"])
    risk_bars = [row for row in observed_bars if str(row["date"]) <= exit_date] or observed_bars
    entry_return = entry_return_price if entry_return_price is not None else entry_price
    highs = [adjusted_bar_price(row, "adj_high", "high") for row in risk_bars]
    lows = [adjusted_bar_price(row, "adj_low", "low") for row in risk_bars]
    path["max_gain_pct"] = pct_change(entry_return, max(highs))
    path["max_drawdown_pct"] = pct_change(entry_return, min(lows))
    return path


def _candidate_future_bars(
    conn: sqlite3.Connection,
    candidate: sqlite3.Row,
    through_date: str,
) -> list[sqlite3.Row]:
    start_date = candidate["confirmation_date"] or candidate["as_of_date"]
    return conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date > ? AND date <= ?
        ORDER BY date
        """,
        (candidate["market"], candidate["ticker"], start_date, through_date),
    ).fetchall()


def _evaluate_candidate_window(
    conn: sqlite3.Connection,
    candidate: sqlite3.Row,
    through_date: str,
    window: list[sqlite3.Row],
    *,
    require_adjusted: bool = False,
    benchmark_ticker: str | None = None,
) -> dict[str, object] | None:
    if not window:
        return None

    market = str(candidate["market"])
    ticker = str(candidate["ticker"])
    if require_adjusted and market == "CN_A":
        if any(adjustment_status(row) != "ADJUSTED" for row in window):
            return None

    execution_bar = window[0]
    execution_date = str(execution_bar["date"])
    fallback_open = float(execution_bar["open"])
    entry = open_5min_vwap_entry(conn, market, ticker, execution_date, fallback_open)
    execution_price = float(entry.price)
    if execution_price <= 0:
        return None
    entry_return_price = adjusted_price_from_raw(execution_bar, execution_price)

    exit_window = window[1:] if market == "CN_A" else window
    if not exit_window:
        return None

    stop_loss = float(candidate["stop_loss"]) if candidate["stop_loss"] is not None else None
    target_1 = float(candidate["target_1"]) if candidate["target_1"] is not None else None
    target_2 = float(candidate["target_2"]) if candidate["target_2"] is not None else None
    exit_through = str(exit_window[-1]["date"])
    intraday_exit = intraday_exit_path(
        conn,
        market,
        ticker,
        execution_date,
        execution_price,
        stop_loss,
        target_1,
        target_2,
        exit_through,
    )
    if intraday_exit is not None:
        exit_type = "HOLD" if intraday_exit.exit_type == "HOLD" else intraday_exit.exit_type
        path = _path_from_intraday_exit(
            market,
            execution_price,
            intraday_exit.date,
            intraday_exit.price,
            exit_type,
            intraday_exit.note,
            window,
            entry_return_price=entry_return_price,
        )
    else:
        path = _long_trade_path(
            exit_window,
            execution_price,
            stop_loss,
            target_1,
            target_2,
            market,
            entry_return_price=entry_return_price,
            ticker=ticker,
            name=str(candidate["name"]),
        )
        path = _override_risk_with_observed_bars(path, execution_price, window, entry_return_price)

    benchmark_ticker_value = benchmark_ticker_for_market(market, benchmark_ticker)
    if benchmark_ticker in (None, "auto"):
        benchmark_ticker_value = benchmark_ticker_for_asset(market, ticker, benchmark_ticker)
    benchmark = benchmark_return_pct(
        conn,
        market,
        execution_date,
        str(path["exit_date"] or path["end_date"]),
        benchmark_ticker=benchmark_ticker_value,
    )
    excess = None
    if benchmark is not None:
        excess = float(path["net_return_pct"]) - benchmark

    t1_note = "A股卖出遵守T+1，买入日不触发退出。" if market == "CN_A" else "非A股允许买入日触发退出。"
    return {
        "candidate_id": int(candidate["id"]),
        "through_date": through_date,
        "observed_days": len(window),
        "reference_date": execution_date,
        "reference_close": execution_price,
        "execution_date": execution_date,
        "execution_price": execution_price,
        "execution_type": entry.execution_type,
        "execution_note": f"{entry.note} {t1_note}",
        **path,
        "benchmark_ticker": benchmark_ticker_value,
        "benchmark_return_pct": benchmark,
        "excess_return_pct": excess,
        "created_at": now_utc(),
    }


def evaluate_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    through_date: str,
    *,
    require_adjusted: bool = False,
    benchmark_ticker: str | None = None,
) -> dict[str, object] | None:
    candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} does not exist")

    future_bars = _candidate_future_bars(conn, candidate, through_date)
    if not future_bars:
        return None

    return _evaluate_candidate_window(
        conn,
        candidate,
        through_date,
        future_bars,
        require_adjusted=require_adjusted,
        benchmark_ticker=benchmark_ticker,
    )


def evaluate_candidate_horizons(
    conn: sqlite3.Connection,
    candidate_id: int,
    through_date: str,
    horizons: tuple[int, ...] = CANDIDATE_HORIZONS,
    *,
    require_adjusted: bool = False,
    benchmark_ticker: str | None = None,
) -> list[dict[str, object]]:
    candidate = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} does not exist")

    future_bars = _candidate_future_bars(conn, candidate, through_date)
    if not future_bars:
        return []

    rows: list[dict[str, object]] = []
    for horizon in horizons:
        observed = min(horizon, len(future_bars))
        if observed <= 0:
            continue
        window = future_bars[:observed]
        row = _evaluate_candidate_window(
            conn,
            candidate,
            through_date,
            window,
            require_adjusted=require_adjusted,
            benchmark_ticker=benchmark_ticker,
        )
        if row is None:
            continue
        row["horizon_days"] = horizon
        rows.append(row)
    return rows


def evaluate_candidates(
    conn: sqlite3.Connection,
    candidate_date: str,
    through_date: str,
    *,
    require_adjusted: bool = False,
    benchmark_ticker: str | None = None,
) -> int:
    candidates = conn.execute(
        "SELECT id FROM candidates WHERE as_of_date = ? ORDER BY id", (candidate_date,)
    ).fetchall()
    rows = []
    for candidate in candidates:
        row = evaluate_candidate(
            conn,
            int(candidate["id"]),
            through_date,
            require_adjusted=require_adjusted,
            benchmark_ticker=benchmark_ticker,
        )
        if row is not None:
            rows.append(row)

    for row in rows:
        conn.execute(
            """
            INSERT INTO candidate_evaluations (
                candidate_id, through_date, observed_days, reference_date,
                reference_close, execution_date, execution_price, execution_type,
                execution_note, end_date, end_close, return_pct, gross_return_pct,
                cost_pct, net_return_pct, net_win, benchmark_ticker,
                benchmark_return_pct, excess_return_pct, max_gain_pct,
                max_drawdown_pct, hit_stop, hit_target_1, hit_target_2,
                exit_type, exit_date, exit_price, exit_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, through_date) DO UPDATE SET
                observed_days=excluded.observed_days,
                reference_date=excluded.reference_date,
                reference_close=excluded.reference_close,
                execution_date=excluded.execution_date,
                execution_price=excluded.execution_price,
                execution_type=excluded.execution_type,
                execution_note=excluded.execution_note,
                end_date=excluded.end_date,
                end_close=excluded.end_close,
                return_pct=excluded.return_pct,
                gross_return_pct=excluded.gross_return_pct,
                cost_pct=excluded.cost_pct,
                net_return_pct=excluded.net_return_pct,
                net_win=excluded.net_win,
                benchmark_ticker=excluded.benchmark_ticker,
                benchmark_return_pct=excluded.benchmark_return_pct,
                excess_return_pct=excluded.excess_return_pct,
                max_gain_pct=excluded.max_gain_pct,
                max_drawdown_pct=excluded.max_drawdown_pct,
                hit_stop=excluded.hit_stop,
                hit_target_1=excluded.hit_target_1,
                hit_target_2=excluded.hit_target_2,
                exit_type=excluded.exit_type,
                exit_date=excluded.exit_date,
                exit_price=excluded.exit_price,
                exit_note=excluded.exit_note,
                created_at=excluded.created_at
            """,
            (
                row["candidate_id"],
                row["through_date"],
                row["observed_days"],
                row["reference_date"],
                row["reference_close"],
                row["execution_date"],
                row["execution_price"],
                row["execution_type"],
                row["execution_note"],
                row["end_date"],
                row["end_close"],
                row["return_pct"],
                row["gross_return_pct"],
                row["cost_pct"],
                row["net_return_pct"],
                row["net_win"],
                row["benchmark_ticker"],
                row["benchmark_return_pct"],
                row["excess_return_pct"],
                row["max_gain_pct"],
                row["max_drawdown_pct"],
                row["hit_stop"],
                row["hit_target_1"],
                row["hit_target_2"],
                row["exit_type"],
                row["exit_date"],
                row["exit_price"],
                row["exit_note"],
                row["created_at"],
            ),
        )
    conn.commit()
    return len(rows)


def evaluate_candidate_horizons_for_date(
    conn: sqlite3.Connection,
    candidate_date: str,
    through_date: str,
    *,
    require_adjusted: bool = False,
    benchmark_ticker: str | None = None,
) -> int:
    candidates = conn.execute(
        "SELECT id FROM candidates WHERE as_of_date = ? ORDER BY id", (candidate_date,)
    ).fetchall()
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        rows.extend(
            evaluate_candidate_horizons(
                conn,
                int(candidate["id"]),
                through_date,
                require_adjusted=require_adjusted,
                benchmark_ticker=benchmark_ticker,
            )
        )

    for row in rows:
        conn.execute(
            """
            INSERT INTO candidate_horizon_evaluations (
                candidate_id, horizon_days, through_date, observed_days,
                reference_date, reference_close, execution_date, execution_price,
                execution_type, execution_note, end_date, end_close, return_pct,
                gross_return_pct, cost_pct, net_return_pct, net_win,
                benchmark_ticker, benchmark_return_pct, excess_return_pct,
                max_gain_pct, max_drawdown_pct, hit_stop, hit_target_1,
                hit_target_2, exit_type, exit_date, exit_price, exit_note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, horizon_days, through_date) DO UPDATE SET
                observed_days=excluded.observed_days,
                reference_date=excluded.reference_date,
                reference_close=excluded.reference_close,
                execution_date=excluded.execution_date,
                execution_price=excluded.execution_price,
                execution_type=excluded.execution_type,
                execution_note=excluded.execution_note,
                end_date=excluded.end_date,
                end_close=excluded.end_close,
                return_pct=excluded.return_pct,
                gross_return_pct=excluded.gross_return_pct,
                cost_pct=excluded.cost_pct,
                net_return_pct=excluded.net_return_pct,
                net_win=excluded.net_win,
                benchmark_ticker=excluded.benchmark_ticker,
                benchmark_return_pct=excluded.benchmark_return_pct,
                excess_return_pct=excluded.excess_return_pct,
                max_gain_pct=excluded.max_gain_pct,
                max_drawdown_pct=excluded.max_drawdown_pct,
                hit_stop=excluded.hit_stop,
                hit_target_1=excluded.hit_target_1,
                hit_target_2=excluded.hit_target_2,
                exit_type=excluded.exit_type,
                exit_date=excluded.exit_date,
                exit_price=excluded.exit_price,
                exit_note=excluded.exit_note,
                created_at=excluded.created_at
            """,
            (
                row["candidate_id"],
                row["horizon_days"],
                row["through_date"],
                row["observed_days"],
                row["reference_date"],
                row["reference_close"],
                row["execution_date"],
                row["execution_price"],
                row["execution_type"],
                row["execution_note"],
                row["end_date"],
                row["end_close"],
                row["return_pct"],
                row["gross_return_pct"],
                row["cost_pct"],
                row["net_return_pct"],
                row["net_win"],
                row["benchmark_ticker"],
                row["benchmark_return_pct"],
                row["excess_return_pct"],
                row["max_gain_pct"],
                row["max_drawdown_pct"],
                row["hit_stop"],
                row["hit_target_1"],
                row["hit_target_2"],
                row["exit_type"],
                row["exit_date"],
                row["exit_price"],
                row["exit_note"],
                row["created_at"],
            ),
        )
    conn.commit()
    return len(rows)


def candidate_strategy_leaderboard(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    through_date: str | None = None,
    *,
    dedupe: bool = False,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[object] = []
    latest_filters: list[str] = []
    latest_params: list[object] = []
    if start_date is not None:
        filters.append("c.as_of_date >= ?")
        params.append(start_date)
    if end_date is not None:
        filters.append("c.as_of_date <= ?")
        params.append(end_date)
    if markets:
        filters.append(f"c.market IN ({', '.join('?' for _ in markets)})")
        params.extend(markets)
    if through_date is not None:
        latest_filters.append("through_date <= ?")
        latest_params.append(through_date)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    latest_where_sql = f"WHERE {' AND '.join(latest_filters)}" if latest_filters else ""
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            {latest_where_sql}
            GROUP BY candidate_id
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
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
            {where_sql}
            {"AND" if where_sql else "WHERE"} st.status != 'RETIRED'
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
            c.weight,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(e.id) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(e.return_pct) AS avg_return_pct,
            AVG(e.net_return_pct) AS avg_net_return_pct,
            AVG(e.benchmark_return_pct) AS avg_benchmark_return_pct,
            AVG(e.excess_return_pct) AS avg_excess_return_pct,
            AVG(e.max_gain_pct) AS avg_max_gain_pct,
            AVG(e.max_drawdown_pct) AS avg_max_drawdown_pct,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_2 = 1 THEN 1.0 ELSE 0.0 END END) AS target_2_rate
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name, c.strategy_version, c.weight
        ORDER BY COALESCE(avg_excess_return_pct, avg_net_return_pct, avg_return_pct, -999) DESC, evaluated_count DESC, candidate_count DESC
        """,
        tuple(latest_params + params),
    ).fetchall()


def strategy_risk_adjusted_metrics(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    through_date: str | None = None,
    *,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> dict[str, dict[str, float]]:
    filters: list[str] = []
    params: list[object] = []
    if start_date is not None:
        filters.append("c.as_of_date >= ?")
        params.append(start_date)
    if end_date is not None:
        filters.append("c.as_of_date <= ?")
        params.append(end_date)
    if markets:
        filters.append(f"c.market IN ({', '.join('?' for _ in markets)})")
        params.extend(markets)
    if through_date is not None:
        filters.append("e.through_date <= ?")
        params.append(through_date)
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    rows = conn.execute(
        f"""
        SELECT c.strategy_id, e.net_return_pct
        FROM candidates c
        JOIN candidate_evaluations e ON e.candidate_id = c.id
        JOIN strategies st ON st.id = c.strategy_id
        {where_sql}
        {"AND" if where_sql else "WHERE"} st.status != 'RETIRED'
        ORDER BY c.strategy_id
        """,
        tuple(params),
    ).fetchall()
    by_strategy: dict[str, list[float]] = {}
    for row in rows:
        sid = str(row["strategy_id"])
        ret = float(row["net_return_pct"])
        by_strategy.setdefault(sid, []).append(ret)
    result: dict[str, dict[str, float]] = {}
    for sid, returns in by_strategy.items():
        result[sid] = {
            "sharpe_ratio": compute_sharpe_ratio(returns),
            "sortino_ratio": compute_sortino_ratio(returns),
            "sample_count": len(returns),
        }
    return result


def candidate_horizon_strategy_leaderboard(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    through_date: str | None = None,
    *,
    horizon_days: int = 10,
    dedupe: bool = False,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[object] = []
    eval_filters = ["horizon_days = ?"]
    eval_params: list[object] = [horizon_days]
    if start_date is not None:
        filters.append("c.as_of_date >= ?")
        params.append(start_date)
    if end_date is not None:
        filters.append("c.as_of_date <= ?")
        params.append(end_date)
    if markets:
        filters.append(f"c.market IN ({', '.join('?' for _ in markets)})")
        params.extend(markets)
    if through_date is not None:
        eval_filters.append("through_date <= ?")
        eval_params.append(through_date)

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    eval_where_sql = f"WHERE {' AND '.join(eval_filters)}"
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, horizon_days, MAX(through_date) AS through_date
            FROM candidate_horizon_evaluations
            {eval_where_sql}
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
            {where_sql}
            {"AND" if where_sql else "WHERE"} st.status != 'RETIRED'
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
            c.weight,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(CASE WHEN e.observed_days >= e.horizon_days THEN e.id END) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.benchmark_return_pct END) AS avg_benchmark_return_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.excess_return_pct END) AS avg_excess_return_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.max_gain_pct END) AS avg_max_gain_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.max_drawdown_pct END) AS avg_max_drawdown_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.hit_target_2 = 1 THEN 1.0 ELSE 0.0 END END) AS target_2_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_pct,
            AVG(CASE WHEN e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate,
            AVG(CASE WHEN e.observed_days >= e.horizon_days AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name, c.strategy_version, c.weight
        ORDER BY COALESCE(avg_excess_return_pct, avg_net_return_pct, avg_return_pct, -999) DESC, evaluated_count DESC, candidate_count DESC
        """,
        tuple(eval_params + params),
    ).fetchall()


def _candidate_segment_leaderboard(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None,
    segment_sql: str,
    *,
    dedupe: bool = True,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[sqlite3.Row]:
    latest_where_sql = "WHERE through_date <= ?" if through_date is not None else ""
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    latest_params: list[object] = [through_date] if through_date is not None else []
    market_sql = ""
    market_params: list[object] = []
    if markets:
        market_sql = f"AND c.market IN ({', '.join('?' for _ in markets)})"
        market_params.extend(markets)
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            {latest_where_sql}
            GROUP BY candidate_id
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        ),
        base_candidates AS (
            SELECT
                c.*,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            WHERE c.as_of_date >= ?
              AND c.as_of_date <= ?
              {market_sql}
              AND st.status != 'RETIRED'
        ),
        selected_candidates AS (
            SELECT *
            FROM base_candidates
            {dedupe_sql}
        )
        SELECT
            {segment_sql} AS segment,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(e.id) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(e.return_pct) AS avg_return_pct,
            AVG(e.net_return_pct) AS avg_net_return_pct,
            AVG(e.benchmark_return_pct) AS avg_benchmark_return_pct,
            AVG(e.excess_return_pct) AS avg_excess_return_pct,
            AVG(e.max_gain_pct) AS avg_max_gain_pct,
            AVG(e.max_drawdown_pct) AS avg_max_drawdown_pct,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_2 = 1 THEN 1.0 ELSE 0.0 END END) AS target_2_rate
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY segment
        ORDER BY COALESCE(avg_excess_return_pct, avg_net_return_pct, avg_return_pct, -999) DESC, evaluated_count DESC, candidate_count DESC
        """,
        tuple(latest_params + [start_date, end_date] + market_params),
    ).fetchall()


def candidate_market_leaderboard(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    dedupe: bool = True,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[sqlite3.Row]:
    return _candidate_segment_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        "c.market",
        dedupe=dedupe,
        markets=markets,
    )


def candidate_action_leaderboard(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    dedupe: bool = True,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[sqlite3.Row]:
    return _candidate_segment_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        """
        CASE
            WHEN UPPER(c.action) LIKE '%CONFIRM%' THEN '次日确认/确认后触发'
            ELSE '买入当天触发'
        END
        """,
        dedupe=dedupe,
        markets=markets,
    )


def suggest_strategy_weight_adjustments(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    min_samples: int = MIN_WEIGHT_SUGGESTION_SAMPLES,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[dict[str, object]]:
    strategy_horizons = {}
    for row in conn.execute("SELECT id, target_horizon_days FROM strategies WHERE status != 'RETIRED'").fetchall():
        strategy_horizons[row["id"]] = int(row["target_horizon_days"] or 10)

    rows: list[sqlite3.Row] = []
    for horizon in sorted(set(strategy_horizons.values()) or {10}):
        horizon_rows = candidate_horizon_strategy_leaderboard(
            conn,
            start_date,
            end_date,
            through_date,
            horizon_days=horizon,
            dedupe=True,
            markets=markets,
        )
        rows.extend(
            row for row in horizon_rows
            if strategy_horizons.get(row["strategy_id"], 10) == horizon
        )

    suggestions: list[dict[str, object]] = []
    for row in rows:
        evaluated = int(row["evaluated_count"] or 0)
        weight = float(row["weight"] or 1.0)
        target_horizon_days = strategy_horizons.get(row["strategy_id"], 10)
        stop_rate = row["stop_rate"]
        net_win_rate = row["net_win_rate"]
        avg_net_return = row["avg_net_return_pct"]
        avg_excess_return = row["avg_excess_return_pct"]
        excess_win_rate = row["excess_win_rate"]
        target_1_rate = row["target_1_rate"]
        recommendation = "KEEP_COLLECTING"
        suggested_weight = weight
        reason = "样本仍少，继续观察。"
        if evaluated < min_samples:
            recommendation = "INSUFFICIENT_SAMPLE"
        elif stop_rate is not None and float(stop_rate) >= 0.45:
            recommendation = "DOWN_WEIGHT"
            suggested_weight = max(0.25, weight * 0.70)
            reason = "去重后止损率过高，先自动降权。"
        elif avg_excess_return is not None and excess_win_rate is not None and float(avg_excess_return) < 0 and float(excess_win_rate) < 0.45:
            recommendation = "DOWN_WEIGHT"
            suggested_weight = max(0.25, weight * 0.80)
            reason = "平均超额收益和超额胜率同时偏弱，先自动降权。"
        elif avg_excess_return is None and avg_net_return is not None and net_win_rate is not None and float(avg_net_return) < 0 and float(net_win_rate) < 0.45:
            recommendation = "DOWN_WEIGHT"
            suggested_weight = max(0.25, weight * 0.80)
            reason = "缺少基准收益，但平均净收益和净胜率同时偏弱，先降权不升权。"
        elif avg_excess_return is None:
            recommendation = "NO_BENCHMARK"
            reason = "缺少基准收益，不能判断alpha，禁止自动升权。"
        elif target_1_rate is not None and excess_win_rate is not None and float(target_1_rate) >= 0.50 and float(excess_win_rate) >= 0.50:
            recommendation = "KEEP_OR_UP_WEIGHT"
            suggested_weight = min(2.0, weight * 1.05)
            reason = "目标触达率和超额胜率较好，可保留或小幅加权。"
        suggestions.append(
            {
                "strategy_id": row["strategy_id"],
                "strategy_name": row["strategy_name"],
                "strategy_version": row["strategy_version"],
                "evaluated_count": evaluated,
                "target_horizon_days": target_horizon_days,
                "current_weight": weight,
                "suggested_weight": suggested_weight,
                "recommendation": recommendation,
                "reason": reason,
                "stop_rate": stop_rate,
                "win_rate": net_win_rate,
                "avg_return_pct": avg_net_return,
                "excess_win_rate": excess_win_rate,
                "avg_excess_return_pct": avg_excess_return,
            }
        )
    return suggestions


def apply_strategy_weight_adjustments(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str | None = None,
    *,
    min_samples: int = MIN_WEIGHT_SUGGESTION_SAMPLES,
    markets: tuple[str, ...] = FORMAL_MARKETS,
    allow_upweight: bool = False,
) -> int:
    suggestions = suggest_strategy_weight_adjustments(
        conn,
        start_date,
        end_date,
        through_date,
        min_samples=min_samples,
        markets=markets,
    )
    changed = 0
    for item in suggestions:
        if item["recommendation"] == "DOWN_WEIGHT":
            if float(item["suggested_weight"]) == float(item["current_weight"]):
                continue
            conn.execute(
                "UPDATE strategies SET weight = ? WHERE id = ?",
                (item["suggested_weight"], item["strategy_id"]),
            )
            changed += 1
        elif allow_upweight and item["recommendation"] == "KEEP_OR_UP_WEIGHT":
            if float(item["suggested_weight"]) == float(item["current_weight"]):
                continue
            if int(item["evaluated_count"]) < 10:
                continue
            conn.execute(
                "UPDATE strategies SET weight = ? WHERE id = ?",
                (item["suggested_weight"], item["strategy_id"]),
            )
            changed += 1
    conn.commit()
    return changed


def score_calibration(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    horizon_days: int = 10,
    markets: tuple[str, ...] = FORMAL_MARKETS,
) -> list[dict[str, object]]:
    market_filter = ""
    market_params: list[object] = []
    if markets:
        market_filter = f"AND c.market IN ({', '.join('?' for _ in markets)})"
        market_params.extend(markets)
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, horizon_days, MAX(through_date) AS td
            FROM candidate_horizon_evaluations
            WHERE horizon_days = ? AND through_date <= ?
            GROUP BY candidate_id, horizon_days
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_horizon_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.horizon_days = e.horizon_days
             AND l.td = e.through_date
        ),
        base_candidates AS (
            SELECT
                c.*,
                ROW_NUMBER() OVER (
                    PARTITION BY c.as_of_date, c.market, c.ticker
                    ORDER BY c.candidate_score DESC, c.id ASC
                ) AS rn
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            WHERE c.as_of_date >= ? AND c.as_of_date <= ?
              {market_filter}
              AND st.status != 'RETIRED'
        )
        SELECT
            CASE
                WHEN c.candidate_score >= 90 THEN '90-100'
                WHEN c.candidate_score >= 80 THEN '80-90'
                WHEN c.candidate_score >= 70 THEN '70-80'
                ELSE '60-70'
            END AS score_bucket,
            COUNT(*) AS sample_count,
            AVG(e.net_return_pct) AS avg_net_return,
            AVG(e.benchmark_return_pct) AS avg_benchmark_return,
            AVG(e.excess_return_pct) AS avg_excess_return,
            AVG(CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END) AS net_win_rate,
            AVG(CASE WHEN e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate,
            AVG(CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END) AS stop_rate,
            AVG(CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END) AS target_rate,
            MIN(e.net_return_pct) AS worst_return,
            MAX(e.max_drawdown_pct) AS worst_drawdown
        FROM base_candidates c
        JOIN latest_eval e ON e.candidate_id = c.id
        WHERE c.rn = 1 AND e.observed_days >= e.horizon_days
        GROUP BY score_bucket
        ORDER BY score_bucket DESC
        """,
        tuple([horizon_days, through_date, start_date, end_date] + market_params),
    ).fetchall()

    result = []
    for row in rows:
        result.append({
            "score_bucket": row["score_bucket"],
            "sample_count": int(row["sample_count"]),
            "avg_net_return": row["avg_net_return"],
            "avg_benchmark_return": row["avg_benchmark_return"],
            "avg_excess_return": row["avg_excess_return"],
            "net_win_rate": row["net_win_rate"],
            "excess_win_rate": row["excess_win_rate"],
            "stop_rate": row["stop_rate"],
            "target_rate": row["target_rate"],
            "worst_return": row["worst_return"],
            "worst_drawdown": row["worst_drawdown"],
        })
    return result
