#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_basic_fields.py —— 补全 universe_light_sina 缺失的 total_mv / pe / pb

背景：
  universe_light_sina 轻量导入只写 code/name/close/latest_amount/industry 等，
  没有 total_mv / pe / pb。这 3262 只因此被 buy_plan 的「市值≥50亿 + PE∈(0,200)」
  初筛静默排除。本脚本用腾讯行情 qt.gtimg.cn 批量补全这三个估值字段。

数据源与前缀：
  腾讯行情统一 ~ 分隔字段：f39=PE, f45=总市值(亿), f46=PB, f3=现价。
  沪市 sh(5/6/9)、深市 sz、北交所 bj(920/8/4) 三种前缀，批量接口 q=sz000001,sh600000,bj920000。

约束（遵守 CLAUDE.md）：
  - 只 update_one $set，禁止 delete/remove。
  - 幂等：只补缺失字段；抓不到的保持原样，不写 None 覆盖好数据。
  - 每个被补全的文档打 valuation_source 标记，便于审计。

用法：
  python3 scripts/backfill_basic_fields.py            # 全量补全
  python3 scripts/backfill_basic_fields.py --limit 60 # 冒烟：只处理前 60 只
  python3 scripts/backfill_basic_fields.py --dry-run  # 只统计，不写库
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pymongo import MongoClient  # noqa: E402

from factor_data_import_service import _load_mongodb_config, _safe_float  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
COL = "stock_basic_info"
BATCH = 50
SLEEP = 0.3  # 批量请求间隔，避免触发限流


def _prefix(code: str) -> str:
    """按代码段返回腾讯行情前缀。"""
    if code.startswith(("920", "8", "4")):
        return "bj"  # 北交所
    if code.startswith(("5", "6", "9")):
        return "sh"  # 沪市（含 688 科创板）
    return "sz"  # 深市（含 300/301/302 创业板、003 主板）


def _fetch_batch(qcodes):
    """一次批量抓取，返回 {code: {pe,pb,total_mv,close}}（只含现价>0 的）。"""
    url = "https://qt.gtimg.cn/q=" + ",".join(qcodes)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    txt = urllib.request.urlopen(req, timeout=12).read().decode("gbk", errors="ignore")
    out = {}
    for line in txt.split("\n"):
        line = line.strip()
        if '="' not in line:
            continue
        var, _, payload = line.partition('="')
        payload = payload.rsplit('"', 1)[0]
        f = payload.split("~")
        raw = var[2:] if var.startswith("v_") else var
        code = raw[2:] if raw[:2] in ("sh", "sz", "bj") else raw
        price = _safe_float(f[3] if len(f) > 3 else None)
        if not price or price <= 0:
            continue
        mv_yi = _safe_float(f[45] if len(f) > 45 else None, None)
        out[code] = {
            "pe": _safe_float(f[39] if len(f) > 39 else None, None),
            "pb": _safe_float(f[46] if len(f) > 46 else None, None),
            "total_mv": round(mv_yi * 1e8, 2) if mv_yi else None,
            "close": price,
        }
    return out


def _collect_targets(col, limit=None):
    """找出 active 且缺 total_mv 或 pe 的文档，按前缀分组。"""
    q = {
        "active": {"$ne": False},
        "$or": [
            {"total_mv": {"$exists": False}},
            {"total_mv": None},
            {"pe": {"$exists": False}},
            {"pe": None},
        ],
    }
    cursor = col.find(q, {"code": 1}).sort("code", 1)
    if limit:
        cursor = cursor.limit(limit)
    groups = {"sh": [], "sz": [], "bj": []}
    for d in cursor:
        code = str(d.get("code", "")).strip()
        if len(code) != 6:
            continue
        groups[_prefix(code)].append(code)
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（冒烟）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    db = MongoClient(cfg["uri"], serverSelectionTimeoutMS=5000)[cfg["database"]]
    col = db[COL]

    groups = _collect_targets(col, args.limit or None)
    total_targets = sum(len(v) for v in groups.values())
    print(f"[{datetime.now():%H:%M:%S}] 待补全 {total_targets} 只 "
          f"(sh={len(groups['sh'])}, sz={len(groups['sz'])}, bj={len(groups['bj'])})")

    if args.dry_run:
        return

    updated = 0
    failed = 0
    fetched_missing_fields = 0  # 抓到了但 pe/total_mv 仍缺（如停牌无市值）

    for pfx in ("sh", "sz", "bj"):
        codes = groups[pfx]
        for i in range(0, len(codes), BATCH):
            chunk = codes[i:i + BATCH]
            qcodes = [f"{pfx}{c}" for c in chunk]
            try:
                got = _fetch_batch(qcodes)
            except Exception as exc:
                failed += len(chunk)
                print(f"[{datetime.now():%H:%M:%S}] 批量失败 {pfx} {i}-{i+len(chunk)}: {exc}")
                time.sleep(SLEEP)
                continue

            for code in chunk:
                row = got.get(code)
                if row is None:
                    failed += 1
                    continue
                patch = {"valuation_source": "tencent_qt_batch", "updated_at": datetime.now().isoformat(timespec="seconds")}
                for k in ("total_mv", "pe", "pb"):
                    if row.get(k) is not None:
                        patch[k] = row[k]
                if "total_mv" not in patch or "pe" not in patch:
                    # 抓到了但关键估值字段仍缺失（如停牌），不写半个数据
                    fetched_missing_fields += 1
                    continue
                col.update_one({"code": code, "active": {"$ne": False}}, {"$set": patch})
                updated += 1

            time.sleep(SLEEP)

    remaining = col.count_documents({
        "active": {"$ne": False},
        "$or": [
            {"total_mv": {"$exists": False}},
            {"total_mv": None},
            {"pe": {"$exists": False}},
            {"pe": None},
        ],
    })
    print(f"[{datetime.now():%H:%M:%S}] 完成：补全 {updated} 只，失败 {failed} 只，"
          f"抓到但估值字段仍缺 {fetched_missing_fields} 只，剩余缺失 {remaining} 只")


if __name__ == "__main__":
    main()
