from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .ledger import now_utc
from .trading_rules import cn_a_limit_pct


def _latest_price_bar(
    conn: sqlite3.Connection, market: str, ticker: str, as_of_date: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (market, ticker, as_of_date),
    ).fetchone()


def _prior_bars(
    conn: sqlite3.Connection, market: str, ticker: str, before_date: str, limit: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date < ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (market, ticker, before_date, limit),
    ).fetchall()


def _price_bar(conn: sqlite3.Connection, market: str, ticker: str, date: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date = ?
        """,
        (market, ticker, date),
    ).fetchone()


def _next_price_date(conn: sqlite3.Connection, market: str, ticker: str, after_date: str) -> str | None:
    row = conn.execute(
        """
        SELECT MIN(date) AS d
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date > ?
        """,
        (market, ticker, after_date),
    ).fetchone()
    return str(row["d"]) if row and row["d"] else None


MIN_DAILY_AMOUNT = 5_000_000.0
MIN_EVENT_IMPORTANCE = 0.75


def _passes_liquidity_filter(bar: sqlite3.Row) -> bool:
    amount = bar["amount"]
    if amount is None:
        return True
    return float(amount) >= MIN_DAILY_AMOUNT


def _market_regime(conn: sqlite3.Connection, market: str, as_of_date: str) -> str:
    index_map = {
        "CN_A": ("CN_A", "000300.SS"),
        "HK": ("HK", "HSI.HK"),
        "US": ("US", "SPY"),
    }
    entry = index_map.get(market)
    if entry is None:
        return "NEUTRAL"
    idx_market, idx_ticker = entry
    bars = conn.execute(
        "SELECT close FROM price_bars WHERE market = ? AND ticker = ? AND date <= ? ORDER BY date DESC LIMIT 60",
        (idx_market, idx_ticker, as_of_date),
    ).fetchall()
    if len(bars) >= 60:
        closes = [float(row["close"]) for row in bars]
        ma20 = sum(closes[:20]) / 20.0
        ma60 = sum(closes[:60]) / 60.0
        current = closes[0]
        if current > ma20 and ma20 > ma60:
            return "BULL"
        if current < ma20 and ma20 < ma60:
            return "BEAR"
        return "NEAR_MA"
    elif len(bars) >= 20:
        closes = [float(row["close"]) for row in bars]
        ma20 = sum(closes[:20]) / 20.0
        current = closes[0]
        if current > ma20 * 1.01:
            return "BULL"
        if current < ma20 * 0.99:
            return "BEAR"
        return "NEAR_MA"
    else:
        proxy_rows = conn.execute(
            """
            SELECT date, AVG(COALESCE(change_pct, 0)) AS avg_change_pct
            FROM price_bars
            WHERE market = ? AND date <= ?
            GROUP BY date
            ORDER BY date DESC
            LIMIT 20
            """,
            (market, as_of_date),
        ).fetchall()
        if len(proxy_rows) < 20:
            return "NEUTRAL"
        proxy_level = 100.0
        proxy_closes: list[float] = []
        for row in reversed(proxy_rows):
            proxy_level *= 1.0 + float(row["avg_change_pct"] or 0.0) / 100.0
            proxy_closes.append(proxy_level)
        closes = list(reversed(proxy_closes))
    if len(closes) < 20:
        return "NEUTRAL"
    current = closes[0]
    ma20 = sum(closes) / len(closes)
    if current > ma20 * 1.01:
        return "BULL"
    if current < ma20 * 0.99:
        return "BEAR"
    return "NEAR_MA"


def _volume_average(rows: list[sqlite3.Row]) -> float | None:
    if not rows:
        return None
    return sum(float(row["volume"]) for row in rows) / len(rows)


def _price_universe(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT DISTINCT
            COALESCE(i.market, p.market) AS market,
            COALESCE(i.ticker, p.ticker) AS ticker,
            COALESCE(i.name, p.ticker) AS name
        FROM price_bars p
        LEFT JOIN instruments i
          ON i.market = p.market
         AND i.ticker = p.ticker
        ORDER BY market, ticker
        """
    ).fetchall()
    return rows


def _is_excluded_name(name: str) -> bool:
    upper_name = name.upper()
    if "ST" in upper_name or "退" in name:
        return True
    # Exclude indexes and ETFs
    index_keywords = ("指数", "ETF", "LOF", "基金")
    return any(kw in name for kw in index_keywords)


WEAK_XINGYE_EVENT_KEYWORDS = (
    "业绩说明会预告",
    "网上业绩说明会的公告",
    "年度股东",
    "股东大会",
    "董事会",
    "监事会",
    "法律意见书",
    "独立董事",
    "权益分派",
    "分红",
    "担保",
    "综合授信额度",
    "申请授信额度",
)


REVALUATION_KEYWORDS = (
    "新能源",
    "汽车",
    "客户",
    "供应链",
    "订单",
    "合同",
    "产能",
    "投产",
    "项目",
    "海外",
    "出口",
    "新产品",
    "产品线",
    "利润率",
    "毛利率",
    "增长引擎",
    "核心增长",
    "收购",
    "并购",
    "重组",
    "股权",
    "业绩预告",
    "净利润",
    "收入增长",
    "利润增长",
)


US_NEWS_EVENT_KEYWORDS = (
    "earnings",
    "guidance",
    "upgrade",
    "target price",
    "price target",
    "contract",
    "order",
    "ai",
    "chip",
    "revenue",
    "profit",
    "评级",
    "目标价",
    "上调",
    "财报",
    "业绩",
    "订单",
    "合同",
    "人工智能",
    "芯片",
)


HK_BUYBACK_KEYWORDS = ("回购", "购回", "注销", "分红", "派息", "股东回报", "buyback")
HK_SOUTHBOUND_KEYWORDS = ("南向", "港股通", "净买入", "加仓", "持股", "资金")
HK_NEWS_EVENT_KEYWORDS = (
    "业绩",
    "财报",
    "收入",
    "利润",
    "指引",
    "AI",
    "人工智能",
    "芯片",
    "汽车",
    "电动车",
    "回购",
    "上调",
    "目标价",
    "订单",
    "合同",
)
CN_HARD_EVENT_KEYWORDS = (
    "订单",
    "合同",
    "回购",
    "增持",
    "业绩预告",
    "净利润",
    "收入增长",
    "重组",
    "收购",
    "并购",
    "新能源",
    "客户",
    "供应链",
    "产能",
    "项目",
    "新产品",
)
GENERIC_RESEARCH_KEYWORDS = (
    "调研",
    "投资者关系",
    "投资者关系活动",
    "业绩说明会",
    "路演",
)
CORE_HARD_EVENT_KEYWORDS = (
    "订单",
    "合同",
    "回购",
    "增持",
    "业绩预告",
    "净利润",
    "收入增长",
    "重组",
    "收购",
    "并购",
)
PEAD_EVENT_TYPES = (
    "EARNINGS_REPORT",
    "EARNINGS_PRELIMINARY",
    "EARNINGS_FORECAST",
    "ANNUAL_REPORT",
    "QUARTERLY_REPORT",
)
PEAD_EVENT_KEYWORDS = (
    "业绩",
    "财报",
    "快报",
    "预告",
    "年报",
    "季报",
    "年度报告",
    "季度报告",
)
PEAD_NET_PROFIT_METRICS = ("扣非净利润增长率(%)", "净利润增长率(%)", "归母净利润增长率(%)")
PEAD_REVENUE_METRICS = ("主营业务收入增长率(%)", "营业收入增长率(%)", "营收增长率(%)")
PEAD_ROE_METRICS = ("ROE TTM(%)", "净资产收益率TTM(%)", "加权净资产收益率(%)", "净资产收益率(%)")
PEAD_MIN_AVG_AMOUNT = 50_000_000.0


def _event_text(event: dict[str, object]) -> str:
    parts = [
        str(event.get("event_type") or ""),
        str(event.get("title") or ""),
        str(event.get("summary") or ""),
        str(event.get("tags") or ""),
    ]
    return " ".join(parts)


def _event_strategy_id(event: sqlite3.Row) -> str | None:
    market = str(event["market"])
    text = _event_text(dict(event))
    lower_text = text.lower()
    event_type = str(event["event_type"])
    if market == "US":
        if event_type.startswith("SEC "):
            return "us_sec_event_momentum"
        if any(keyword.lower() in lower_text for keyword in US_NEWS_EVENT_KEYWORDS):
            return "us_news_event_momentum"
        return None
    if market == "HK":
        if any(keyword.lower() in lower_text for keyword in HK_BUYBACK_KEYWORDS):
            return "hk_buyback_recovery"
        if any(keyword.lower() in lower_text for keyword in HK_SOUTHBOUND_KEYWORDS):
            return "hk_southbound_recovery"
        if any(keyword.lower() in lower_text for keyword in HK_NEWS_EVENT_KEYWORDS):
            return "hk_news_recovery"
        return None
    if market == "CN_A":
        event_dict = dict(event)
        if _is_weak_xingye_event(event_dict):
            return None
        if _is_strong_cn_event(event_dict, []):
            return "a_share_hard_event_catalyst"
    return None


