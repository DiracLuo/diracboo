from __future__ import annotations

import csv
import gzip
import json
import math
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import estimate_financial_published_date, upsert_many
from .ledger import now_utc
from .market_data import Instrument


IMPORTANT_EVENT_KEYWORDS = {
    "调研": 0.86,
    "投资者关系": 0.84,
    "业绩说明会": 0.78,
    "授信": 0.72,
    "借贷": 0.68,
    "合同": 0.72,
    "订单": 0.76,
    "回购": 0.74,
    "增持": 0.70,
    "业绩预告": 0.72,
    "重组": 0.76,
    "收购": 0.68,
}

FINANCIAL_METRICS = {
    "主营业务收入增长率(%)": "%",
    "净利润增长率(%)": "%",
    "销售毛利率(%)": "%",
    "净资产收益率(%)": "%",
    "加权净资产收益率(%)": "%",
    "资产负债率(%)": "%",
    "每股经营性现金流(元)": "元",
    "加权每股收益(元)": "元",
}
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_FORMS = {"10-K": 0.84, "10-Q": 0.78, "8-K": 0.72, "S-1": 0.74}
SEC_USER_AGENT = "AlphaLedger research tool contact: ldylkj@163.com"
NEWS_IMPORTANCE_KEYWORDS = {
    "earnings": 0.78,
    "guidance": 0.76,
    "upgrade": 0.72,
    "buyback": 0.74,
    "contract": 0.72,
    "订单": 0.74,
    "回购": 0.74,
    "业绩": 0.72,
    "财报": 0.76,
    "上调": 0.70,
    "评级": 0.68,
    "南向": 0.70,
    "增持": 0.70,
}


@dataclass(frozen=True)
class EventFetchResult:
    corporate_events: int = 0
    research_events: int = 0
    financial_metrics: int = 0
    money_flows: int = 0
    instruments: int = 0
    errors: tuple[str, ...] = ()


def _akshare():
    try:
        import akshare as ak  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("AkShare is not installed. Run: python -m pip install akshare") from exc
    return ak


def _request_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers=headers
        or {
            "User-Agent": SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": urllib.parse.urlparse(url).netloc,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} while fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while fetching {url}: {exc.reason}") from exc


def normalize_cn_code(code: str) -> tuple[str, str, str]:
    pure = re.sub(r"\D", "", str(code))
    if pure.startswith(("8", "4", "92")):
        return f"{pure}.BJ", f"bj{pure}", "bj"
    if pure.startswith(("6", "9")):
        return f"{pure}.SS", f"sh{pure}", "sh"
    if pure.startswith(("0", "3")):
        return f"{pure}.SZ", f"sz{pure}", "sz"
    return f"{pure}.BJ", f"bj{pure}", "bj"


def _score_event(event_type: str, title: str) -> float:
    text = f"{event_type} {title}"
    score = 0.35
    for keyword, value in IMPORTANT_EVENT_KEYWORDS.items():
        if keyword in text:
            score = max(score, value)
    return score


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "nan", "NaN", "None"}:
        return None
    if text.endswith("%"):
        text = text[:-1]
    unit = 1.0
    if text.endswith("万"):
        unit = 10_000.0
        text = text[:-1]
    elif text.endswith("亿"):
        unit = 100_000_000.0
        text = text[:-1]
    try:
        return float(text) * unit
    except ValueError:
        return None


def _safe_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "nan", "NaT", "None"}:
        return None
    match = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"(\d{8})", text)
    if match:
        raw = match.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return None


def _in_date_range(date_text: str | None, start: date, end: date) -> bool:
    if not date_text:
        return False
    try:
        current = date.fromisoformat(date_text)
    except ValueError:
        return False
    return start <= current <= end


