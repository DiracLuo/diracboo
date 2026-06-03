from __future__ import annotations

import json
import os
import subprocess
import time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from czsc import CZSC, Freq, RawBar

from .alpha_factors import MULTI_MODEL_CONFIGS, _resolve_model_version
from .data_ops import CONFIDENCE_HIGH, audit_data_coverage
from .metrics import (
    FORMAL_MARKETS,
    candidate_action_leaderboard,
    candidate_horizon_strategy_leaderboard,
    candidate_market_leaderboard,
    candidate_strategy_leaderboard,
    score_calibration,
    strategy_risk_adjusted_metrics,
    suggest_strategy_weight_adjustments,
)
from .screener import _is_excluded_name


def fmt_pct(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def fmt_price(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def fmt_rate(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


MAX_ACTIONABLE_CANDIDATES = 5
FORMAL_MARKET_LABEL = ", ".join(FORMAL_MARKETS)
MODEL_LABELS = {
    ("qlib_alpha158", "t5_full_20260601"): "M1 (2024~ T+5)",
    ("qlib_alpha158_20250101", "t10_v3"): "M2 (2025~ T+10)",
    ("qlib_alpha158_20260101", "t10_v3"): "M3 (2026~ T+10)",
}


def _lb_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("LONGBRIDGE_LOG_PATH", "/private/tmp/alpha_ledger_longbridge_logs")
    Path(env["LONGBRIDGE_LOG_PATH"]).mkdir(parents=True, exist_ok=True)
    try:
        log_dir = Path.home() / "Library" / "Logs" / "Longbridge"
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        temp_home = Path("/private/tmp/alpha_ledger_longbridge_home")
        temp_home.mkdir(parents=True, exist_ok=True)
        os.chmod(temp_home, 0o700)
        source = Path(os.environ.get("HOME", "")).expanduser() / ".longbridge"
        target = temp_home / ".longbridge"
        if source.exists() and not target.exists() and not target.is_symlink():
            try:
                target.symlink_to(source, target_is_directory=True)
            except OSError:
                pass
        env["HOME"] = str(temp_home)
    return env


def _run_lb(args: list[str] | tuple[str, ...], timeout: int = 30) -> dict | None:
    cmd = ["longbridge", *args]
    if "--format" not in args:
        cmd.extend(["--format", "json"])
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_lb_env(),
        )
        if result.returncode != 0:
            return None
        text = result.stdout.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = _extract_json_payload(text)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    finally:
        time.sleep(0.15)
    return None


def _extract_json_payload(text: str) -> Any:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            data, _end = decoder.raw_decode(text[idx:])
            return data
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON payload", text, 0)


def _to_lb_symbol(ticker: str) -> str:
    symbol = str(ticker).strip().upper()
    if symbol.endswith(".SS"):
        return symbol[:-3] + ".SH"
    return symbol


def _norm_key(key: object) -> str:
    return "".join(
        char for char in str(key).lower()
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def _walk_values(data: Any):
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _walk_values(value)
    elif isinstance(data, list):
        yield data
        for value in data:
            yield from _walk_values(value)


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "--", "None", "null"}:
        return None
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100000000.0
    elif "万" in text:
        multiplier = 10000.0
    cleaned = (
        text.replace(",", "")
        .replace("%", "")
        .replace("+", "")
        .replace("(", "")
        .replace(")", "")
        .replace("（", "")
        .replace("）", "")
        .replace("亿", "")
        .replace("万", "")
        .replace("元", "")
        .replace("￥", "")
    )
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def _first_value(data: Any, keys: tuple[str, ...]) -> object | None:
    normalized = {_norm_key(key) for key in keys}
    for node in _walk_values(data):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if _norm_key(key) in normalized:
                return value
    return None


def _first_number(data: Any, keys: tuple[str, ...]) -> float | None:
    return _as_float(_first_value(data, keys))


def _find_number_by_terms(data: Any, terms: tuple[str, ...]) -> float | None:
    normalized_terms = tuple(_norm_key(term) for term in terms)
    for node in _walk_values(data):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            norm = _norm_key(key)
            if all(term in norm for term in normalized_terms):
                number = _as_float(value)
                if number is not None:
                    return number
    return None


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    abs_value = abs(value)
    if abs_value >= 100000000:
        return f"{sign}{value / 100000000:.2f} 亿"
    if abs_value >= 10000:
        return f"{sign}{value / 10000:.2f} 万"
    return f"{sign}{value:.2f}"


def _fmt_money_short(value: float | None) -> str:
    if value is None:
        return "-"
    abs_value = abs(value)
    if abs_value >= 100000000:
        return f"{value / 100000000:.2f}亿"
    if abs_value >= 10000:
        return f"{value / 10000:.0f}万"
    return f"{value:.0f}"


def _fmt_flow_money(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    abs_value = abs(value)
    if abs_value >= 10000000:
        return f"{sign}{value / 100000000:.1f}亿"
    if abs_value >= 10000:
        return f"{sign}{value / 10000:.0f}万"
    return f"{sign}{value:.0f}"


def _fmt_signed_pct(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_percentile(value: float | None) -> str:
    if value is None:
        return "-"
    pct = value * 100.0 if abs(value) <= 1.0 else value
    return f"{pct:.0f}%"


def _capital_value(data: Any, size: str) -> float | None:
    # Longbridge CLI 返回 {capital_in: {large, medium, small}, capital_out: {large, medium, small}}
    if isinstance(data, dict):
        cap_in = data.get("capital_in", {})
        cap_out = data.get("capital_out", {})
        if isinstance(cap_in, dict) and isinstance(cap_out, dict):
            in_val = _as_float(cap_in.get(size))
            out_val = _as_float(cap_out.get(size))
            if in_val is not None and out_val is not None:
                return in_val - out_val
    # 兼容扁平格式
    aliases = {
        "large": (
            "large_net_inflow",
            "large_order_net_inflow",
            "large_net_amount",
            "large_order_net_amount",
            "big_order_net_inflow",
            "big_net_inflow",
            "大单净流入",
            "大单净额",
        ),
        "medium": (
            "medium_net_inflow",
            "medium_order_net_inflow",
            "medium_net_amount",
            "medium_order_net_amount",
            "中单净流入",
            "中单净额",
        ),
        "small": (
            "small_net_inflow",
            "small_order_net_inflow",
            "small_net_amount",
            "small_order_net_amount",
            "小单净流入",
            "小单净额",
        ),
    }
    value = _first_number(data, aliases[size])
    if value is not None:
        return value
    terms = {"large": ("large", "net"), "medium": ("medium", "net"), "small": ("small", "net")}
    value = _find_number_by_terms(data, terms[size])
    if value is not None:
        return value
    cn_terms = {"large": ("大单", "净"), "medium": ("中单", "净"), "small": ("小单", "净")}
    return _find_number_by_terms(data, cn_terms[size])


def _market_temp_label(value: float) -> str:
    if value >= 80:
        return "过热"
    if value >= 60:
        return "偏热"
    if value >= 40:
        return "中性"
    if value >= 20:
        return "偏冷"
    return "低迷"


def _market_overview() -> str:
    lines: list[str] = []
    temp_data = _run_lb(["market-temp", "CN"])
    # market-temp 返回 {items: [{field: "Temperature", value: "84"}, ...]}
    temp = None
    desc = ""
    temp_items = temp_data.get("items", []) if isinstance(temp_data, dict) else (temp_data if isinstance(temp_data, list) else [])
    if isinstance(temp_items, list):
        for item in temp_items:
            if isinstance(item, dict):
                field = item.get("field", "").lower()
                value = item.get("value", "")
                if field == "temperature":
                    try:
                        temp = float(value)
                    except (ValueError, TypeError):
                        pass
                elif field == "description":
                    desc = value

    lines.append("## 市场环境")
    lines.append("")
    if temp is not None:
        label = desc if desc else _market_temp_label(temp)
        lines.append(f"- 市场温度：{temp:.0f}/100（{label}）")

    indices = [
        ("000300.SH", "沪深300"),
        ("000905.SH", "中证500"),
        ("399006.SZ", "创业板指"),
    ]
    for symbol, name in indices:
        time.sleep(0.15)
        data = _run_lb(["capital", symbol])
        flow = _capital_value(data, "large") if data else None
        if flow is not None:
            lines.append(f"- {name}({symbol})：大单净流入 {_fmt_money(flow)}")
        else:
            lines.append(f"- {name}({symbol})：暂无数据")
    return "\n".join(lines)


def _calc_rsi(closes: list[float], period: int = 14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _calc_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    def ema(data: list[float], n: int):
        k = 2 / (n + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    macd = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif[-1], dea[-1], macd[-1]


def _calc_kdj(highs: list[float], lows: list[float], closes: list[float], period: int = 9):
    if len(closes) < period:
        return None, None, None
    k, d = 50.0, 50.0
    for i in range(period - 1, len(closes)):
        h = max(highs[i - period + 1:i + 1])
        l = min(lows[i - period + 1:i + 1])
        rsv = (closes[i] - l) / (h - l) * 100 if h != l else 50
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    j = 3 * k - 2 * d
    return k, d, j


def _calc_risk(closes: list[float]):
    import math

    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] != 0]
    if len(returns) < 20:
        return {}
    vol = (sum(r**2 for r in returns[-20:]) / 20) ** 0.5 * math.sqrt(252) * 100
    peak = closes[0]
    max_dd = 0
    for c in closes:
        peak = max(peak, c)
        dd = (peak - c) / peak * 100
        max_dd = max(max_dd, dd)
    sr = sorted(returns)
    var95 = sr[int(len(sr) * 0.05)] * 100
    return {"vol": vol, "max_dd": max_dd, "var95": var95}


def _record_field(record: dict, keys: tuple[str, ...]) -> object | None:
    normalized = {_norm_key(key) for key in keys}
    for key, value in record.items():
        if _norm_key(key) in normalized:
            return value
    return None


def _extract_records(data: Any) -> list[dict]:
    best: list[dict] = []
    for node in _walk_values(data):
        if not isinstance(node, list):
            continue
        records = [item for item in node if isinstance(item, dict)]
        if len(records) > len(best):
            best = records
    return best


def _extract_kline(data: Any) -> tuple[list[float], list[float], list[float]]:
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for record in _extract_records(data):
        close = _as_float(_record_field(record, ("close", "c", "closing_price", "收盘价")))
        high = _as_float(_record_field(record, ("high", "h", "highest", "最高价")))
        low = _as_float(_record_field(record, ("low", "l", "lowest", "最低价")))
        if close is None or high is None or low is None:
            continue
        closes.append(close)
        highs.append(high)
        lows.append(low)
    return highs, lows, closes


def _parse_bar_dt(value: object, fallback_id: int) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 100000000000:
            number /= 1000.0
        return datetime.fromtimestamp(number)
    text = str(value or "").strip()
    if text:
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).replace(tzinfo=None)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(text[:10], fmt)
            except ValueError:
                continue
    return datetime(1970, 1, 1) + timedelta(days=fallback_id)


def _to_czsc_bars(ticker: str, data: Any) -> list[RawBar]:
    bars: list[RawBar] = []
    for idx, record in enumerate(_extract_records(data)):
        open_price = _as_float(_record_field(record, ("open", "o", "opening_price", "开盘价")))
        close = _as_float(_record_field(record, ("close", "c", "closing_price", "收盘价")))
        high = _as_float(_record_field(record, ("high", "h", "highest", "最高价")))
        low = _as_float(_record_field(record, ("low", "l", "lowest", "最低价")))
        if open_price is None or close is None or high is None or low is None:
            continue
        dt_value = _record_field(record, ("date", "datetime", "timestamp", "time", "trade_date", "日期", "时间"))
        volume = _as_float(_record_field(record, ("volume", "vol", "成交量"))) or 0.0
        amount = _as_float(_record_field(record, ("amount", "turnover", "成交额"))) or 0.0
        bars.append(
            RawBar(
                symbol=_to_lb_symbol(ticker),
                dt=_parse_bar_dt(dt_value, idx),
                freq=Freq.D,
                open=open_price,
                close=close,
                high=high,
                low=low,
                vol=volume,
                amount=amount,
                id=idx,
            )
        )
    bars.sort(key=lambda bar: bar.dt)
    unique: list[RawBar] = []
    seen: set[datetime] = set()
    for idx, bar in enumerate(bars):
        if bar.dt in seen:
            continue
        seen.add(bar.dt)
        unique.append(
            RawBar(
                symbol=bar.symbol,
                dt=bar.dt,
                freq=bar.freq,
                open=bar.open,
                close=bar.close,
                high=bar.high,
                low=bar.low,
                vol=bar.vol,
                amount=bar.amount,
                id=idx,
            )
        )
    return unique if len(unique) >= 20 else []


def _fmt_chan_dt(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%m-%d")
    text = str(value or "")
    return text[5:10] if len(text) >= 10 and text[4] in "-/" else text[:10]


def _chan_mark_text(mark: object) -> str:
    text = str(mark)
    if text in {"G", "Mark.G", "顶"} or "顶" in text:
        return "顶分型"
    if text in {"D", "Mark.D", "底"} or "底" in text:
        return "底分型"
    return text


def _chan_direction_text(direction: object) -> str:
    text = str(direction)
    if "上" in text or text.lower() == "up":
        return "上升笔"
    if "下" in text or text.lower() == "down":
        return "下降笔"
    return text


def _fmt_fx(c: CZSC) -> str:
    fx_list = list(getattr(c, "fx_list", []) or getattr(c, "ubi_fxs", []) or [])
    if not fx_list:
        return "-"
    fx = fx_list[-1]
    mark = _chan_mark_text(getattr(fx, "mark", ""))
    price = _as_float(getattr(fx, "fx", None))
    price_text = f"{price:.2f}" if price is not None else "-"
    return f"{mark} {_fmt_chan_dt(getattr(fx, 'dt', ''))} @{price_text}"


def _fmt_bi(c: CZSC) -> str:
    bi_list = list(getattr(c, "bi_list", []) or getattr(c, "finished_bis", []) or [])
    if not bi_list:
        return "-"
    bi = bi_list[-1]
    direction = _chan_direction_text(getattr(bi, "direction", ""))
    start = _fmt_chan_dt(getattr(bi, "sdt", ""))
    end = _fmt_chan_dt(getattr(bi, "edt", ""))
    low = _as_float(getattr(bi, "low", None))
    high = _as_float(getattr(bi, "high", None))
    if low is None or high is None:
        return f"{direction} {start}~{end}"
    return f"{direction} {start}~{end} [{low:.2f}, {high:.2f}]"


def _latest_zs_range(c: CZSC) -> tuple[float, float] | None:
    zs_list = getattr(c, "zs_list", None)
    if zs_list:
        zs = zs_list[-1]
        low = _as_float(getattr(zs, "dd", None) or getattr(zs, "low", None) or getattr(zs, "zg", None))
        high = _as_float(getattr(zs, "gg", None) or getattr(zs, "high", None) or getattr(zs, "zd", None))
        if low is not None and high is not None:
            return min(low, high), max(low, high)

    bi_list = list(getattr(c, "bi_list", []) or getattr(c, "finished_bis", []) or [])
    if len(bi_list) < 3:
        return None
    for i in range(len(bi_list) - 3, -1, -1):
        subset = bi_list[i:i + 3]
        highs = [_as_float(getattr(bi, "high", None)) for bi in subset]
        lows = [_as_float(getattr(bi, "low", None)) for bi in subset]
        if any(value is None for value in highs + lows):
            continue
        upper = min(highs)  # type: ignore[arg-type]
        lower = max(lows)  # type: ignore[arg-type]
        if upper > lower:
            return float(lower), float(upper)
    return None


def _fmt_zs(c: CZSC) -> str:
    zs_range = _latest_zs_range(c)
    if not zs_range:
        return "-"
    low, high = zs_range
    return f"{low:.2f}~{high:.2f}"


def _fmt_signal(c: CZSC) -> str:
    bi_list = list(getattr(c, "bi_list", []) or getattr(c, "finished_bis", []) or [])
    zs_range = _latest_zs_range(c)
    if not bi_list:
        return "暂无明确信号"

    last_bi = bi_list[-1]
    direction = _chan_direction_text(getattr(last_bi, "direction", ""))
    current = _as_float(getattr(c.bars_raw[-1], "close", None)) if getattr(c, "bars_raw", None) else None
    bi_low = _as_float(getattr(last_bi, "low", None))
    bi_high = _as_float(getattr(last_bi, "high", None))
    if current is None or bi_low is None or bi_high is None or not zs_range:
        return "暂无明确信号"

    zs_low, zs_high = zs_range
    if direction == "上升笔":
        if bi_low < zs_low and current <= zs_low * 1.03:
            return "一买观察"
        if zs_low <= bi_low <= zs_high and current > bi_low:
            return "二买观察"
        if bi_low >= zs_high * 0.98 and current > zs_high:
            return "三买观察"
    if direction == "下降笔":
        if bi_high > zs_high and current >= zs_high * 0.97:
            return "一卖观察"
        if zs_low <= bi_high <= zs_high and current < bi_high:
            return "二卖观察"
        if bi_high <= zs_low * 1.02 and current < zs_low:
            return "三卖观察"
    return "暂无明确信号"


def _chan_analysis_section(stocks: list[tuple[str, str, str]]) -> str:
    lines = ["## 缠论分析（czsc · Longbridge 300日K线）", ""]
    lines.append("| 股票 | 类型 | 最近分型 | 最近一笔 | 中枢 | 买卖点 |")
    lines.append("|------|------|----------|----------|------|--------|")

    for ticker, name, label in stocks:
        lb_sym = _to_lb_symbol(ticker)
        time.sleep(0.15)
        kline = _run_lb(["kline", lb_sym, "--period", "day", "--count", "300"])
        if not kline:
            lines.append(f"| {_md_cell(name)} | {_md_cell(label)} | - | - | - | 无数据 |")
            continue

        bars = _to_czsc_bars(ticker, kline)
        if not bars:
            lines.append(f"| {_md_cell(name)} | {_md_cell(label)} | - | - | - | 数据不足 |")
            continue

        try:
            c = CZSC(bars)
            fx_str = _fmt_fx(c)
            bi_str = _fmt_bi(c)
            zs_str = _fmt_zs(c)
            signal_str = _fmt_signal(c)
        except Exception as exc:
            fx_str = bi_str = zs_str = "-"
            signal_str = f"分析失败：{type(exc).__name__}"
        lines.append(
            f"| {_md_cell(name)} | {_md_cell(label)} | {_md_cell(fx_str)} | "
            f"{_md_cell(bi_str)} | {_md_cell(zs_str)} | {_md_cell(signal_str)} |"
        )

    lines.append("")
    return "\n".join(lines)


def _macd_signal(closes: list[float]) -> str:
    if len(closes) < 2:
        return "-"
    dif, dea, _macd = _calc_macd(closes)
    prev_dif, prev_dea, _prev_macd = _calc_macd(closes[:-1])
    if prev_dif <= prev_dea and dif > dea:
        return "金叉"
    if prev_dif >= prev_dea and dif < dea:
        return "死叉"
    return "多头" if dif > dea else "空头"


def _metric_pair(data: Any, metric: str) -> tuple[float | None, float | None]:
    metric_norm = _norm_key(metric)
    value_aliases = {
        "pe": ("pe", "pe_ttm", "pettm", "price_earning_ratio", "市盈率"),
        "pb": ("pb", "pb_ratio", "price_book_ratio", "市净率"),
    }
    pct_aliases = {
        "pe": ("pe_percentile", "pe_rank", "pe_industry_percentile", "pe_percentile_rank", "市盈率分位"),
        "pb": ("pb_percentile", "pb_rank", "pb_industry_percentile", "pb_percentile_rank", "市净率分位"),
    }
    value = _first_number(data, value_aliases[metric_norm])
    pct = _first_number(data, pct_aliases[metric_norm])
    if value is not None or pct is not None:
        return value, pct

    for node in _walk_values(data):
        if not isinstance(node, dict):
            continue
        label = _record_field(node, ("metric", "name", "indicator", "指标", "项目"))
        if label is None or metric_norm not in _norm_key(label):
            continue
        value = _as_float(_record_field(node, ("value", "current", "latest", "值", "当前值")))
        pct = _as_float(_record_field(node, ("percentile", "rank", "percentile_rank", "industry_percentile", "分位", "排名")))
        return value, pct
    return None, None


def _format_capital(data: dict | None) -> str:
    if not data:
        return "暂无数据（非交易时段可能为空）"
    large = _capital_value(data, "large")
    medium = _capital_value(data, "medium")
    small = _capital_value(data, "small")
    if large is None and medium is None and small is None:
        return "暂无数据（非交易时段可能为空）"
    return (
        f"大单 {_fmt_flow_money(large)} / "
        f"中单 {_fmt_flow_money(medium)} / 小单 {_fmt_flow_money(small)}"
    )


def _format_technical(data: dict | None) -> tuple[str, str]:
    if not data:
        return "暂无K线数据", "暂无足够K线数据"
    highs, lows, closes = _extract_kline(data)
    if not closes:
        return "暂无K线数据", "暂无足够K线数据"

    rsi = _calc_rsi(closes)
    k, d, j = _calc_kdj(highs, lows, closes)
    tech_parts = [f"MACD {_macd_signal(closes)}"]
    tech_parts.append(f"RSI {rsi:.1f}" if rsi is not None else "RSI -")
    if k is not None and d is not None and j is not None:
        tech_parts.append(f"KDJ K{k:.0f}/D{d:.0f}/J{j:.0f}")
    else:
        tech_parts.append("KDJ -")

    risk = _calc_risk(closes)
    if risk:
        risk_line = (
            f"波动率 {risk['vol']:.1f}% · "
            f"最大回撤 -{risk['max_dd']:.1f}% · VaR(95%) {risk['var95']:.1f}%"
        )
    else:
        risk_line = "暂无足够K线数据"
    return f"{' · '.join(tech_parts)}", risk_line


def _format_valuation(data: dict | None) -> str:
    if not data:
        return "暂无估值数据"
    pe, pe_pct = _metric_pair(data, "pe")
    pb, pb_pct = _metric_pair(data, "pb")
    if pe is None and pb is None:
        return "暂无估值数据"
    pe_text = f"PE {pe:.1f}" if pe is not None else "PE -"
    pb_text = f"PB {pb:.1f}" if pb is not None else "PB -"
    if pe_pct is not None:
        pe_text += f"（行业{_fmt_percentile(pe_pct)}分位）"
    if pb_pct is not None:
        pb_text += f"（行业{_fmt_percentile(pb_pct)}分位）"
    return f"{pe_text} · {pb_text}"


def _format_financial_report(data: dict | None) -> str:
    if not data:
        return "暂无最新财报数据"
    period = _first_value(data, ("period", "quarter", "report_period", "fiscal_period", "报告期", "季度"))
    revenue = _first_number(data, ("revenue", "operating_revenue", "total_revenue", "营业收入", "营收"))
    revenue_yoy = _first_number(data, ("revenue_yoy", "operating_revenue_yoy", "revenue_growth", "营收同比", "营业收入同比"))
    profit = _first_number(data, ("net_profit", "net_income", "net_profit_attributable", "归母净利润", "净利润"))
    profit_yoy = _first_number(data, ("net_profit_yoy", "net_income_yoy", "profit_growth", "净利同比", "净利润同比"))
    if revenue is None and profit is None:
        return "暂无最新财报数据"

    prefix = str(period) if period else "最新"
    parts = []
    if revenue is not None:
        text = f"营收 {_fmt_money_short(revenue)}"
        if revenue_yoy is not None:
            text += f"({_fmt_signed_pct(revenue_yoy)})"
        parts.append(text)
    if profit is not None:
        text = f"净利 {_fmt_money_short(profit)}"
        if profit_yoy is not None:
            text += f"({_fmt_signed_pct(profit_yoy)})"
        parts.append(text)
    return f"{prefix} {' · '.join(parts)}"


def _md_cell(value: object) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def _stock_deep_analysis(ticker: str, name: str | None = None) -> str:
    symbol = _to_lb_symbol(ticker)
    capital = _run_lb(["capital", symbol])
    kline = _run_lb(["kline", symbol, "--period", "day", "--count", "60"])
    valuation = _run_lb(["valuation", symbol])
    financial = _run_lb(["financial-report", symbol])

    technical_line, risk_line = _format_technical(kline)
    display_name = name or ticker
    rows = [
        ("资金流", _format_capital(capital)),
        ("技术面", technical_line),
        ("估值", _format_valuation(valuation)),
        ("财报", _format_financial_report(financial)),
        ("风险", risk_line),
    ]
    return "\n".join(
        [
            f"**{_md_cell(display_name)} `{ticker}`**",
            "| 维度 | 分析 |",
            "|------|------|",
            *[f"| {dim} | {_md_cell(text)} |" for dim, text in rows],
        ]
    )


def _industry_name(record: dict) -> str:
    value = _record_field(record, ("industry", "name", "sector", "行业", "板块"))
    return str(value) if value else "-"


def _record_pct(record: dict) -> float | None:
    return _as_float(_record_field(record, ("change_pct", "pct_chg", "change_rate", "chg", "涨跌幅", "涨幅")))


def _market_insights() -> str:
    lines: list[str] = []
    rank_data = _run_lb(["industry-rank", "--market", "CN"])
    # industry-rank 返回 {items: [{name, chg, lists: [{name, chg, ...}]}]}
    # 需要展平 lists 获取子行业数据
    rank_rows = []
    if rank_data and isinstance(rank_data, dict):
        for item in rank_data.get("items", []):
            for sub in item.get("lists", []):
                if sub.get("name") and sub.get("chg"):
                    rank_rows.append(sub)
    rank_rows = rank_rows[:10]
    if rank_rows:
        lines.append("## 行业热度 Top 10")
        lines.append("")
        lines.append("| 排名 | 行业 | 涨跌幅 |")
        lines.append("|---:|---|---:|")
        for idx, record in enumerate(rank_rows, start=1):
            lines.append(f"| {idx} | {_industry_name(record)} | {_fmt_signed_pct(_record_pct(record))} |")
        lines.append("")

    calendar_data = _run_lb([
        "finance-calendar",
        "--category",
        "report",
        "--start",
        "2026-05-29",
        "--end",
        "2026-06-05",
    ])
    calendar_rows = _extract_records(calendar_data) if calendar_data else []
    if calendar_rows:
        lines.append("## 近期催化剂")
        lines.append("")
        for record in calendar_rows[:20]:
            date_value = _record_field(record, ("date", "event_date", "report_date", "披露日期", "日期"))
            name = _record_field(record, ("name", "company_name", "title", "event", "公司", "证券简称"))
            ticker = _record_field(record, ("symbol", "ticker", "code", "代码"))
            label = str(name or ticker or "财报")
            date_text = str(date_value or "-")
            if len(date_text) >= 10 and date_text[4] == "-" and date_text[7] == "-":
                date_text = date_text[5:10]
            suffix = f" `{ticker}`" if ticker and name else ""
            lines.append(f"- {date_text} {label}{suffix} 财报")
    return "\n".join(lines).rstrip()


def _latest_price_date(conn: sqlite3.Connection, market: str = "CN_A") -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS max_date FROM price_bars WHERE market = ?",
        (market,),
    ).fetchone()
    return str(row["max_date"]) if row and row["max_date"] else None


def _next_business_day(date_value: str) -> str:
    day = datetime.strptime(date_value, "%Y-%m-%d").date()
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.isoformat()


def daily_action_plan(conn: sqlite3.Connection, as_of_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH strategy_perf AS (
            SELECT
                c.strategy_id,
                AVG(e.net_return_pct) AS avg_net_return,
                AVG(e.excess_return_pct) AS avg_excess_return,
                AVG(CASE WHEN e.net_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                AVG(CASE WHEN e.net_return_pct > 0 THEN e.net_return_pct END) AS avg_win,
                ABS(AVG(CASE WHEN e.net_return_pct <= 0 THEN e.net_return_pct END)) AS avg_loss
            FROM candidates c
            JOIN candidate_evaluations e ON e.candidate_id = c.id
            WHERE c.market = 'CN_A'
              AND c.as_of_date >= date(?, '-60 day')
              AND c.as_of_date < ?
            GROUP BY c.strategy_id
        ),
        scored AS (
        SELECT
            c.*,
            st.name AS strategy_name,
            st.version AS strategy_version,
            st.weight,
            COALESCE(
                c.expected_value_score,
                (
                    COALESCE(sp.win_rate, 0.50) * COALESCE(sp.avg_win, 4.0)
                    - (1.0 - COALESCE(sp.win_rate, 0.50)) * COALESCE(sp.avg_loss, 4.0)
                    - 0.18
                    + COALESCE(sp.avg_excess_return, 0.0)
                    + COALESCE(c.reward_risk_ratio, 0.0) * 0.5
                    + c.candidate_score / 100.0
                )
            ) AS expected_value_rank,
            CASE
                WHEN COALESCE(c.confirmation_status, 'PENDING') = 'CONFIRMED'
                     AND c.confirmation_date = ?
                THEN 1
                WHEN c.data_date IS NULL OR c.data_date = ''
                THEN 0
                WHEN c.market = 'CN_A' AND c.data_date = ?
                THEN 1
                ELSE 0
            END AS data_is_fresh,
            CASE
                WHEN c.data_date IS NOT NULL AND c.data_date != ''
                     AND c.market = 'CN_A'
                     AND c.data_date = ?
                     AND c.as_of_date = ?
                     AND c.action = 'BUY_CANDIDATE'
                     AND COALESCE(c.confirmation_status, 'PENDING') = 'PENDING'
                     AND c.candidate_score >= 78
                     AND COALESCE(c.reward_risk_ratio, 0) >= 1.5
                THEN '今日新信号'
                WHEN COALESCE(c.confirmation_status, 'PENDING') = 'CONFIRMED'
                     AND c.confirmation_date = ?
                     AND c.candidate_score >= 78
                     AND COALESCE(c.reward_risk_ratio, 0) >= 1.0
                THEN '今日确认'
                WHEN c.candidate_score >= 82
                     AND COALESCE(c.confirmation_status, 'PENDING') != 'CANCELLED'
                     AND COALESCE(c.reward_risk_ratio, 0) >= 1.5
                THEN '重点等确认'
                WHEN c.action LIKE '%CONFIRM%'
                     AND COALESCE(c.confirmation_status, 'PENDING') != 'CANCELLED'
                THEN '等确认'
                ELSE '观察'
            END AS plan_bucket,
            COALESCE(c.reward_risk_ratio,
                CASE WHEN COALESCE(c.stop_loss, 0) > 0 AND c.entry_price > c.stop_loss
                THEN (c.target_1 - c.entry_price) / (c.entry_price - c.stop_loss)
                END, 0) AS reward_risk
        FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        LEFT JOIN strategy_perf sp ON sp.strategy_id = c.strategy_id
        WHERE (c.as_of_date = ? OR c.confirmation_date = ?)
          AND c.market = 'CN_A'
          AND st.status != 'RETIRED'
          AND NOT (
              c.strategy_id = 'a_share_hard_event_catalyst'
              AND (
                  c.trigger_condition LIKE '%调研%'
                  OR c.trigger_condition LIKE '%投资者关系%'
                  OR c.trigger_condition LIKE '%业绩说明会%'
              )
              AND c.trigger_condition NOT LIKE '%订单%'
              AND c.trigger_condition NOT LIKE '%合同%'
              AND c.trigger_condition NOT LIKE '%回购%'
              AND c.trigger_condition NOT LIKE '%增持%'
              AND c.trigger_condition NOT LIKE '%业绩预告%'
              AND c.trigger_condition NOT LIKE '%净利润%'
              AND c.trigger_condition NOT LIKE '%收入增长%'
              AND c.trigger_condition NOT LIKE '%重组%'
              AND c.trigger_condition NOT LIKE '%收购%'
              AND c.trigger_condition NOT LIKE '%并购%'
          )
        ),
        planned AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY market, ticker
                    ORDER BY expected_value_rank DESC, candidate_score * weight DESC, id ASC
                ) AS rn
            FROM scored
        )
        SELECT *
        FROM planned
        WHERE rn = 1
          AND reward_risk >= 1.0
        ORDER BY
            CASE plan_bucket
                WHEN '今日新信号' THEN 1
                WHEN '今日确认' THEN 2
                WHEN '重点等确认' THEN 3
                WHEN '等确认' THEN 4
                ELSE 5
            END,
            expected_value_rank DESC,
            candidate_score * weight DESC,
            ticker
        """,
        (
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
            as_of_date,
        ),
    ).fetchall()


def _model_top_picks(conn: sqlite3.Connection, as_of_date: str) -> dict[str, list[dict]]:
    """每个模型返回 percentile 最高的 3 只股票（排除 ST/ETF/指数/基金）。

    Returns: {model_label: [{ticker, name, percentiles, close, change_pct}]}
    """
    model_configs = []
    for idx, config in enumerate(MULTI_MODEL_CONFIGS, start=1):
        model_name, model_version, _score_field, percentile_field, *rest = config
        short_label = f"M{idx}"
        display_label = str(rest[0]) if rest else MODEL_LABELS.get(
            (model_name, model_version),
            f"{short_label} ({model_name} {model_version})",
        )
        model_configs.append(
            {
                "model_name": model_name,
                "model_version": model_version,
                "percentile_field": percentile_field,
                "short_label": short_label,
                "display_label": display_label,
            }
        )

    all_scores: dict[str, dict[str, float]] = {}
    for config in model_configs:
        resolved_ver = _resolve_model_version(
            conn, config["model_name"], config["model_version"], as_of_date,
        )
        if not resolved_ver:
            continue
        config["resolved_version"] = resolved_ver
        row = conn.execute(
            """
            SELECT MAX(score_date) AS d
            FROM model_scores
            WHERE model_name = ?
              AND model_version = ?
              AND market = 'CN_A'
              AND score_date <= ?
            """,
            (config["model_name"], resolved_ver, as_of_date),
        ).fetchone()
        if not row or not row["d"]:
            continue

        rows = conn.execute(
            """
            SELECT ticker, percentile
            FROM model_scores
            WHERE model_name = ?
              AND model_version = ?
              AND market = 'CN_A'
              AND score_date = ?
            """,
            (config["model_name"], resolved_ver, row["d"]),
        ).fetchall()
        for score_row in rows:
            if score_row["percentile"] is None:
                continue
            percentile = float(score_row["percentile"])
            if percentile > 1.0:
                percentile /= 100.0
            ticker = str(score_row["ticker"])
            all_scores.setdefault(ticker, {})[str(config["short_label"])] = percentile

    candidate_rows = conn.execute(
        """
        SELECT DISTINCT ticker
        FROM candidates
        WHERE market = 'CN_A' AND as_of_date = ?
        """,
        (as_of_date,),
    ).fetchall()
    candidate_tickers = {str(row["ticker"]) for row in candidate_rows}

    result: dict[str, list[dict]] = {}
    for config in model_configs:
        resolved_ver = config.get("resolved_version") or _resolve_model_version(
            conn, config["model_name"], config["model_version"], as_of_date,
        )
        if not resolved_ver:
            continue
        row = conn.execute(
            """
            SELECT MAX(score_date) AS d
            FROM model_scores
            WHERE model_name = ?
              AND model_version = ?
              AND market = 'CN_A'
              AND score_date <= ?
            """,
            (config["model_name"], resolved_ver, as_of_date),
        ).fetchone()
        if not row or not row["d"]:
            continue

        score_rows = conn.execute(
            """
            SELECT ticker, percentile
            FROM model_scores
            WHERE model_name = ?
              AND model_version = ?
              AND market = 'CN_A'
              AND score_date = ?
            ORDER BY percentile DESC
            """,
            (config["model_name"], resolved_ver, row["d"]),
        ).fetchall()

        picks: list[dict] = []
        for score_row in score_rows:
            ticker = str(score_row["ticker"])
            if ticker in candidate_tickers:
                continue

            price_row = conn.execute(
                """
                SELECT
                    p.ticker,
                    COALESCE(i.name, p.ticker) AS name,
                    p.close,
                    prev.close AS prev_close
                FROM price_bars p
                LEFT JOIN instruments i
                  ON i.market = p.market
                 AND i.ticker = p.ticker
                LEFT JOIN price_bars prev
                  ON prev.market = p.market
                 AND prev.ticker = p.ticker
                 AND prev.date = (
                     SELECT MAX(date)
                     FROM price_bars
                     WHERE market = p.market
                       AND ticker = p.ticker
                       AND date < p.date
                 )
                WHERE p.market = 'CN_A'
                  AND p.ticker = ?
                  AND p.date = ?
                """,
                (ticker, as_of_date),
            ).fetchone()
            if not price_row:
                continue

            name = str(price_row["name"])
            if _is_excluded_name(name):
                continue

            close = float(price_row["close"])
            prev_close = price_row["prev_close"]
            change_pct = None
            if prev_close is not None and float(prev_close) > 0:
                change_pct = (close - float(prev_close)) / float(prev_close) * 100.0

            picks.append({
                "ticker": ticker,
                "name": name,
                "percentiles": all_scores.get(ticker, {}),
                "close": close,
                "change_pct": change_pct,
            })
            if len(picks) >= 3:
                break

        if picks:
            result[str(config["display_label"])] = picks

    return result


def render_daily_plan(conn: sqlite3.Connection, as_of_date: str) -> str:
    rows = daily_action_plan(conn, as_of_date)

    def _fmt_model_pct(row: sqlite3.Row, field: str) -> str:
        val = row[field] if field in row.keys() else None
        return f"{float(val):.0%}" if val is not None else "-"

    def _fmt_model_pick_pct(pick: dict, label: str) -> str:
        val = pick.get("percentiles", {}).get(label)
        return f"{float(val):.0%}" if val is not None else "-"

    latest_date = _latest_price_date(conn, "CN_A")
    stale = latest_date is not None and as_of_date > latest_date
    data_status = "STALE_DATA" if stale else "FRESH"
    audit = audit_data_coverage(
        conn,
        as_of_date,
        as_of_date,
        "CN_A",
        write=False,
        ignore_adjustment_for_short_term=True,
    )
    confidence_level = audit.confidence_level
    trade_plan_date = _next_business_day(as_of_date if not stale else latest_date or as_of_date)
    lines: list[str] = []
    lines.append(f"# Alpha Ledger Daily Plan - {as_of_date}")
    lines.append("")
    lines.append(f"- data_as_of_date: `{as_of_date}`")
    lines.append(f"- 数据说明：策略筛选基于 {as_of_date} 本地数据；Longbridge 实时数据在报告生成时获取")
    lines.append(f"- trade_plan_date: `{trade_plan_date}`")
    lines.append(f"- data_status: `{data_status}`")
    lines.append(f"- confidence_level: `{confidence_level}`")
    if stale:
        lines.append(f"- 最新完整行情仅到 `{latest_date}`，本报告不生成“今日可买”。")
    if confidence_level != CONFIDENCE_HIGH:
        lines.append("- 数据审计未达到 HIGH_CONFIDENCE，本报告不输出强买入结论。")
    if confidence_level == CONFIDENCE_HIGH and audit.adjustment_coverage_pct < 95.0:
        lines.append(f"- adjustment_note: 复权覆盖 {audit.adjustment_coverage_pct:.1f}%，短期策略可用，中长期回测建议补全前复权（`python scripts/backfill_qfq.py`）。")
    elif confidence_level == CONFIDENCE_HIGH and audit.adjustment_coverage_pct >= 95.0:
        lines.append(f"- adjustment_note: 全量复权数据（{audit.adjustment_coverage_pct:.1f}%），回测结论可靠。")
    for note in audit.notes:
        lines.append(f"- data_note: {note}")
    lines.append(f"- 正式交易范围：{FORMAL_MARKET_LABEL}。美股/港股暂为实验数据，不进入今日买入清单。")
    lines.append("")
    overview = _market_overview()
    if overview:
        lines.append(overview)
        lines.append("")
    if not rows:
        lines.append("暂无可操作候选。")
        return "\n".join(lines).rstrip() + "\n"

    # MEDIUM_CONFIDENCE 也显示今日新信号/今日确认，但加风险提示
    fresh = [] if stale else [r for r in rows if r["plan_bucket"] == "今日新信号"]
    confirmed_today = [] if stale else [r for r in rows if r["plan_bucket"] == "今日确认"]
    confirmation = [r for r in rows if r["plan_bucket"] in ("重点等确认", "等确认")]
    observation = [r for r in rows if r["plan_bucket"] == "观察"]
    top_picks = _model_top_picks(conn, as_of_date)
    analysis_cache: dict[str, str] = {}

    def _append_deep_analysis_section(title: str, stocks: list[tuple[str, str]]) -> None:
        if not stocks:
            return
        lines.append(f"### {title} · 深度分析")
        lines.append("")
        lines.append("> 数据来源：Longbridge CLI")
        lines.append("")
        for ticker, name in stocks:
            if ticker not in analysis_cache:
                analysis_cache[ticker] = _stock_deep_analysis(ticker, name)
            if analysis_cache[ticker]:
                lines.append(analysis_cache[ticker])
                lines.append("")

    if fresh:
        lines.append(f"## 今日新信号（基于 {as_of_date} 数据筛选，最多 {MAX_ACTIONABLE_CANDIDATES} 只）")
        lines.append("")
        lines.append("| 股票 | 代码 | 策略 | 策略分 | M1 | M2 | M3 | 建议入手 | 止损 | 目标 | 风报比 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in fresh[:MAX_ACTIONABLE_CANDIDATES]:
            stop = float(row["stop_loss"] or 0.0)
            sig_close = float(row["signal_close"] or row["entry_price"] or 0.0)
            bz_low = round(sig_close * 0.985, 2)
            m1 = _fmt_model_pct(row, "model_percentile")
            m2 = _fmt_model_pct(row, "model_percentile_2")
            m3 = _fmt_model_pct(row, "model_percentile_3")
            model_str = f"{m1} | {m2} | {m3}"
            target_str = f"{fmt_price(row['target_1'])} / {fmt_price(row['target_2'])}"
            lines.append(
                "| "
                f"{row['name']} | `{row['ticker']}` | {row['strategy_name']} | "
                f"{float(row['candidate_score']):.1f} | {model_str} | "
                f"{fmt_price(bz_low)} | {fmt_price(stop)} | {target_str} | "
                f"{float(row['reward_risk']):.2f} |"
            )
        lines.append("")
        _append_deep_analysis_section(
            "今日新信号",
            [(str(row["ticker"]), str(row["name"])) for row in fresh[:MAX_ACTIONABLE_CANDIDATES]],
        )

    if confirmed_today:
        lines.append(f"## 今日确认信号（往日信号 + {as_of_date} 确认，执行价以确认日次日为准）")
        lines.append("")
        lines.append("| 股票 | 代码 | 策略 | 策略分 | M1 | M2 | M3 | 确认日收盘 | 建议入手 | 止损 | 目标 | 风报比 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in confirmed_today[:MAX_ACTIONABLE_CANDIDATES]:
            stop = float(row["stop_loss"] or 0.0)
            sig_close = float(row["signal_close"] or row["entry_price"] or 0.0)
            bz_low = float(row["buy_zone_low"] or 0.0)
            confirm_close_row = conn.execute(
                "SELECT close FROM price_bars WHERE market=? AND ticker=? AND date=?",
                (row["market"], row["ticker"], as_of_date),
            ).fetchone()
            confirm_close = float(confirm_close_row[0]) if confirm_close_row else None
            confirm_display = fmt_price(confirm_close) if confirm_close else "-"
            m1 = _fmt_model_pct(row, "model_percentile")
            m2 = _fmt_model_pct(row, "model_percentile_2")
            m3 = _fmt_model_pct(row, "model_percentile_3")
            model_str = f"{m1} | {m2} | {m3}"
            target_str = f"{fmt_price(row['target_1'])} / {fmt_price(row['target_2'])}"
            lines.append(
                "| "
                f"{row['name']} | `{row['ticker']}` | {row['strategy_name']} | "
                f"{float(row['candidate_score']):.1f} | {model_str} | {confirm_display} | "
                f"{fmt_price(bz_low)} | {fmt_price(stop)} | {target_str} | "
                f"{float(row['reward_risk']):.2f} |"
            )
        lines.append("")
        _append_deep_analysis_section(
            "今日确认",
            [(str(row["ticker"]), str(row["name"])) for row in confirmed_today[:MAX_ACTIONABLE_CANDIDATES]],
        )

    if top_picks:
        lines.append("## 模型选股（Top 3，仅供参考，非策略筛选）")
        lines.append("")
        lines.append("> 每个模型选出预测分数最高的 3 只股票。仅供参考，不计入正式买入清单。")
        lines.append("")
        for model_label, picks in top_picks.items():
            lines.append(f"### {model_label}")
            lines.append("")
            lines.append("| 股票 | 代码 | M1 | M2 | M3 | 收盘价 | 涨跌幅 |")
            lines.append("|---|---|---:|---:|---:|---:|---:|")
            for p in picks:
                chg_str = f"{p['change_pct']:.1f}%" if p.get("change_pct") is not None else "-"
                lines.append(
                    f"| {p['name']} | `{p['ticker']}` | "
                    f"{_fmt_model_pick_pct(p, 'M1')} | {_fmt_model_pick_pct(p, 'M2')} | {_fmt_model_pick_pct(p, 'M3')} | "
                    f"{p['close']:.2f} | {chg_str} |"
                )
            lines.append("")
            _append_deep_analysis_section(
                str(model_label),
                [(str(p["ticker"]), str(p["name"])) for p in picks],
            )

    high_priority = [r for r in confirmation if r["plan_bucket"] == "重点等确认"]
    normal_confirmation = [r for r in confirmation if r["plan_bucket"] == "等确认"]

    if high_priority:
        lines.append("## 重点等确认（高分 WATCH_PULLBACK，等待回调企稳确认）")
        lines.append("")
        lines.append("| 股票 | 代码 | 策略 | 策略分 | M1 | M2 | M3 | 建议入手 | 止损 | 目标 | 风报比 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for row in high_priority[:10]:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 40:
                trigger = trigger[:37] + "..."
            sig_close = float(row["signal_close"] or row["entry_price"] or 0.0)
            bz_low = round(sig_close * 0.985, 2)
            m1 = _fmt_model_pct(row, "model_percentile")
            m2 = _fmt_model_pct(row, "model_percentile_2")
            m3 = _fmt_model_pct(row, "model_percentile_3")
            model_str = f"{m1} | {m2} | {m3}"
            target_str = f"{fmt_price(row['target_1'])} / {fmt_price(row['target_2'])}"
            lines.append(
                "| "
                f"{row['name']} | `{row['ticker']}` | {row['strategy_name']} | "
                f"{float(row['candidate_score']):.1f} | {model_str} | "
                f"{fmt_price(bz_low)} | {fmt_price(row['stop_loss'])} | {target_str} | "
                f"{float(row['reward_risk']):.2f} | {trigger} |"
            )
        lines.append("")

    if normal_confirmation:
        lines.append("## 等确认（WATCH_OR_BUY_ON_CONFIRMATION，等待次日确认）")
        lines.append("")
        lines.append("| 股票 | 代码 | 策略 | 策略分 | M1 | M2 | M3 | 建议入手 | 止损 | 目标 | 风报比 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for row in normal_confirmation[:10]:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 40:
                trigger = trigger[:37] + "..."
            sig_close = float(row["signal_close"] or row["entry_price"] or 0.0)
            bz_low = round(sig_close * 0.985, 2)
            m1 = _fmt_model_pct(row, "model_percentile")
            m2 = _fmt_model_pct(row, "model_percentile_2")
            m3 = _fmt_model_pct(row, "model_percentile_3")
            model_str = f"{m1} | {m2} | {m3}"
            target_str = f"{fmt_price(row['target_1'])} / {fmt_price(row['target_2'])}"
            lines.append(
                "| "
                f"{row['name']} | `{row['ticker']}` | {row['strategy_name']} | "
                f"{float(row['candidate_score']):.1f} | {model_str} | "
                f"{fmt_price(bz_low)} | {fmt_price(row['stop_loss'])} | {target_str} | "
                f"{float(row['reward_risk']):.2f} | {trigger} |"
            )
        lines.append("")

    if observation:
        lines.append(f"## 观察池（{len(observation)} 只）")
        lines.append("")
        lines.append("- 以下候选风报比不足或条件不满足，仅供研究参考，不建议直接买入。")
        lines.append("")

    lines.append("## 淘汰规则")
    lines.append("")
    lines.append("- 跌破止损或事件窗口低点，直接淘汰。")
    lines.append("- 等确认候选若次日不放量承接或收盘跌回触发位下方，降级观察。")
    lines.append("- 同一股票同日多策略重叠时，只按最高分策略处理，避免重复下注。")

    chan_stocks: list[tuple[str, str, str]] = []
    for row in fresh[:MAX_ACTIONABLE_CANDIDATES]:
        chan_stocks.append((str(row["ticker"]), str(row["name"]), "今日新信号"))
    for row in confirmed_today[:MAX_ACTIONABLE_CANDIDATES]:
        chan_stocks.append((str(row["ticker"]), str(row["name"]), "今日确认"))
    for model_label, picks in top_picks.items():
        for p in picks:
            chan_stocks.append((str(p["ticker"]), str(p["name"]), str(model_label)))

    if chan_stocks:
        lines.append("")
        lines.append(_chan_analysis_section(chan_stocks))

    return "\n".join(lines).rstrip() + "\n"


def write_daily_plan(
    conn: sqlite3.Connection,
    as_of_date: str,
    out_path: Path | str | None = None,
) -> Path:
    path = Path(out_path) if out_path else Path("reports") / f"daily_plan_{as_of_date}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_daily_plan(conn, as_of_date), encoding="utf-8")
    return path


def replay_daily_summary(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            GROUP BY candidate_id
        ),
        latest_eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        )
        SELECT
            c.as_of_date,
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(e.id) AS evaluated_count,
            AVG(e.return_pct) AS avg_return_pct,
            AVG(e.net_return_pct) AS avg_net_return_pct,
            AVG(e.benchmark_return_pct) AS avg_benchmark_return_pct,
            AVG(e.excess_return_pct) AS avg_excess_return_pct,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_target_1 = 1 THEN 1.0 ELSE 0.0 END END) AS target_1_rate,
            AVG(CASE WHEN e.id IS NOT NULL THEN CASE WHEN e.hit_stop = 1 THEN 1.0 ELSE 0.0 END END) AS stop_rate
        FROM candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        JOIN strategies st ON st.id = c.strategy_id
        WHERE c.as_of_date >= ? AND c.as_of_date <= ?
          AND c.market = 'CN_A'
          AND st.status != 'RETIRED'
        GROUP BY c.as_of_date
        ORDER BY c.as_of_date
        """,
        (start_date, end_date),
    ).fetchall()


def replay_samples(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    *,
    order: str,
    limit: int = 15,
) -> list[sqlite3.Row]:
    direction = "ASC" if order == "worst" else "DESC"
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            GROUP BY candidate_id
        )
        SELECT
            c.as_of_date,
            c.name,
            c.ticker,
            c.market,
            st.name AS strategy_name,
            st.version AS strategy_version,
            c.candidate_score,
            c.action,
            c.entry_price,
            c.stop_loss,
            c.target_1,
            c.trigger_condition,
            e.observed_days,
            e.execution_date,
            e.execution_price,
            e.execution_type,
            e.end_date,
            e.end_close,
            e.return_pct,
            e.net_return_pct,
            e.benchmark_return_pct,
            e.excess_return_pct,
            e.max_gain_pct,
            e.max_drawdown_pct,
            e.hit_stop,
            e.hit_target_1,
            e.hit_target_2,
            e.exit_type,
            e.exit_date,
            e.exit_price,
            e.exit_note
        FROM candidates c
        JOIN strategies st ON st.id = c.strategy_id
        JOIN latest l ON l.candidate_id = c.id
        JOIN candidate_evaluations e
          ON e.candidate_id = c.id
         AND e.through_date = l.through_date
        WHERE c.as_of_date >= ? AND c.as_of_date <= ?
          AND c.market = 'CN_A'
          AND st.status != 'RETIRED'
        ORDER BY e.net_return_pct {direction}, c.candidate_score DESC
        LIMIT ?
        """,
        (start_date, end_date, limit),
    ).fetchall()


def replay_horizon_strategy_matrix(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    *,
    dedupe: bool = True,
) -> list[sqlite3.Row]:
    dedupe_sql = "WHERE rn = 1" if dedupe else ""
    return conn.execute(
        f"""
        WITH latest AS (
            SELECT candidate_id, horizon_days, MAX(through_date) AS through_date
            FROM candidate_horizon_evaluations
            WHERE through_date <= ?
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
            WHERE c.as_of_date >= ?
              AND c.as_of_date <= ?
              AND c.market = 'CN_A'
              AND st.status != 'RETIRED'
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
            COUNT(DISTINCT c.id) AS candidate_count,
            COUNT(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_5d,
            AVG(CASE WHEN e.horizon_days = 5 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_5d,
            COUNT(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.benchmark_return_pct END) AS avg_benchmark_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN e.excess_return_pct END) AS avg_excess_return_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_10d,
            AVG(CASE WHEN e.horizon_days = 10 AND e.observed_days >= e.horizon_days AND e.excess_return_pct IS NOT NULL THEN CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS excess_win_rate_10d,
            COUNT(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_20d,
            AVG(CASE WHEN e.horizon_days = 20 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_20d,
            COUNT(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.id END) AS completed_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.return_pct END) AS avg_return_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN e.net_return_pct END) AS avg_net_return_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.return_pct > 0 THEN 1.0 ELSE 0.0 END END) AS win_rate_60d,
            AVG(CASE WHEN e.horizon_days = 60 AND e.observed_days >= e.horizon_days THEN CASE WHEN e.net_win = 1 THEN 1.0 ELSE 0.0 END END) AS net_win_rate_60d
        FROM selected_candidates c
        LEFT JOIN latest_eval e ON e.candidate_id = c.id
        GROUP BY c.strategy_id, c.strategy_name, c.strategy_version
        ORDER BY COALESCE(avg_net_return_10d, avg_net_return_5d, -999) DESC, completed_10d DESC, candidate_count DESC
        """,
        (through_date, start_date, end_date),
    ).fetchall()


def replay_data_quality_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    price_end_date: str | None = None,
) -> sqlite3.Row:
    price_end = price_end_date or end_date
    return conn.execute(
        """
        WITH formal_candidates AS (
            SELECT id
            FROM candidates
            WHERE as_of_date >= ?
              AND as_of_date <= ?
              AND market = 'CN_A'
        )
        SELECT
            (SELECT COUNT(*) FROM formal_candidates) AS candidate_count,
            (
                SELECT COUNT(*)
                FROM candidate_evaluations e
                JOIN formal_candidates c ON c.id = e.candidate_id
                WHERE e.execution_type = 'NEXT_OPEN_DAILY'
            ) AS daily_fallback_count,
            (
                SELECT COUNT(*)
                FROM price_bars p
                JOIN candidates c
                  ON c.market = p.market
                 AND c.ticker = p.ticker
                 AND c.as_of_date = p.date
                WHERE c.id IN (SELECT id FROM formal_candidates)
                  AND COALESCE(p.volume, 0) <= 0
            ) AS zero_volume_signal_day_count,
            (
                SELECT COUNT(*)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker != '000300.SS'
                  AND date >= ?
                  AND date <= ?
            ) AS price_bar_count,
            (
                SELECT COUNT(*)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker != '000300.SS'
                  AND date >= ?
                  AND date <= ?
                  AND adjustment_status = 'ADJUSTED'
            ) AS adjusted_price_bar_count,
            (
                SELECT COUNT(*)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker != '000300.SS'
                  AND date >= ?
                  AND date <= ?
                  AND adjustment_status = 'RAW_FALLBACK'
            ) AS raw_fallback_price_bar_count,
            (
                SELECT COUNT(DISTINCT date)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND ticker = '000300.SS'
                  AND date >= ?
                  AND date <= ?
            ) AS benchmark_day_count,
            (
                SELECT COUNT(DISTINCT date)
                FROM price_bars
                WHERE market = 'CN_A'
                  AND date >= ?
                  AND date <= ?
            ) AS trading_day_count
        """,
        (
            start_date,
            end_date,
            start_date,
            price_end,
            start_date,
            price_end,
            start_date,
            price_end,
            start_date,
            price_end,
            start_date,
            price_end,
        ),
    ).fetchone()


def replay_first_signal_strategy_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            WHERE through_date <= ?
            GROUP BY candidate_id
        ),
        eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        ),
        ranked AS (
            SELECT
                c.*,
                st.name AS strategy_name,
                st.version AS strategy_version,
                e.net_return_pct,
                e.benchmark_return_pct,
                e.excess_return_pct,
                ROW_NUMBER() OVER (
                    PARTITION BY c.market, c.ticker, c.strategy_id
                    ORDER BY c.as_of_date, c.id
                ) AS first_signal_rank
            FROM candidates c
            JOIN strategies st ON st.id = c.strategy_id
            LEFT JOIN eval e ON e.candidate_id = c.id
            WHERE c.as_of_date >= ?
              AND c.as_of_date <= ?
              AND c.market IN ('CN_A')
        )
        SELECT
            strategy_id,
            strategy_name,
            strategy_version,
            COUNT(*) AS candidate_count,
            SUM(CASE WHEN net_return_pct IS NOT NULL THEN 1 ELSE 0 END) AS evaluated_count,
            AVG(candidate_score) AS avg_candidate_score,
            AVG(net_return_pct) AS avg_net_return_pct,
            AVG(benchmark_return_pct) AS avg_benchmark_return_pct,
            AVG(excess_return_pct) AS avg_excess_return_pct,
            AVG(CASE WHEN excess_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS excess_win_rate,
            AVG(CASE WHEN net_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS net_win_rate
        FROM ranked
        WHERE first_signal_rank = 1
        GROUP BY strategy_id, strategy_name, strategy_version
        ORDER BY avg_excess_return_pct IS NULL, avg_excess_return_pct DESC, evaluated_count DESC
        """,
        (through_date, start_date, end_date),
    ).fetchall()


def replay_event_quality_summary(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
            SELECT candidate_id, MAX(through_date) AS through_date
            FROM candidate_evaluations
            WHERE through_date <= ?
            GROUP BY candidate_id
        ),
        eval AS (
            SELECT e.*
            FROM candidate_evaluations e
            JOIN latest l
              ON l.candidate_id = e.candidate_id
             AND l.through_date = e.through_date
        )
        SELECT
            CASE
                WHEN c.trigger_condition LIKE '%调研%'
                  OR c.trigger_condition LIKE '%投资者关系%'
                  OR c.trigger_condition LIKE '%业绩说明会%'
                THEN '泛调研/IR'
                ELSE '硬事件/其他'
            END AS segment,
            COUNT(*) AS candidate_count,
            SUM(CASE WHEN e.net_return_pct IS NOT NULL THEN 1 ELSE 0 END) AS evaluated_count,
            AVG(c.candidate_score) AS avg_candidate_score,
            AVG(e.net_return_pct) AS avg_net_return_pct,
            AVG(e.benchmark_return_pct) AS avg_benchmark_return_pct,
            AVG(e.excess_return_pct) AS avg_excess_return_pct,
            AVG(CASE WHEN e.excess_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS excess_win_rate,
            AVG(CASE WHEN e.net_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS net_win_rate,
            NULL AS target_1_rate,
            NULL AS stop_rate,
            NULL AS avg_max_gain_pct,
            NULL AS avg_max_drawdown_pct
        FROM candidates c
        LEFT JOIN eval e ON e.candidate_id = c.id
        WHERE c.as_of_date >= ?
          AND c.as_of_date <= ?
          AND c.market = 'CN_A'
          AND c.strategy_id IN ('a_share_hard_event_catalyst', 'xingye_style_prepositioning')
        GROUP BY segment
        ORDER BY avg_excess_return_pct IS NULL, avg_excess_return_pct DESC
        """,
        (through_date, start_date, end_date),
    ).fetchall()


def render_replay_report(conn: sqlite3.Connection, start_date: str, end_date: str, through_date: str) -> str:
    leaderboard = candidate_strategy_leaderboard(conn, start_date, end_date, through_date)
    deduped_leaderboard = candidate_strategy_leaderboard(
        conn,
        start_date,
        end_date,
        through_date,
        dedupe=True,
    )
    horizon_matrix = replay_horizon_strategy_matrix(conn, start_date, end_date, through_date, dedupe=True)
    market_leaderboard = candidate_market_leaderboard(conn, start_date, end_date, through_date)
    action_leaderboard = candidate_action_leaderboard(conn, start_date, end_date, through_date)
    first_signal_leaderboard = replay_first_signal_strategy_summary(conn, start_date, end_date, through_date)
    event_quality = replay_event_quality_summary(conn, start_date, end_date, through_date)
    weight_suggestions = suggest_strategy_weight_adjustments(conn, start_date, end_date, through_date)
    calibration = score_calibration(conn, start_date, end_date, through_date, 10)
    risk_metrics = strategy_risk_adjusted_metrics(conn, start_date, end_date, through_date)
    data_quality = replay_data_quality_summary(conn, start_date, end_date, through_date)
    daily = replay_daily_summary(conn, start_date, end_date)
    winners = replay_samples(conn, start_date, end_date, order="best")
    losers = replay_samples(conn, start_date, end_date, order="worst")
    total_candidates = sum(int(row["candidate_count"]) for row in daily)
    total_evaluated = sum(int(row["evaluated_count"]) for row in daily)
    if data_quality is not None:
        price_bar_count = int(data_quality["price_bar_count"] or 0)
        adjusted_price_bar_count = int(data_quality["adjusted_price_bar_count"] or 0)
        raw_fallback_price_bar_count = int(data_quality["raw_fallback_price_bar_count"] or 0)
        benchmark_day_count = int(data_quality["benchmark_day_count"] or 0)
        trading_day_count = int(data_quality["trading_day_count"] or 0)
    else:
        price_bar_count = 0
        adjusted_price_bar_count = 0
        raw_fallback_price_bar_count = 0
        benchmark_day_count = 0
        trading_day_count = 0
    adjusted_coverage = adjusted_price_bar_count / price_bar_count * 100.0 if price_bar_count else 0.0
    benchmark_coverage = benchmark_day_count / trading_day_count * 100.0 if trading_day_count else 0.0

    lines: list[str] = []
    lines.append(f"# Alpha Ledger Replay - {start_date} to {end_date}")
    lines.append("")
    lines.append("## 结论摘要")
    lines.append("")
    lines.append(
        f"- 回放区间覆盖 {len(daily)} 个有候选日期，共 {total_candidates} 个候选，"
        f"其中 {total_evaluated} 个已用 {through_date} 后验验证。"
    )
    lines.append(f"- 正式统计范围：{FORMAL_MARKET_LABEL}。美股/港股保留为实验能力，但不进入本报告正式收益结论。")
    lines.append("- 回放只使用候选日当时可见的价格、事件日期和已披露财务数据；历史资金流若无可回放数据，不参与打分。")
    lines.append("- 候选日不假设可在收盘成交；有分时数据时按次一交易日开盘前5根K线VWAP估算入场，缺分时才回退日线开盘价。")
    lines.append("- 收益统计默认使用复权价格；成交价仍展示原始价格。")
    if adjusted_coverage < 95.0:
        lines.append(f"- WARNING: 前复权覆盖率仅 {adjusted_coverage:.1f}%，当前回测收益不属于高置信正式结论。")
    if benchmark_coverage < 95.0:
        lines.append(f"- WARNING: 沪深300基准覆盖率仅 {benchmark_coverage:.1f}%，不能可靠判断 alpha。")
    lines.append("- 固定周期榜只统计完整走满 T+5/T+10/T+20/T+60 的样本；未走满的候选继续等待，不计入正式胜率。")
    lines.append("- 策略榜同时展示原始候选和去重候选；去重规则为同一日期同一股票只保留分数最高的策略。")
    lines.append("- 首信号口径只保留同一股票同一策略窗口的第一次正式信号，避免连续上涨股票重复放大胜率。")
    lines.append("")

    lines.append("## 固定持有周期策略榜")
    lines.append("")
    if horizon_matrix:
        lines.append("| 策略 | 候选数 | T+5样本 | T+5净均值 | T+5净胜率 | T+10样本 | T+10净均值 | T+10基准 | T+10超额 | T+10超额胜率 | T+20样本 | T+20净均值 | T+20净胜率 | T+60样本 | T+60净均值 | T+60净胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in horizon_matrix:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}@{row['strategy_version']}` | {row['candidate_count']} | "
                f"{row['completed_5d']} | {fmt_pct(row['avg_net_return_5d'])} | {fmt_rate(row['net_win_rate_5d'])} | "
                f"{row['completed_10d']} | {fmt_pct(row['avg_net_return_10d'])} | {fmt_pct(row['avg_benchmark_return_10d'])} | "
                f"{fmt_pct(row['avg_excess_return_10d'])} | {fmt_rate(row['excess_win_rate_10d'])} | "
                f"{row['completed_20d']} | {fmt_pct(row['avg_net_return_20d'])} | {fmt_rate(row['net_win_rate_20d'])} | "
                f"{row['completed_60d']} | {fmt_pct(row['avg_net_return_60d'])} | {fmt_rate(row['net_win_rate_60d'])} |"
            )
    else:
        lines.append("暂无固定周期候选回放数据。")
    lines.append("")

    def append_strategy_table(title: str, rows: list[sqlite3.Row], risk_metrics: dict[str, dict[str, float]] | None = None) -> None:
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("暂无策略回放数据。")
            lines.append("")
            return
        lines.append("| 策略 | 候选数 | 已验证 | 均分 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 目标1率 | 目标2率 | 止损率 | 夏普 | 索提诺 | MFE | MAE |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            sid = str(row["strategy_id"])
            rm = (risk_metrics or {}).get(sid, {})
            sharpe = rm.get("sharpe_ratio", 0.0)
            sortino = rm.get("sortino_ratio", 0.0)
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}@{row['strategy_version']}` | {row['candidate_count']} | "
                f"{row['evaluated_count']} | {fmt_price(row['avg_candidate_score'])} | "
                f"{fmt_pct(row['avg_net_return_pct'])} | {fmt_pct(row['avg_benchmark_return_pct'])} | "
                f"{fmt_pct(row['avg_excess_return_pct'])} | {fmt_rate(row['excess_win_rate'])} | "
                f"{fmt_rate(row['net_win_rate'])} | "
                f"{fmt_rate(row['target_1_rate'])} | {fmt_rate(row['target_2_rate'])} | "
                f"{fmt_rate(row['stop_rate'])} | {sharpe:.2f} | {sortino:.2f} | "
                f"{fmt_pct(row['avg_max_gain_pct'])} | "
                f"{fmt_pct(row['avg_max_drawdown_pct'])} |"
            )
        lines.append("")

    append_strategy_table("## 截止日候选策略胜率", leaderboard, risk_metrics)
    append_strategy_table("## 截止日去重后策略胜率", deduped_leaderboard, risk_metrics)

    lines.append("## 同股同策略首信号胜率")
    lines.append("")
    if first_signal_leaderboard:
        lines.append("| 策略 | 候选数 | 已验证 | 均分 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in first_signal_leaderboard:
            lines.append(
                "| "
                f"{row['strategy_name']} `{row['strategy_id']}@{row['strategy_version']}` | {row['candidate_count']} | "
                f"{row['evaluated_count']} | {fmt_price(row['avg_candidate_score'])} | "
                f"{fmt_pct(row['avg_net_return_pct'])} | {fmt_pct(row['avg_benchmark_return_pct'])} | "
                f"{fmt_pct(row['avg_excess_return_pct'])} | {fmt_rate(row['excess_win_rate'])} | "
                f"{fmt_rate(row['net_win_rate'])} |"
            )
    else:
        lines.append("暂无首信号回放数据。")
    lines.append("")

    def append_segment_table(title: str, rows: list[sqlite3.Row], segment_name: str) -> None:
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("暂无分组回放数据。")
            lines.append("")
            return
        lines.append(f"| {segment_name} | 候选数 | 已验证 | 均分 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 目标1率 | 止损率 | MFE | MAE |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                "| "
                f"{row['segment']} | {row['candidate_count']} | {row['evaluated_count']} | "
                f"{fmt_price(row['avg_candidate_score'])} | {fmt_pct(row['avg_net_return_pct'])} | "
                f"{fmt_pct(row['avg_benchmark_return_pct'])} | {fmt_pct(row['avg_excess_return_pct'])} | "
                f"{fmt_rate(row['excess_win_rate'])} | {fmt_rate(row['net_win_rate'])} | {fmt_rate(row['target_1_rate'])} | "
                f"{fmt_rate(row['stop_rate'])} | {fmt_pct(row['avg_max_gain_pct'])} | "
                f"{fmt_pct(row['avg_max_drawdown_pct'])} |"
            )
        lines.append("")

    append_segment_table("## 分市场表现", market_leaderboard, "市场")
    append_segment_table("## 触发类型表现", action_leaderboard, "触发类型")
    append_segment_table("## 事件质量表现", event_quality, "事件质量")

    lines.append("## 分数校准（T+10净收益）")
    lines.append("")
    if calibration:
        lines.append("| 分数桶 | 样本 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 止损率 | 目标率 | 最差收益 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in calibration:
            lines.append(
                "| "
                f"{row['score_bucket']} | {row['sample_count']} | {fmt_pct(row['avg_net_return'])} | "
                f"{fmt_pct(row['avg_benchmark_return'])} | {fmt_pct(row['avg_excess_return'])} | "
                f"{fmt_rate(row['excess_win_rate'])} | {fmt_rate(row['net_win_rate'])} | {fmt_rate(row['stop_rate'])} | "
                f"{fmt_rate(row['target_rate'])} | {fmt_pct(row['worst_return'])} |"
            )
        high = calibration[0]
        low = calibration[-1]
        high_metric = high["avg_excess_return"] if high["avg_excess_return"] is not None else high["avg_net_return"]
        low_metric = low["avg_excess_return"] if low["avg_excess_return"] is not None else low["avg_net_return"]
        if high_metric is not None and low_metric is not None and float(high_metric) <= float(low_metric):
            lines.append("")
            lines.append("- WARNING: 高分桶超额收益不优于低分桶，当前分数不应单独作为买入排序依据。")
    else:
        lines.append("暂无完整 T+10 分数校准样本。")
    lines.append("")

    lines.append("## 数据质量审计")
    lines.append("")
    lines.append(
        f"- 正式候选数：{int(data_quality['candidate_count'] or 0)}；"
        f"缺分时而回退日线开盘的评估：{int(data_quality['daily_fallback_count'] or 0)}；"
        f"信号日零成交候选：{int(data_quality['zero_volume_signal_day_count'] or 0)}。"
    )
    lines.append(
        f"- 前复权覆盖：{adjusted_price_bar_count}/{price_bar_count} ({adjusted_coverage:.1f}%)；"
        f"原始价格回退：{raw_fallback_price_bar_count}。"
    )
    lines.append(
        f"- 基准覆盖：沪深300 {benchmark_day_count}/{trading_day_count} 个交易日 ({benchmark_coverage:.1f}%)。"
    )
    lines.append("- 回退日线开盘或零成交样本会降低执行可信度，正式调参时应单独复核。")
    lines.append("")

    lines.append("## 策略权重建议（基于各策略目标周期）")
    lines.append("")
    if weight_suggestions:
        lines.append("| 策略 | 目标周期 | 已验证 | 当前权重 | 建议权重 | 建议 | 止损率 | 超额胜率 | 平均超额 | 平均净收益 | 原因 |")
        lines.append("|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---|")
        for item in weight_suggestions:
            lines.append(
                "| "
                f"{item['strategy_name']} `{item['strategy_id']}@{item.get('strategy_version', 'v1')}` | T+{item.get('target_horizon_days', 10)} | "
                f"{item['evaluated_count']} | "
                f"{float(item['current_weight']):.2f} | {float(item['suggested_weight']):.2f} | "
                f"{item['recommendation']} | {fmt_rate(item['stop_rate'])} | "
                f"{fmt_rate(item.get('excess_win_rate'))} | {fmt_pct(item.get('avg_excess_return_pct'))} | "
                f"{fmt_pct(item['avg_return_pct'])} | "
                f"{item['reason']} |"
            )
    else:
        lines.append("暂无策略权重建议。")
    lines.append("")

    lines.append("## 每日回放概览")
    lines.append("")
    if daily:
        lines.append("| 日期 | 候选数 | 已验证 | 平均净收益 | 平均基准 | 平均超额 | 超额胜率 | 净胜率 | 目标1率 | 止损率 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in daily:
            lines.append(
                "| "
                f"{row['as_of_date']} | {row['candidate_count']} | {row['evaluated_count']} | "
                f"{fmt_pct(row['avg_net_return_pct'])} | {fmt_pct(row['avg_benchmark_return_pct'])} | "
                f"{fmt_pct(row['avg_excess_return_pct'])} | {fmt_rate(row['excess_win_rate'])} | "
                f"{fmt_rate(row['net_win_rate'])} | "
                f"{fmt_rate(row['target_1_rate'])} | {fmt_rate(row['stop_rate'])} |"
            )
    else:
        lines.append("暂无每日候选。")
    lines.append("")

    def append_sample_table(title: str, rows: list[sqlite3.Row]) -> None:
        lines.append(title)
        lines.append("")
        if not rows:
            lines.append("暂无样本。")
            lines.append("")
            return
        lines.append("| 日期 | 股票 | 策略 | 分数 | 计划入场 | 执行价 | 退出 | 退出价 | 净收益 | 基准 | 超额 | 最大浮盈 | 最大回撤 | 触发摘要 |")
        lines.append("|---|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---|")
        for row in rows:
            trigger = str(row["trigger_condition"]).replace("|", "/")
            if len(trigger) > 90:
                trigger = trigger[:87] + "..."
            lines.append(
                "| "
                f"{row['as_of_date']} | {row['name']} `{row['ticker']}` | {row['strategy_name']} `{row['strategy_version']}` | "
                f"{float(row['candidate_score']):.1f} | {fmt_price(row['entry_price'])} | "
                f"{fmt_price(row['execution_price'])} | {row['exit_type']} {row['exit_date']} | "
                f"{fmt_price(row['exit_price'])} | "
                f"{fmt_pct(row['net_return_pct'])} | {fmt_pct(row['benchmark_return_pct'])} | "
                f"{fmt_pct(row['excess_return_pct'])} | "
                f"{fmt_pct(row['max_gain_pct'])} | {fmt_pct(row['max_drawdown_pct'])} | "
                f"{trigger} |"
            )
        lines.append("")

    append_sample_table("## 最强样本", winners)
    append_sample_table("## 最弱样本", losers)

    return "\n".join(lines).rstrip() + "\n"


def write_replay_report(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    through_date: str,
    out_path: Path | str | None = None,
) -> Path:
    path = (
        Path(out_path)
        if out_path
        else Path("reports") / f"replay_{start_date}_{end_date}_through_{through_date}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_replay_report(conn, start_date, end_date, through_date), encoding="utf-8")
    return path
