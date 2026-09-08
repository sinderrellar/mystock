#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extend_history_daily_quotes.py —— 把 tencent_kline 源股票的历史用同花顺 last360 拉长

背景：
  daily_quotes 里有两批 K 线：
    - tencent_kline（3464 只）：腾讯 fqkline `day,,,200,qfq` 只取了 200 根，最早只到 ~2025-11
    - ths_kline（1741 只）：同花顺 last360 取了 360 根，最早到 ~2025-03

  结果 2025-03 ~ 2025-10 只有 1681 只 ths_kline 有数据，缺 3464 只 tencent_kline。
  本脚本把这 3464 只用同花顺 last360 重取（360 根，回到 ~2025-03），拉长其历史。

数据源一致性：
  腾讯 qfq 与同花顺 last360 都是前复权，抽查 000977 最近 5 天 4 天完全一致，
  仅最后一天（09-04）腾讯是盘中未结算值（78.27），同花顺是结算值（77.72）。
  同花顺更准，故直接整体替换。

写库口径（遵守 CLAUDE.md 禁 delete）：
  用 data_source='tencent_kline' 写入（ReplaceOne 原地替换，key 含 data_source），
  这样同 code+trade_date 不产生重复文档，也不需要删除旧文档——只 update/replace。
  （data_source 保留 'tencent_kline' 作为该股票 K 线序列的稳定标签，实际刷新源是同花顺，
   见 archive 归档说明。）

用法：
  python3 scripts/extend_history_daily_quotes.py --dry-run     # 只统计
  python3 scripts/extend_history_daily_quotes.py --limit 50    # 冒烟
  python3 scripts/extend_history_daily_quotes.py               # 全量 3464 只
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
LOOKBACK_BARS = 360  # last360：回到 ~2025-03
RETRIES = 3
BACKOFF = 3


def _ths_date(ymd: str) -> str:
    if len(ymd) == 8:
        return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    return ymd


def _fetch_ths_kline(code: str) -> list:
    """同花顺日线（前复权），字段 date,open,high,low,close,volume,amount。"""
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
            "data_source": "tencent_kline",  # 稳定序列标签（刷新源为同花顺，见 docstring）
            "period": "daily",
        })
    return docs


def _collect_targets(store: MongoFactorDataStore):
    """找出 tencent_kline 源的股票（200 根、历史浅，需拉长到 360 根）。"""
    quotes = store.db[store.collections["daily_quotes"]]
    codes = sorted(quotes.distinct("code", {"data_source": "tencent_kline"}))
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（冒烟）")
    ap.add_argument("--workers", type=int, default=4, help="并发线程数")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    store = MongoFactorDataStore(cfg)

    codes = _collect_targets(store)
    if args.limit:
        codes = codes[:args.limit]
    print(f"[{datetime.now():%H:%M:%S}] 待拉长历史的 tencent_kline 股票 {len(codes)} 只 (workers={args.workers})")

    if args.dry_run:
        return

    done = 0
    failed = 0
    t0 = time.time()
    batch: list = []
    BATCH_SIZE = 50

    def _flush():
        nonlocal batch
        if batch:
            store.upsert_quotes(batch)
            batch = []

    def _fetch_one(code):
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
        fut_map = {ex.submit(_fetch_one, code): code for code in codes}
        for i, fut in enumerate(as_completed(fut_map), 1):
            code = fut_map[fut]
            docs = fut.result()
            if not docs:
                failed += 1
                if failed <= 20:
                    print(f"  [失败] {code}")
                continue
            batch.extend(docs)
            done += 1
            if len(batch) >= BATCH_SIZE:
                _flush()
            if i % 100 == 0:
                _flush()
                el = time.time() - t0
                print(f"  进度 {i}/{len(codes)} (成功 {done}, 失败 {failed}, {i / el if el else 0:.1f} 只/s)")

    _flush()
    el = time.time() - t0
    print(f"[{datetime.now():%H:%M:%S}] 完成：拉长 {done} 只，失败 {failed} 只，"
          f"耗时 {el:.0f}s ({done / el if el else 0:.1f} 只/s)")


if __name__ == "__main__":
    main()