def _series_get(item: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        try:
            value = item.get(name)
        except AttributeError:
            value = None
        if value not in (None, ""):
            return value
    return None


def _market_code_for_akshare(instrument: Instrument) -> str:
    if instrument.market == "HK":
        digits = re.sub(r"\D", "", instrument.ticker)
        return digits.zfill(4)
    if instrument.market == "US":
        return instrument.ticker.replace(".US", "").upper()
    return re.sub(r"\D", "", instrument.ticker)


def _score_news(event_type: str, title: str, summary: str = "") -> float:
    text = f"{event_type} {title} {summary}".lower()
    score = _score_event(event_type, title)
    for keyword, value in NEWS_IMPORTANCE_KEYWORDS.items():
        if keyword.lower() in text:
            score = max(score, value)
    return score


def instrument_from_cn(code: str, name: str) -> Instrument:
    ticker, source_symbol, _ = normalize_cn_code(code)
    return Instrument(
        market="CN_A",
        ticker=ticker,
        name=name,
        source="sina_cn",
        source_symbol=source_symbol,
        active=True,
        tags=("event_pool",),
    )


def normalize_market(value: str) -> str:
    market = value.strip().upper().replace("-", "_")
    aliases = {
        "A": "CN_A",
        "ASHARE": "CN_A",
        "CN": "CN_A",
        "CN_A": "CN_A",
        "HK": "HK",
        "HKG": "HK",
        "HKEX": "HK",
        "US": "US",
        "USA": "US",
        "NYSE": "US",
        "NASDAQ": "US",
    }
    if market not in aliases:
        raise ValueError(f"Unsupported market: {value}")
    return aliases[market]


def instrument_from_market_ticker(market: str, ticker: str, name: str) -> Instrument:
    normalized_market = normalize_market(market)
    if normalized_market == "CN_A":
        return instrument_from_cn(ticker, name)
    if normalized_market == "US":
        normalized_ticker = ticker.strip().upper().replace(".US", "")
        return Instrument(
            market="US",
            ticker=normalized_ticker,
            name=name,
            source="sina_us",
            source_symbol=normalized_ticker,
            active=True,
            tags=("event_pool",),
        )
    digits = re.sub(r"\D", "", ticker)
    if not digits:
        raise ValueError(f"Invalid HK ticker: {ticker}")
    return Instrument(
        market="HK",
        ticker=f"{digits.zfill(4)}.HK",
        name=name,
        source="tencent_hk",
        source_symbol=f"hk{digits.zfill(5)}",
        active=True,
        tags=("event_pool",),
    )


def normalize_event_row(row: dict[str, str]) -> tuple[dict[str, object], Instrument]:
    event_date = (row.get("event_date") or row.get("date") or "").strip()
    raw_ticker = (row.get("ticker") or row.get("symbol") or "").strip()
    raw_market = (row.get("market") or "").strip()
    if not raw_market or not raw_ticker or not event_date:
        raise ValueError("market, ticker, and event_date are required")
    name = (row.get("name") or raw_ticker).strip()
    instrument = instrument_from_market_ticker(raw_market, raw_ticker, name)
    event_type = (row.get("event_type") or row.get("type") or "事件").strip()
    title = (row.get("title") or row.get("summary") or event_type).strip()
    importance_score = _safe_float(row.get("importance_score"))
    if importance_score is None:
        importance_score = _score_event(event_type, title)
    event = {
        "market": instrument.market,
        "ticker": instrument.ticker,
        "name": name,
        "event_date": event_date,
        "event_type": event_type,
        "title": title,
        "source": (row.get("source") or "manual.csv").strip(),
        "source_url": (row.get("source_url") or row.get("url") or "").strip(),
        "importance_score": importance_score,
        "summary": (row.get("summary") or title).strip(),
        "created_at": now_utc(),
    }
    return event, instrument


def import_events_csv(conn: sqlite3.Connection, path: Path | str) -> EventFetchResult:
    event_rows: list[dict[str, object]] = []
    instruments: dict[tuple[str, str], Instrument] = {}
    errors: list[str] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            try:
                event, instrument = normalize_event_row(row)
            except Exception as exc:
                errors.append(f"row {index}: {exc}")
                continue
            event_rows.append(event)
            instruments[(instrument.market, instrument.ticker)] = instrument
    return EventFetchResult(
        corporate_events=upsert_many(
            conn,
            "corporate_events",
            event_rows,
            ("market", "ticker", "event_date", "event_type", "title"),
        ),
        instruments=upsert_many(
            conn,
            "instruments",
            [instrument.as_row() for instrument in instruments.values()],
            ("market", "ticker"),
        ),
        errors=tuple(errors),
    )


def fetch_us_sec_filings(
    instruments: list[Instrument],
    start: date,
    end: date,
    limit_per_symbol: int = 80,
    throttle_seconds: float = 0.12,
) -> tuple[list[dict[str, object]], list[str]]:
    us_instruments = [instrument for instrument in instruments if instrument.market == "US"]
    if not us_instruments:
        return [], []

    errors: list[str] = []
    rows: list[dict[str, object]] = []
    ticker_payload = _request_json(SEC_COMPANY_TICKERS_URL)
    ticker_map: dict[str, tuple[str, str]] = {}
    for item in ticker_payload.values():
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).upper()
        cik_value = item.get("cik_str")
        if not ticker or cik_value is None:
            continue
        ticker_map[ticker] = (str(cik_value).zfill(10), str(item.get("title", ticker)))

    for instrument in us_instruments:
        ticker = instrument.ticker.upper().replace(".US", "")
        cik_item = ticker_map.get(ticker)
        if cik_item is None:
            errors.append(f"SEC ticker map missing {instrument.ticker}")
            continue
        cik, company_title = cik_item
        try:
            payload = _request_json(SEC_SUBMISSIONS_URL.format(cik=cik))
        except Exception as exc:
            errors.append(f"SEC submissions {instrument.ticker}: {exc}")
            continue
        recent = payload.get("filings", {})
        recent = recent.get("recent", {}) if isinstance(recent, dict) else {}
        forms = recent.get("form", []) if isinstance(recent, dict) else []
        filing_dates = recent.get("filingDate", []) if isinstance(recent, dict) else []
        report_dates = recent.get("reportDate", []) if isinstance(recent, dict) else []
        accession_numbers = recent.get("accessionNumber", []) if isinstance(recent, dict) else []
        primary_docs = recent.get("primaryDocument", []) if isinstance(recent, dict) else []
        descriptions = recent.get("primaryDocDescription", []) if isinstance(recent, dict) else []

        imported = 0
        for index, form in enumerate(forms):
            form_text = str(form)
            if form_text not in SEC_FORMS:
                continue
            filing_date = str(filing_dates[index]) if index < len(filing_dates) else ""
            if not _in_date_range(filing_date, start, end):
                continue
            accession = str(accession_numbers[index]) if index < len(accession_numbers) else ""
            primary_doc = str(primary_docs[index]) if index < len(primary_docs) else ""
            report_date = str(report_dates[index]) if index < len(report_dates) else ""
            description = str(descriptions[index]) if index < len(descriptions) else form_text
            accession_path = accession.replace("-", "")
            source_url = ""
            if accession and primary_doc:
                source_url = (
                    "https://www.sec.gov/Archives/edgar/data/"
                    f"{int(cik)}/{accession_path}/{primary_doc}"
                )
            title = f"{form_text} filed"
            summary = f"{company_title} filed {form_text}"
            if report_date:
                summary += f" for report date {report_date}"
            if description and description != form_text:
                summary += f"; {description}"
            rows.append(
                {
                    "market": "US",
                    "ticker": ticker,
                    "name": instrument.name or company_title,
                    "event_date": filing_date,
                    "event_type": f"SEC {form_text}",
                    "title": title,
                    "source": "sec.submissions",
                    "source_url": source_url,
                    "importance_score": SEC_FORMS[form_text],
                    "summary": summary,
                    "created_at": now_utc(),
                }
            )
            imported += 1
            if imported >= limit_per_symbol:
                break
        if throttle_seconds > 0:
            time.sleep(throttle_seconds)
    return rows, errors


