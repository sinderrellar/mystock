#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_daily_quotes.py —— 补齐 stock_daily_quotes 缺失的历史日线

背景：
  universe_light_sina 轻量导入（3262 只）只写 code/name/close/latest_amount/industry，
  没有历史日线。precompute_history 的输入源是 daily_quotes，没有日线就无法算
  trend/factor。结果 buy_plan 按成交额 top200 的池（几乎全是 301xxx 等创业板新股）
  与因子集合零交集，recommendations=0。

  本脚本用同花顺 K 线 d.10jqka.com.cn 逐只补齐日线（last360，约 360 交易日），
  写入 stock_daily_quotes（与 FactorDataImporter 完全同构）。
  注：腾讯 fqkline 连续请求约 600 次后返回 501 限流，东财 push2his 会 RemoteDisconnected，
  同花顺 d.10jqka.com.cn 单前缀（hs_）覆盖沪深、不区分市场、更耐受，故选同花顺为主源。

范围：
  只处理 active 且 market ∈ {主板, 创业板, 科创板} 且 code 以 6/0/3 开头的 A 股。
  - 北交所 920/8/4 开头的 code 不在范围内：precompute_history 只算 6/0/3 开头，
    补了也用不上，属后续增强。
  - 已有日线的股票跳过（幂等，可断点重跑）。

约束（遵守 CLAUDE.md）：
  - 只 upsert（ReplaceOne $setOnInsert created_at），禁止 delete/remove。
  - 幂等：已存在同 code+trade_date+data_source 的文档原样覆盖，不产生脏数据。

用法：
  python3 scripts/backfill_daily_quotes.py                 # 全量补齐
  python3 scripts/backfill_daily_quotes.py --limit 50      # 冒烟：只处理 50 只
  python3 scripts/backfill_daily_quotes.py --dry-run       # 只统计，不写库
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from factor_data_import_service import (  # noqa: E402
    MongoFactorDataStore,
    _load_mongodb_config,
    _safe_float,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
DEFAULT_SLEEP = 0.15  # 同花顺接口较耐受
RETRIES = 3
BACKOFF = 3  # 失败退避秒数
LOOKBACK_BARS = 360  # last360：覆盖 12 月动量 + MA60


def _ths_date(ymd: str) -> str:
    """YYYYMMDD → YYYY-MM-DD。"""
    if len(ymd) == 8:
        return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return ymd


def _fetch_ths_kline(code: str) -> list:
    """同花顺日线（前复权），返回 [{code, trade_date, open, high, low, close, volume, amount, ...}]。

    数据格式：data 字符串以 ; 分隔，每段 date,open,high,low,close,volume,amount,...
    """
    url = f"http://d.10jqka.com.cn/v6/line/hs_{code}/01/last{LOOKBACK_BARS}.js"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "http://stockpage.10jqka.com.cn/",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="ignore")

    m = re.search(r"\((.*)\)", body, re.S)
    if not m:
        return []
    payload = json.loads(m.group(1))
    data = payload.get("data", "") or ""
    docs = []
    for line in data.split(";"):
        parts = line.split(",")
        if len(parts) < 7:
            continue
        # date, open, high, low, close, volume, amount
        close = _safe_float(parts[4], None)
        if close is None or close <= 0:
            continue
        docs.append({
            "code": code,
            "symbol": code,
            "market": "A股",
            "currency": "CNY",
            "trade_date": _ths_date(parts[0]),
            "open": _safe_float(parts[1], None),
            "high": _safe_float(parts[2], None),
            "low": _safe_float(parts[3], None),
            "close": close,
            "volume": _safe_float(parts[5], None),
            "amount": _safe_float(parts[6], None),
            "data_source": "ths_kline",
            "period": "daily",
        })
    return docs


def _collect_missing(store: MongoFactorDataStore, limit: int | None = None):
    """找出 active 且缺日线的 A 股（6/0/3 开头），返回 [(code, name), ...]。"""
    basic = store.db[store.collections["basic_info"]]
    quotes = store.db[store.collections["daily_quotes"]]

    existing = set(quotes.distinct("code"))
    print(f"  已有日线的股票: {len(existing)} 只")

    q = {
        "active": {"$ne": False},
        "market": {"$in": ["主板", "创业板", "科创板"]},
    }
    cursor = basic.find(q, {"code": 1, "name": 1}).sort("code", 1)
    if limit:
        cursor = cursor.limit(limit)

    missing = []
    for d in cursor:
        code = str(d.get("code", "")).strip()
        if len(code) != 6:
            continue
        if not (code.startswith("6") or code.startswith("0") or code.startswith("3")):
            continue  # 北交所/B股 不在 precompute 范围内
        if code in existing:
            continue
        missing.append((code, d.get("name", code)))
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（冒烟）")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="逐只请求间隔秒数")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    store = MongoFactorDataStore(cfg)

    missing = _collect_missing(store, args.limit or None)
    print(f"[{datetime.now():%H:%M:%S}] 待补齐日线 {len(missing)} 只 (workers={args.workers})")

    if args.dry_run:
        return

    done = 0
    failed = 0
    t0 = time.time()
    batch: list = []
    BATCH_SIZE = 50  # 攒够一批再 bulk_write，减少写放大

    def _flush():
        nonlocal batch
        if batch:
            store.upsert_quotes(batch)
            batch = []

    def _fetch_one(code, name):
        for attempt in range(RETRIES):
            try:
                docs = _fetch_ths_kline(code)
                if docs:
                    return docs
            except Exception:
                time.sleep(BACKOFF * (attempt + 1))
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        fut_map = {ex.submit(_fetch_one, code, name): (code, name) for code, name in missing}
        for i, fut in enumerate(as_completed(fut_map), 1):
            code, name = fut_map[fut]
            docs = fut.result()
            if not docs:
                failed += 1
                if failed <= 20:
                    print(f"  [失败] {code} {name}")
                continue
            batch.extend(docs)
            done += 1
            if len(batch) >= BATCH_SIZE:
                _flush()
            if i % 100 == 0:
                _flush()
                el = time.time() - t0
                print(f"  进度 {i}/{len(missing)} (成功 {done}, 失败 {failed}, {i / el if el else 0:.1f} 只/s)")

    _flush()
    el = time.time() - t0
    print(f"[{datetime.now():%H:%M:%S}] 完成：补齐 {done} 只，失败 {failed} 只，"
          f"耗时 {el:.0f}s ({done / el if el else 0:.1f} 只/s)")


if __name__ == "__main__":
    main()
