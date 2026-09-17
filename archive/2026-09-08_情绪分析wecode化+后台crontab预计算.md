# 情绪分析 wecode 化 + 后台 crontab 预计算

日期：2026-09-08

## 为什么改

组合全景 `review()` 的慢根因是情绪分析：每次刷新对持仓逐只拉新闻 + 调 LLM（原直连 DeepSeek API，~7-9s/次）。上一轮已加「标的+市场+当日」双层缓存，但仍有三个遗留问题，老板连下三令：

1. **LLM 后端要统一**：「不使用 deepseek 的接口，改用 wecode 去解析」——情绪分析改走 wecode（Claude Code CLI 封装，`/root/.wecode/bin/wecode -p`），与投资助手/智辩（`debate_engine.run_agent`）共用同一条 copilot.weibo.com 代理通道，不再维护两套 LLM 后端。
2. **情绪要盘中刷新**：「盘前半小时、早盘 3 次，下午盘前一次，下午盘中 3 次」——改为后台 crontab 每交易日盘中定时预热，`review()` 命中缓存秒回。
3. **覆盖所有用户持仓**：「所有的持仓文件都要算一下，解析到哪个算哪个」「然后记录到不同用户名下」——枚举全部 `data/portfolio_<user>.yaml`，按用户分组逐只算。

## 改了什么

### 1. `scripts/event_driven_strategy.py` —— LLM 后端 DeepSeek → wecode

- `NewsSentimentAnalyzer.__init__`：删除 `self._client = Anthropic(...)` / `self._model`（DeepSeek 直连），`self._enabled` 从 `bool(llm.get("api_key"))` 改为 `os.path.exists(WECODE_BIN)`（wecode 二进制是否存在）。
- `_call_llm`：从 `self._client.messages.create(...)` 改为 `from debate_engine import run_agent, clean_output`，`run_agent(prompt, timeout=max(self._timeout, 120))` + `clean_output` 剔除 wecode warning 行。`max_tokens` 参数保留仅兼容旧签名（wecode 自行控制生成长度）。重试逻辑不变。

### 2. `scripts/strategy_signals.py` —— 情绪缓存 key 日级 → 半小时分桶

- `_event_cache_key`：`代码:市场:日期:时:分桶`，分钟对齐到 0/30（如 `00700:港股:2026-09-08:11:00`）。盘中突发新闻半小时内反映，兼顾新鲜度与 LLM 调用频率。

### 3. `scripts/sentiment_precompute.py`（新增）—— crontab 预热脚本

- `glob data/portfolio_*.yaml`（天然排除 `portfolio.yaml` 默认空仓、`.example.yaml`、`.org` 备份），从文件名拆 user。
- 逐文件 `yaml.safe_load` 取 positions，逐只调 `StrategySignalCollector._event_signal(pos)`（复用「查缓存→miss 现场算→写缓存」完整链路）。
- 「解析到哪个算哪个」：单文件解析失败 / 单只计算失败只 skip 打日志，不中断整批。
- 按 `(code, market)` 去重：同一股票被多用户持有只算一次（情绪是股票属性，新闻全局一致）；但枚举按用户文件展开，覆盖所有用户持仓。
- ETF 在 `_event_signal` 内已跳过，自然落入 skip 计数。

### 4. `config/crontab_stock.txt` + live crontab —— 8 次/交易日

- 盘前半小时 `9:00`；早盘 3 次 `9:45 / 10:30 / 11:15`；下午盘前 `12:30`；下午盘中 3 次 `13:15 / 14:00 / 14:45`。均 `* * 1-5`（工作日），日志 `logs/sentiment_precompute.log`。

## 怎么验证

1. `py_compile` 通过。
2. wecode 单测：`NewsSentimentAnalyzer().analyze_sentiment_batch(['腾讯发布财报，利润超预期，回购加码'])` → `[{'sentiment':'positive','score':0.9,...}]`，`_enabled=True`。
3. 全量跑 `sentiment_precompute.py`：枚举 2 个持仓文件（liuyu20 5 只、zhongyue3 1 只），成功 5 / 跳过 1（ETF 560860）/ 去重后 6 只，全部写入 `event_signal_cache` 半小时分桶 key。
4. `crontab -l` 确认 8 条 `sentiment_precompute.py` 均已安装（曾手滑一条 9:00 缺 `cd &&`，已 sed 修正复核）。

## 局限 / 待办

- **「记录到不同用户名下」口径**：情绪是股票属性（新闻全局一致），缓存按「代码:市场:时间」全局存，同一股票多用户持有只算一次；枚举按用户文件展开保证覆盖。若老板要的是每个用户名下单独存一份（加 user 维度 key），需再改 `_event_cache_key` + `_event_signal`。
- **300750 市场口径不一致**：zhongyue3 的 yaml 里宁德时代 market=「创业板」，而旧缓存里有 `300750:A股:...` 残留，同一标的出现两个 market 标签、会各算一次。数据层小事（新闻按 code 拉，不受 market 影响），但可考虑把「创业板」统一成「A股」。
- **首次仍会实时兜底**：老板已选「缓存 miss 就实时获取」，cron 只是预热器；cron 未跑到或 miss 时 review 仍走 wecode 实时算，不会「不可用」，代价是偶尔慢一次。
- 情绪缓存里的 `sentiment_score` 取的是时间衰减加权均值；wecode 返回的 `positive/negative_keywords` 偶为空数组（模型输出选择），核心 `sentiment/score` 稳定不受影响。
