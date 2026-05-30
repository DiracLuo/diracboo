"""Ticker normalization helpers shared by data sources and Qlib bridges."""

from __future__ import annotations

import re


CN_A_SUFFIX_TO_QLIB_PREFIX = {
    ".SS": "SH",
    ".SZ": "SZ",
    ".BJ": "BJ",
}

_PREFIX_TO_SUFFIX = {
    "SH": ".SS",
    "SZ": ".SZ",
    "BJ": ".BJ",
}


def normalize_cn_a_ticker(value: str) -> str:
    """Return Alpha Ledger's canonical CN_A ticker.

    Shanghai tickers are stored as ``.SS`` internally. Boundary forms such as
    ``600519.SH``, ``SH600519``, ``sh.600519`` and ``600519.SS`` all normalize
    to ``600519.SS``.
    """
    text = str(value).strip().upper()
    compact = text.replace(".", "")

    prefix = compact[:2]
    if prefix in _PREFIX_TO_SUFFIX and compact[2:].isdigit():
        return f"{compact[2:]}{_PREFIX_TO_SUFFIX[prefix]}"

    dotted = re.fullmatch(r"(\d{6})\.(SH|SS|SZ|BJ)", text)
    if dotted:
        code, suffix = dotted.group(1), dotted.group(2)
        return f"{code}{'.SS' if suffix in {'SH', 'SS'} else f'.{suffix}'}"

    digits = re.sub(r"\D", "", text)
    if len(digits) == 6:
        if digits.startswith(("8", "4", "92")):
            return f"{digits}.BJ"
        if digits.startswith(("0", "3")):
            return f"{digits}.SZ"
        if digits.startswith(("6", "9")):
            return f"{digits}.SS"

    return text


def normalize_ticker(ticker: str, market: str | None = None) -> str:
    """Normalize tickers where Alpha Ledger has a market-specific convention."""
    if market == "CN_A":
        return normalize_cn_a_ticker(ticker)
    return str(ticker).strip().upper()


def cn_a_source_symbol(ticker: str) -> tuple[str, str]:
    """Return ``(source_symbol, exchange_prefix)`` for a canonical CN_A ticker."""
    canonical = normalize_cn_a_ticker(ticker)
    match = re.fullmatch(r"(\d{6})\.(SS|SZ|BJ)", canonical)
    if not match:
        raise ValueError(f"Cannot derive CN_A source symbol from {ticker}")
    code, suffix = match.group(1), match.group(2)
    prefix = {"SS": "sh", "SZ": "sz", "BJ": "bj"}[suffix]
    return f"{prefix}{code}", prefix


def cn_a_to_baostock_symbol(ticker: str) -> str:
    """Convert a CN_A ticker to BaoStock's ``sh.600519`` style."""
    canonical = normalize_cn_a_ticker(ticker)
    source_symbol, prefix = cn_a_source_symbol(canonical)
    if prefix == "bj":
        raise ValueError(f"BaoStock does not support BJ stocks: {ticker}")
    return f"{prefix}.{source_symbol[2:]}"


def ticker_to_qlib_instrument(ticker: str) -> str:
    """Convert Alpha Ledger CN_A ticker to Qlib instrument name."""
    canonical = normalize_cn_a_ticker(ticker)
    for suffix, prefix in CN_A_SUFFIX_TO_QLIB_PREFIX.items():
        if canonical.endswith(suffix):
            return f"{prefix}{canonical[: -len(suffix)]}"
    raise ValueError(f"Cannot convert ticker '{ticker}' to Qlib instrument: unknown suffix")


def ticker_to_qlib_filename(ticker: str) -> str:
    """Convert Alpha Ledger CN_A ticker to Qlib CSV filename."""
    return f"{ticker_to_qlib_instrument(ticker)}.csv"


def qlib_instrument_to_ticker(instrument: str) -> str | None:
    """Convert Qlib instrument or filename to Alpha Ledger's canonical ticker."""
    stem = str(instrument).strip().upper().removesuffix(".CSV")
    for prefix, suffix in _PREFIX_TO_SUFFIX.items():
        if stem.startswith(prefix) and stem[len(prefix) :].isdigit():
            return f"{stem[len(prefix):]}{suffix}"
    return None


def qlib_filename_to_ticker(filename: str) -> str:
    """Convert Qlib CSV filename to Alpha Ledger ticker."""
    ticker = qlib_instrument_to_ticker(filename)
    if ticker is None:
        raise ValueError(f"Cannot convert filename '{filename}' to Alpha Ledger ticker")
    return ticker
