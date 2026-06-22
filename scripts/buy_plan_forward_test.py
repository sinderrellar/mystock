#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forward test for buy_plan recommendation snapshots.

Reads MongoDB `buy_plan_snapshots`, evaluates recommendation performance after
T+1/T+5/T+20 trading days, and optionally persists results to
`buy_plan_forward_tests`.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from bson import ObjectId

from entry_engine import evaluate as entry_evaluate
from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config
from portfolio_controller import _build_entry_input


HORIZONS = (1, 5, 20)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _load_trading_days(store: MongoFactorDataStore) -> List[str]:
    coll = store.db[store.collections["daily_quotes"]]
    query = {"period": "daily", "data_source": store.quote_source}
    return sorted(d for d in coll.distinct("trade_date", query) if d)


def _next_or_same_trade_date(days: List[str], run_date: str) -> Optional[str]:
    for day in days:
        if day >= run_date:
            return day
    return None


def _future_trade_date(days: List[str], anchor: str, horizon: int) -> Optional[str]:
    try:
        idx = days.index(anchor)
    except ValueError:
        return None
    pos = idx + horizon
    return days[pos] if pos < len(days) else None


def _close_map(store: MongoFactorDataStore, codes: List[str], trade_date: str) -> Dict[str, float]:
    coll = store.db[store.collections["daily_quotes"]]
    query = {
        "code": {"$in": codes},
        "trade_date": trade_date,
        "period": "daily",
        "data_source": store.quote_source,
    }
    result: Dict[str, float] = {}
    for doc in coll.find(query, {"code": 1, "close": 1, "_id": 0}):
        close = _safe_float(doc.get("close"), 0)
        if close > 0:
            result[str(doc["code"])] = close
    return result


def _series_closes(
    store: MongoFactorDataStore,
    code: str,
    start_date: str,
    end_date: str,
) -> List[float]:
    coll = store.db[store.collections["daily_quotes"]]
    query = {
        "code": code,
        "trade_date": {"$gte": start_date, "$lte": end_date},
        "period": "daily",
        "data_source": store.quote_source,
    }
    closes: List[float] = []
    for doc in coll.find(query, {"close": 1, "_id": 0}).sort("trade_date", 1):
        close = _safe_float(doc.get("close"), 0)
        if close > 0:
            closes.append(close)
    return closes


def _max_drawdown_from_entry(entry_close: float, closes: List[float]) -> Optional[float]:
    if entry_close <= 0 or not closes:
        return None
    low = min(closes)
    return round((low / entry_close - 1) * 100, 2)


def _entry_signal(candidate: Dict[str, Any]) -> Dict[str, Any]:
    try:
        stock = _build_entry_input(candidate)
        entry = entry_evaluate(stock)
        signal = entry.get("signal", {})
        return {
            "action_type": signal.get("action_type"),
            "confidence": signal.get("confidence"),
            "entry_score": entry.get("entry_score"),
            "technical_score": entry.get("technical_score"),
            "alpha_bonus": entry.get("alpha_bonus"),
        }
    except Exception as exc:
        return {"action_type": None, "error": str(exc)}


