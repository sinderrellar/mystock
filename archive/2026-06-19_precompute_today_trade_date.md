# precompute today 交易日解析修复

## 改了什么

- `scripts/precompute_history.py`
  - 新增 `_resolve_today_trade_date()`。
  - `--date today` 不再直接使用自然日/UTC 日期。
  - 改为从 `stock_daily_quotes` 中寻找不晚于北京时间今天、且日线覆盖数不少于 50 的最新交易日。
  - 如果发生回退，会明确打印：
    - 自然日 today 是哪天
    - 回退到哪个交易日
    - 该交易日有多少只股票日线
- `scripts/business_group_loader.py`
  - 读取 `industry_groups.yaml` 和各组 YAML 时显式使用 UTF-8。
  - 修复 Windows 默认 GBK 环境下预计算启动后读取中文配置失败的问题。

## 为什么改

自然日不等于可预计算交易日：

- A 股可能休市。
- 当天可能未收盘。
- 行情导入可能尚未完成。
- 代码原来使用 UTC 日期，和 A 股所在的北京时间交易日语义不一致。

因此 `python3 scripts/precompute_history.py --date today` 可能尝试计算一个 MongoDB 中没有行情的日期，报出“仅 0 只股票有行情”。

验证过程中还发现 `BusinessGroupLoader` 未指定编码，Windows 下读取中文 YAML 可能触发 `UnicodeDecodeError: 'gbk' codec can't decode ...`，会阻断 precompute 正常执行，因此一并修复。

## 怎么验证

```bash
python3 scripts/precompute_history.py --date today --meta-only
```

预期行为：

- 如果今天已有足够行情，计算今天。
- 如果今天没有足够行情，明确回退到最新可用交易日。
- 不再对 0 覆盖的自然日直接失败。

完整链路可继续运行：

```bash
python3 scripts/precompute_history.py --date today
```
