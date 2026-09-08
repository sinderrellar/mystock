# 修复「有因子=0」：latest_amount 单位错乱（元 vs 万元）

日期：2026-09-07

## 结论（根因）

前端漏斗「有因子」层 = 0 的根因不是前端代码，而是 **`stock_basic_info.latest_amount` 字段单位错乱**：

- 全系统约定 `latest_amount` 单位为**万元**（`buy_plan.py get_layer0_filter` 里 `latest_amount >= min_amount/10000`，`data_import_pipeline.py` 里 `>= 5_000 万元`）。
- 但库里有两批数据用了不同单位：
  | data_source | 只数 | 单位 | 例 |
  |-------------|------|------|----|
  | `tencent_a` | 2293 | 万元 ✓ | 茅台 602259 万元 = 60.2 亿 |
  | 缺失（universe_light_sina 轻量导入） | 3262 | **元 ✗** | 易点天下 5338215709 元 = 53.4 亿 |

## 后果链

```
轻量导入写元（数值比万元大 1e4 倍）
  → buy_plan _broad_screen 按 latest_amount 降序取 top200：全被元值股票占据
      （301xxx 等创业板新股，实际成交额并不高）
  → 前端漏斗「成交额」层用万元阈值（min 5000 万 / max 50 亿）去比元值
  → 元值股票（74,469,073 元）被当成 74 亿万元，超上限全被筛掉
  → pool ∩ 初筛结果 = 0 → 「有因子」= 0
```

同时这也是 buy_plan 之前 `recommendations=0` 的帮凶之一（候选池取到了错误的 top200）。

## 改了什么

### 1. 数据修正（一次性）`scripts/normalize_latest_amount.py`（新增）

对 `data_source` 缺失的 3262 只，`latest_amount` 除以 10000（元→万元），并把 `data_source` 补记为 `universe_light_sina`（幂等：重复跑不会二次除）。

```
已修正 3262 只：latest_amount 元→万元
校验：latest_amount > 200 万（200 亿）的残留 0 只
```

### 2. 导入代码修正 `scripts/factor_data_import_service.py`

`_sync_a_universe_basic` 原来直接写东财 spot 的「成交额」（元）：

```python
# 改前
"latest_amount": _safe_float(row.get("成交额")),
# 改后
"latest_amount": _safe_float(row.get("成交额")) / 1e4 if _safe_float(row.get("成交额")) is not None else None,
```

防止老板下次跑 `sync-universe-basic` 时把单位又写回元。

## 怎么验证

- 修正后 `BuyPlanEngine().run()`：`Layer0 生存过滤 1822→1769`，候选池从 301xxx 新股变成真实高成交额股票（亚盛集团/国芳集团/神农种业/芒果超媒…）。
- API `/api/buyplan?top=15`：`universe=5555, pool=200, recommendations=15`。
- 复刻前端漏斗：`全市场 5555 → 板块 5214 → 初筛 1707 → 有因子 165`（原来 0）。
- **无需重启 API server**（`/api/buyplan` 每次请求实时算，读的就是修正后的 MongoDB）。老板刷新浏览器（Ctrl+F5）即可看到「有因子」不再是 0。

## 已知边界

- 前端 `BOUNDS.mvYi` 上限 2000 亿、`BOUNDS.amountWan` 上限 50 亿会把茅台/宁德/平安等超大盘蓝筹挡在漏斗外（有意为之：明日选股瞄准中小盘动量，不吃蓝筹）。若老板想放宽，改 `web/src/buyplan.js` 的 `BOUNDS`。
- `market_data_provider.py:596` 的 AKShare 实时行情 `latest_amount` 仍是元（瞬时 metric，不落库，未纳入本次修正），与 `_quote_from_mongo`（万元）口径不一致，属历史遗留，待后续统一。
