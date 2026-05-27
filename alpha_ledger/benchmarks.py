from __future__ import annotations

from .market_data import Instrument


CN_A_BENCHMARKS = (
    Instrument("CN_A", "000300.SS", "沪深300", "sina_cn", "sh000300", True, ("benchmark", "index")),
    Instrument("CN_A", "000905.SS", "中证500", "sina_cn", "sh000905", True, ("benchmark", "index")),
    Instrument("CN_A", "000852.SS", "中证1000", "sina_cn", "sh000852", True, ("benchmark", "index")),
    Instrument("CN_A", "399006.SZ", "创业板指", "sina_cn", "sz399006", True, ("benchmark", "index")),
    Instrument("CN_A", "000688.SS", "科创50", "sina_cn", "sh000688", True, ("benchmark", "index")),
    Instrument("CN_A", "899050.BJ", "北证50", "sina_cn", "bj899050", True, ("benchmark", "index")),
)


def cn_a_benchmark_for_ticker(ticker: str) -> str:
    code = ticker.upper().split(".")[0]
    suffix = ticker.upper().split(".")[-1] if "." in ticker else ""
    if suffix == "BJ" or code.startswith(("8", "920", "430")):
        return "899050.BJ"
    if code.startswith(("300", "301")):
        return "399006.SZ"
    if code.startswith(("688", "689")):
        return "000688.SS"
    if code.startswith(("002", "003")):
        return "000852.SS"
    return "000300.SS"


def benchmark_for_asset(market: str, ticker: str, explicit: str | None = None) -> str | None:
    if explicit and explicit != "auto":
        return explicit
    if market == "CN_A":
        return cn_a_benchmark_for_ticker(ticker)
    return None
