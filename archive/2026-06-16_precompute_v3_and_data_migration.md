# 2026-06-16 precompute v3 + 数据迁移

## precompute v3
- 三张表：stock_meta / stock_trends / stock_factors
- trend/factor 拆层：--trends-only / --factors-only
- 4 套因子并存：每只股票 × 4 组权重+评分阶梯
- 实时和历史统一：precompute --date today 替代 compute-signals
- pyramid_multifactor_strategy: override_weights + override_scoring 参数

## 下游迁移
- sector_radar: load_all_industry_data 改读 stock_trends + stock_factors
- buy_plan: _enrich 改读 stock_trends + stock_factors（因子先、趋势后+fallback）
- buy_plan: get_layer0_filter PE 从 config 读取，不再 hardcode
- buy_plan: _get_market_breadth 改读 stock_trends
- crontab: compute-signals 替换为 precompute_history --date today

## 数据清理
- stock_signals 集合删除
- stock_signals_history 集合删除
- stock_labeler.py 删除 + 引用清理
- data_import_pipeline.py compute_signal_cache 函数删除
