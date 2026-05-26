from __future__ import annotations

import csv
import http.client
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .ledger import now_utc


DEFAULT_UNIVERSE_PATH = Path("data/universe/default_universe.csv")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SINA_CN_DAILY_URL = (
    "https://quotes.sina.cn/cn/api/jsonp.php/var%20_data=/"
    "CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
)
SINA_CN_INTRADAY_URL = (
    "https://quotes.sina.cn/cn/api/jsonp.php/var%20_data=/"
    "CN_MarketDataService.getKLineData?symbol={symbol}&scale={scale}&ma=no&datalen={datalen}"
)
SINA_US_DAILY_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_data=/"
    "US_MinKService.getDailyK?symbol={symbol}&___qn=3n"
)
TENCENT_HK_DAILY_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
    "param={symbol},day,,,{datalen},qfq"
)


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Instrument:
    market: str
    ticker: str
    name: str
    source: str
    source_symbol: str
    active: bool
    tags: tuple[str, ...]

    def as_row(self) -> dict[str, object]:
        return {
            "market": self.market,
            "ticker": self.ticker,
            "name": self.name,
            "source": self.source,
            "source_symbol": self.source_symbol,
            "active": int(self.active),
            "tags_json": json.dumps(list(self.tags), ensure_ascii=False, sort_keys=True),
            "created_at": now_utc(),
        }


CN_A_BENCHMARK_INSTRUMENTS = (
    Instrument(
        market="CN_A",
        ticker="000300.SS",
        name="沪深300",
        source="sina_cn",
        source_symbol="sh000300",
        active=True,
        tags=("benchmark", "index"),
    ),
)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def read_universe(
    path: Path | str = DEFAULT_UNIVERSE_PATH,
    markets: set[str] | None = None,
    symbols: set[str] | None = None,
) -> list[Instrument]:
    universe_path = Path(path)
    with universe_path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        instruments = []
        for row in rows:
            market = row["market"].strip()
            ticker = row["ticker"].strip()
            if markets and market not in markets:
                continue
            if symbols and ticker not in symbols and row["source_symbol"].strip() not in symbols:
                continue
            instruments.append(
                Instrument(
                    market=market,
                    ticker=ticker,
                    name=row["name"].strip(),
                    source=row.get("source", "yahoo").strip() or "yahoo",
                    source_symbol=row["source_symbol"].strip(),
                    active=row.get("active", "1").strip() not in {"0", "false", "False"},
                    tags=tuple(
                        tag.strip()
                        for tag in row.get("tags", "").split("|")
                        if tag.strip()
                    ),
                )
            )
    return [instrument for instrument in instruments if instrument.active]


def read_db_instruments(
    conn: sqlite3.Connection,
    markets: set[str] | None = None,
    symbols: set[str] | None = None,
) -> list[Instrument]:
    clauses = ["active = 1"]
    params: list[object] = []
    if markets:
        clauses.append(f"market IN ({','.join(['?'] * len(markets))})")
        params.extend(sorted(markets))
    rows = conn.execute(
        f"""
        SELECT market, ticker, name, source, source_symbol, active, tags_json
        FROM instruments
        WHERE {' AND '.join(clauses)}
        ORDER BY market, ticker
        """,
        params,
    ).fetchall()
    instruments: list[Instrument] = []
    for row in rows:
        if symbols and row["ticker"] not in symbols and row["source_symbol"] not in symbols:
            continue
        tags = tuple(json.loads(row["tags_json"] or "[]"))
        instruments.append(
            Instrument(
                market=row["market"],
                ticker=row["ticker"],
                name=row["name"],
                source=row["source"],
                source_symbol=row["source_symbol"],
                active=bool(row["active"]),
                tags=tags,
            )
        )
    return instruments


