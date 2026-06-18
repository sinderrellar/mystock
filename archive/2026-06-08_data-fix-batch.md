# 2026-06-08 数据管道修复

## 改了什么

### 1. `get_bars()` 接入 Yahoo Finance fallback（market_data_provider.py:611-656）
- MongoDB 无数据或数据过期时，自动走 Yahoo Finance 补齐日线
- 命中后回写 MongoDB（`_write_bars_to_mongo`），下次直接命中
- 利用已有的 `_yahoo_symbols()` 和 `_get_yahoo_bars()` 方法，之前完整实现但从未被调用

### 2. KDJ 跳过最新一天修复（market_data_provider.py:958）
- `range(n-1, 0, -1)` → `range(n-1, -1, -1)`，多迭代到索引 0（最新价格）
- 修复前 K/D/J 值都是昨天的，J 值偏差 3-5 个点

### 3. AKShare spot 缓存永不过期（market_data_provider.py:325-345）
- 三个 lookup 方法（A 股/ETF/港股）加了 60 秒 TTL
- 新增 `_spot_expired()` 和 `_spot_fetched_at` 时间戳跟踪
- 跨午休运行时不再用过期的上午数据

### 4. FX 汇率兜底脱离现实（market_data_provider.py:1628-1702）
- 消除硬编码兜底值（旧值 6.8，实际 ~7.25）
- 每次 Sina/Yahoo 成功获取后落盘 `data/fx_fallback.json`
- 兜底时读文件用上次真实汇率，文件不存在才用 7.25 绝对兜底
- 新增 `_save_fx_fallback()` / `_load_fx_fallback()` helper

### 5. 情绪分析：关键词匹配 → LLM（event_driven_strategy.py:349-421）
- 删除 42 个正面/负面中文关键词列表和 `in text` 匹配逻辑
- 改用 DeepSeek Flash（`deepseek-v4-flash`）做语义级情绪分析
- 能处理否定（"否认业绩下滑"）、条件、引用等语境
- 返回格式向后兼容（`positive_keywords`/`negative_keywords` 保留空列表）
- 新增 `key_factors` 和 `method` 字段
- 配置在 `config_complete.yaml` 的 `llm` 节

### 6. 市场情绪阈值波动率自适应（market_data_provider.py:1776-1804）
- 固定 5% 阈值 → z-score（`return_20d / vol_20d`）
- z > 1 → bullish, z < -1 → bearish
- 高波动指数（创业板 8%+）不再误判，低波动指数（上证 2%）不再迟钝
- 返回结果新增 `avg_z_score` 字段

### 7. RSI Cutler's → Wilder's 平滑（market_data_provider.py:985-1006）
- 简单平均 → Wilder 指数平滑 `(prev × 13 + current) / 14`
- 使用全部 bars 递推，与 TradingView 等平台可交叉验证

### 8. LLM 情绪无批量/无缓存/无重试（event_driven_strategy.py:396-510）
- 新增 `_text_hash` 缓存去重（同一条新闻只调一次 LLM）
- 新增 `analyze_sentiment_batch()` 批量方法（30 条/批次，150→5 次 API 调用）
- 新增 `_call_llm()` 带 2 次重试（间隔 1s）
- 两个调用循环（`get_hot_stocks_from_news` 和逐股情绪分析）重构为批量模式

### 9. `get_bars` 新鲜度阈值 4→1 天（market_data_provider.py:643, 661）
- `age > 4` → `age > 1`，对应日频决策节奏
- 周四 review 不会再拿着周二的 K 线

### 10. `_quote_from_akshare` 无 TTL 缓存（market_data_provider.py:554-590）
- 复用 `_lookup_*_price` 层的 spot 缓存，不再各自拉全量 DataFrame
- 新增 `_ensure_a_stock_spot()` / `_ensure_hk_spot()` 封装 TTL 刷新
- `_lookup_a_stock_price` / `_lookup_hk_price` 同步切换到 `_ensure_*_spot()`
- 6 个持仓的 `get_quote_snapshot()` 从 6 次全量拉取降到 A 股/港股各最多 1 次

### 11. AKShare spot TTL 60s → 300s（market_data_provider.py:124）
- 60s 太短，单次 review 中途就会过期 → 5 分钟覆盖全流程

### 12. 【致命】MACD 返回最旧值，不是最新值（market_data_provider.py:1027-1029）
- `dif[-1]` / `dea[-1]` / `histogram` 全取的 130+ 天前的最旧值
- → `dif[0]` / `dea[0]` / `dif[0]-dea[0]`
- pre-existing bug，portfolio_strategy 展示的 MACD 一直是错的

### 13. `_trend_llm_context` 永远空 dict（market_data_provider.py:858）
- `locals().get("result", {})` 在 `get_trend_signal` 中没有 `result` 变量
- → dict 先赋值给 `result`，再传入
- pre-existing bug，LLM 趋势上下文一直为空

### 14. Yahoo bars range 6mo → 1y（market_data_provider.py:678）
- 6mo ≈ 130 交易日，`get_trend_signal` 需要 180 天 lookback，踩在数据边界
- → 1y（~250 交易日）

### 15. 合成今日 K 线不检查 available（market_data_provider.py:778）
- `get_price()` 所有行情源挂掉时返回 `cost_price`，`price > 0` 能通过
- → 加 `available` 检查，行情不可用时跳过合成

### 16. 趋势分类边界值 `>` → `>=`（market_data_provider.py:1064/1068/1072）
- `latest == ma20` 精确相等时输出"低于 MA20" → 改为 `>=`

### 17. get_bars 日期字符串比较——格式不一致（market_data_provider.py:666-668）
- Tushare 格式 "20260607" vs Yahoo 格式 "2026-06-07"，字符串直接比较时 `-` < `0` 导致 Yahoo 数据永远不被采用
- → 用 `_parse_date` 转 datetime 对象再比较
- 新鲜度阈值 1 天现在真正生效

## 涉及文件
- `scripts/market_data_provider.py` — 14 处改动
- `scripts/event_driven_strategy.py` — 情绪分析重构（LLM + 批量 + 缓存 + 重试）
- `config/config_complete.yaml` — 新增 `llm` 配置节
- `data/fx_fallback.json` — 新增（运行时自动生成）
