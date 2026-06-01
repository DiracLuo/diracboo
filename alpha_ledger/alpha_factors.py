"""Cross-sectional alpha factors for candidate ranking.

Inspired by Vibe-Trading's Alpha Zoo (WorldQuant 101, Qlib 158).
These factors rank all stocks on a given date; higher rank = stronger signal.
Used to adjust candidate scores after screening, not as standalone signals.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class FactorResult:
    ticker: str
    factor_name: str
    raw_value: float
    rank_pct: float  # 0-100, percentile rank within universe


def _ts_max(values: list[float], window: int) -> float:
    return max(values[-window:]) if len(values) >= window else max(values) if values else 0.0


def _ts_mean(values: list[float], window: int) -> float:
    subset = values[-window:]
    return sum(subset) / len(subset) if subset else 0.0


def _ts_std(values: list[float], window: int) -> float:
    subset = values[-window:]
    if len(subset) < 2:
        return 0.0
    mean = sum(subset) / len(subset)
    return (sum((x - mean) ** 2 for x in subset) / len(subset)) ** 0.5


def _rank_pct(values: list[float]) -> list[float]:
    """Assign percentile ranks (0-100) to a list of values. Higher value = higher rank."""
    if not values:
        return []
    sorted_vals = sorted(enumerate(values), key=lambda x: x[1])
    n = len(sorted_vals)
    ranks = [0.0] * n
    for rank, (idx, _) in enumerate(sorted_vals):
        ranks[idx] = rank / max(1, n - 1) * 100.0
    return ranks


def _load_bars_for_date(conn: sqlite3.Connection, as_of_date: str, lookback: int = 60) -> dict[str, list[dict]]:
    """Load price bars for all active CN_A instruments up to as_of_date."""
    rows = conn.execute(
        """
        SELECT p.market, p.ticker, p.date, p.open, p.close, p.high, p.low, p.volume, p.amount
        FROM price_bars p
        JOIN instruments i ON i.market = p.market AND i.ticker = p.ticker
        WHERE p.market = 'CN_A'
          AND p.date <= ?
          AND i.active = 1
        ORDER BY p.ticker, p.date
        """,
        (as_of_date,),
    ).fetchall()
    bars: dict[str, list[dict]] = {}
    for r in rows:
        ticker = str(r["ticker"])
        bars.setdefault(ticker, []).append({
            "date": str(r["date"]),
            "open": float(r["open"] or 0),
            "close": float(r["close"] or 0),
            "high": float(r["high"] or 0),
            "low": float(r["low"] or 0),
            "volume": float(r["volume"] or 0),
            "amount": float(r["amount"] or 0),
        })
    # Keep only last `lookback` bars per ticker
    return {t: bl[-lookback:] for t, bl in bars.items() if len(bl) >= 5}


# ---- Individual factors ----

def factor_breakout_strength(bars: list[dict], window: int = 20) -> float | None:
    """close / max(close, window). Higher = closer to breakout."""
    if len(bars) < window:
        return None
    closes = [b["close"] for b in bars]
    current = closes[-1]
    high = _ts_max(closes, window)
    return current / high if high > 0 else None


def factor_volume_ratio(bars: list[dict], window: int = 10) -> float | None:
    """volume / avg(volume, window). Higher = more active."""
    if len(bars) < window:
        return None
    volumes = [b["volume"] for b in bars]
    current = volumes[-1]
    avg = _ts_mean(volumes, window)
    return current / avg if avg > 0 else None


def factor_momentum_5d(bars: list[dict]) -> float | None:
    """5-day return. Higher = stronger short-term momentum."""
    if len(bars) < 6:
        return None
    closes = [b["close"] for b in bars]
    return (closes[-1] / closes[-6] - 1.0) if closes[-6] > 0 else None


def factor_momentum_20d(bars: list[dict]) -> float | None:
    """20-day return. Higher = stronger medium-term momentum."""
    if len(bars) < 21:
        return None
    closes = [b["close"] for b in bars]
    return (closes[-1] / closes[-21] - 1.0) if closes[-21] > 0 else None


def factor_volatility_20d(bars: list[dict]) -> float | None:
    """20-day return std. Lower = less volatile (quality)."""
    if len(bars) < 21:
        return None
    closes = [b["close"] for b in bars]
    returns = [(closes[i] / closes[i - 1] - 1.0) for i in range(-20, 0) if closes[i - 1] > 0]
    if len(returns) < 10:
        return None
    return _ts_std(returns, len(returns))


def factor_turnover_rate(bars: list[dict], window: int = 10) -> float | None:
    """Average turnover rate over window. Higher = more liquid."""
    if len(bars) < window:
        return None
    amounts = [b["amount"] for b in bars]
    return _ts_mean(amounts, window)


def factor_price_to_ma(bars: list[dict], window: int = 20) -> float | None:
    """close / MA(window). Higher = further above MA."""
    if len(bars) < window:
        return None
    closes = [b["close"] for b in bars]
    ma = _ts_mean(closes, window)
    return closes[-1] / ma if ma > 0 else None


# ---- Factor registry ----

FACTOR_FUNCTIONS = {
    "breakout_20d": factor_breakout_strength,
    "volume_ratio_10d": factor_volume_ratio,
    "momentum_5d": factor_momentum_5d,
    "momentum_20d": factor_momentum_20d,
    "volatility_20d": factor_volatility_20d,
    "price_to_ma20": factor_price_to_ma,
}

# Factors where HIGHER raw value = BETTER signal (long bias)
POSITIVE_FACTORS = {"breakout_20d", "volume_ratio_10d", "momentum_5d", "momentum_20d", "price_to_ma20"}

# Factors where LOWER raw value = BETTER signal
NEGATIVE_FACTORS = {"volatility_20d"}

# 多模型评分配置：(model_name, model_version, score_field, percentile_field)
MULTI_MODEL_CONFIGS = [
    ("qlib_alpha158", "t5_full_20260601", "model_score", "model_percentile"),
    ("qlib_alpha158_20250101", "t10_v3", "model_score_2", "model_percentile_2"),
    ("qlib_alpha158_20260101", "t10_v3", "model_score_3", "model_percentile_3"),
]


def compute_cross_sectional_ranks(
    conn: sqlite3.Connection,
    as_of_date: str,
    factors: dict[str, str] | None = None,
) -> dict[str, dict[str, float]]:
    """Compute cross-sectional factor ranks for all active CN_A stocks.

    Returns: {ticker: {factor_name: rank_pct}} where rank_pct is 0-100.
    """
    if factors is None:
        factors = FACTOR_FUNCTIONS

    bars_map = _load_bars_for_date(conn, as_of_date)
    if not bars_map:
        return {}

    factor_values: dict[str, dict[str, float | None]] = {}
    for ticker, bars in bars_map.items():
        factor_values[ticker] = {}
        for fname, func in factors.items():
            if fname in POSITIVE_FACTORS or fname in NEGATIVE_FACTORS:
                factor_values[ticker][fname] = func(bars)

    result: dict[str, dict[str, float]] = {}
    for fname in factors:
        valid = [(t, fv[fname]) for t, fv in factor_values.items() if fv.get(fname) is not None]
        if len(valid) < 10:
            continue
        values = [v[1] for v in valid]
        if fname in NEGATIVE_FACTORS:
            values = [-v for v in values]
        ranks = _rank_pct(values)
        for (ticker, _), rank in zip(valid, ranks):
            result.setdefault(ticker, {})[fname] = rank

    return result


def compute_factor_bonus(
    conn: sqlite3.Connection,
    as_of_date: str,
    tickers: list[str],
    *,
    max_bonus: float = 8.0,
    weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Compute score bonus for specific tickers based on factor ranks.

    Returns {ticker: bonus} where bonus is in [-max_bonus, max_bonus].
    """
    if weights is None:
        weights = {
            "breakout_20d": 0.30,
            "volume_ratio_10d": 0.25,
            "momentum_5d": 0.20,
            "momentum_20d": 0.15,
            "volatility_20d": 0.10,
        }

    ranks = compute_cross_sectional_ranks(conn, as_of_date)
    if not ranks:
        return {}

    bonuses: dict[str, float] = {}
    for ticker in tickers:
        ticker_ranks = ranks.get(ticker, {})
        if not ticker_ranks:
            bonuses[ticker] = 0.0
            continue
        total_weight = 0.0
        weighted_rank = 0.0
        for fname, w in weights.items():
            if fname in ticker_ranks:
                weighted_rank += ticker_ranks[fname] * w
                total_weight += w
        if total_weight > 0:
            avg_rank = weighted_rank / total_weight
            # Map 0-100 rank to [-max_bonus, +max_bonus]
            bonuses[ticker] = (avg_rank - 50.0) / 50.0 * max_bonus
        else:
            bonuses[ticker] = 0.0

    return bonuses


