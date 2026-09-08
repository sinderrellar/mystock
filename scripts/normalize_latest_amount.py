#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_latest_amount.py —— 修正 stock_basic_info.latest_amount 的单位错乱

背景（根因）：
  全系统约定 latest_amount 单位为「万元」（见 buy_plan.py get_layer0_filter 的
  `latest_amount >= min_amount/10000`，data_import_pipeline.py 的 `>= 5_000 万元`）。

  但 basic_info 里有两批数据用了不同单位：
    - data_source='tencent_a'（2293 只）：万元（茅台 602259 万元 = 60.2 亿）✓
    - data_source=缺失（3262 只，来自 universe_light_sina 轻量导入）：元
      （易点天下 5338215709 元 = 53.4 亿）✗

  后果：
    1. buy_plan `_broad_screen` 按 latest_amount 降序取 top200，元值股票的数值
      比万元值大 1e4 倍，把成交额 top200 池全部污染成 301xxx 等轻量创业板新股
      （实际这些股票成交额并不高）。
    2. 前端漏斗「成交额」层用万元阈值（min 5000 万）去比元值，把 pool 全筛掉，
      导致「有因子」层 = 0。

修复：
  对 data_source 缺失的这批（3262 只）把 latest_amount 除以 10000（元→万元），
  并把 data_source 补记为 'universe_light_sina'（幂等：重复跑不会二次除）。

约束（遵守 CLAUDE.md）：
  只 update（$set / $mul），不 delete/drop/remove。幂等可重跑。

用法：
  python3 scripts/normalize_latest_amount.py --dry-run   # 只统计
  python3 scripts/normalize_latest_amount.py             # 正式修正
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不改库")
    args = ap.parse_args()

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    store = MongoFactorDataStore(cfg)
    basic = store.db[store.collections["basic_info"]]

    # data_source 缺失/为 None 的这批（轻量导入，latest_amount 是元）
    flt = {"data_source": {"$exists": False}}

    n_total = basic.count_documents(flt)
    n_amount = basic.count_documents({**flt, "latest_amount": {"$exists": True, "$ne": None}})
    print(f"data_source 缺失: {n_total} 只，其中含 latest_amount: {n_amount} 只")

    if n_amount == 0:
        print("无需修正（可能已处理过）。")
        return

    if args.dry_run:
        # 抽样展示修正前后
        for d in basic.find({**flt, "latest_amount": {"$exists": True, "$ne": None}},
                            {"code": 1, "name": 1, "latest_amount": 1, "_id": 0}).sort("latest_amount", -1).limit(5):
            print(f"  {d['code']} {d.get('name','')} {d['latest_amount']} → {d['latest_amount'] / 10000}")
        return

    # 元 → 万元：$mul / 10000，并补 data_source 标记（幂等）
    r = basic.update_many(
        {**flt, "latest_amount": {"$exists": True, "$ne": None}},
        {
            "$mul": {"latest_amount": 1 / 10000},
            "$set": {"data_source": "universe_light_sina"},
        },
    )
    print(f"已修正 {r.modified_count} 只：latest_amount 元→万元，data_source 补记为 universe_light_sina")

    # 校验：修正后不应再有超大 latest_amount
    big = basic.count_documents({"latest_amount": {"$gt": 2_000_000}})
    print(f"校验：latest_amount > 200 万（200 亿元）的残留 {big} 只（应为 0）")


if __name__ == "__main__":
    main()