def fetch_stock_news_events(
    instruments: list[Instrument],
    start: date,
    end: date,
    limit_per_symbol: int = 60,
) -> tuple[list[dict[str, object]], list[str]]:
    ak = _akshare()
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for instrument in instruments:
        if instrument.market not in {"US", "HK"}:
            continue
        symbol = _market_code_for_akshare(instrument)
        try:
            df = ak.stock_news_em(symbol=symbol)
        except Exception as exc:
            errors.append(f"stock news {instrument.market} {instrument.ticker}: {exc}")
            continue
        for _, item in df.head(limit_per_symbol).iterrows():
            event_date = _safe_date(
                _series_get(item, ("发布时间", "时间", "日期", "date", "datetime", "Date"))
            )
            if not _in_date_range(event_date, start, end):
                continue
            title = str(_series_get(item, ("新闻标题", "标题", "title", "Title")) or "")
            summary = str(_series_get(item, ("新闻内容", "内容", "摘要", "summary", "Summary")) or title)
            if not title:
                continue
            source = str(_series_get(item, ("文章来源", "来源", "source", "Source")) or "akshare.stock_news_em")
            source_url = str(_series_get(item, ("新闻链接", "链接", "url", "URL")) or "")
            rows.append(
                {
                    "market": instrument.market,
                    "ticker": instrument.ticker,
                    "name": instrument.name,
                    "event_date": event_date,
                    "event_type": "市场新闻",
                    "title": title,
                    "source": source,
                    "source_url": source_url,
                    "importance_score": _score_news("市场新闻", title, summary),
                    "summary": summary[:500],
                    "created_at": now_utc(),
                }
            )
    return rows, errors