def _event_strategy_profile(strategy_id: str) -> tuple[str, str, str]:
    profiles = {
        "us_sec_event_momentum": (
            "SEC披露进入可交易窗口，若披露后价格承接和成交量确认，可能出现事件后动量。",
            "SEC披露并不天然利好；若跌破事件日低点或披露内容被解读为利空，应剔除。",
            "WATCH_OR_BUY_ON_CONFIRMATION",
        ),
        "us_news_event_momentum": (
            "公司特异性新闻、评级或目标价变化被量价确认，可能形成短期动量。",
            "过滤宏观早报和无公司特异性的转载新闻；次日不承接则降级。",
            "WATCH_OR_BUY_ON_CONFIRMATION",
        ),
        "hk_buyback_recovery": (
            "回购、派息或股东回报线索叠加价格不破位，可能推动港股估值修复。",
            "港股回购也可能只是托底；若南向和成交额不配合，应降低仓位。",
            "WATCH_CONFIRMATION",
        ),
        "hk_southbound_recovery": (
            "南向资金或港股通关注叠加趋势承接，可能带来港股修复行情。",
            "南向新闻容易滞后；若价格不确认，不应仅凭资金标题买入。",
            "WATCH_CONFIRMATION",
        ),
        "hk_news_recovery": (
            "业绩、AI、芯片、汽车等公司特异性新闻进入修复窗口，等待量价承接。",
            "港股新闻噪音较高，必须确认新闻与公司收入或利润有映射。",
            "WATCH_CONFIRMATION",
        ),
        "a_share_hard_event_catalyst": (
            "硬公告能够映射到业绩/估值变化，且价格没有破位，进入事件候选池。",
            "普通调研只作辅助证据；若跌破事件窗口低点，应快速降级。",
            "WATCH_OR_BUY_ON_CONFIRMATION",
        ),
    }
    return profiles.get(
        strategy_id,
        (
            "事件进入观察窗口，等待量价确认后再决定是否交易。",
            "未知事件策略需要人工复核；若价格不承接或跌破事件窗口低点，应剔除。",
            "WATCH_CONFIRMATION",
        ),
    )


def _is_weak_xingye_event(event: dict[str, object]) -> bool:
    text = _event_text(event)
    if "授信" in text and any(keyword in text for keyword in ("无抵押", "信用", "战略", "扩产", "项目")):
        return False
    return any(keyword in text for keyword in WEAK_XINGYE_EVENT_KEYWORDS)


def _has_revaluation_mapping(event: dict[str, object], financial_flags: list[str]) -> bool:
    text = _event_text(event)
    if any(keyword in text for keyword in REVALUATION_KEYWORDS):
        return True
    if len(financial_flags) >= 2 and any(flag.startswith(("净利润增长", "收入增长")) for flag in financial_flags):
        return True
    return False


def _is_generic_research_event(event: dict[str, object]) -> bool:
    text = _event_text(event)
    return any(keyword in text for keyword in GENERIC_RESEARCH_KEYWORDS)


def _is_strong_cn_event(event: dict[str, object], financial_flags: list[str]) -> bool:
    text = _event_text(event)
    if _is_generic_research_event(event):
        return any(keyword in text for keyword in CORE_HARD_EVENT_KEYWORDS) and _has_revaluation_mapping(event, financial_flags)
    if any(keyword in text for keyword in CN_HARD_EVENT_KEYWORDS):
        return True
    importance = float(event.get("importance_score") or 0.0)
    return importance >= 0.88 and _has_revaluation_mapping(event, financial_flags)


def _recent_high(rows: list[sqlite3.Row]) -> float:
    return max(float(row["high"]) for row in rows)


def _recent_low(rows: list[sqlite3.Row]) -> float:
    return min(float(row["low"]) for row in rows)


def _average_close(rows: list[sqlite3.Row]) -> float:
    return sum(float(row["close"]) for row in rows) / len(rows)