def evaluate_snapshot(store: MongoFactorDataStore, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    trading_days = _load_trading_days(store)
    run_date = str(snapshot.get("run_date") or snapshot.get("generated_at", "")[:10])
    anchor_date = _next_or_same_trade_date(trading_days, run_date)
    recommendations = snapshot.get("recommendations") or []
    codes = [str(r.get("code")) for r in recommendations if r.get("code")]

    if not anchor_date:
        return {
            "ok": False,
            "snapshot_id": str(snapshot.get("_id")),
            "run_date": run_date,
            "error": "no trading day available at or after run_date",
        }

    entry_closes = _close_map(store, codes, anchor_date)
    future_dates = {h: _future_trade_date(trading_days, anchor_date, h) for h in HORIZONS}
    future_closes = {
        h: _close_map(store, codes, date) if date else {}
        for h, date in future_dates.items()
    }

    items: List[Dict[str, Any]] = []
    for rank, rec in enumerate(recommendations, start=1):
        code = str(rec.get("code"))
        entry_close = entry_closes.get(code)
        horizons: Dict[str, Any] = {}
        max_drawdown_pct = None

        for h in HORIZONS:
            date = future_dates[h]
            future_close = future_closes[h].get(code) if date else None
            if entry_close and future_close:
                ret = round((future_close / entry_close - 1) * 100, 2)
                horizons[f"t{h}"] = {
                    "date": date,
                    "close": future_close,
                    "return_pct": ret,
                }
                series = _series_closes(store, code, anchor_date, date)
                dd = _max_drawdown_from_entry(entry_close, series)
                if dd is not None:
                    max_drawdown_pct = dd if max_drawdown_pct is None else min(max_drawdown_pct, dd)
            else:
                horizons[f"t{h}"] = {
                    "date": date,
                    "close": future_close,
                    "return_pct": None,
                    "reason": "missing price",
                }

        items.append({
            "rank": rank,
            "code": code,
            "name": rec.get("name"),
            "industry": rec.get("industry"),
            "alpha_score": rec.get("alpha_score"),
            "strategy_tags": rec.get("strategy_tags", []),
            "entry_date": anchor_date,
            "entry_close": entry_close,
            "entry_signal": _entry_signal(rec),
            "horizons": horizons,
            "max_drawdown_pct": max_drawdown_pct,
        })

    summary: Dict[str, Any] = {}
    for h in HORIZONS:
        vals = [
            item["horizons"][f"t{h}"]["return_pct"]
            for item in items
            if item["horizons"][f"t{h}"].get("return_pct") is not None
        ]
        summary[f"t{h}"] = {
            "count": len(vals),
            "avg_return_pct": round(sum(vals) / len(vals), 2) if vals else None,
            "win_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None,
        }

    return {
        "ok": True,
        "schema_version": 1,
        "snapshot_id": str(snapshot.get("_id")),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "run_date": run_date,
        "entry_date": anchor_date,
        "market_context": snapshot.get("market_context", {}),
        "pipeline": snapshot.get("pipeline", {}),
        "items": items,
        "summary": summary,
        "computed_at": _utc_now(),
    }


def persist_result(store: MongoFactorDataStore, result: Dict[str, Any]) -> None:
    coll = store.db["buy_plan_forward_tests"]
    coll.create_index([("snapshot_id", 1)], unique=True, background=True)
    coll.create_index([("run_date", -1)], background=True)
    coll.create_index([("items.code", 1), ("run_date", -1)], background=True)
    coll.replace_one(
        {"snapshot_id": result["snapshot_id"]},
        result,
        upsert=True,
    )


def _print_result(result: Dict[str, Any]) -> None:
    if not result.get("ok"):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    print("=" * 88)
    print(f"Buy Plan Forward Test | snapshot={result['snapshot_id']} | entry={result['entry_date']}")
    print("=" * 88)
    for key, row in result.get("summary", {}).items():
        print(
            f"{key.upper():>4}: count={row['count']:<3} "
            f"avg={row['avg_return_pct']}% win={row['win_rate_pct']}%"
        )
    print("-" * 88)
    for item in result.get("items", []):
        h = item["horizons"]
        print(
            f"{item['rank']:>2} {item['code']} {str(item.get('name') or '')[:8]:<8} "
            f"entry={item.get('entry_close')} "
            f"T1={h['t1'].get('return_pct')}% "
            f"T5={h['t5'].get('return_pct')}% "
            f"T20={h['t20'].get('return_pct')}% "
            f"dd={item.get('max_drawdown_pct')}% "
            f"entry_signal={item.get('entry_signal', {}).get('action_type')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward test buy_plan snapshots")
    parser.add_argument("--snapshot-id", default=None, help="指定 buy_plan_snapshots _id")
    parser.add_argument("--all", action="store_true", help="刷新全部快照")
    parser.add_argument("--limit", type=int, default=20, help="--all 时最多处理多少条快照")
    parser.add_argument("--no-save", action="store_true", help="只打印，不写 buy_plan_forward_tests")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    store = MongoFactorDataStore(_load_mongodb_config(config_path))
    snapshots = store.db["buy_plan_snapshots"]

    if args.snapshot_id:
        docs = [snapshots.find_one({"_id": ObjectId(args.snapshot_id)})]
        docs = [d for d in docs if d]
    elif args.all:
        docs = list(snapshots.find({}, sort=[("generated_at", -1)], limit=args.limit))
    else:
        doc = snapshots.find_one({}, sort=[("generated_at", -1)])
        docs = [doc] if doc else []

    if not docs:
        print("No buy_plan_snapshots found.")
        return

    results = []
    for snapshot in docs:
        result = evaluate_snapshot(store, snapshot)
        results.append(result)
        if result.get("ok") and not args.no_save:
            persist_result(store, result)

    if args.json:
        print(json.dumps(results if args.all else results[0], ensure_ascii=False, indent=2, default=str))
    else:
        for result in results:
            _print_result(result)


if __name__ == "__main__":
    main()
