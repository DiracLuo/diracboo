"""简化缠论分析模块。

基于日线数据实现：
- K线包含处理（合并）
- 顶底分型识别
- 笔识别（分型 + 至少5根合并K线）
- 中枢识别（至少3笔重叠区间）
- 当前位置判断（趋势方向、中枢关系）
- 买卖点判断（简化版）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MergedKline:
    """合并后的K线。"""
    idx: int
    start_date: str
    end_date: str
    high: float
    low: float
    open: float
    close: float
    volume: float
    direction: int = 0  # 1=上升, -1=下降, 0=未定


@dataclass
class Fractal:
    """分型（顶或底）。"""
    idx: int
    date: str
    price: float
    kind: str  # "top" or "bottom"


@dataclass
class Stroke:
    """笔。"""
    start: Fractal
    end: Fractal
    direction: int  # 1=上升笔, -1=下降笔
    bars_count: int = 0

    @property
    def amplitude(self) -> float:
        return abs(self.end.price - self.start.price)

    @property
    def amplitude_pct(self) -> float:
        if self.start.price == 0:
            return 0.0
        return self.amplitude / self.start.price * 100


@dataclass
class Hub:
    """中枢。"""
    strokes: list[Stroke]
    high: float  # 中枢上沿 = min(各笔高点)
    low: float   # 中枢下沿 = max(各笔低点)

    @property
    def center(self) -> float:
        return (self.high + self.low) / 2

    @property
    def range_pct(self) -> float:
        if self.center == 0:
            return 0.0
        return (self.high - self.low) / self.center * 100


@dataclass
class ChanResult:
    """缠论分析结果。"""
    strokes: list[Stroke] = field(default_factory=list)
    hubs: list[Hub] = field(default_factory=list)
    trend: str = "震荡"  # "上升趋势"、"下降趋势"、"震荡"
    position: str = "中枢内部"  # "中枢上方"、"中枢下方"、"中枢内部"
    current_price: float = 0.0
    latest_hub: Hub | None = None
    buy_signal: str = ""  # "一买"、"二买"、"三买" 或 ""
    sell_signal: str = ""  # "一卖"、"二卖"、"三卖" 或 ""
    summary: str = ""


class ChanAnalyzer:
    """简化缠论分析器。"""

    def __init__(self, klines: list[dict]):
        """klines: [{date, open, high, low, close, volume}]"""
        self.klines = klines

    def merge_klines(self) -> list[MergedKline]:
        """K线包含处理。"""
        if not self.klines:
            return []

        merged = []
        for i, k in enumerate(self.klines):
            mk = MergedKline(
                idx=i,
                start_date=k["date"],
                end_date=k["date"],
                high=k["high"],
                low=k["low"],
                open=k["open"],
                close=k["close"],
                volume=k["volume"],
            )
            if not merged:
                merged.append(mk)
                continue

            prev = merged[-1]
            # 包含关系：当前K线被前一根包含，或前一根被当前包含
            if (mk.high <= prev.high and mk.low >= prev.low) or \
               (mk.high >= prev.high and mk.low <= prev.low):
                # 合并：取高点最大值和低点最小值
                if len(merged) >= 2 and merged[-2].high > merged[-2].low:
                    direction = 1 if merged[-1].high > merged[-2].high else -1
                else:
                    direction = 1 if mk.close > mk.open else -1

                if direction == 1:  # 上升中合并
                    prev.high = max(prev.high, mk.high)
                    prev.low = max(prev.low, mk.low)
                else:  # 下降中合并
                    prev.high = min(prev.high, mk.high)
                    prev.low = min(prev.low, mk.low)
                prev.end_date = mk.end_date
                prev.volume += mk.volume
            else:
                merged.append(mk)

        # 设置方向
        for i in range(1, len(merged)):
            if merged[i].high > merged[i - 1].high:
                merged[i].direction = 1
            elif merged[i].high < merged[i - 1].high:
                merged[i].direction = -1
            else:
                merged[i].direction = merged[i - 1].direction

        return merged

    def find_fractals(self, merged: list[MergedKline]) -> list[Fractal]:
        """识别顶底分型。"""
        fractals = []
        for i in range(1, len(merged) - 1):
            prev, curr, next_ = merged[i - 1], merged[i], merged[i + 1]
            # 顶分型：中间K线高点最高
            if curr.high > prev.high and curr.high > next_.high:
                fractals.append(Fractal(
                    idx=i,
                    date=curr.end_date,
                    price=curr.high,
                    kind="top",
                ))
            # 底分型：中间K线低点最低
            elif curr.low < prev.low and curr.low < next_.low:
                fractals.append(Fractal(
                    idx=i,
                    date=curr.end_date,
                    price=curr.low,
                    kind="bottom",
                ))
        return fractals

    def find_strokes(self, merged: list[MergedKline], fractals: list[Fractal]) -> list[Stroke]:
        """识别笔（顶底分型交替 + 至少5根合并K线）。"""
        if len(fractals) < 2:
            return []

        strokes = []
        i = 0
        while i < len(fractals) - 1:
            start = fractals[i]
            # 找下一个不同类型的分型
            j = i + 1
            while j < len(fractals) and fractals[j].kind == start.kind:
                j += 1
            if j >= len(fractals):
                break

            end = fractals[j]
            # 检查距离：至少4根合并K线（笔的标准）
            bars_count = abs(end.idx - start.idx)
            if bars_count >= 4:
                direction = 1 if end.price > start.price else -1
                # 验证方向一致性
                if (start.kind == "bottom" and direction == 1) or \
                   (start.kind == "top" and direction == -1):
                    strokes.append(Stroke(
                        start=start,
                        end=end,
                        direction=direction,
                        bars_count=bars_count,
                    ))
                    i = j
                    continue
            i += 1

        return strokes

    def find_hubs(self, strokes: list[Stroke]) -> list[Hub]:
        """识别中枢（至少3笔有重叠区间）。"""
        if len(strokes) < 3:
            return []

        hubs = []
        i = 0
        while i <= len(strokes) - 3:
            # 取3笔
            s1, s2, s3 = strokes[i], strokes[i + 1], strokes[i + 2]
            # 计算重叠区间
            highs = [max(s.start.price, s.end.price) for s in [s1, s2, s3]]
            lows = [min(s.start.price, s.end.price) for s in [s1, s2, s3]]
            hub_high = min(highs)  # 重叠上沿
            hub_low = max(lows)    # 重叠下沿

            if hub_high > hub_low:
                # 有重叠，形成中枢
                hub = Hub(
                    strokes=[s1, s2, s3],
                    high=hub_high,
                    low=hub_low,
                )
                hubs.append(hub)
                i += 3
            else:
                i += 1

        return hubs

    def determine_position(self, current_price: float, hubs: list[Hub], strokes: list[Stroke]) -> tuple[str, str]:
        """判断当前位置和趋势。"""
        if not hubs:
            # 没有中枢，看笔的方向
            if strokes:
                last = strokes[-1]
                if last.direction == 1:
                    return "上升（无中枢）", "中枢上方"
                else:
                    return "下降（无中枢）", "中枢下方"
            return "震荡", "中枢内部"

        latest_hub = hubs[-1]

        # 判断趋势：连续中枢上移=上升，下移=下降
        if len(hubs) >= 2:
            prev_hub = hubs[-2]
            if latest_hub.low > prev_hub.high:
                trend = "上升趋势"
            elif latest_hub.high < prev_hub.low:
                trend = "下降趋势"
            else:
                trend = "震荡"
        else:
            # 只有一个中枢，看最后一笔方向
            if strokes and strokes[-1].direction == 1:
                trend = "偏多震荡"
            elif strokes and strokes[-1].direction == -1:
                trend = "偏空震荡"
            else:
                trend = "震荡"

        # 判断位置
        if current_price > latest_hub.high:
            position = "中枢上方"
        elif current_price < latest_hub.low:
            position = "中枢下方"
        else:
            position = "中枢内部"

        return trend, position

    def detect_signals(self, strokes: list[Stroke], hubs: list[Hub], current_price: float) -> tuple[str, str]:
        """简化版买卖点检测。"""
        buy_signal = ""
        sell_signal = ""

        if not strokes or not hubs:
            return buy_signal, sell_signal

        latest_hub = hubs[-1]
        last_stroke = strokes[-1]

        # 一买：下降趋势中，价格跌破中枢下沿后回升
        if len(strokes) >= 5:
            recent = strokes[-5:]
            down_count = sum(1 for s in recent if s.direction == -1)
            if down_count >= 3 and current_price < latest_hub.low:
                # 检查最后一笔是否开始向上
                if last_stroke.direction == 1 and last_stroke.amplitude_pct > 3:
                    buy_signal = "一买（趋势反转）"

        # 二买：一买后回踩不破前低
        if not buy_signal and len(strokes) >= 3:
            s1, s2, s3 = strokes[-3], strokes[-2], strokes[-1]
            if s1.direction == 1 and s2.direction == -1 and s3.direction == 1:
                if s2.end.price > s1.start.price:  # 回踩不破前低
                    buy_signal = "二买（回踩确认）"

        # 三买：回踩中枢上沿不破
        if not buy_signal and len(strokes) >= 2:
            s1, s2 = strokes[-2], strokes[-1]
            if s1.direction == 1 and s2.direction == -1:
                if s2.end.price >= latest_hub.high * 0.98:  # 接近中枢上沿
                    buy_signal = "三买（中枢上沿支撑）"

        # 卖点：对称逻辑
        if len(strokes) >= 5:
            recent = strokes[-5:]
            up_count = sum(1 for s in recent if s.direction == 1)
            if up_count >= 3 and current_price > latest_hub.high:
                if last_stroke.direction == -1 and last_stroke.amplitude_pct > 3:
                    sell_signal = "一卖（趋势反转）"

        if not sell_signal and len(strokes) >= 3:
            s1, s2, s3 = strokes[-3], strokes[-2], strokes[-1]
            if s1.direction == -1 and s2.direction == 1 and s3.direction == -1:
                if s2.end.price < s1.start.price:
                    sell_signal = "二卖（反弹确认）"

        return buy_signal, sell_signal

    def analyze(self) -> ChanResult:
        """执行完整缠论分析。"""
        if not self.klines:
            return ChanResult(summary="无K线数据")

        merged = self.merge_klines()
        fractals = self.find_fractals(merged)
        strokes = self.find_strokes(merged, fractals)
        hubs = self.find_hubs(strokes)

        current_price = self.klines[-1]["close"]
        trend, position = self.determine_position(current_price, hubs, strokes)
        buy_signal, sell_signal = self.detect_signals(strokes, hubs, current_price)

        # 生成摘要
        summary_parts = []

        # 最近一笔
        if strokes:
            last = strokes[-1]
            direction_str = "上升" if last.direction == 1 else "下降"
            summary_parts.append(
                f"最近一笔：{direction_str}笔，{last.start.date}~{last.end.date}，"
                f"幅度 {last.amplitude_pct:.1f}%"
            )

        # 中枢
        if hubs:
            latest = hubs[-1]
            summary_parts.append(
                f"最近中枢：{latest.low:.2f}~{latest.high:.2f}，"
                f"当前价 {current_price:.2f} {position}"
            )

        # 趋势
        summary_parts.append(f"趋势：{trend}")

        # 买卖点
        if buy_signal:
            summary_parts.append(f"买点信号：{buy_signal}")
        if sell_signal:
            summary_parts.append(f"卖点信号：{sell_signal}")
        if not buy_signal and not sell_signal:
            summary_parts.append("无明确买卖点")

        return ChanResult(
            strokes=strokes,
            hubs=hubs,
            trend=trend,
            position=position,
            current_price=current_price,
            latest_hub=hubs[-1] if hubs else None,
            buy_signal=buy_signal,
            sell_signal=sell_signal,
            summary="；".join(summary_parts),
        )


def format_chan_analysis(result: ChanResult, ticker: str, name: str) -> str:
    """将缠论分析结果格式化为 markdown。"""
    lines = []
    lines.append(f"<details>")
    lines.append(f"<summary>缠论分析：{name} {ticker}</summary>")
    lines.append("")
    lines.append(f"- {result.summary}")

    if result.strokes:
        last = result.strokes[-1]
        direction_str = "↑" if last.direction == 1 else "↓"
        lines.append(f"- 最近一笔：{direction_str} {last.start.date} → {last.end.date}，幅度 {last.amplitude_pct:.1f}%")

    if result.latest_hub:
        h = result.latest_hub
        lines.append(f"- 中枢区间：{h.low:.2f} ~ {h.high:.2f}（振幅 {h.range_pct:.1f}%）")

    if result.buy_signal:
        lines.append(f"- **🟢 {result.buy_signal}**")
    if result.sell_signal:
        lines.append(f"- **🔴 {result.sell_signal}**")

    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)