def _average_true_range(rows: list[sqlite3.Row]) -> float | None:
    if len(rows) < 2:
        return None
    chronological = list(reversed(rows))
    true_ranges: list[float] = []
    previous_close = float(chronological[0]["close"])
    for row in chronological[1:]:
        high = float(row["high"])
        low = float(row["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = float(row["close"])
    if not true_ranges:
        return None
    return sum(true_ranges) / len(true_ranges)


def _target_prices(
    market: str,
    close: float,
    recent_rows: list[sqlite3.Row],
    fallback_1: float,
    fallback_2: float,
) -> tuple[float, float]:
    if market != "CN_A":
        return fallback_1, fallback_2
    atr = _average_true_range(recent_rows[:15])
    if atr is None or atr <= 0:
        return fallback_1, fallback_2
    return close + atr * 2.0, close + atr * 3.5


def _dynamic_cn_a_stop(close: float, structural_stop: float, recent_rows: list[sqlite3.Row]) -> float:
    atr = _average_true_range(recent_rows[:15])
    if atr is None or atr <= 0:
        raw_stop = structural_stop
    else:
        raw_stop = max(structural_stop, close - 2.0 * atr)
    min_stop = close * 0.90
    max_stop = close * 0.97
    return min(max(raw_stop, min_stop), max_stop)


def _candidate(
    *,
    as_of_date: str,
    market: str,
    ticker: str,
    name: str,
    strategy_id: str,
    score: float,
    action: str,
    close: float,
    stop_loss: float,
    target_1: float,
    target_2: float,
    thesis: str,
    trigger_condition: str,
    risk_notes: str,
    evidence: list[dict[str, object]],
    data_date: str = "",
    trailing_stop_pct: float | None = None,
    trailing_activation_pct: float | None = None,
) -> dict[str, object]:
    reward_risk = round((target_1 - close) / (close - stop_loss), 2) if stop_loss > 0 and close > stop_loss else 0.0
    effective_score = min(score, 100.0)
    if reward_risk < 1.0:
        effective_score = min(effective_score, 70.0)
    return {
        "as_of_date": as_of_date,
        "market": market,
        "ticker": ticker,
        "name": name,
        "strategy_id": strategy_id,
        "candidate_score": effective_score,
        "action": action,
        "entry_price": close,
        "signal_close": close,
        "buy_zone_low": round(close * 0.985, 2),
        "buy_zone_high": round(close * 1.015, 2),
        "stop_loss": round(stop_loss, 2),
        "target_1": round(target_1, 2),
        "target_2": round(target_2, 2),
        "reward_risk_ratio": reward_risk,
        "trailing_stop_pct": trailing_stop_pct,
        "trailing_activation_pct": trailing_activation_pct,
        "thesis": thesis,
        "trigger_condition": trigger_condition,
        "risk_notes": risk_notes,
        "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        "status": "WATCHLIST",
        "confirmation_status": "PENDING",
        "data_date": data_date,
        "created_at": now_utc(),
    }


def _date_minus(date_value: str, days: int) -> str:
    current = datetime.strptime(date_value, "%Y-%m-%d").date()
    return (current - timedelta(days=days)).isoformat()


def _trading_window_start(conn: sqlite3.Connection, market: str, as_of_date: str, lookback_days: int) -> str:
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM price_bars
        WHERE market = ? AND date <= ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (market, as_of_date, lookback_days),
    ).fetchall()
    if rows:
        return str(rows[-1]["date"])
    # 没有行情日历时退回自然日，避免事件表为空库时报错。
    return _date_minus(as_of_date, lookback_days * 2)


def _event_rows(conn: sqlite3.Connection, as_of_date: str, lookback_days: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM corporate_events
        WHERE event_date >= ?
          AND event_date <= ?
          AND importance_score >= ?
        ORDER BY importance_score DESC, event_date DESC
        """,
        (_date_minus(as_of_date, lookback_days), as_of_date, MIN_EVENT_IMPORTANCE),
    ).fetchall()


def _first_price_bar_on_or_after(
    conn: sqlite3.Connection, market: str, ticker: str, start_date: str, end_date: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM price_bars
        WHERE market = ? AND ticker = ? AND date >= ? AND date <= ?
        ORDER BY date ASC
        LIMIT 1
        """,
        (market, ticker, start_date, end_date),
    ).fetchone()


def _change_pct_from_prior(bar: sqlite3.Row, prior: list[sqlite3.Row]) -> float:
    if bar["change_pct"] is not None:
        return float(bar["change_pct"])
    if not prior:
        return 0.0
    previous_close = float(prior[0]["close"])
    if previous_close <= 0:
        return 0.0
    return (float(bar["close"]) / previous_close - 1.0) * 100.0


def _amount_value(row: sqlite3.Row) -> float | None:
    if row["amount"] is not None:
        return float(row["amount"])
    close = row["close"]
    volume = row["volume"]
    if close is None or volume is None:
        return None
    return float(close) * float(volume)


def _amount_average(rows: list[sqlite3.Row]) -> float | None:
    values = [_amount_value(row) for row in rows]
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _latest_pead_financials(
    conn: sqlite3.Connection, market: str, ticker: str, as_of_date: str
) -> tuple[bool, bool, dict[str, float]]:
    metric_names = PEAD_NET_PROFIT_METRICS + PEAD_REVENUE_METRICS + PEAD_ROE_METRICS
    placeholders = ", ".join("?" for _ in metric_names)
    rows = conn.execute(
        f"""
        SELECT metric_name, metric_value, report_date, published_date
        FROM financial_metrics
        WHERE market = ? AND ticker = ?
          AND report_date <= ?
          AND COALESCE(published_date, report_date) <= ?
          AND metric_name IN ({placeholders})
        ORDER BY report_date DESC, COALESCE(published_date, report_date) DESC
        """,
        (market, ticker, as_of_date, as_of_date, *metric_names),
    ).fetchall()
    if not rows:
        return True, False, {}

    latest_report_date = str(rows[0]["report_date"])
    values: dict[str, float] = {}
    for row in rows:
        if str(row["report_date"]) != latest_report_date or row["metric_value"] is None:
            continue
        name = str(row["metric_name"])
        value = float(row["metric_value"])
        if name in PEAD_NET_PROFIT_METRICS and "net_profit_growth" not in values:
            values["net_profit_growth"] = value
        elif name in PEAD_REVENUE_METRICS and "revenue_growth" not in values:
            values["revenue_growth"] = value
        elif name in PEAD_ROE_METRICS and "roe_ttm" not in values:
            values["roe_ttm"] = value

    if not values:
        return True, False, {}
    passes = True
    if values.get("net_profit_growth", 25.0) < 25.0:
        passes = False
    if values.get("revenue_growth", 10.0) < 10.0:
        passes = False
    if values.get("roe_ttm", 8.0) < 8.0:
        passes = False
    return passes, True, values


def _latest_model_percentiles(
    conn: sqlite3.Connection, market: str, ticker: str, as_of_date: str
) -> dict[str, float]:
    try:
        from .alpha_factors import MODEL_SCORE_FIELDS, _resolve_model_version
    except Exception:
        return {}

    percentiles: dict[str, float] = {}
    model_rows = conn.execute(
        """
        SELECT model_name, model_version
        FROM model_registry
        WHERE status = 'PRODUCTION'
        ORDER BY id
        LIMIT 3
        """
    ).fetchall()
    for idx, (row, (_score_field, percentile_field)) in enumerate(
        zip(model_rows, MODEL_SCORE_FIELDS),
        start=1,
    ):
        model_name = str(row["model_name"])
        model_version = str(row["model_version"])
        resolved_version = _resolve_model_version(conn, model_name, model_version, as_of_date)
        if resolved_version is None:
            continue
        row = conn.execute(
            """
            SELECT MAX(score_date) AS d
            FROM model_scores
            WHERE model_name = ? AND model_version = ? AND score_date <= ?
            """,
            (model_name, resolved_version, as_of_date),
        ).fetchone()
        if not row or not row["d"]:
            continue
        score_row = conn.execute(
            """
            SELECT percentile
            FROM model_scores
            WHERE model_name = ? AND model_version = ?
              AND market = ? AND ticker = ? AND score_date = ?
            """,
            (model_name, resolved_version, market, ticker, row["d"]),
        ).fetchone()
        if score_row is None or score_row["percentile"] is None:
            continue
        percentile = float(score_row["percentile"])
        if percentile > 1.0:
            percentile /= 100.0
        percentiles[f"M{idx}"] = percentile
        percentiles[percentile_field] = percentile
    return percentiles


def _passes_pead_model_filter(model_percentiles: dict[str, float]) -> bool:
    production_values = [
        float(value)
        for key, value in model_percentiles.items()
        if key in {"M1", "M2", "M3"}
    ]
    if len(production_values) < 3:
        return False
    return (
        sum(1 for value in production_values if value >= 0.60) >= 2
        or any(value >= 0.70 for value in production_values)
    )


def _latest_financial_flags(conn: sqlite3.Connection, market: str, ticker: str, as_of_date: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT metric_name, metric_value, report_date, published_date
        FROM financial_metrics
        WHERE market = ? AND ticker = ? AND report_date <= ?
          AND COALESCE(published_date, report_date) <= ?
        ORDER BY COALESCE(published_date, report_date) DESC, report_date DESC
        """,
        (market, ticker, as_of_date, as_of_date),
    ).fetchall()
    flags: list[str] = []
    seen: set[str] = set()
    for row in rows:
        name = row["metric_name"]
        if name in seen:
            continue
        seen.add(name)
        value = row["metric_value"]
        if value is None:
            continue
        value = float(value)
        if name == "净利润增长率(%)" and value > 10:
            flags.append(f"净利润增长{value:.1f}%")
        elif name == "主营业务收入增长率(%)" and value > 8:
            flags.append(f"收入增长{value:.1f}%")
        elif name == "销售毛利率(%)" and value > 30:
            flags.append(f"毛利率{value:.1f}%")
        elif name == "资产负债率(%)" and value < 45:
            flags.append(f"资产负债率{value:.1f}%")
    return flags[:3]


def _latest_money_flow(conn: sqlite3.Connection, market: str, ticker: str, as_of_date: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM money_flows
        WHERE market = ? AND ticker = ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        (market, ticker, as_of_date),
    ).fetchone()


def _xingye_event_rows(conn: sqlite3.Connection, as_of_date: str, lookback_days: int = 25) -> list[dict[str, object]]:
    start_date = _date_minus(as_of_date, lookback_days)
    corporate = conn.execute(
        """
        SELECT
            market,
            ticker,
            name,
            event_date,
            event_type,
            title,
            summary,
            source_url,
            importance_score,
            '' AS tags
        FROM corporate_events
        WHERE event_date >= ?
          AND event_date <= ?
          AND (
                importance_score >= 0.68
             OR event_type LIKE '%调研%'
             OR title LIKE '%调研%'
             OR title LIKE '%投资者%'
             OR title LIKE '%订单%'
             OR title LIKE '%合同%'
             OR title LIKE '%授信%'
             OR title LIKE '%客户%'
             OR title LIKE '%产能%'
             OR title LIKE '%业绩%'
          )
        ORDER BY importance_score DESC, event_date DESC
        """,
        (start_date, as_of_date),
    ).fetchall()
    research = conn.execute(
        """
        SELECT
            market,
            ticker,
            name,
            published_date AS event_date,
            event_type,
            summary AS title,
            summary,
            source_url,
            quality_score AS importance_score,
            revaluation_tags_json AS tags
        FROM research_events
        WHERE published_date >= ?
          AND published_date <= ?
        ORDER BY quality_score DESC, published_date DESC
        """,
        (start_date, as_of_date),
    ).fetchall()
    rows: list[dict[str, object]] = []
    for row in list(corporate) + list(research):
        rows.append({key: row[key] for key in row.keys()})
    return rows


def _latest_event_by_ticker(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    selected: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row["market"]), str(row["ticker"]))
        current = selected.get(key)
        if current is None:
            selected[key] = row
            continue
        current_score = float(current.get("importance_score") or 0.0)
        row_score = float(row.get("importance_score") or 0.0)
        if (row_score, str(row["event_date"])) > (current_score, str(current["event_date"])):
            selected[key] = row
    return list(selected.values())


def screen_event_catalyst(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for event in _event_rows(conn, as_of_date):
        key = (event["market"], event["ticker"])
        if key in seen:
            continue
        seen.add(key)
        if _is_excluded_name(str(event["name"])):
            continue
        strategy_id = _event_strategy_id(event)
        if strategy_id is None:
            continue
        bar = _latest_price_bar(conn, event["market"], event["ticker"], as_of_date)
        if bar is None:
            continue
        if not _passes_liquidity_filter(bar):
            continue
        prior = _prior_bars(conn, event["market"], event["ticker"], bar["date"], 10)
        if len(prior) < 5:
            continue
        close = float(bar["close"])
        change_pct = float(bar["change_pct"] or 0.0)
        volume_ratio = float(bar["volume"]) / (_volume_average(prior[:10]) or 1.0)
        if change_pct < 0 and volume_ratio < 1.1:
            continue
        low_10 = _recent_low(prior[:10])
        financial_flags = _latest_financial_flags(conn, event["market"], event["ticker"], as_of_date)
        if event["market"] == "CN_A" and not _is_strong_cn_event(dict(event), financial_flags):
            continue
        flow = _latest_money_flow(conn, event["market"], event["ticker"], as_of_date)
        flow_bonus = 0.0
        flow_text = ""
        if flow is not None and flow["net_inflow"] is not None and float(flow["net_inflow"]) > 0:
            flow_bonus = 6.0
            flow_text = f"，资金净流入 {float(flow['net_inflow']) / 10000:.1f}万"

        score = (
            43.0
            + float(event["importance_score"]) * 30.0
            + min(max(change_pct, 0.0) * 1.8, 8.0)
            + min(max(volume_ratio - 1.0, 0.0) * 4.0, 10.0)
            + min(len(financial_flags) * 3.0, 9.0)
            + flow_bonus
        )
        if strategy_id == "us_sec_event_momentum":
            score += 5.0 if "8-K" in str(event["event_type"]) else 2.0
        elif strategy_id == "hk_buyback_recovery":
            score += 4.0
        elif strategy_id == "a_share_hard_event_catalyst":
            score += 3.0
        thesis, risk_notes, action = _event_strategy_profile(strategy_id)
        title = event["title"]
        flags_text = f"；财务支持：{', '.join(financial_flags)}" if financial_flags else ""
        target_1, target_2 = _target_prices(event["market"], close, prior, close * 1.06, close * 1.13)
        stop_loss = max(low_10, close * 0.9)
        if event["market"] == "CN_A":
            stop_loss = _dynamic_cn_a_stop(close, stop_loss, prior)
        reward_risk = (target_1 - close) / (close - stop_loss) if close > stop_loss else 0
        if reward_risk < 1.0:
            continue
        if reward_risk < 1.5:
            score -= 5
        candidates.append(
            _candidate(
                as_of_date=as_of_date,
                market=event["market"],
                ticker=event["ticker"],
                name=event["name"],
                strategy_id=strategy_id,
                score=score,
                action=action,
                close=close,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                thesis=thesis,
                trigger_condition=(
                    f"{event['event_date']} {event['event_type']}：{title}；"
                    f"{bar['date']} 涨幅 {change_pct:.2f}%，量比 {volume_ratio:.2f}{flow_text}{flags_text}。"
                ),
                risk_notes=risk_notes,
                evidence=[
                    {
                        "type": "corporate_event",
                        "title": title,
                        "url": event["source_url"],
                    }
                ],
                data_date=str(bar["date"]),
            )
        )
    return candidates


def _pead_event_rows(conn: sqlite3.Connection, as_of_date: str, lookback_days: int = 5) -> list[dict[str, object]]:
    start_date = _trading_window_start(conn, "CN_A", as_of_date, lookback_days)
    event_type_placeholders = ", ".join("?" for _ in PEAD_EVENT_TYPES)
    keyword_clause = " OR ".join("event_type LIKE ?" for _ in PEAD_EVENT_KEYWORDS)
    rows = conn.execute(
        f"""
        SELECT *
        FROM corporate_events
        WHERE market = 'CN_A'
          AND event_date >= ?
          AND event_date <= ?
          AND (
                UPPER(event_type) IN ({event_type_placeholders})
             OR {keyword_clause}
          )
        ORDER BY event_date DESC, importance_score DESC
        """,
        (start_date, as_of_date, *PEAD_EVENT_TYPES, *[f"%{keyword}%" for keyword in PEAD_EVENT_KEYWORDS]),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def screen_cn_a_pead_quality_surprise(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for event in _latest_event_by_ticker(_pead_event_rows(conn, as_of_date)):
        market = str(event["market"])
        ticker = str(event["ticker"])
        name = str(event["name"])
        if market != "CN_A" or _is_excluded_name(name):
            continue

        model_percentiles = _latest_model_percentiles(conn, market, ticker, as_of_date)
        m1 = model_percentiles.get("M1")
        m2 = model_percentiles.get("M2")
        m3 = model_percentiles.get("M3")
        if not _passes_pead_model_filter(model_percentiles):
            continue

        financial_ok, _has_financial_data, financial_values = _latest_pead_financials(conn, market, ticker, as_of_date)
        if not financial_ok:
            continue

        signal_bar = _latest_price_bar(conn, market, ticker, as_of_date)
        if signal_bar is None:
            continue
        signal_prior = _prior_bars(conn, market, ticker, signal_bar["date"], 25)
        if len(signal_prior) < 20:
            continue

        reaction_bar = _first_price_bar_on_or_after(
            conn, market, ticker, str(event["event_date"]), as_of_date
        )
        if reaction_bar is None:
            reaction_bar = signal_bar
        reaction_prior = _prior_bars(conn, market, ticker, reaction_bar["date"], 20)
        if len(reaction_prior) < 10:
            continue

        close = float(signal_bar["close"])
        reaction_high = float(reaction_bar["high"])
        reaction_low = float(reaction_bar["low"])
        reaction_change_pct = _change_pct_from_prior(reaction_bar, reaction_prior)
        reaction_volume_ratio = float(reaction_bar["volume"]) / (_volume_average(reaction_prior[:10]) or 1.0)
        ma20 = _average_close(signal_prior[:20])
        past_20_return_pct = (close / float(signal_prior[19]["close"]) - 1.0) * 100.0
        avg_amount_20 = _amount_average([signal_bar, *signal_prior[:19]])
        recent_5_with_signal = [signal_bar, *signal_prior[:4]]
        limit_pct = cn_a_limit_pct(ticker, name) * 100.0
        limit_up_count_5 = sum(
            1 for row in recent_5_with_signal if float(row["change_pct"] or 0.0) >= limit_pct * 0.98
        )

        if not (-2.0 <= reaction_change_pct <= 7.0):
            continue
        if reaction_low >= reaction_high * 0.97:
            continue
        if not (1.2 <= reaction_volume_ratio <= 2.8):
            continue
        if close <= ma20:
            continue
        if past_20_return_pct > 25.0:
            continue
        if limit_up_count_5 > 1:
            continue
        if avg_amount_20 is not None and avg_amount_20 <= PEAD_MIN_AVG_AMOUNT:
            continue

        score = 60.0
        net_profit_growth = financial_values.get("net_profit_growth")
        revenue_growth = financial_values.get("revenue_growth")
        roe_ttm = financial_values.get("roe_ttm")
        if net_profit_growth is not None:
            score += min(15.0, 5.0 + max(net_profit_growth - 25.0, 0.0) / 10.0)
        if revenue_growth is not None:
            score += min(8.0, 3.0 + max(revenue_growth - 10.0, 0.0) / 8.0)
        if 1.5 <= reaction_volume_ratio <= 2.0:
            score += 5.0
        elif 1.2 <= reaction_volume_ratio <= 2.4:
            score += 3.0
        distance_to_ma20_pct = (close / ma20 - 1.0) * 100.0
        if 0.0 <= distance_to_ma20_pct <= 3.0:
            score += 5.0
        elif 0.0 <= distance_to_ma20_pct <= 8.0:
            score += 3.0
        score = min(score, 100.0)

        stop_loss = max(reaction_low, close * 0.94)
        if stop_loss >= close:
            continue
        target_1 = close * 1.08
        target_2 = close * 1.15
        reward_risk = (target_1 - close) / (close - stop_loss) if close > stop_loss else 0.0
        if reward_risk < 1.0:
            continue

        financial_parts = []
        if net_profit_growth is not None:
            financial_parts.append(f"净利润增长 {net_profit_growth:.1f}%")
        if revenue_growth is not None:
            financial_parts.append(f"营收增长 {revenue_growth:.1f}%")
        if roe_ttm is not None:
            financial_parts.append(f"ROE {roe_ttm:.1f}%")
        financial_text = "；财务过滤：" + "，".join(financial_parts) if financial_parts else "；财务数据缺失，跳过基本面硬过滤"
        model_text = (
            f"；模型分 M1 {(m1 or 0.0) * 100:.1f}%，"
            f"M2 {(m2 or 0.0) * 100:.1f}%，M3 {(m3 or 0.0) * 100:.1f}%"
        )
        title = str(event["title"])

        candidates.append(
            _candidate(
                as_of_date=as_of_date,
                market=market,
                ticker=ticker,
                name=name,
                strategy_id="cn_a_pead_quality_surprise",
                score=score,
                action="WATCH_OR_BUY_ON_CONFIRMATION",
                close=close,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                thesis=(
                    "近期财报/业绩类公告后价格反应温和，盈利质量和模型分通过硬过滤，"
                    "市场可能仍在消化超预期信息。"
                ),
                trigger_condition=(
                    f"{event['event_date']} {event['event_type']}：{title}；"
                    f"{reaction_bar['date']} 公告后反应涨幅 {reaction_change_pct:.2f}%，"
                    f"量比10日 {reaction_volume_ratio:.2f}，当日区间 {reaction_low:.2f}-{reaction_high:.2f}；"
                    f"{signal_bar['date']} 收盘 {close:.2f}，距MA20 {distance_to_ma20_pct:.1f}%，"
                    f"近20日涨幅 {past_20_return_pct:.1f}%，近5日涨停 {limit_up_count_5} 次，"
                    f"20日均额 {(avg_amount_20 or 0.0) / 10000:.0f}万{financial_text}{model_text}。"
                ),
                risk_notes=(
                    "财报事件需要人工复核实际值相对预告或一致预期的超预期幅度；"
                    "若跌破公告反应日低点、-6%止损线或MA20，应剔除。"
                ),
                evidence=[
                    {
                        "type": "corporate_event",
                        "title": title,
                        "url": event.get("source_url"),
                    },
                    {
                        "type": "price_action",
                        "title": "公告后温和反应且未过度延伸",
                        "url": None,
                    },
                    {
                        "type": "model_filter",
                        "model_percentile_1": m1,
                        "model_percentile_2": m2,
                        "model_percentile_3": m3,
                    },
                ],
                data_date=str(signal_bar["date"]),
            )
        )
    return candidates


def screen_xingye_style_prepositioning(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for event in _latest_event_by_ticker(_xingye_event_rows(conn, as_of_date)):
        market = str(event["market"])
        ticker = str(event["ticker"])
        if market != "CN_A":
            continue
        if _is_excluded_name(str(event["name"])):
            continue
        if _is_weak_xingye_event(event):
            continue
        event_strength = float(event.get("importance_score") or 0.0)
        if event_strength < 0.74:
            continue
        bar = _latest_price_bar(conn, market, ticker, as_of_date)
        if bar is None:
            continue
        if not _passes_liquidity_filter(bar):
            continue
        prior = _prior_bars(conn, market, ticker, bar["date"], 20)
        if len(prior) < 10:
            continue

        close = float(bar["close"])
        open_price = float(bar["open"])
        low = float(bar["low"])
        change_pct = float(bar["change_pct"] or 0.0)
        recent_5 = prior[:5]
        recent_10 = prior[:10]
        recent_20 = prior[:20] if len(prior) >= 20 else prior
        volume_ratio_5 = float(bar["volume"]) / (_volume_average(recent_5) or 1.0)
        volume_ratio_20 = float(bar["volume"]) / (_volume_average(recent_20) or 1.0)
        up_days_10 = sum(1 for row in recent_10 if float(row["close"]) > float(row["open"]))
        platform_range_pct = (_recent_high(recent_10) / _recent_low(recent_10) - 1.0) * 100.0
        pre_volume_lift = (_volume_average(recent_5) or 0.0) / (_volume_average(recent_10) or 1.0)
        prior_big_up_days = sum(1 for row in recent_5 if float(row["change_pct"] or 0.0) >= 5.0)
        high_10 = _recent_high(recent_10)
        high_20 = _recent_high(recent_20)
        low_10 = _recent_low(recent_10)
        ma_10 = _average_close(recent_10)
        prior_5_return_pct = (float(recent_5[0]["close"]) / float(recent_5[-1]["close"]) - 1.0) * 100.0
        close_from_low_10_pct = (close / low_10 - 1.0) * 100.0

        first_sun = close > open_price and 2.5 <= change_pct <= 7.2 and 1.45 <= volume_ratio_5 <= 3.2
        near_breakout = close >= high_10 * 0.975 or close >= high_20 * 0.95
        base_ok = platform_range_pct <= 14.0 and close <= high_20 * 1.02 and close_from_low_10_pct <= 16.0
        accumulation_ok = up_days_10 >= 2 and 0.8 <= pre_volume_lift <= 2.2 and prior_big_up_days <= 1
        not_single_day_blowoff = volume_ratio_20 <= 3.8 and prior_5_return_pct <= 12.0
        trend_floor_ok = close >= ma_10 and low >= low_10 * 0.98
        if not (
            first_sun
            and near_breakout
            and base_ok
            and accumulation_ok
            and not_single_day_blowoff
            and trend_floor_ok
        ):
            continue

        financial_flags = _latest_financial_flags(conn, market, ticker, as_of_date)
        if not _has_revaluation_mapping(event, financial_flags):
            continue
        flow = _latest_money_flow(conn, market, ticker, as_of_date)
        flow_bonus = 0.0
        flow_text = ""
        if flow is not None and flow["net_inflow"] is not None and float(flow["net_inflow"]) > 0:
            flow_bonus = 5.0
            flow_text = f"，资金净流入 {float(flow['net_inflow']) / 10000:.1f}万"

        score = (
            38.0
            + float(event.get("importance_score") or 0.0) * 22.0
            + min(volume_ratio_5 * 8.0, 18.0)
            + min(volume_ratio_20 * 5.0, 10.0)
            + (8.0 if near_breakout else 0.0)
            + (7.0 if accumulation_ok else 0.0)
            + min(len(financial_flags) * 3.0, 9.0)
            + flow_bonus
        )
        action = "WATCH_CONFIRMATION"
        flags_text = f"；财务支持：{', '.join(financial_flags)}" if financial_flags else ""
        title = str(event["title"])
        target_1, target_2 = _target_prices(market, close, prior, close * 1.07, close * 1.16)
        stop_loss = _dynamic_cn_a_stop(close, max(low_10, low, close * 0.91), prior)
        reward_risk = (target_1 - close) / (close - stop_loss) if close > stop_loss else 0
        if reward_risk < 1.0:
            continue
        if reward_risk < 1.5:
            score -= 5
        candidates.append(
            _candidate(
                as_of_date=as_of_date,
                market=market,
                ticker=ticker,
                name=str(event["name"]),
                strategy_id="xingye_style_prepositioning",
                score=score,
                action=action,
                close=close,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                thesis=(
                    "事件披露后出现可解释业务重估的新线索，股价没有立即高潮，而是在平台内吸筹整理；"
                    "当前放量首阳或临界突破，形态接近兴业科技式重估启动。"
                ),
                trigger_condition=(
                    f"{event['event_date']} {event['event_type']}：{title}；"
                    f"{bar['date']} 涨幅 {change_pct:.2f}%，量比5日 {volume_ratio_5:.2f}，"
                    f"量比20日 {volume_ratio_20:.2f}，10日红盘 {up_days_10}/10，"
                    f"平台振幅 {platform_range_pct:.1f}%，近5日涨幅 {prior_5_return_pct:.1f}%，"
                    f"距10日低点 {close_from_low_10_pct:.1f}%，近5日大阳 {prior_big_up_days} 天{flow_text}{flags_text}。"
                ),
                risk_notes=(
                    "这是资金提前介入的量价代理信号，不等于能确认主力身份；首次触发只进入观察，"
                    "次日不破启动日中枢或继续放量承接才升级，跌破启动日低点应剔除。"
                ),
                evidence=[
                    {
                        "type": "event_cluster",
                        "title": title,
                        "url": event.get("source_url"),
                    },
                    {
                        "type": "price_action",
                        "title": "平台整理后的放量首阳/临界突破",
                        "url": None,
                    },
                ],
                data_date=str(bar["date"]),
                trailing_stop_pct=3.0,
                trailing_activation_pct=8.0,
            )
        )
    return candidates


def screen_trend_breakout(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for item in _price_universe(conn):
        if _is_excluded_name(str(item["name"])):
            continue
        bar = _latest_price_bar(conn, item["market"], item["ticker"], as_of_date)
        if bar is None:
            continue
        if not _passes_liquidity_filter(bar):
            continue
        prior = _prior_bars(conn, item["market"], item["ticker"], bar["date"], 30)
        if len(prior) < 20:
            continue
        recent_20 = prior[:20]
        recent_10 = prior[:10]
        volume_ratio = float(bar["volume"]) / (_volume_average(recent_20) or 1.0)
        close = float(bar["close"])
        high_20 = _recent_high(recent_20)
        low_10 = _recent_low(recent_10)
        ma_20 = _average_close(recent_20)
        change_pct = float(bar["change_pct"] or 0.0)
        breakout = close >= high_20 * 0.998 and close > ma_20 and volume_ratio >= 1.15 and change_pct > 0.8
        if not breakout:
            continue
        regime = _market_regime(conn, item["market"], as_of_date)
        if regime == "BEAR":
            continue
        volume_bonus = min(volume_ratio * 9.0, 18.0) if volume_ratio <= 2.5 else max(18.0 - (volume_ratio - 2.5) * 12.0, 0.0)
        score = 52.0 + volume_bonus + min(max((close / high_20 - 1.0) * 500.0, 0), 10.0)
        if regime == "NEAR_MA":
            score *= 0.8
        action = "WATCH_PULLBACK" if change_pct >= 8.5 else "BUY_CANDIDATE"
        target_1, target_2 = _target_prices(item["market"], close, prior, close * 1.08, close * 1.16)
        stop_loss = _dynamic_cn_a_stop(close, max(low_10, close * 0.92), prior)
        reward_risk_val = (target_1 - close) / (close - stop_loss) if close > stop_loss else 0
        if reward_risk_val < 1.0:
            continue
        if reward_risk_val < 1.5:
            score -= 5
        candidates.append(
            _candidate(
                as_of_date=as_of_date,
                market=item["market"],
                ticker=item["ticker"],
                name=item["name"],
                strategy_id="trend_breakout",
                score=score,
                action=action,
                close=close,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                thesis="收盘价突破近20日高位区，且成交量放大，趋势可能进入延续段。",
                trigger_condition=(
                    f"{bar['date']} 收盘 {close:.2f}，近20日高点 {high_20:.2f}，"
                    f"成交量为20日均量 {volume_ratio:.2f} 倍，涨幅 {change_pct:.2f}%。"
                ),
                risk_notes=(
                    "强趋势策略容易拥挤；单日涨幅过大时不追高，优先等待回踩或次日不破突破位确认。"
                    "若跌回突破位或成交量无法延续，应快速降级。"
                ),
                evidence=[{"type": "price_action", "title": "20日突破与放量确认", "url": None},
                          {"type": "breakout_reference", "breakout_volume": float(bar["volume"]),
                           "breakout_close": close, "avg_volume_10d": _volume_average(recent_10)}],
                data_date=str(bar["date"]),
                trailing_stop_pct=3.0,
                trailing_activation_pct=8.0,
            )
        )
    return candidates


def screen_abnormal_volume(conn: sqlite3.Connection, as_of_date: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for item in _price_universe(conn):
        if _is_excluded_name(str(item["name"])):
            continue
        bar = _latest_price_bar(conn, item["market"], item["ticker"], as_of_date)
        if bar is None:
            continue
        if not _passes_liquidity_filter(bar):
            continue
        prior = _prior_bars(conn, item["market"], item["ticker"], bar["date"], 20)
        if len(prior) < 10:
            continue
        volume_ratio = float(bar["volume"]) / (_volume_average(prior[:10]) or 1.0)
        change_pct = float(bar["change_pct"] or 0.0)
        close = float(bar["close"])
        open_price = float(bar["open"])
        low_10 = _recent_low(prior[:10])
        abnormal = close > open_price and 2.8 <= change_pct <= 8.5 and volume_ratio >= 1.65
        if not abnormal:
            continue
        regime = _market_regime(conn, item["market"], as_of_date)
        if regime == "BEAR":
            continue
        volume_bonus = min(volume_ratio * 12.0, 20.0) if volume_ratio <= 2.5 else max(20.0 - (volume_ratio - 2.5) * 15.0, 0.0)
        score = 45.0 + volume_bonus + min(change_pct * 2.0, 15.0)
        if regime == "NEAR_MA":
            score *= 0.8
        target_1, target_2 = _target_prices(item["market"], close, prior, close * 1.07, close * 1.14)
        stop_loss = _dynamic_cn_a_stop(close, max(low_10, close * 0.9), prior)
        reward_risk_val = (target_1 - close) / (close - stop_loss) if close > stop_loss else 0
        if reward_risk_val < 1.0:
            continue
        if reward_risk_val < 1.5:
            score -= 5
        candidates.append(
            _candidate(
                as_of_date=as_of_date,
                market=item["market"],
                ticker=item["ticker"],
                name=item["name"],
                strategy_id="abnormal_volume_small_midcap",
                score=score,
                action="WATCH_OR_BUY_ON_CONFIRMATION",
                close=close,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                thesis="价格出现非极端放量中阳，可能反映资金开始试探或新催化被交易。",
                trigger_condition=(
                    f"{bar['date']} 涨幅 {change_pct:.2f}%，成交量为近10日均量 {volume_ratio:.2f} 倍。"
                ),
                risk_notes="异常放量误报很多，必须结合公告、行业催化或后续不破启动日低点确认。",
                evidence=[{"type": "price_action", "title": "异常放量中阳", "url": None}],
                data_date=str(bar["date"]),
            )
        )
    return candidates


def clear_candidates_for_date(conn: sqlite3.Connection, as_of_date: str) -> None:
    candidate_ids = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM candidates WHERE as_of_date = ?", (as_of_date,)).fetchall()
    ]
    if candidate_ids:
        placeholders = ", ".join("?" for _ in candidate_ids)
        conn.execute(
            f"DELETE FROM candidate_horizon_evaluations WHERE candidate_id IN ({placeholders})",
            tuple(candidate_ids),
        )
        conn.execute(
            f"DELETE FROM candidate_evaluations WHERE candidate_id IN ({placeholders})",
            tuple(candidate_ids),
        )
    conn.execute("DELETE FROM candidates WHERE as_of_date = ?", (as_of_date,))


def screen_all(conn: sqlite3.Connection, as_of_date: str, replace_existing: bool = True) -> int:
    candidates = (
        screen_event_catalyst(conn, as_of_date)
        + screen_cn_a_pead_quality_surprise(conn, as_of_date)
        + screen_xingye_style_prepositioning(conn, as_of_date)
        + screen_trend_breakout(conn, as_of_date)
        + screen_abnormal_volume(conn, as_of_date)
    )
    try:
        from .alpha_factors import attach_model_scores
        candidates = attach_model_scores(conn, as_of_date, candidates)
    except Exception:
        pass  # Model scores are optional; screener works without them
    candidates.sort(key=lambda c: float(c.get("candidate_score", 0)), reverse=True)
    sector_counts: dict[str, int] = {}
    filtered: list[dict[str, object]] = []
    for c in candidates:
        ticker = str(c.get("ticker", ""))
        sector_key = ticker[:3] if c.get("market") == "CN_A" and len(ticker) >= 3 else ticker
        count = sector_counts.get(sector_key, 0)
        if count >= 2:
            continue
        sector_counts[sector_key] = count + 1
        filtered.append(c)
    candidates = filtered
    with conn:
        if replace_existing:
            clear_candidates_for_date(conn, as_of_date)
        for row in candidates:
            conn.execute(
                """
                INSERT INTO candidates (
                    as_of_date, market, ticker, name, strategy_id, candidate_score,
                    action, entry_price, signal_close, buy_zone_low, buy_zone_high, stop_loss,
                    target_1, target_2, reward_risk_ratio, trailing_stop_pct, trailing_activation_pct,
                    thesis, trigger_condition, risk_notes,
                    evidence_json, status, confirmation_status, data_date, created_at,
                    model_score, model_percentile, model_score_2, model_percentile_2,
                    model_score_3, model_percentile_3
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(as_of_date, market, ticker, strategy_id) DO UPDATE SET
                    candidate_score=excluded.candidate_score,
                    action=excluded.action,
                    entry_price=excluded.entry_price,
                    signal_close=excluded.signal_close,
                    model_score=excluded.model_score,
                    model_percentile=excluded.model_percentile,
                    model_score_2=excluded.model_score_2,
                    model_percentile_2=excluded.model_percentile_2,
                    model_score_3=excluded.model_score_3,
                    model_percentile_3=excluded.model_percentile_3,
                    buy_zone_low=excluded.buy_zone_low,
                    buy_zone_high=excluded.buy_zone_high,
                    stop_loss=excluded.stop_loss,
                    target_1=excluded.target_1,
                    target_2=excluded.target_2,
                    reward_risk_ratio=excluded.reward_risk_ratio,
                    trailing_stop_pct=excluded.trailing_stop_pct,
                    trailing_activation_pct=excluded.trailing_activation_pct,
                    thesis=excluded.thesis,
                    trigger_condition=excluded.trigger_condition,
                    risk_notes=excluded.risk_notes,
                    evidence_json=excluded.evidence_json,
                    status=excluded.status,
                    confirmation_status=excluded.confirmation_status,
                    data_date=excluded.data_date,
                    created_at=excluded.created_at
                """,
                (
                    row["as_of_date"],
                    row["market"],
                    row["ticker"],
                    row["name"],
                    row["strategy_id"],
                    row["candidate_score"],
                    row["action"],
                    row["entry_price"],
                    row.get("signal_close", row["entry_price"]),
                    row["buy_zone_low"],
                    row["buy_zone_high"],
                    row["stop_loss"],
                    row["target_1"],
                    row["target_2"],
                    row["reward_risk_ratio"],
                    row.get("trailing_stop_pct"),
                    row.get("trailing_activation_pct"),
                    row["thesis"],
                    row["trigger_condition"],
                    row["risk_notes"],
                    row["evidence_json"],
                    row["status"],
                    row["confirmation_status"],
                    row["data_date"],
                    row["created_at"],
                    row.get("model_score"),
                    row.get("model_percentile"),
                    row.get("model_score_2"),
                    row.get("model_percentile_2"),
                    row.get("model_score_3"),
                    row.get("model_percentile_3"),
                ),
            )
    return len(candidates)


def compute_intraday_metrics(bars: list) -> dict:
    """Compute VWAP, last close, high/low from intraday bar rows.

    Accepts rows with columns: open, close, high, low, volume, amount.
    Returns dict with keys: vwap, last_close, intraday_high, intraday_low.
    """
    total_volume = sum(float(row["volume"] or 0.0) for row in bars)
    total_amount = sum(float(row["amount"] or 0.0) for row in bars if row["amount"] is not None)
    weighted_close = sum(float(row["close"]) * float(row["volume"] or 0.0) for row in bars)
    avg_close = weighted_close / total_volume if total_volume > 0 else float(bars[-1]["close"])
    if total_volume > 0 and total_amount > 0:
        vwap = total_amount / total_volume
        if avg_close > 0 and vwap > avg_close * 10:
            vwap /= 100.0
        if avg_close > 0 and not (avg_close * 0.2 <= vwap <= avg_close * 5):
            vwap = avg_close
    else:
        vwap = avg_close
    last_close = float(bars[-1]["close"])
    intraday_high = max(float(row["high"]) for row in bars)
    intraday_low = min(float(row["low"]) for row in bars)
    return {
        "vwap": vwap,
        "last_close": last_close,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
    }


def compute_intraday_conclusion(bars: list) -> str:
    """Compute a simple intraday conclusion string from 1-minute bar rows.

    Returns a Chinese-limited conclusion string joined by '；', or empty string
    if bars are empty or no conclusions apply.  No buy zones, targets, or
    risk-reward — conclusions only.
    """
    if not bars:
        return ""
    metrics = compute_intraday_metrics(bars)
    vwap = metrics["vwap"]
    last_close = metrics["last_close"]
    intraday_high = metrics["intraday_high"]
    intraday_low = metrics["intraday_low"]
    intraday_range = intraday_high - intraday_low if intraday_high > intraday_low else 0.0
    conclusions: list[str] = []
    if vwap > 0 and intraday_range > 0:
        if last_close >= vwap:
            conclusions.append("VWAP 支撑")
        else:
            conclusions.append("VWAP 下方运行")
    if intraday_high > 0:
        close_vs_high_pct = (intraday_high - last_close) / intraday_high * 100
        if close_vs_high_pct <= 0.5:
            conclusions.append("强势收盘")
        elif close_vs_high_pct >= 5.0:
            conclusions.append("尾盘回落")
    if intraday_high > 0 and intraday_range > 0:
        pullback_pct = (intraday_high - last_close) / intraday_high * 100
        if pullback_pct >= 3.0:
            conclusions.append(f"高点回撤 {pullback_pct:.1f}%")
    if intraday_range > 0:
        mid = (intraday_high + intraday_low) / 2
        if last_close < mid and last_close > 0:
            conclusions.append("弱势收盘")
    return "；".join(conclusions)


def fetch_intraday_bars(
    conn: sqlite3.Connection, market: str, ticker: str, date: str,
) -> list:
    """Fetch 1-minute intraday bars for a given market/ticker/date from DB."""
    return conn.execute(
        """
        SELECT open, close, high, low, volume, amount
        FROM intraday_bars
        WHERE market = ? AND ticker = ? AND date = ?
        ORDER BY datetime
        """,
        (market, ticker, date),
    ).fetchall()


def refine_candidates_with_intraday(conn: sqlite3.Connection, as_of_date: str) -> int:
    """Use same-day intraday bars to refine candidate buy zones before report output.

    This keeps screening based on daily/event/model signals, then uses intraday
    microstructure only as execution context for the next trading day.
    """
    rows = conn.execute(
        """
        SELECT *
        FROM candidates
        WHERE market = 'CN_A'
          AND (as_of_date = ? OR confirmation_date = ?)
          AND (
              action = 'BUY_CANDIDATE'
              OR action = 'WATCH_PULLBACK'
              OR action LIKE '%CONFIRM%'
              OR confirmation_status = 'CONFIRMED'
          )
        """,
        (as_of_date, as_of_date),
    ).fetchall()
    updated = 0
    for candidate in rows:
        bars = fetch_intraday_bars(conn, candidate["market"], candidate["ticker"], as_of_date)
        if not bars:
            continue
        metrics = compute_intraday_metrics(bars)
        vwap = metrics["vwap"]
        last_close = metrics["last_close"]
        intraday_high = metrics["intraday_high"]
        intraday_low = metrics["intraday_low"]
        stop_loss = float(candidate["stop_loss"] or 0.0)
        target_1 = float(candidate["target_1"] or 0.0)
        signal_close = float(candidate["signal_close"] or candidate["entry_price"] or last_close)

        anchor = min(last_close, vwap) if vwap > 0 else last_close
        upper_anchor = max(last_close, vwap, signal_close)
        buy_zone_low = round(max(stop_loss * 1.01 if stop_loss > 0 else 0.0, anchor * 0.985), 2)
        buy_zone_high = round(min(upper_anchor * 1.01, signal_close * 1.02), 2)
        if buy_zone_high <= buy_zone_low:
            buy_zone_high = round(buy_zone_low * 1.015, 2)
        reward_risk = (
            round((target_1 - buy_zone_high) / (buy_zone_high - stop_loss), 2)
            if target_1 > 0 and stop_loss > 0 and buy_zone_high > stop_loss
            else float(candidate["reward_risk_ratio"] or 0.0)
        )
        conclusion_str = compute_intraday_conclusion(bars)
        note = (
            f"分时复核：VWAP {vwap:.2f}，尾盘 {last_close:.2f}，"
            f"日内区间 {intraday_low:.2f}-{intraday_high:.2f}，"
            f"次日参考买入区间 {buy_zone_low:.2f}-{buy_zone_high:.2f}。"
        )
        if conclusion_str:
            note += f" 分时结论：{conclusion_str}。"
        risk_notes = str(candidate["risk_notes"] or "")
        if "分时复核：" not in risk_notes:
            risk_notes = f"{risk_notes} {note}".strip()
        try:
            evidence = json.loads(candidate["evidence_json"] or "[]")
            if not isinstance(evidence, list):
                evidence = []
        except Exception:
            evidence = []
        evidence = [
            item for item in evidence
            if not (isinstance(item, dict) and item.get("type") == "intraday_execution_context")
        ]
        evidence.append(
            {
                "type": "intraday_execution_context",
                "date": as_of_date,
                "vwap": round(vwap, 4),
                "last_close": round(last_close, 4),
                "intraday_high": round(intraday_high, 4),
                "intraday_low": round(intraday_low, 4),
                "buy_zone_low": buy_zone_low,
                "buy_zone_high": buy_zone_high,
                "conclusions": conclusion_str.split("；") if conclusion_str else [],
            }
        )
        conn.execute(
            """
            UPDATE candidates
            SET buy_zone_low = ?,
                buy_zone_high = ?,
                reward_risk_ratio = ?,
                risk_notes = ?,
                evidence_json = ?
            WHERE id = ?
            """,
            (
                buy_zone_low,
                buy_zone_high,
                reward_risk,
                risk_notes,
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                candidate["id"],
            ),
        )
        updated += 1
    conn.commit()
    return updated


def latest_candidates(conn: sqlite3.Connection, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, s.name AS strategy_name
        FROM candidates c
        JOIN strategies s ON s.id = c.strategy_id
        WHERE c.as_of_date = ?
          AND s.status != 'RETIRED'
        ORDER BY c.candidate_score DESC, c.ticker
        """,
        (as_of_date,),
    ).fetchall()


def confirm_candidates(conn: sqlite3.Connection, as_of_date: str) -> tuple[int, int]:
    pending = conn.execute(
        """
        SELECT c.* FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        WHERE c.as_of_date < ?
          AND c.action LIKE '%CONFIRM%'
          AND c.status = 'WATCHLIST'
          AND COALESCE(c.confirmation_status, 'PENDING') = 'PENDING'
          AND st.status != 'RETIRED'
        """,
        (as_of_date,),
    ).fetchall()

    confirmed = 0
    cancelled = 0
    for candidate in pending:
        reference_date = str(candidate["data_date"] or candidate["as_of_date"])
        next_date = _next_price_date(conn, candidate["market"], candidate["ticker"], reference_date)
        if next_date is None or next_date > as_of_date:
            continue
        bar = _price_bar(conn, candidate["market"], candidate["ticker"], next_date)
        prev_bar = _price_bar(conn, candidate["market"], candidate["ticker"], reference_date)
        if bar is None or prev_bar is None:
            conn.execute(
                "UPDATE candidates SET status = 'CANCELLED', confirmation_status = 'CANCELLED', confirmation_date = ?, confirmation_reason = ? WHERE id = ?",
                (next_date or as_of_date, "次日无行情数据", int(candidate["id"])),
            )
            cancelled += 1
            continue

        close = float(bar["close"])
        volume = float(bar["volume"])
        low = float(bar["low"])
        prev_close = float(prev_bar["close"])
        prev_volume = float(prev_bar["volume"])
        stop = float(candidate["stop_loss"]) if candidate["stop_loss"] else None

        reasons = []
        confirmed_ok = True
        if close < prev_close:
            confirmed_ok = False
            reasons.append(f"次日收盘{close:.2f}低于候选日收盘{prev_close:.2f}")
        if volume < prev_volume * 0.8:
            confirmed_ok = False
            volume_ratio = volume / prev_volume * 100 if prev_volume else 0.0
            reasons.append(f"次日成交量{volume:.0f}缩量至候选日{volume_ratio:.0f}%")
        if stop is not None and low <= stop:
            confirmed_ok = False
            reasons.append(f"次日最低{low:.2f}跌破止损{stop:.2f}")

        if confirmed_ok:
            original_entry = float(candidate["entry_price"])
            scale = close / original_entry if original_entry > 0 else 1.0
            new_stop = round(float(candidate["stop_loss"]) * scale, 2) if candidate["stop_loss"] else None
            new_target_1 = round(float(candidate["target_1"]) * scale, 2) if candidate["target_1"] else None
            new_target_2 = round(float(candidate["target_2"]) * scale, 2) if candidate["target_2"] else None
            new_bz_low = round(close * 0.985, 2)
            new_bz_high = round(close * 1.015, 2)
            new_rrr = (new_target_1 - close) / (close - new_stop) if new_stop and close > new_stop and new_target_1 else 0.0
            conn.execute(
                "UPDATE candidates SET status = 'CONFIRMED', confirmation_status = 'CONFIRMED', "
                "confirmation_date = ?, confirmation_reason = ?, entry_price = ?, "
                "stop_loss = ?, target_1 = ?, target_2 = ?, "
                "buy_zone_low = ?, buy_zone_high = ?, reward_risk_ratio = ? WHERE id = ?",
                (next_date, "次日价格承接、未缩量、未破止损", close,
                 new_stop, new_target_1, new_target_2,
                 new_bz_low, new_bz_high, round(new_rrr, 2), int(candidate["id"])),
            )
            confirmed += 1
        else:
            reason_text = "；".join(reasons)
            conn.execute(
                "UPDATE candidates SET status = 'CANCELLED', confirmation_status = 'CANCELLED', confirmation_date = ?, confirmation_reason = ? WHERE id = ?",
                (next_date, reason_text, int(candidate["id"])),
            )
            cancelled += 1

    conn.commit()
    return confirmed, cancelled


def _count_trading_days(conn: sqlite3.Connection, market: str, ticker: str, start_date: str, end_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT date) as cnt FROM price_bars WHERE market=? AND ticker=? AND date > ? AND date <= ?",
        (market, ticker, start_date, end_date),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def _extract_breakout_volume(evidence_json: str) -> float | None:
    try:
        import json
        evidence = json.loads(evidence_json)
        for item in evidence:
            if item.get("type") == "breakout_reference":
                return float(item.get("breakout_volume", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _bars_for_candidate(conn: sqlite3.Connection, candidate: sqlite3.Row, as_of_date: str) -> list[sqlite3.Row]:
    data_date = str(candidate["data_date"] or candidate["as_of_date"])
    return conn.execute(
        "SELECT date, open, close, high, low, volume FROM price_bars "
        "WHERE market=? AND ticker=? AND date > ? AND date <= ? ORDER BY date",
        (str(candidate["market"]), str(candidate["ticker"]), data_date, as_of_date),
    ).fetchall()


def confirm_pullback_candidates(
    conn: sqlite3.Connection,
    as_of_date: str,
    *,
    pullback_min_pct: float = 5.0,
    pullback_max_pct: float = 10.0,
    volume_shrink_ratio: float = 0.50,
    window_days: int = 10,
) -> tuple[int, int, int]:
    pending = conn.execute(
        """
        SELECT c.* FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        WHERE c.action = 'WATCH_PULLBACK'
          AND COALESCE(c.confirmation_status, 'PENDING') = 'PENDING'
          AND c.status = 'WATCHLIST'
          AND st.status != 'RETIRED'
        """,
    ).fetchall()

    confirmed = 0
    cancelled = 0
    waiting = 0

    for candidate in pending:
        data_date = str(candidate["data_date"] or candidate["as_of_date"])
        entry_price = float(candidate["entry_price"])
        stop_loss = float(candidate["stop_loss"]) if candidate["stop_loss"] else 0.0
        breakout_volume = _extract_breakout_volume(str(candidate["evidence_json"]))

        days_since = _count_trading_days(
            conn, str(candidate["market"]), str(candidate["ticker"]),
            data_date, as_of_date,
        )

        if days_since > window_days:
            conn.execute(
                "UPDATE candidates SET status='CANCELLED', confirmation_status='CANCELLED', "
                "confirmation_date=?, confirmation_reason=? WHERE id=?",
                (as_of_date, f"回调确认超时：{days_since}个交易日未满足三日确认形态", int(candidate["id"])),
            )
            cancelled += 1
            continue

        bars = _bars_for_candidate(conn, candidate, as_of_date)
        if len(bars) < 3:
            waiting += 1
            continue

        found = False
        for i in range(len(bars) - 2):
            day_t = bars[i]
            day_t1 = bars[i + 1]
            day_t2 = bars[i + 2]

            t_close = float(day_t["close"])
            t_low = float(day_t["low"])
            t_volume = float(day_t["volume"] or 0)
            t1_open = float(day_t1["open"])
            t1_close = float(day_t1["close"])
            t1_low = float(day_t1["low"])
            t1_high = float(day_t1["high"])
            t2_close = float(day_t2["close"])

            pullback_pct = (entry_price - t_close) / entry_price * 100.0 if entry_price > 0 else 0.0
            if not (pullback_min_pct <= pullback_pct <= pullback_max_pct):
                continue

            if breakout_volume and breakout_volume > 0:
                if t_volume >= breakout_volume * volume_shrink_ratio:
                    continue

            if stop_loss > 0 and t_low <= stop_loss:
                continue

            if day_t1["low"] < day_t["low"]:
                continue
            if t1_close < t1_open:
                continue

            if t2_close <= t1_close:
                continue

            confirm_date = str(day_t2["date"])
            original_entry = float(candidate["entry_price"])
            scale = t2_close / original_entry if original_entry > 0 else 1.0
            new_stop = round(stop_loss * scale, 2) if stop_loss > 0 else None
            new_target_1 = round(float(candidate["target_1"]) * scale, 2) if candidate["target_1"] else None
            new_target_2 = round(float(candidate["target_2"]) * scale, 2) if candidate["target_2"] else None
            new_bz_low = round(t2_close * 0.985, 2)
            new_bz_high = round(t2_close * 1.015, 2)
            new_rrr = (new_target_1 - t2_close) / (t2_close - new_stop) if new_stop and t2_close > new_stop and new_target_1 else 0.0
            conn.execute(
                "UPDATE candidates SET action='BUY_CANDIDATE', confirmation_status='CONFIRMED', "
                "confirmation_date=?, confirmation_reason=?, entry_price=?, "
                "stop_loss=?, target_1=?, target_2=?, "
                "buy_zone_low=?, buy_zone_high=?, reward_risk_ratio=? WHERE id=?",
                (
                    confirm_date,
                    f"三日确认：T日回调{pullback_pct:.1f}%缩量，T+1企稳不创新低，T+2回升收盘{t2_close:.2f}>{t1_close:.2f}",
                    t2_close, new_stop, new_target_1, new_target_2,
                    new_bz_low, new_bz_high, round(new_rrr, 2),
                    int(candidate["id"]),
                ),
            )
            confirmed += 1
            found = True
            break

        if not found:
            waiting += 1

    conn.commit()
    return confirmed, cancelled, waiting
