# 修复 event_driven_strategy.py MongoDB collection 名称不一致

## 改了什么

`scripts/event_driven_strategy.py` 中 `FundamentalValidator` 和 `TechnicalValidator` 的 MongoDB 连接和 collection 名称。

### 涉及位置

| 类 | 方法 | 原值 | 新值 |
|---|---|---|---|
| FundamentalValidator | `_init_db` | `os.environ.get('MONGO_URI')` 直连 | `_load_mongodb_config()` + `MongoFactorDataStore` |
| FundamentalValidator | `get_fundamental_data` L716 | `self.db['stock_basic']` | `self.db[self._collections['basic_info']]` (即 `stock_basic_info`) |
| FundamentalValidator | `get_fundamental_data` L725 | `self.db['stock_financial']` | `self.db[self._collections['financial_data']]` (即 `stock_financial_data`) |
| FundamentalValidator | `get_fundamental_data` L728 | `sort=[('report_date', -1)]` | `sort=[('report_period', -1)]` |
| TechnicalValidator | `_init_db` | `os.environ.get('MONGO_URI')` 直连 | `_load_mongodb_config()` + `MongoFactorDataStore` |
| TechnicalValidator | `get_recent_quotes` L905 | `self.db['stock_daily']` | `self.db[self._collections['daily_quotes']]` (即 `stock_daily_quotes`) |

## 为什么改

系统的 MongoDB collection 定义在 `factor_data_import_service.py` 的 `_load_mongodb_config()` 中：
- `basic_info` → `stock_basic_info`
- `financial_data` → `stock_financial_data`
- `daily_quotes` → `stock_daily_quotes`

但两个 Validator 各自独立写了连接逻辑，collection 名用错了（`stock_basic`、`stock_financial`、`stock_daily`），且用 `os.environ.get('MONGO_URI')` 读 URI，可能连不上。

**影响**：`get_event_driven_stocks()` 中所有股票的 `fundamental_score` 和 `technical_score` 都静默返回默认值 0.5，从不报错，从不工作。

## 怎么验证

1. 运行 `python scripts/event_driven_strategy.py run`，检查日志中是否有 "MongoDB 连接成功 (FundamentalValidator)" 和 "(TechnicalValidator)"
2. 检查输出中 `fundamental_score` 和 `technical_score` 是否有非 0.5 的值（即真实数据被读取并评分）
3. 对比修复前后 `get_fundamental_data` 的返回结果——修复前永远返回 `{}`，修复后应返回实际数据