def adjust_candidate_scores(
    conn: sqlite3.Connection,
    as_of_date: str,
    candidates: list[dict[str, object]],
    *,
    max_bonus: float = 8.0,
) -> list[dict[str, object]]:
    """Adjust candidate scores based on cross-sectional factor ranks.

    Modifies candidate_score in-place and returns the list.
    """
    if not candidates:
        return candidates

    tickers = [str(c["ticker"]) for c in candidates]
    bonuses = compute_factor_bonus(conn, as_of_date, tickers, max_bonus=max_bonus)

    for c in candidates:
        ticker = str(c["ticker"])
        bonus = bonuses.get(ticker, 0.0)
        old_score = float(c["candidate_score"])
        new_score = max(0.0, min(100.0, old_score + bonus))
        c["candidate_score"] = round(new_score, 2)
        c["factor_bonus"] = round(bonus, 2)

    return candidates


def attach_model_scores(
    conn: sqlite3.Connection,
    as_of_date: str,
    candidates: list[dict[str, object]],
    *,
    model_configs: list[tuple[str, str, str, str]] | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
) -> list[dict[str, object]]:
    """Attach Qlib model predictions to candidates without modifying candidate_score.

    Adds model score and percentile fields to each candidate dict.

    Returns the list; candidate_score is NOT modified.
    """
    if not candidates:
        return candidates

    tickers = [str(c["ticker"]) for c in candidates]
    placeholders = ",".join("?" for _ in tickers)

    if model_name is not None or model_version is not None:
        configs = [(
            model_name or "qlib_alpha158",
            model_version or "t5_full_20260601",
            "model_score",
            "model_percentile",
        )]
    else:
        configs = model_configs or MULTI_MODEL_CONFIGS

    for cfg_model_name, cfg_model_version, score_field, percentile_field in configs:
        # 查找 as_of_date 当日或之前最近一次可用模型分。
        row = conn.execute(
            """
            SELECT MAX(score_date) AS d
            FROM model_scores
            WHERE model_name = ? AND model_version = ? AND score_date <= ?
            """,
            (cfg_model_name, cfg_model_version, as_of_date),
        ).fetchone()
        if not row or not row["d"]:
            continue

        score_date = row["d"]
        rows = conn.execute(
            f"""
            SELECT ticker, score, percentile
            FROM model_scores
            WHERE model_name = ? AND model_version = ?
              AND score_date = ? AND ticker IN ({placeholders})
            """,
            [cfg_model_name, cfg_model_version, score_date, *tickers],
        ).fetchall()

        score_map = {str(r["ticker"]): (float(r["score"]), float(r["percentile"])) for r in rows}
        for c in candidates:
            ticker = str(c["ticker"])
            pair = score_map.get(ticker)
            if pair is None:
                continue
            c[score_field] = round(pair[0], 6)
            c[percentile_field] = round(pair[1], 4)

    return candidates
