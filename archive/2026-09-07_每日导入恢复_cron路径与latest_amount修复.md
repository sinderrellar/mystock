# 每日导入恢复：修 cron 路径 + 补导入缺失 collection + 修 latest_amount 第二处单位 bug

日期：2026-09-07

## 背景

老板问「每天需要导的数据都正常吗」。排查发现定时任务全挂、每日导入数据全缺失。

## 问题 1：cron 定时任务全部没跑起来（最严重）

**根因**：crontab 每行 `cd /data2/liuyu20/mystock/scripts && venv/bin/python ...`，`venv/bin/python` 是**相对路径**，cd 到 `scripts/` 后解析成 `scripts/venv/bin/python`（不存在）。venv 实际在项目根 `/data2/liuyu20/mystock/venv/`。

**证据**：`logs/import.log` 末尾 47 行全是 `/bin/bash: venv/bin/python: No such file or directory`，mtime 当天 11:37（当天宏观新闻任务又失败一次）。

**修复**：`crontab -l` 里 `venv/bin/python` 全部替换为绝对路径 `/data2/liuyu20/mystock/venv/bin/python`（已备份 crontab 到 /tmp）。

## 问题 2：每日导入对应的 collection 在 tradingagents 库里不存在

MongoDB 只有 1 个库 `tradingagents`。补导入后各 collection 状态：

| collection | 状态 | 说明 |
|-----------|------|------|
| stock_hsgt_flow（沪深港通） | ✅ 4 条 | |
| market_news（宏观新闻） | ✅ 200 条 | tushare major_news 无权限 → 降级 AKShare |
| top_list（龙虎榜） | ✅ 108 条 | |
| stock_shortsell（港股做空） | ✅ 744 条 | HKEX 免费源 |
| industry_moneyflow（行业资金流） | ✅ 330 条 | tushare moneyflow 聚合 |
| stock_signals（个股资金流+一致预期） | ⏳ 后台跑 | moneyflow 456→…，forecast 随后 |
| market_moneyflow（全市场资金流） | ❌ tushare 无权限 | `moneyflow_mkt_dc` 接口无权限 |
| stock_southbound（南向） | 跳过 | portfolio 空，无港股持仓 |
| stock_dividend（股息率） | 跳过 | portfolio 空，无 A 股持仓 |

## 问题 3（顺带发现）：import_universe_light 的 latest_amount 单位 bug

`import_universe_light`（每周日 20:00 定时）写 `latest_amount` 用 `ak.stock_zh_a_spot()` 的「成交额」（元）**没除以 1e4**，与 `_sync_a_universe_basic` 之前修的是同一个 bug。每周跑一次就会把成交额单位写回元，重新触发「有因子=0」。

**修复**：`scripts/data_import_pipeline.py` `import_universe_light` 里 `latest_amount` 加 `/1e4`（元→万元），与 `factor_data_import_service.py:656` 一致。

**排查结论**（全量 latest_amount 写入点）：
- `_sync_a_universe_basic`（东财 spot，元）→ 已修 /1e4 ✅
- `import_universe_light`（Sina spot，元）→ 本次修 /1e4 ✅
- `_fetch_basic_via_tencent_a`（腾讯 fields[37]，万元）→ 本来就对 ✅
- `_fetch_basic_via_eastmoney`（f6，元）/ `_fetch_basic_via_tencent`（fields[6]，成交量）→ 无人调用的死代码，不影响
- `market_data_provider.py:596`（AKShare 实时，元）→ 瞬时 metric 不落库，已知遗留

## 历史回补（本轮前面已完成）

- `scripts/extend_history_daily_quotes.py` 把 3464 只 tencent_kline 拉长到 360 根（见同日归档）。
- 重跑 precompute（`--date 2026-09-04`）：meta=5000 trend=5000 factor=5000。

## 已知边界

- `market_moneyflow` 需 tushare 更高权限（`moneyflow_mkt_dc`），或改走 akshare 兜底（未做）。行业级资金流已由 industry_moneyflow 覆盖，可按需后续补。
- `import_universe_quotes`（每日 19:00 行情刷新）未手动跑：它按「缺当天日线才补」增量逻辑走，今晚 cron 会自动跑；其 200 根 tencent 写入不会删掉历史回补的 360 根（ReplaceOne 只按 trade_date 覆盖，更早的根不删）。
- 资金流/一致预期还在后台跑（~30/40 分钟），跑完 stock_signals 才有完整数据。
