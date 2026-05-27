from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class EntryExecution:
    price: float
    execution_type: str
    note: str


@dataclass(frozen=True)
class ExitExecution:
    date: str
    price: float
    exit_type: str
    note: str


def has_intraday_bars(conn: sqlite3.Connection, market: str, ticker: str, date: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM intraday_bars
        WHERE market = ? AND ticker = ? AND date = ?
        LIMIT 1
        """,
        (market, ticker, date),
    ).fetchone()
    return row is not None


def open_5min_vwap_entry(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    date: str,
    fallback_open: float,
) -> EntryExecution:
    rows = conn.execute(
        """
        SELECT open, close, volume, amount
        FROM intraday_bars
        WHERE market = ? AND ticker = ? AND date = ?
        ORDER BY datetime
        LIMIT 5
        """,
        (market, ticker, date),
    ).fetchall()
    if not rows:
        return EntryExecution(fallback_open, "NEXT_OPEN_DAILY", "无分时数据，回退到日线开盘价。")

    total_volume = sum(float(row["volume"] or 0.0) for row in rows)
    total_amount = sum(float(row["amount"] or 0.0) for row in rows if row["amount"] is not None)
    weighted = sum(float(row["close"]) * float(row["volume"] or 0.0) for row in rows)
    average_close = weighted / total_volume if total_volume > 0 else 0.0
    if total_volume > 0 and total_amount > 0:
        vwap = total_amount / total_volume
        if average_close > 0 and vwap > average_close * 10:
            # AkShare A-share minute volume is commonly reported in lots, while amount is in yuan.
            vwap = vwap / 100.0
        if average_close <= 0 or average_close * 0.2 <= vwap <= average_close * 5:
            return EntryExecution(vwap, "OPEN_5MIN_VWAP", "按开盘前5根分时成交额/成交量估算入场。")

    if total_volume > 0:
        return EntryExecution(weighted / total_volume, "OPEN_5MIN_VWAP", "按开盘前5根分时收盘价加权估算入场。")
    return EntryExecution(float(rows[0]["open"]), "OPEN_FIRST_INTRADAY", "分时无成交量，按第一根分时开盘价。")


def intraday_exit_path(
    conn: sqlite3.Connection,
    market: str,
    ticker: str,
    entry_date: str,
    entry_price: float,
    stop_loss: float | None,
    target_1: float | None,
    target_2: float | None,
    through_date: str,
    *,
    max_exit_bars: int | None = None,
    trailing_stop_pct: float | None = None,
    trailing_activation_pct: float | None = None,
) -> ExitExecution | None:
    start_operator = ">" if market == "CN_A" else ">="
    rows = conn.execute(
        f"""
        SELECT date, time, open, close, high, low
        FROM intraday_bars
        WHERE market = ? AND ticker = ? AND date {start_operator} ? AND date <= ?
        ORDER BY datetime
        """,
        (market, ticker, entry_date, through_date),
    ).fetchall()
    if max_exit_bars is not None:
        rows = rows[:max_exit_bars]
    if not rows:
        return None

    stop = stop_loss
    first_target = target_1
    second_target = target_2
    last = rows[-1]
    trailing_active = False
    trailing_stop_price = 0.0
    highest_since_entry = entry_price
    for row in rows:
        open_price = float(row["open"])
        low = float(row["low"])
        high = float(row["high"])
        date = str(row["date"])
        time = str(row["time"])

        if stop is not None and open_price <= stop:
            return ExitExecution(date, open_price, "STOP_LOSS", f"{time} 开盘低于或等于止损价。")
        if first_target is not None and open_price >= first_target:
            return ExitExecution(date, open_price, "TARGET_1", f"{time} 开盘高于或等于目标1。")
        if second_target is not None and open_price >= second_target:
            return ExitExecution(date, open_price, "TARGET_2", f"{time} 开盘高于或等于目标2。")

        stop_hit = stop is not None and low <= stop
        target_1_hit = first_target is not None and high >= first_target
        target_2_hit = second_target is not None and high >= second_target
        if stop_hit and (target_1_hit or target_2_hit):
            return ExitExecution(date, stop, "STOP_LOSS", f"{time} 同一分时触及止损和目标，按止损优先。")
        if stop_hit:
            return ExitExecution(date, stop, "STOP_LOSS", f"{time} 盘中触及止损。")
        if target_1_hit:
            return ExitExecution(date, first_target, "TARGET_1", f"{time} 盘中触及目标1。")
        if target_2_hit:
            return ExitExecution(date, second_target, "TARGET_2", f"{time} 盘中触及目标2。")

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
                    return ExitExecution(date, open_price, "TRAILING_STOP", f"{time} 追踪止损触发：最高 {highest_since_entry:.2f}，止损线 {trailing_stop_price:.2f}。")
                if low <= trailing_stop_price:
                    return ExitExecution(date, trailing_stop_price, "TRAILING_STOP", f"{time} 追踪止损触发：最高 {highest_since_entry:.2f}，止损线 {trailing_stop_price:.2f}。")

    return ExitExecution(str(last["date"]), float(last["close"]), "HOLD", "分时窗口未触发止损/目标，按最后一根分时收盘。")
