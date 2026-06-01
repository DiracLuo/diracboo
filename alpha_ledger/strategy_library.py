from __future__ import annotations

from .db import dump_json
from .ledger import now_utc


STRATEGIES = [
    {
        "id": "trend_breakout",
        "name": "强趋势突破",
        "market_scope": "CN_A",
        "thesis": "在行业或指数强势阶段，价格突破中期平台且成交量确认时，趋势延续概率提高。",
        "entry_rules_json": dump_json(
            {
                "price": "收盘价突破20-60日平台或接近新高",
                "volume": "成交量大于20日均量1.5倍",
                "relative_strength": "强于对应指数",
                "avoid": "远离均线过大或连续高潮后不追高",
            }
        ),
        "exit_rules_json": dump_json(
            {
                "stop": "跌回突破平台或跌破启动日低点",
                "take_profit": "分批止盈，保留趋势仓直到跌破10/20日线",
            }
        ),
        "status": "ACTIVE",
        "weight": 1.0,
    },
    {
        "id": "abnormal_volume_small_midcap",
        "name": "中小盘异常放量异动",
        "market_scope": "CN_A",
        "thesis": "中小盘低关注度股票在催化出现前后，量价异动往往先于市场共识。",
        "entry_rules_json": dump_json(
            {
                "market_cap": "中小市值、流动性足够",
                "volume": "成交量显著放大但尚未连续涨停或高潮",
                "catalyst": "公告、订单、行业主题、调研或产品事件",
            }
        ),
        "exit_rules_json": dump_json(
            {
                "stop": "异动日低点或平台下沿失守",
                "take_profit": "连续加速或换手异常放大后减仓",
            }
        ),
        "status": "ACTIVE",
        "weight": 0.9,
    },
    {
        "id": "us_sec_event_momentum",
        "name": "美股SEC重大披露后动量",
        "market_scope": "US",
        "thesis": "8-K、10-Q、10-K等真实披露若叠加承接和放量，常是美股事件后再定价窗口。",
        "entry_rules_json": dump_json(
            {
                "event": "近5日 SEC 8-K/10-Q/10-K/S-1 披露",
                "price": "披露后不破位，最好红盘或温和放量",
                "confirmation": "次日不破事件日低点或继续放量承接",
            }
        ),
        "exit_rules_json": dump_json(
            {
                "stop": "跌破事件日低点或披露被市场解读为利空",
                "take_profit": "事件后动量衰减或到达第一目标",
            }
        ),
        "status": "EXPERIMENTAL",
        "weight": 1.0,
    },
    {
        "id": "us_news_event_momentum",
        "name": "美股新闻评级事件动量",
        "market_scope": "US",
        "thesis": "评级上调、目标价上调、订单、AI/产品催化等新闻若被量价确认，可形成短期动量。",
        "entry_rules_json": dump_json(
            {
                "event": "评级、目标价、财报、订单、产品或行业催化",
                "price": "新闻窗口内红盘承接或放量不跌",
                "avoid": "泛宏观早报、无公司特异性的转载新闻",
            }
        ),
        "exit_rules_json": dump_json({"stop": "跌破新闻窗口低点", "take_profit": "短线动量衰减"}),
        "status": "EXPERIMENTAL",
        "weight": 0.9,
    },
    {
        "id": "hk_buyback_recovery",
        "name": "港股回购修复",
        "market_scope": "HK",
        "thesis": "港股龙头持续回购或大额回购，若叠加趋势修复，容易推动估值底部抬升。",
        "entry_rules_json": dump_json(
            {
                "event": "回购、注销、分红派息或股东回报强化",
                "price": "回购后不再创新低并站回短期均线",
                "flow": "优先叠加南向资金或成交额改善",
            }
        ),
        "exit_rules_json": dump_json({"stop": "跌破回购窗口低点", "take_profit": "估值修复或趋势破坏"}),
        "status": "EXPERIMENTAL",
        "weight": 0.95,
    },
    {
        "id": "hk_southbound_recovery",
        "name": "港股南向资金修复",
        "market_scope": "HK",
        "thesis": "南向资金连续关注的港股龙头，在趋势不破位时更容易出现估值修复行情。",
        "entry_rules_json": dump_json(
            {
                "event": "南向资金、港股通、资金净买入或持股变化",
                "price": "价格承接改善，避免单日冲高回落",
                "confirmation": "连续资金线索或成交额放大",
            }
        ),
        "exit_rules_json": dump_json({"stop": "跌破资金窗口低点", "take_profit": "南向热度衰减或趋势破坏"}),
        "status": "EXPERIMENTAL",
        "weight": 0.9,
    },
    {
        "id": "hk_news_recovery",
        "name": "港股业绩新闻修复",
        "market_scope": "HK",
        "thesis": "业绩、AI、芯片、汽车、平台经济等公司特异性新闻，若叠加价格承接，可进入修复观察池。",
        "entry_rules_json": dump_json(
            {
                "event": "业绩、财报、业务增长、产品周期或行业政策新闻",
                "price": "新闻后红盘或放量不破位",
                "avoid": "泛市场新闻、无公司特异性的新闻",
            }
        ),
        "exit_rules_json": dump_json({"stop": "跌破新闻窗口低点", "take_profit": "修复兑现或趋势破坏"}),
        "status": "EXPERIMENTAL",
        "weight": 0.85,
    },
    {
        "id": "a_share_hard_event_catalyst",
        "name": "A股硬事件催化",
        "market_scope": "CN_A",
        "thesis": "A股只保留可映射到业绩或估值框架变化的硬事件，剔除普通会议、担保、弱公告。",
        "entry_rules_json": dump_json(
            {
                "event": "订单、合同、回购、增持、业绩预告、重组并购等硬事件；普通调研只作辅助证据",
                "price": "事件后不破位，最好有温和放量或首阳",
                "avoid": "普通调研、业绩说明会、担保授信、股东大会、纯转载新闻",
            }
        ),
        "exit_rules_json": dump_json({"stop": "跌破事件窗口低点", "take_profit": "事件兑现或加速后分批"}),
        "status": "ACTIVE",
        "weight": 0.9,
    },
    {
        "id": "cn_a_pead_quality_surprise",
        "name": "A股财报超预期漂移",
        "market_scope": "CN_A",
        "thesis": "利用A股财报披露后的信息消化滞后。业绩超预期但公告后未过度反应的股票，存在后续漂移机会。",
        "entry_rules_json": dump_json(
            {
                "event": "近1-5个交易日披露季报/年报/业绩快报/业绩预告修正",
                "profit_growth": "扣非净利润同比>=25%，营收同比>=10%，ROE TTM>=8%",
                "surprise": "实际值在预告上沿70%以上，或净利高于一致预期>=10%",
                "post_announcement": "公告后首日涨幅-2%到+7%，非一字涨停，量比1.2-2.8，收盘在MA20上方",
                "not_overextended": "过去20日涨幅<=25%，过去5日涨停次数<=1",
                "model_requirement": "M2或M3>=60%",
            }
        ),
        "exit_rules_json": dump_json(
            {
                "stop": "跌破公告日低点或-6%，取更近者",
                "take_profit": "+8%减半，+15%或T+20清仓；跌破MA20清仓",
            }
        ),
        "status": "ACTIVE",
        "weight": 0.8,
    },
    {
        "id": "xingye_style_prepositioning",
        "name": "兴业科技型重估埋伏启动",
        "market_scope": "CN_A",
        "thesis": (
            "兴业科技给出的核心启示不是普通调研后上涨，而是旧标签公司出现可验证的新增长叙事，"
            "股价没有马上高潮，随后在平台内出现温和吸筹与放量首阳，市场开始从旧估值框架切到新估值框架。"
        ),
        "entry_rules_json": dump_json(
            {
                "revaluation": "事件必须映射到新客户、新订单、新产能、新产品、海外业务、利润率改善或业务标签切换",
                "base": "事件后没有连续大涨，10日平台振幅可控，股价在平台内横盘或缓慢抬升",
                "accumulation": "启动前3-10日成交量温和抬升，红盘天数不弱，但没有单日高潮",
                "trigger": "2.5%-8.5%放量阳线、站上近10日高点或接近20日高点",
                "confirmation": "优先观察次日是否不破启动日中枢或继续放量承接",
                "avoid": "ST、退市风险、普通业绩说明会预告、普通担保授信、连续加速、公告无法映射到业绩",
            }
        ),
        "exit_rules_json": dump_json(
            {
                "stop": "跌破启动日低点或事件前平台下沿",
                "take_profit": "短线达到7%-16%目标后分批，涨停或连续加速必须复盘兑现风险",
            }
        ),
        "status": "ACTIVE",
        "weight": 1.15,
    },
]


def rows() -> list[dict[str, object]]:
    created_at = now_utc()
    default_version = "v1.1-cn-a-formal"
    horizon_by_strategy = {
        "trend_breakout": 20,
        "cn_a_pead_quality_surprise": 20,
        "xingye_style_prepositioning": 10,
        "abnormal_volume_small_midcap": 5,
    }
    rows: list[dict[str, object]] = []
    for strategy in STRATEGIES:
        rows.append(
            {
                **strategy,
                "version": default_version,
                "target_horizon_days": horizon_by_strategy.get(str(strategy["id"]), 10),
                "created_at": created_at,
            }
        )
    return rows