def _to_unix_seconds(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _request_json(url: str, timeout: int = 20) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MarketDataError(f"HTTP {exc.code} while fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise MarketDataError(f"Network error while fetching {url}: {exc.reason}") from exc
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        raise MarketDataError(f"Network error while fetching {url}: {exc}") from exc


def _request_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise MarketDataError(f"HTTP {exc.code} while fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise MarketDataError(f"Network error while fetching {url}: {exc.reason}") from exc
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        raise MarketDataError(f"Network error while fetching {url}: {exc}") from exc


def _extract_jsonp(text: str) -> object:
    cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.S).strip()
    match = re.search(r"var\s+[_a-zA-Z0-9]+\s*=\s*(.*?);\s*$", cleaned, flags=re.S)
    if not match:
        raise MarketDataError("Could not parse JSONP response")
    payload = match.group(1).strip()
    if payload.startswith("(") and payload.endswith(")"):
        payload = payload[1:-1].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MarketDataError("Could not decode JSONP payload") from exc


def _akshare():
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise MarketDataError("AkShare is not installed. Run: python -m pip install akshare") from exc
    return ak


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if value == "":
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _series_get(row: Any, names: tuple[str, ...]) -> object:
    for name in names:
        try:
            value = row[name]
        except Exception:
            continue
        if not _is_missing(value):
            return value
    raise MarketDataError(f"Missing expected column, tried: {', '.join(names)}")


def _cn_a_plain_symbol(instrument: Instrument) -> str:
    match = re.search(r"(\d{6})", instrument.source_symbol or instrument.ticker)
    if match:
        return match.group(1)
    raise MarketDataError(f"Could not derive A-share symbol from {instrument.ticker}")


def _coerce_intraday_datetime(value: object) -> datetime:
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    text = str(value).strip().replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise MarketDataError(f"Could not parse intraday datetime: {value}")


def _row_to_intraday_bar(instrument: Instrument, row: Any, start: date, end: date) -> dict[str, object] | None:
    bar_datetime = _coerce_intraday_datetime(_series_get(row, ("时间", "日期", "day", "datetime", "date")))
    bar_date = bar_datetime.date()
    if bar_date < start or bar_date > end:
        return None
    amount = None
    try:
        amount_value = _series_get(row, ("成交额", "amount"))
        amount = float(amount_value)
    except MarketDataError:
        amount = None
    return {
        "market": instrument.market,
        "ticker": instrument.ticker,
        "datetime": bar_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "date": bar_date.isoformat(),
        "time": bar_datetime.strftime("%H:%M:%S"),
        "open": float(_series_get(row, ("开盘", "open"))),
        "close": float(_series_get(row, ("收盘", "最新价", "close"))),
        "high": float(_series_get(row, ("最高", "high"))),
        "low": float(_series_get(row, ("最低", "low"))),
        "volume": float(_series_get(row, ("成交量", "volume"))),
        "amount": amount,
    }


def _pct_change(previous: float | None, current: float | None) -> float | None:
    if previous in (None, 0) or current is None:
        return None
    return (current / previous - 1.0) * 100.0


def _row_to_bar(
    instrument: Instrument,
    row_date: str,
    open_price: object,
    close_price: object,
    high_price: object,
    low_price: object,
    volume: object,
    previous_close: float | None,
    amount: object | None = None,
    adj_open: object | None = None,
    adj_close: object | None = None,
    adj_high: object | None = None,
    adj_low: object | None = None,
    adjustment_status: str = "RAW_FALLBACK",
) -> dict[str, object]:
    open_float = float(open_price)
    close_float = float(close_price)
    high_float = float(high_price)
    low_float = float(low_price)
    adj_open_float = open_float if adj_open is None else float(adj_open)
    adj_close_float = close_float if adj_close is None else float(adj_close)
    adj_high_float = high_float if adj_high is None else float(adj_high)
    adj_low_float = low_float if adj_low is None else float(adj_low)
    adj_factor = adj_close_float / close_float if close_float else 1.0
    amount_float = None if amount in (None, "", {}) else float(amount)
    amplitude_pct = None
    if previous_close not in (None, 0):
        amplitude_pct = (high_float - low_float) / float(previous_close) * 100.0
    return {
        "market": instrument.market,
        "ticker": instrument.ticker,
        "date": row_date,
        "open": open_float,
        "close": close_float,
        "high": high_float,
        "low": low_float,
        "volume": float(volume),
        "amount": amount_float,
        "amplitude_pct": amplitude_pct,
        "change_pct": _pct_change(previous_close, close_float),
        "turnover_pct": None,
        "adj_open": adj_open_float,
        "adj_close": adj_close_float,
        "adj_high": adj_high_float,
        "adj_low": adj_low_float,
        "adj_factor": adj_factor,
        "adjustment_status": adjustment_status,
    }


def _mark_raw_fallback(bar: dict[str, object]) -> dict[str, object]:
    close = float(bar["close"])
    bar.setdefault("adj_open", bar["open"])
    bar.setdefault("adj_close", bar["close"])
    bar.setdefault("adj_high", bar["high"])
    bar.setdefault("adj_low", bar["low"])
    bar.setdefault("adj_factor", float(bar["adj_close"]) / close if close else 1.0)
    bar.setdefault("adjustment_status", "RAW_FALLBACK")
    return bar


def _within_range(row_date: str, start: date, end: date) -> bool:
    current = parse_date(row_date)
    return start <= current <= end


def fetch_yahoo_bars(instrument: Instrument, start: date, end: date) -> list[dict[str, object]]:
    if instrument.source.lower() != "yahoo":
        raise MarketDataError(f"Unsupported source for {instrument.ticker}: {instrument.source}")

    query = urllib.parse.urlencode(
        {
            "period1": _to_unix_seconds(start),
            # Yahoo's period2 is exclusive, so add one day to include the requested end date.
            "period2": _to_unix_seconds(end + timedelta(days=1)),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(instrument.source_symbol)) + "?" + query
    payload = _request_json(url)
    chart = payload.get("chart", {})
    error = chart.get("error") if isinstance(chart, dict) else None
    if error:
        raise MarketDataError(f"Yahoo error for {instrument.source_symbol}: {error}")
    result = chart.get("result") if isinstance(chart, dict) else None
    if not result:
        return []

    item = result[0]
    timestamps = item.get("timestamp") or []
    indicators = item.get("indicators", {})
    quotes = indicators.get("quote", [{}])
    quote = quotes[0] if quotes else {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    adjclose_items = indicators.get("adjclose", [{}])
    adjclose = adjclose_items[0].get("adjclose") if adjclose_items else []

    bars: list[dict[str, object]] = []
    previous_close: float | None = None
    for index, timestamp in enumerate(timestamps):
        open_price = opens[index] if index < len(opens) else None
        high_price = highs[index] if index < len(highs) else None
        low_price = lows[index] if index < len(lows) else None
        close_price = closes[index] if index < len(closes) else None
        volume = volumes[index] if index < len(volumes) else None
        if None in {open_price, high_price, low_price, close_price, volume}:
            continue
        bar_date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date().isoformat()
        high = float(high_price)
        low = float(low_price)
        close = float(close_price)
        adj_close_value = adjclose[index] if index < len(adjclose) else None
        factor = float(adj_close_value) / close if adj_close_value not in (None, 0) and close else 1.0
        amplitude_pct = None
        if previous_close not in (None, 0):
            amplitude_pct = (high - low) / float(previous_close) * 100.0

        bars.append(
            {
                "market": instrument.market,
                "ticker": instrument.ticker,
                "date": bar_date,
                "open": float(open_price),
                "close": close,
                "high": high,
                "low": low,
                "volume": float(volume),
                "amount": None,
                "amplitude_pct": amplitude_pct,
                "change_pct": _pct_change(previous_close, close),
                "turnover_pct": None,
                "adj_open": float(open_price) * factor,
                "adj_close": close * factor,
                "adj_high": high * factor,
                "adj_low": low * factor,
                "adj_factor": factor,
                "adjustment_status": "ADJUSTED" if adj_close_value is not None else "RAW_FALLBACK",
            }
        )
        previous_close = close
    return bars


def fetch_sina_cn_bars(instrument: Instrument, start: date, end: date) -> list[dict[str, object]]:
    datalen = max((end - start).days + 45, 120)
    url = SINA_CN_DAILY_URL.format(
        symbol=urllib.parse.quote(instrument.source_symbol),
        datalen=datalen,
    )
    payload = _extract_jsonp(_request_text(url))
    if not isinstance(payload, list):
        raise MarketDataError(f"Unexpected Sina CN payload for {instrument.source_symbol}")

    bars: list[dict[str, object]] = []
    previous_close: float | None = None
    for row in payload:
        if not isinstance(row, dict):
            continue
        row_date = row["day"]
        close = float(row["close"])
        if _within_range(row_date, start, end):
            bars.append(
                _row_to_bar(
                    instrument,
                    row_date,
                    row["open"],
                    row["close"],
                    row["high"],
                    row["low"],
                    row["volume"],
                    previous_close,
                )
            )
        previous_close = close
    return bars


def fetch_akshare_cn_adjusted_daily_map(
    instrument: Instrument,
    start: date,
    end: date,
    adjust: str = "qfq",
) -> dict[str, dict[str, float]]:
    if instrument.market != "CN_A":
        return {}
    if "index" in instrument.tags or "benchmark" in instrument.tags:
        return {}
    symbol = _cn_a_plain_symbol(instrument)
    ak = _akshare()
    try:
        frame = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=adjust,
        )
    except Exception as exc:
        raise MarketDataError(f"AkShare adjusted daily error for {symbol}: {exc}") from exc
    if frame is None or getattr(frame, "empty", False):
        return {}

    rows: dict[str, dict[str, float]] = {}
    for _, row in frame.iterrows():
        row_date = str(_series_get(row, ("日期", "date", "day")))[:10]
        if not _within_range(row_date, start, end):
            continue
        rows[row_date] = {
            "adj_open": float(_series_get(row, ("开盘", "open"))),
            "adj_close": float(_series_get(row, ("收盘", "close"))),
            "adj_high": float(_series_get(row, ("最高", "high"))),
            "adj_low": float(_series_get(row, ("最低", "low"))),
        }
    return rows


def fetch_cn_a_bars(
    instrument: Instrument,
    start: date,
    end: date,
    adjust: str | None = "qfq",
) -> list[dict[str, object]]:
    bars = fetch_sina_cn_bars(instrument, start, end)
    if not bars:
        return bars
    if "index" in instrument.tags or "benchmark" in instrument.tags:
        for bar in bars:
            bar["adj_open"] = bar["open"]
            bar["adj_close"] = bar["close"]
            bar["adj_high"] = bar["high"]
            bar["adj_low"] = bar["low"]
            bar["adj_factor"] = 1.0
            bar["adjustment_status"] = "ADJUSTED"
        return bars
    if adjust not in {"qfq"}:
        return [_mark_raw_fallback(bar) for bar in bars]
    try:
        adjusted = fetch_akshare_cn_adjusted_daily_map(instrument, start, end, adjust=adjust)
    except MarketDataError:
        return [_mark_raw_fallback(bar) for bar in bars]
    if not adjusted:
        return [_mark_raw_fallback(bar) for bar in bars]
    for bar in bars:
        values = adjusted.get(str(bar["date"]))
        if values is None:
            _mark_raw_fallback(bar)
            continue
        close = float(bar["close"])
        bar.update(values)
        bar["adj_factor"] = float(values["adj_close"]) / close if close else 1.0
        bar["adjustment_status"] = "ADJUSTED"
    return bars


def fetch_sina_us_bars(instrument: Instrument, start: date, end: date) -> list[dict[str, object]]:
    url = SINA_US_DAILY_URL.format(symbol=urllib.parse.quote(instrument.source_symbol))
    payload = _extract_jsonp(_request_text(url))
    if not isinstance(payload, list):
        raise MarketDataError(f"Unexpected Sina US payload for {instrument.source_symbol}")

    bars: list[dict[str, object]] = []
    previous_close: float | None = None
    for row in payload:
        if not isinstance(row, dict):
            continue
        row_date = row["d"]
        close = float(row["c"])
        if _within_range(row_date, start, end):
            bars.append(
                _row_to_bar(
                    instrument,
                    row_date,
                    row["o"],
                    row["c"],
                    row["h"],
                    row["l"],
                    row["v"],
                    previous_close,
                    row.get("a"),
                )
            )
        previous_close = close
    return bars


def fetch_tencent_hk_bars(instrument: Instrument, start: date, end: date) -> list[dict[str, object]]:
    datalen = max((end - start).days + 45, 120)
    url = TENCENT_HK_DAILY_URL.format(
        symbol=urllib.parse.quote(instrument.source_symbol),
        datalen=datalen,
    )
    payload = _request_json(url)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MarketDataError(f"Unexpected Tencent HK payload for {instrument.source_symbol}")
    item = data.get(instrument.source_symbol)
    if not isinstance(item, dict):
        raise MarketDataError(f"No Tencent HK data for {instrument.source_symbol}")
    rows = item.get("qfqday") or item.get("day") or []

    bars: list[dict[str, object]] = []
    previous_close: float | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        row_date = row[0]
        close = float(row[2])
        if _within_range(row_date, start, end):
            amount = row[8] if len(row) > 8 else None
            bars.append(
                _row_to_bar(
                    instrument,
                    row_date,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    previous_close,
                    amount,
                    adjustment_status="ADJUSTED",
                )
            )
        previous_close = close
    return bars


def fetch_instrument_bars(
    instrument: Instrument,
    start: date,
    end: date,
    adjust: str | None = "qfq",
) -> list[dict[str, object]]:
    source = instrument.source.lower()
    if source == "yahoo":
        return fetch_yahoo_bars(instrument, start, end)
    if source == "sina_cn":
        return fetch_cn_a_bars(instrument, start, end, adjust=adjust)
    if source == "sina_us":
        return fetch_sina_us_bars(instrument, start, end)
    if source == "tencent_hk":
        return fetch_tencent_hk_bars(instrument, start, end)
    raise MarketDataError(f"Unsupported source for {instrument.ticker}: {instrument.source}")


def fetch_bars(
    instruments: Iterable[Instrument],
    start: date,
    end: date,
    throttle_seconds: float = 0.15,
    adjust: str | None = "qfq",
) -> tuple[list[dict[str, object]], list[str]]:
    all_bars: list[dict[str, object]] = []
    errors: list[str] = []
    for instrument in instruments:
        try:
            all_bars.extend(fetch_instrument_bars(instrument, start, end, adjust=adjust))
        except MarketDataError as exc:
            errors.append(f"{instrument.ticker} {instrument.source_symbol}: {exc}")
        if throttle_seconds > 0:
            time.sleep(throttle_seconds)
    return all_bars, errors


def fetch_akshare_cn_intraday_bars(
    instrument: Instrument,
    start: date,
    end: date,
    period: str = "5",
) -> list[dict[str, object]]:
    if instrument.market != "CN_A":
        raise MarketDataError(f"Intraday fetch currently supports CN_A only, got {instrument.market}")
    symbol = _cn_a_plain_symbol(instrument)
    ak = _akshare()
    try:
        frame = ak.stock_zh_a_hist_min_em(
            symbol=symbol,
            start_date=f"{start.isoformat()} 09:30:00",
            end_date=f"{end.isoformat()} 15:00:00",
            period=period,
            adjust="",
        )
    except Exception as exc:
        raise MarketDataError(f"AkShare intraday error for {symbol}: {exc}") from exc
    if frame is None or getattr(frame, "empty", False):
        return []

    bars: list[dict[str, object]] = []
    for _, row in frame.iterrows():
        bar = _row_to_intraday_bar(instrument, row, start, end)
        if bar is not None:
            bars.append(bar)
    return bars


def fetch_sina_cn_intraday_bars(
    instrument: Instrument,
    start: date,
    end: date,
    period: str = "5",
) -> list[dict[str, object]]:
    if instrument.market != "CN_A":
        raise MarketDataError(f"Intraday fetch currently supports CN_A only, got {instrument.market}")
    try:
        scale = int(period)
    except ValueError as exc:
        raise MarketDataError(f"Unsupported intraday period: {period}") from exc
    bars_per_day = max(1, 240 // scale)
    datalen = max(((end - start).days + 8) * bars_per_day, 500)
    url = SINA_CN_INTRADAY_URL.format(
        symbol=urllib.parse.quote(instrument.source_symbol),
        scale=period,
        datalen=datalen,
    )
    payload = _extract_jsonp(_request_text(url))
    if not isinstance(payload, list):
        raise MarketDataError(f"Unexpected Sina CN intraday payload for {instrument.source_symbol}")
    bars: list[dict[str, object]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        bar = _row_to_intraday_bar(instrument, row, start, end)
        if bar is not None:
            bars.append(bar)
    return bars


def fetch_cn_a_intraday_bars(
    instrument: Instrument,
    start: date,
    end: date,
    period: str = "5",
) -> list[dict[str, object]]:
    sina_error = None
    try:
        bars = fetch_sina_cn_intraday_bars(instrument, start, end, period=period)
        if bars:
            return bars
    except MarketDataError as exc:
        sina_error = exc

    try:
        bars = fetch_akshare_cn_intraday_bars(instrument, start, end, period=period)
        if bars:
            return bars
    except MarketDataError as exc:
        if sina_error is not None:
            raise MarketDataError(f"{sina_error}; AkShare fallback failed: {exc}") from exc
        raise

    if sina_error is not None:
        raise MarketDataError(f"{sina_error}; AkShare fallback returned no rows")
    return []


def fetch_intraday_bars(
    instruments: Iterable[Instrument],
    start: date,
    end: date,
    period: str = "5",
    throttle_seconds: float = 0.15,
) -> tuple[list[dict[str, object]], list[str]]:
    all_bars: list[dict[str, object]] = []
    errors: list[str] = []
    for instrument in instruments:
        if instrument.market != "CN_A":
            continue
        try:
            all_bars.extend(fetch_cn_a_intraday_bars(instrument, start, end, period=period))
        except MarketDataError as exc:
            errors.append(f"{instrument.ticker} {instrument.source_symbol}: {exc}")
        if throttle_seconds > 0:
            time.sleep(throttle_seconds)
    return all_bars, errors