def fetch_hk_dividend_events(
    instruments: list[Instrument],
    start: date,
    end: date,
    limit_per_symbol: int = 40,
) -> tuple[list[dict[str, object]], list[str]]:
    ak = _akshare()
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for instrument in instruments:
        if instrument.market != "HK":
            continue
        symbol = _market_code_for_akshare(instrument)
        try:
            df = ak.stock_hk_dividend_payout_em(symbol=symbol)
        except Exception as exc:
            errors.append(f"HK dividend {instrument.ticker}: {exc}")
            continue
        for _, item in df.head(limit_per_symbol).iterrows():
            event_date = _safe_date(
                _series_get(item, ("除净日", "派息日", "公告日", "财政年度", "日期", "截止过户日期"))
            )
            if not _in_date_range(event_date, start, end):
                continue
            title = "港股分红派息"
            summary = "；".join(f"{column}:{item[column]}" for column in df.columns[:8])
            rows.append(
                {
                    "market": "HK",
                    "ticker": instrument.ticker,
                    "name": instrument.name,
                    "event_date": event_date,
                    "event_type": "分红派息",
                    "title": title,
                    "source": "akshare.stock_hk_dividend_payout_em",
                    "source_url": "",
                    "importance_score": _score_news("分红派息", title, summary),
                    "summary": summary[:500],
                    "created_at": now_utc(),
                }
            )
    return rows, errors


def fetch_hk_financial_metrics(
    instruments: list[Instrument],
    start: date,
    end: date,
    limit_periods: int = 8,
) -> tuple[list[dict[str, object]], list[str]]:
    ak = _akshare()
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    skip_columns = {"报告期", "日期", "截止日期", "公布日期", "币种"}
    valuation_indicators = ("总市值", "市盈率(TTM)", "市净率")
    for instrument in instruments:
        if instrument.market != "HK":
            continue
        symbol = _market_code_for_akshare(instrument)
        try:
            df = ak.stock_financial_hk_analysis_indicator_em(symbol=symbol, indicator="报告期")
        except Exception as exc:
            try:
                df = ak.stock_hk_financial_indicator_em(symbol=symbol)
            except Exception as fallback_exc:
                valuation_imported = False
                for indicator in valuation_indicators:
                    try:
                        valuation_df = ak.stock_hk_valuation_baidu(
                            symbol=symbol,
                            indicator=indicator,
                            period="近一年",
                        )
                    except Exception:
                        continue
                    if valuation_df.empty:
                        continue
                    item = valuation_df.tail(1).iloc[0]
                    report_date = _safe_date(_series_get(item, ("日期", "date", "时间"))) or end.isoformat()
                    value = _safe_float(_series_get(item, (indicator, "value", "数值", "收盘")))
                    if value is None:
                        for column in valuation_df.columns:
                            if str(column) in {"日期", "date", "时间"}:
                                continue
                            value = _safe_float(item[column])
                            if value is not None:
                                break
                    if value is None:
                        continue
                    rows.append(
                        {
                            "market": "HK",
                            "ticker": instrument.ticker,
                            "report_date": report_date,
                            "published_date": report_date,
                            "metric_name": indicator,
                            "metric_value": value,
                            "unit": None,
                            "source": "akshare.stock_hk_valuation_baidu",
                            "created_at": now_utc(),
                        }
                    )
                    valuation_imported = True
                if not valuation_imported:
                    errors.append(f"HK financials {instrument.ticker}: {exc}; fallback: {fallback_exc}")
                continue
        if df.empty:
            continue
        for _, item in df.head(limit_periods).iterrows():
            report_date = _safe_date(_series_get(item, ("报告期", "日期", "截止日期", "公布日期")))
            published_date = _safe_date(_series_get(item, ("公布日期", "公告日期", "披露日期")))
            if report_date is None:
                report_date = end.isoformat()
            if published_date is None:
                published_date = estimate_financial_published_date(report_date)
            if date.fromisoformat(report_date) > end:
                continue
            for column in df.columns:
                if str(column) in skip_columns:
                    continue
                value = _safe_float(item[column])
                if value is None:
                    continue
                rows.append(
                    {
                        "market": "HK",
                        "ticker": instrument.ticker,
                        "report_date": report_date,
                        "published_date": published_date,
                        "metric_name": str(column),
                        "metric_value": value,
                        "unit": None,
                        "source": "akshare.stock_financial_hk_analysis_indicator_em",
                        "created_at": now_utc(),
                    }
                )
    return rows, errors


