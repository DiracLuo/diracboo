# Alpha Ledger

Alpha Ledger 是一个以盈利为目标的股票预测与策略进化账本。它不追求一开始就做成复杂平台，而是先把三件事做扎实：

1. 事前预测：每个候选股必须留下当时的买入逻辑、价格、触发条件、止损、目标和风险。
2. 事中跟踪：每天更新是否触发买点、止损、止盈、突破或失效。
3. 事后验证：用 T+5 / T+10 / T+20 / T+60 收益、回撤、胜率、盈亏比和超额收益淘汰策略。

系统、平台和自动化都只是手段；最终目标是让能够赚钱的策略获得更高权重，让不能赚钱的逻辑尽快暴露。

## 当前 MVP

v0.1 包含：

- SQLite 预测账本
- 策略库与策略权重
- 兴业科技 `002674.SZ` 案例数据
- 兴业科技型重估埋伏启动策略
- A 股公告、调研、财务、资金流接入
- 历史候选回放与候选策略胜率报告
- 固定持有周期候选验证：T+5 / T+10 / T+20 / T+60
- 保守退出口径：止损和止盈同日触及时，若无日内顺序数据，按止损先发生处理
- A 股候选目标价优先使用 ATR 波动目标，美股和港股保留策略默认目标
- 策略目标周期字段，用于区分短线异动、事件催化和中期趋势策略
- 事后评估与 Markdown 报告
- 账本完整性校验

## 快速运行

```bash
python -m alpha_ledger bootstrap --as-of 2026-05-25
```

运行后会生成：

- `data/alpha_ledger.sqlite`：本地预测账本
- `reports/alpha_report_2026-05-25.md`：策略与信号报告

也可以拆开执行：

```bash
python -m alpha_ledger init
python -m alpha_ledger seed
python -m alpha_ledger fetch-prices --start 2026-04-01 --end 2026-05-25 --markets US,HK,CN_A
python -m alpha_ledger fetch-events --start 2026-04-01 --end 2026-05-15 --markets CN_A --skip-money-flow
python -m alpha_ledger screen --as-of 2026-05-13
python -m alpha_ledger replay --start 2026-04-01 --end 2026-05-15 --through 2026-05-25
python -m alpha_ledger evaluate --as-of 2026-05-25
python -m alpha_ledger audit --as-of 2026-05-25
python -m alpha_ledger report --as-of 2026-05-25
python -m alpha_ledger verify
```

## 常用命令

查看信号：

```bash
python -m alpha_ledger signals
```

查看策略排行榜：

```bash
python -m alpha_ledger leaderboard
```

查看某天候选股：

```bash
python -m alpha_ledger candidates --as-of 2026-05-13
```

回放一段历史区间，并把每一天的候选用后续日期验证。`through` 是本次回放可看到的最后日期；报告会同时输出两类结果：

- 固定周期验证：按候选日后第一个交易日开盘价执行，分别统计 T+5 / T+10 / T+20 / T+60，只有走满周期的样本才计入正式胜率。
- 截止日复盘：统一观察到 `through`，用于阶段复盘和查看最大浮盈/回撤。

```bash
python -m alpha_ledger replay \
  --start 2026-04-01 \
  --end 2026-05-15 \
  --through 2026-05-25
```

接入默认美股、港股、A股样本池行情：

```bash
python -m alpha_ledger fetch-prices --start 2026-04-01 --end 2026-05-25 --markets US,HK,CN_A
```

审计策略是否样本不足、拥挤或可能失效：

```bash
python -m alpha_ledger audit --as-of 2026-05-25
```

校验预测账本是否被改过：

```bash
python -m alpha_ledger verify
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

手动添加一条新预测：

```bash
python -m alpha_ledger add-signal \
  --date 2026-05-25 \
  --ticker EXAMPLE \
  --name "示例股票" \
  --market US \
  --strategy-id trend_breakout \
  --entry-price 100 \
  --buy-zone-low 98 \
  --buy-zone-high 102 \
  --stop-loss 94 \
  --target-1 110 \
  --target-2 120 \
  --horizon-days 20 \
  --confidence B \
  --thesis "放量突破平台，行业强度改善" \
  --trigger-condition "收盘价站上前高且成交量大于20日均量1.5倍" \
  --risk-notes "跌破平台则突破失败"
```

## 第一批策略

- `trend_breakout`：强趋势突破
- `post_earnings_momentum`：财报后动量
- `crowded_short_reversal`：空头拥挤反转
- `hk_value_repair`：港股低估值修复
- `hk_internet_trend_recovery`：港股互联网龙头趋势恢复
- `abnormal_volume_small_midcap`：中小盘异常放量异动
- `event_catalyst_reaction`：公告调研事件催化
- `xingye_style_prepositioning`：兴业科技型重估埋伏启动

兴业科技案例已经沉淀为一个统一策略：旧标签公司出现新增长叙事，事件后不立即高潮，而是在平台内吸筹整理并放量首阳启动。

## 重要原则

Alpha Ledger 不是投资建议生成器，也不会保证任何单只股票盈利。它的价值在于把每次判断变成可验证样本，让真实收益决定策略权重。

常见策略名不是 alpha。强趋势、财报动量、估值修复这类名字市场上人人都知道，所以系统默认把它们视为“待验证假设”，而不是可直接信任的赚钱机器。每个策略都必须经过样本外跟踪、拥挤风险审计和失效降权。
