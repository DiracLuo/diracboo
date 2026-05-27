from __future__ import annotations


def cn_a_limit_pct(ticker: str, name: str = "") -> float:
    """Return the daily price limit ratio for an A-share instrument."""
    normalized = ticker.upper()
    stock_name = name.upper()
    if "ST" in stock_name or "退" in name:
        return 0.05
    code = normalized.split(".")[0]
    suffix = normalized.split(".")[-1] if "." in normalized else ""
    if suffix == "BJ" or code.startswith(("8", "920", "430")):
        return 0.30
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def is_one_price_limit_up_from_values(
    previous_close: float | None,
    open_price: float,
    high: float,
    low: float,
    ticker: str,
    name: str = "",
) -> bool:
    if previous_close is None or previous_close <= 0:
        return False
    limit_pct = cn_a_limit_pct(ticker, name)
    one_price = abs(high - low) <= max(open_price * 0.001, 0.01)
    return open_price >= previous_close * (1.0 + limit_pct) * 0.995 and one_price


def is_one_price_limit_down_from_values(
    previous_close: float | None,
    open_price: float,
    high: float,
    low: float,
    ticker: str,
    name: str = "",
) -> bool:
    if previous_close is None or previous_close <= 0:
        return False
    limit_pct = cn_a_limit_pct(ticker, name)
    one_price = abs(high - low) <= max(open_price * 0.001, 0.01)
    return open_price <= previous_close * (1.0 - limit_pct) * 1.006 and one_price
