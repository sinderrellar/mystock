# 修复 event_driven_strategy.py 三个轻度问题

## 改了什么

### 1. `analyze_sentiment` 不写 MongoDB 持久化缓存

`analyze_sentiment` 单条调用（L530-531）只写 `self._sentiment_cache`（内存），不调用 `_save_mongo_cache`。进程重启后缓存全丢，即使之前已经 LLM 分析过同样的文本。

- `scripts/event_driven_strategy.py` L533: `self._sentiment_cache[key] = result` 之后增加 `self._save_mongo_cache(key, result)`

### 2. `get_hot_stocks_from_news` 每次 new 一个 `NewsSentimentAnalyzer`

`NewsDataProvider.get_hot_stocks_from_news` 内每次调用都 `NewsSentimentAnalyzer()`，重复：加载 config → 连 MongoDB → 创建 Anthropic client → 创建 TTL 索引。

- `NewsDataProvider.__init__`: 增加 `self._sentiment_analyzer = None`
- `get_hot_stocks_from_news` L287: `sentiment_analyzer = NewsSentimentAnalyzer()` → 改为 lazy init，复用实例

### 3. `_calculate_event_score` 死代码 `sentiment < -0.3`

`get_event_driven_stocks` L1222 已过滤 `avg_sentiment < -0.3`，`_calculate_event_score` 中 `elif sentiment < -0.3` 永远不会触发。

- 删除 `elif sentiment < -0.3` 分支，保留 `elif sentiment < -0.1`（区间 [-0.3, -0.1) 仍可达）

## 为什么改

- **缓存不持久化**：LLM 调用是昂贵的，同一条新闻的已分析结果应在进程重启后仍能命中
- **重复初始化**：每次 `get_hot_stocks_from_news` 都建 MongoDB 连接和 TTL 索引，浪费资源
- **死代码**：被过滤条件涵盖的逻辑分支会误导阅读者，且让 future change 产生意外行为

## 怎么验证

1. 运行两次 `analyze_sentiment` 对同一文本，第二次应返回 `"method": "llm"`（内存命中）；重启进程后再运行，MongoDB 中有记录则 `method` 为 `"llm_cached"`
2. 两次调用 `get_hot_stocks_from_news`，观察日志中 "MongoDB 连接成功" 只出现一次（在 `NewsSentimentAnalyzer` 初始化中）
3. 审查 `_calculate_event_score` 逻辑，确认无 sentiment < -0.3 的路径

### 4. `get_event_driven_stocks` 取 `market_news` 但从未使用

L1193-1195 拉取市场热点新闻后无任何引用，浪费一次 AKShare API 调用。

- 删除 `market_news = self.news_provider.get_market_hot_news(limit=50)` 及关联 logger

### 5. 汇总统计把缺失数据的 0.5 也计入平均

`run()` 中的 `avg_fundamental`/`avg_technical` 对所有股票求平均，包括 `available=False`
的（composite 已排除缺失维度，但汇总统计未排除）。

- `avg_fundamental`/`avg_technical` 改为只统计 `fundamental_details.available == True` 的股票
- `high_fundamental`/`high_technical` 同样只计有数据的股票
