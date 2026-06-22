#!/usr/bin/env python3
"""
导入全量历史财务数据 — 从 akshare 拉取所有历史季度，写入 stock_financial_data。

与现有实时导入完全独立：
  - 不修改 basic_info
  - 不依赖 stock_signals
  - 每条记录加 report_period + estimated_disclosure，供 precompute 按日期截断

用法:
  python3 import_historical_financials.py                    # 全部 A 股
  python3 import_historical_financials.py --codes 000001,600956  # 指定股票
  python3 import_historical_financials.py --start 2025-01-01  # 只导入 2025 年后
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config, _safe_float


def estimate_disclosure_date(report_period: str) -> str:
    """估算财报实际可获取日期。

    中国披露规则：
    - 年报(12-31): 次年4月30日前 → period + 120天
    - 半年报(06-30): 8月31日前 → period + 60天
    - 季报(03-31, 09-30): 次月底前 → period + 45天

    用保守估值避免 look-ahead。
    """
    dt = datetime.strptime(report_period, "%Y%m%d")
    month = dt.month
    day = dt.day

    if month == 12 and day == 31:
        # 年报：次年4月30日
        return (dt + timedelta(days=120)).strftime("%Y-%m-%d")
    elif month == 6 and day == 30:
        # 半年报：8月31日
        return (dt + timedelta(days=62)).strftime("%Y-%m-%d")
    else:
        # 季报：次月底
        return (dt + timedelta(days=45)).strftime("%Y-%m-%d")


def extract_all_periods(code: str, df: Any) -> List[Dict[str, Any]]:
    """从 akshare stock_financial_abstract DataFrame 提取所有有数据的期间。

    Returns:
        List of financial data dicts, one per report_period.
    """
    if df is None or getattr(df, "empty", True):
        return []

    label_cols = {"选项", "指标"}
    date_cols = [c for c in df.columns if c not in label_cols]
    if not date_cols:
        return []

    indicator_col = "指标" if "指标" in df.columns else None
    if not indicator_col:
        return []

    # 建立指标名→series 映射
    indicator_map: Dict[str, Dict[str, Any]] = {}
    for _, row in df.iterrows():
        key = str(row.get(indicator_col) or "").strip()
        if not key or key == "nan":
            continue
        indicator_map[key] = {col: row.get(col) for col in date_cols}

    def pick_from(values: Dict[str, Any], *names: str) -> Optional[float]:
        for name in names:
            if name in values:
                return _safe_float(values[name])
        return None

    results = []
    for col in date_cols:
        col_values = {k: v.get(col) for k, v in indicator_map.items()}

        # 跳过无数据的列
        revenue = pick_from(col_values, "营业总收入", "营业收入")
        if revenue is None:
            continue

        report_period = col.strip()
        if len(report_period) != 8 or not report_period.isdigit():
            continue

        net_income = pick_from(col_values, "归母净利润", "净利润", "归属母公司股东的净利润")
        eps = pick_from(col_values, "基本每股收益")
        bps = pick_from(col_values, "每股净资产")

        # 跳过利润为负或缺失的（数据不完整）
        if net_income is None and eps is None:
            continue

        # 上一期（用于增速计算）
        prev_values = None
        idx = date_cols.index(col)
        if idx + 1 < len(date_cols):
            prev_col = date_cols[idx + 1]
            prev_values = {k: v.get(prev_col) for k, v in indicator_map.items()}

        roe = pick_from(col_values, "净资产收益率(ROE)", "净资产收益率", "加权净资产收益率")
        gross_margin = pick_from(col_values, "毛利率", "销售毛利率")
        net_margin = pick_from(col_values, "销售净利率", "净利率")

        revenue_growth = pick_from(col_values, "营业总收入增长率", "营业收入同比增长率",
                                   "营业总收入同比增长率", "营收同比增长率")
        profit_growth = pick_from(col_values, "归属母公司净利润增长率", "净利润同比增长率",
                                  "归属母公司股东的净利润同比增长率", "归母净利润同比增长率")
        debt = pick_from(col_values, "资产负债率")
        roa = pick_from(col_values, "总资产报酬率(ROA)", "总资产报酬率")

        revenue_growth_prev = None
        profit_growth_prev = None
        if prev_values:
            revenue_growth_prev = pick_from(prev_values, "营业总收入增长率", "营业收入同比增长率",
                                            "营收同比增长率")
            profit_growth_prev = pick_from(prev_values, "归属母公司净利润增长率", "净利润同比增长率",
                                           "归属母公司股东的净利润同比增长率", "归母净利润同比增长率")

        result = {
            "code": code,
            "symbol": code,
            "full_symbol": f"{code}.SH" if code.startswith("6") else f"{code}.SZ",
            "market": "A股",
            "currency": "CNY",
            "report_period": report_period,
            "estimated_disclosure": estimate_disclosure_date(report_period),
            "report_type": "historical",
            "data_source": "akshare_stock_financial_abstract_historical",
            "roe": roe,
            "gross_margin": gross_margin,
            "net_margin": net_margin,
            "revenue": revenue,
            "revenue_growth": revenue_growth,
            "profit_growth": profit_growth,
            "net_income": net_income,
            "debt_to_assets": debt,
            "eps": eps,
            "bps": bps,
            "roa": roa,
            "revenue_growth_prev": revenue_growth_prev,
            "profit_growth_prev": profit_growth_prev,
        }
        results.append(result)

    return results


def import_one_stock(store: MongoFactorDataStore, code: str,
                     min_period: Optional[str] = None,
                     skip_existing: bool = True) -> int:
    """导入单只股票全量历史财务数据。返回写入条数。"""
    # 断点续传：如果已有 2025Q4 数据则跳过
    if skip_existing and min_period:
        existing = store.db[store.collections["financial_data"]].find_one(
            {"code": code, "report_period": "20251231"})
        if existing:
            return -1  # -1 = skipped

    import akshare as ak

    try:
        df = ak.stock_financial_abstract(symbol=code)
    except Exception as e:
        print(f"  {code} fetch 失败: {e}")
        return 0

    periods = extract_all_periods(code, df)

    if min_period:
        periods = [p for p in periods if p["report_period"] >= min_period]

    written = 0
    for doc in periods:
        rp = doc["report_period"]
        # upsert: 同一 code + report_period 只保留一份
        store.db[store.collections["financial_data"]].replace_one(
            {"code": code, "report_period": rp},
            doc,
            upsert=True,
        )
        written += 1

    return written


def main():
    parser = argparse.ArgumentParser(description="导入全量历史财务数据")
    parser.add_argument("--codes", help="指定股票代码，逗号分隔")
    parser.add_argument("--start", help="最早 report_period，如 2025-01-01")
    parser.add_argument("--limit", type=int, default=0, help="限制导入数量")
    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    store = MongoFactorDataStore(_load_mongodb_config(config_path))

    min_period = None
    if args.start:
        min_period = args.start.replace("-", "")

    # 获取股票列表
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        # 从 daily_quotes 取最近有交易的 A 股
        pipeline = [
            {"$match": {"period": "daily", "data_source": store.quote_source}},
            {"$sort": {"trade_date": -1}},
            {"$group": {"_id": "$code", "last_date": {"$first": "$trade_date"}}},
            {"$match": {"last_date": {"$gte": "2026-06-01"}}},
        ]
        codes = sorted([
            d["_id"] for d in store.db[store.collections["daily_quotes"]].aggregate(pipeline)
            if d["_id"].startswith(("6", "0", "3"))
        ])

    if args.limit and args.limit > 0:
        codes = codes[:args.limit]

    print(f"导入 {len(codes)} 只股票的历史财务数据")
    if min_period:
        print(f"  最早 report_period: {min_period}")

    total_periods = 0
    ok = 0
    fail = 0
    skipped = 0

    for i, code in enumerate(codes):
        if (i + 1) % 50 == 0:
            print(f"  进度: {i+1}/{len(codes)} ({total_periods} 条)")

        try:
            n = import_one_stock(store, code, min_period=min_period)
            if n > 0:
                ok += 1
                total_periods += n
            elif n == -1:
                skipped += 1
            else:
                fail += 1
        except Exception as e:
            print(f"  {code} 异常: {e}")
            fail += 1

        time.sleep(0.12)  # 速率限制

    print(f"\n完成: {ok}/{len(codes)} 只成功, {skipped} 跳过, {fail} 失败, 共 {total_periods} 条财务记录")

    # 显示当前集合状态
    cnt = store.db[store.collections["financial_data"]].count_documents({})
    period_dist = list(store.db[store.collections["financial_data"]].aggregate([
        {"$group": {"_id": "$report_period", "n": {"$sum": 1}}},
        {"$sort": {"_id": -1}},
        {"$limit": 8},
    ]))
    print(f"\nstock_financial_data 现状: {cnt} 条")
    print("  最新 report_period 分布:")
    for p in period_dist:
        print(f"    {p['_id']}: {p['n']} 条")


if __name__ == "__main__":
    main()