def fetch_hk_southbound_events(
    instruments: list[Instrument],
    start: date,
    end: date,
) -> tuple[list[dict[str, object]], list[str]]:
    hk_codes = {_market_code_for_akshare(instrument): instrument for instrument in instruments if instrument.market == "HK"}
    if not hk_codes:
        return [], []
    ak = _akshare()
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        df = ak.stock_hsgt_stock_statistics_em(
            symbol="南向持股",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as exc:
        return [], [f"HK southbound statistics: {exc}"]

    for _, item in df.iterrows():
        raw_code = str(_series_get(item, ("股票代码", "代码", "证券代码", "持股代码")) or "")
        code = re.sub(r"\D", "", raw_code).zfill(4)
        instrument = hk_codes.get(code)
        if instrument is None:
            continue
        event_date = _safe_date(_series_get(item, ("日期", "持股日期", "交易日期")))
        if not _in_date_range(event_date, start, end):
            continue
        summary = "；".join(f"{column}:{item[column]}" for column in df.columns[:10])
        rows.append(
            {
                "market": "HK",
                "ticker": instrument.ticker,
                "name": instrument.name,
                "event_date": event_date,
                "event_type": "南向持股",
                "title": "南向资金持股统计更新",
                "source": "akshare.stock_hsgt_stock_statistics_em",
                "source_url": "",
                "importance_score": _score_news("南向持股", instrument.name, summary),
                "summary": summary[:500],
                "created_at": now_utc(),
            }
        )
    return rows, errors


def fetch_notice_events(date_value: date, limit: int | None = None) -> tuple[list[dict[str, object]], list[Instrument]]:
    ak = _akshare()
    df = ak.stock_notice_report(symbol="全部", date=date_value.strftime("%Y%m%d"))
    if limit:
        df = df.head(limit)

    rows: list[dict[str, object]] = []
    instruments: dict[tuple[str, str], Instrument] = {}
    for _, item in df.iterrows():
        ticker, _, _ = normalize_cn_code(str(item["代码"]))
        name = str(item["名称"])
        event_type = str(item["公告类型"])
        title = str(item["公告标题"])
        rows.append(
            {
                "market": "CN_A",
                "ticker": ticker,
                "name": name,
                "event_date": str(item["公告日期"]),
                "event_type": event_type,
                "title": title,
                "source": "akshare.stock_notice_report",
                "source_url": str(item.get("网址", "")),
                "importance_score": _score_event(event_type, title),
                "summary": title,
                "created_at": now_utc(),
            }
        )
        instrument = instrument_from_cn(str(item["代码"]), name)
        instruments[(instrument.market, instrument.ticker)] = instrument
    return rows, list(instruments.values())


def fetch_individual_notices(
    instruments: list[Instrument], start: date, end: date, limit_per_symbol: int | None = 20
) -> list[dict[str, object]]:
    ak = _akshare()
    rows: list[dict[str, object]] = []
    for instrument in instruments:
        if instrument.market != "CN_A":
            continue
        code = re.sub(r"\D", "", instrument.ticker)
        try:
            df = ak.stock_individual_notice_report(
                security=code,
                begin_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
        except Exception:
            continue
        if limit_per_symbol:
            df = df.head(limit_per_symbol)
        for _, item in df.iterrows():
            event_type = str(item["公告类型"])
            title = str(item["公告标题"])
            rows.append(
                {
                    "market": "CN_A",
                    "ticker": instrument.ticker,
                    "name": str(item.get("名称", instrument.name)),
                    "event_date": str(item["公告日期"]),
                    "event_type": event_type,
                    "title": title,
                    "source": "akshare.stock_individual_notice_report",
                    "source_url": str(item.get("网址", "")),
                    "importance_score": _score_event(event_type, title),
                    "summary": title,
                    "created_at": now_utc(),
                }
            )
    return rows


def fetch_financial_metrics(
    instruments: list[Instrument], start_year: int = 2025, limit_periods: int = 6
) -> list[dict[str, object]]:
    ak = _akshare()
    rows: list[dict[str, object]] = []
    for instrument in instruments:
        if instrument.market != "CN_A":
            continue
        code = re.sub(r"\D", "", instrument.ticker)
        try:
            df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(start_year))
        except Exception:
            continue
        if df.empty:
            continue
        df = df.tail(limit_periods)
        for _, item in df.iterrows():
            report_date = str(item["日期"])
            published_date = estimate_financial_published_date(report_date)
            for metric, unit in FINANCIAL_METRICS.items():
                if metric not in item:
                    continue
                rows.append(
                    {
                        "market": instrument.market,
                        "ticker": instrument.ticker,
                        "report_date": report_date,
                        "published_date": published_date,
                        "metric_name": metric,
                        "metric_value": _safe_float(item[metric]),
                        "unit": unit,
                        "source": "akshare.stock_financial_analysis_indicator",
                        "created_at": now_utc(),
                    }
                )
    return rows


def fetch_research_reports(instruments: list[Instrument], limit_per_symbol: int = 10) -> list[dict[str, object]]:
    ak = _akshare()
    rows: list[dict[str, object]] = []
    for instrument in instruments:
        if instrument.market != "CN_A":
            continue
        code = re.sub(r"\D", "", instrument.ticker)
        try:
            df = ak.stock_research_report_em(symbol=code)
        except Exception:
            continue
        for _, item in df.head(limit_per_symbol).iterrows():
            title = str(item.get("报告名称", ""))
            date_value = str(item.get("日期", ""))
            rows.append(
                {
                    "market": instrument.market,
                    "ticker": instrument.ticker,
                    "name": str(item.get("股票简称", instrument.name)),
                    "event_date": date_value,
                    "event_type": "卖方研报",
                    "title": title,
                    "source": "akshare.stock_research_report_em",
                    "source_url": str(item.get("报告PDF链接", "")),
                    "importance_score": 0.58,
                    "summary": f"{item.get('机构', '')} {item.get('东财评级', '')} {title}".strip(),
                    "created_at": now_utc(),
                }
            )
    return rows


def fetch_current_money_flow(as_of: date, limit: int = 300) -> list[dict[str, object]]:
    ak = _akshare()
    df = ak.stock_fund_flow_individual(symbol="即时")
    rows: list[dict[str, object]] = []
    for _, item in df.head(limit).iterrows():
        ticker, _, _ = normalize_cn_code(str(item["股票代码"]))
        rows.append(
            {
                "market": "CN_A",
                "ticker": ticker,
                "date": as_of.isoformat(),
                "name": str(item["股票简称"]),
                "net_inflow": _safe_float(item.get("净额")),
                "inflow": _safe_float(item.get("流入资金")),
                "outflow": _safe_float(item.get("流出资金")),
                "turnover_amount": _safe_float(item.get("成交额")),
                "turnover_rate": _safe_float(item.get("换手率")),
                "source": "akshare.stock_fund_flow_individual",
                "created_at": now_utc(),
            }
        )
    return rows


def fetch_events_to_db(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    instruments: list[Instrument],
    notice_limit_per_day: int | None = 80,
    fetch_daily_notices: bool = True,
    fetch_individual: bool = True,
    fetch_financials: bool = True,
    fetch_research: bool = True,
    fetch_money_flow: bool = True,
) -> EventFetchResult:
    errors: list[str] = []
    new_instruments: list[Instrument] = []
    corporate_rows: list[dict[str, object]] = []
    cn_instruments = [instrument for instrument in instruments if instrument.market == "CN_A"]
    hk_instruments = [instrument for instrument in instruments if instrument.market == "HK"]
    us_instruments = [instrument for instrument in instruments if instrument.market == "US"]

    if fetch_daily_notices and cn_instruments:
        current = start
        while current <= end:
            try:
                rows, instruments_from_events = fetch_notice_events(current, notice_limit_per_day)
                corporate_rows.extend(rows)
                new_instruments.extend(instruments_from_events)
            except Exception as exc:
                errors.append(f"notice {current}: {exc}")
            current += timedelta(days=1)

    if fetch_individual:
        if cn_instruments:
            try:
                corporate_rows.extend(fetch_individual_notices(cn_instruments, start, end))
            except Exception as exc:
                errors.append(f"individual notices: {exc}")
        if us_instruments:
            try:
                rows, row_errors = fetch_us_sec_filings(us_instruments, start, end)
                corporate_rows.extend(rows)
                errors.extend(row_errors)
            except Exception as exc:
                errors.append(f"SEC filings: {exc}")
        if hk_instruments:
            try:
                rows, row_errors = fetch_hk_dividend_events(hk_instruments, start, end)
                corporate_rows.extend(rows)
                errors.extend(row_errors)
            except Exception as exc:
                errors.append(f"HK dividend events: {exc}")

    if fetch_research:
        if cn_instruments:
            try:
                corporate_rows.extend(fetch_research_reports(cn_instruments))
            except Exception as exc:
                errors.append(f"research reports: {exc}")
        if us_instruments or hk_instruments:
            try:
                rows, row_errors = fetch_stock_news_events(us_instruments + hk_instruments, start, end)
                corporate_rows.extend(rows)
                errors.extend(row_errors)
            except Exception as exc:
                errors.append(f"US/HK stock news: {exc}")
        if hk_instruments:
            try:
                rows, row_errors = fetch_hk_southbound_events(hk_instruments, start, end)
                corporate_rows.extend(rows)
                errors.extend(row_errors)
            except Exception as exc:
                errors.append(f"HK southbound events: {exc}")

    financial_rows: list[dict[str, object]] = []
    if fetch_financials:
        if cn_instruments:
            try:
                financial_rows.extend(fetch_financial_metrics(cn_instruments, start_year=start.year - 1))
            except Exception as exc:
                errors.append(f"financial metrics: {exc}")
        if hk_instruments:
            try:
                rows, row_errors = fetch_hk_financial_metrics(hk_instruments, start, end)
                financial_rows.extend(rows)
                errors.extend(row_errors)
            except Exception as exc:
                errors.append(f"HK financial metrics: {exc}")

    money_flow_rows: list[dict[str, object]] = []
    if fetch_money_flow and cn_instruments:
        try:
            money_flow_rows = fetch_current_money_flow(end)
        except Exception as exc:
            errors.append(f"money flow: {exc}")

    instrument_rows = [instrument.as_row() for instrument in {instrument for instrument in new_instruments}]
    return EventFetchResult(
        corporate_events=upsert_many(
            conn,
            "corporate_events",
            corporate_rows,
            ("market", "ticker", "event_date", "event_type", "title"),
        ),
        financial_metrics=upsert_many(
            conn,
            "financial_metrics",
            financial_rows,
            ("market", "ticker", "report_date", "metric_name"),
        ),
        money_flows=upsert_many(
            conn,
            "money_flows",
            money_flow_rows,
            ("market", "ticker", "date", "source"),
        ),
        instruments=upsert_many(conn, "instruments", instrument_rows, ("market", "ticker")),
        errors=tuple(errors),
    )
