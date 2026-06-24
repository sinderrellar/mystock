#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据导入管道

用法:
    # 导入持仓+观察池（每日盘后）
    python3 data_import_pipeline.py import-portfolio

    # 全量导入（每周日）
    python3 data_import_pipeline.py import-all

对应 crontab:
    # 交易日 15:30 — 持仓数据 + 行业资金流
    30 15 * * 1-5 cd PROJECT_DIR/scripts && python3 data_import_pipeline.py import-portfolio
    30 15 * * 1-5 cd PROJECT_DIR/scripts && python3 data_import_pipeline.py import-industry-moneyflow

    # 周日 20:00 — 全市场基础 + 持仓财务 + 行业资金流
    0 20 * * 0 cd PROJECT_DIR/scripts && python3 data_import_pipeline.py import-all
"""

import argparse
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

import yaml
from pymongo import ASCENDING

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from factor_data_import_service import (
    ConfigError,
    FactorDataImporter,
    MongoFactorDataStore,
    _load_mongodb_config,
    _load_yaml,
    validate_system_config,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SlidingWindowRateLimiter:
    """Simple sliding-window limiter for serial Tushare API calls."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0) -> None:
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = window_seconds
        self._calls: Deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= self.window_seconds:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_for = self.window_seconds - (now - self._calls[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.window_seconds:
                self._calls.popleft()
        self._calls.append(time.monotonic())


def _load_portfolio_positions(portfolio_path: str, include_watchlist: bool = True) -> List[Dict[str, Any]]:
    portfolio = _load_yaml(portfolio_path)
    positions = list(portfolio.get("positions") or [])
    if include_watchlist:
        positions.extend(portfolio.get("watchlist") or [])
    return [p for p in positions if p.get("code")]


# ============================================================
# 行业分类导入
# ============================================================

# ============================================================
# 持仓数据导入（基础信息 + K线 + 财务）
# ============================================================

def import_portfolio(portfolio_path: str, store: MongoFactorDataStore,
                     quote_limit: int = 180, sleep_seconds: float = 0.6) -> Dict[str, Any]:
    """导入持仓和观察池的全部数据。"""
    positions = _load_portfolio_positions(portfolio_path, include_watchlist=True)
    if not positions:
        return {"ok": False, "error": "无持仓数据"}

    importer = FactorDataImporter(store, quote_limit=quote_limit)
    print(f"导入 {len(positions)} 只股票（持仓+观察池）...")

    stats = importer.sync_positions(positions, sleep_seconds=sleep_seconds)
    ok_count = sum(1 for item in stats.get("items", []) if item.get("ok"))
    fail_count = stats.get("failed", 0)

    for item in stats.get("items", []):
        if item.get("ok"):
            warnings = f"，警告: {'; '.join(item['warnings'])}" if item.get("warnings") else ""
            print(f"  ✓ {item['code']} {item.get('name','')}: K线 {item['quotes']}条, 财务{'有' if item['financial'] else '无'}{warnings}")
        else:
            print(f"  ✗ {item['code']}: {item.get('error', '失败')}")

    # 补申万行业编码
    _enrich_industry_codes(store, positions)

    print(f"持仓导入完成: 成功 {ok_count}, 失败 {fail_count}")
    return {"ok": True, "stats": stats}


def _get_sw_industry_map() -> Dict[str, str]:
    """通过 tushare stock_basic 获取 A股 6位代码 -> 行业名称 映射。"""
    try:
        pro = _get_tushare_pro()
        if pro is None:
            return {}
        basic = pro.stock_basic(exchange='', list_status='L',
                                fields='ts_code,industry')
        sw_map: Dict[str, str] = {}
        for _, row in basic.iterrows():
            ts_code = str(row.get('ts_code', ''))
            industry_name = str(row.get('industry', ''))
            code = ts_code.split('.')[0]
            if industry_name and industry_name != 'None':
                sw_map[code] = industry_name
        return sw_map
    except Exception as e:
        print(f"  通过 tushare 获取行业映射失败: {e}")
        return {}


def _enrich_industry_codes(store: MongoFactorDataStore,
                           positions: List[Dict[str, Any]]) -> None:
    """给持仓股票的 MongoDB 文档补上申万行业编码。"""
    a_codes = [p["code"] for p in positions if p.get("market") == "A股"]
    if not a_codes:
        return
    try:
        sw_map = _get_sw_industry_map()
        for code in a_codes:
            ind = sw_map.get(code)
            if ind:
                store.db[store.collections["basic_info"]].update_one(
                    {"code": code},
                    {"$set": {"industry_code": ind, "industry": ind}},
                )
    except Exception:
        pass  # 行业编码补齐失败不影响主流程


# ============================================================
# 通用股票深度导入（选股候选按需补齐）
# ============================================================

def import_stocks(codes: List[str], store: MongoFactorDataStore,
                  quote_limit: int = 180, sleep_seconds: float = 0.6) -> Dict[str, Any]:
    """导入指定股票代码的完整数据（K线+基础+财务）。

    Args:
        codes: 股票代码列表，如 ['600941', '000001', '00700']
    """
    if not codes:
        return {"ok": False, "error": "未提供股票代码"}

    positions = []
    for code in codes:
        code = str(code).strip()
        if not code:
            continue
        # 根据代码前缀判断市场
        if len(code) <= 5 and not code.startswith(("5", "6", "0", "1", "2", "3")):
            market = "港股"
        elif code.startswith(("5", "6", "9")):
            market = "A股"
        else:
            market = "A股"

        positions.append({
            "code": code,
            "market": market,
            "asset_type": "stock",
            "name": code,  # 导入后会被覆盖
        })

    importer = FactorDataImporter(store, quote_limit=quote_limit)
    print(f"导入 {len(positions)} 只股票（K线+基础+财务）...")

    stats = importer.sync_positions(positions, sleep_seconds=sleep_seconds)
    ok_count = sum(1 for item in stats.get("items", []) if item.get("ok"))
    fail_count = stats.get("failed", 0)

    for item in stats.get("items", []):
        if item.get("ok"):
            warnings = f"，警告: {'; '.join(item['warnings'])}" if item.get("warnings") else ""
            print(f"  ✓ {item['code']} {item.get('name','')}: K线 {item['quotes']}条, 财务{'有' if item['financial'] else '无'}{warnings}")
        else:
            print(f"  ✗ {item['code']}: {item.get('error', '失败')}")

    print(f"导入完成: 成功 {ok_count}, 失败 {fail_count}")
    return {"ok": True, "stats": stats}


# ============================================================
# 全市场日线预导入 — 供 buy_plan 等策略直接查询
# ============================================================

def _normalize_tushare_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10:
        return text.replace("-", "")
    return text


def _next_date(date_text: str) -> str:
    dt = datetime.strptime(date_text, "%Y-%m-%d")
    return (dt + timedelta(days=1)).strftime("%Y%m%d")


def _ts_code(code: str) -> str:
    return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"


def _fetch_tushare_qfq_bar(ts_module, pro, ts_code: str, start: str, end: str):
    """Call ts.pro_bar while capturing its internal stdout/stderr noise."""
    import contextlib
    import io

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        df = ts_module.pro_bar(
            ts_code=ts_code,
            api=pro,
            start_date=start,
            end_date=end,
            freq="D",
            adj="qfq",
        )

    noise = captured.getvalue().strip()
    needs_fallback = (
        (df is None or df.empty) and noise and "trade_date" in noise
    ) or (
        df is not None and not df.empty and "trade_date" not in df.columns
    ) or (
        noise and "trade_date" in noise
    )
    if needs_fallback:
        reason = noise.splitlines()[0] if noise else "missing trade_date"
        fallback = _fetch_tushare_daily_qfq_bar(pro, ts_code, start, end)
        if fallback is None or fallback.empty:
            raise RuntimeError(f"pro_bar failed ({reason}); daily+adj_factor fallback empty")
        return fallback, reason
    if df is None or df.empty:
        return df, None
    return df, None


def _fetch_tushare_daily_qfq_bar(pro, ts_code: str, start: str, end: str):
    """Build qfq bars from Tushare daily + adj_factor when pro_bar is malformed."""
    daily = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
    if daily is None or daily.empty or "trade_date" not in daily.columns:
        return daily
    adj = pro.adj_factor(ts_code=ts_code, start_date=start, end_date=end)
    if adj is None or adj.empty or "trade_date" not in adj.columns or "adj_factor" not in adj.columns:
        return daily

    merged = daily.merge(adj[["trade_date", "adj_factor"]], on="trade_date", how="left")
    if merged["adj_factor"].dropna().empty:
        return daily
    latest_factor = float(
        merged.dropna(subset=["adj_factor"]).sort_values("trade_date", ascending=False)["adj_factor"].iloc[0]
    )
    if latest_factor <= 0:
        return daily
    ratio = merged["adj_factor"] / latest_factor
    for col in ("open", "high", "low", "close", "pre_close"):
        if col in merged.columns:
            merged[col] = merged[col] * ratio
    return merged


def _quote_pool_query(pool: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "display_market": "A股",
        "market": {"$in": pool.get("markets", ["主板", "创业板", "科创板"])},
        "latest_amount": {"$gte": pool.get("min_amount", 50000000) / 10000},
        "total_mv": {"$gte": pool.get("min_market_cap", 5000000000)},
    }


def _a_share_universe_query() -> Dict[str, Any]:
    return {
        "display_market": "A股",
        "market": {"$in": ["主板", "创业板", "科创板"]},
    }


def _quote_scope_query(scope: str, pool: Dict[str, Any]) -> Dict[str, Any]:
    if scope == "universe":
        return _a_share_universe_query()
    return _quote_pool_query(pool)


def _normalize_trade_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None


def _parse_trade_date_flexible(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text or text == "无":
        return None
    patterns = ("%Y-%m-%d", "%Y%m%d", "%d %b %Y", "%d %B %Y")
    candidates = [text, text.title(), text.upper()]
    for candidate in candidates:
        for pattern in patterns:
            try:
                return datetime.strptime(candidate, pattern).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _latest_doc_by_trade_date(coll, query: Dict[str, Any], projection: Dict[str, Any], limit: int = 120) -> Optional[Dict[str, Any]]:
    best_doc: Optional[Dict[str, Any]] = None
    best_dt: Optional[datetime] = None
    for doc in coll.find(query, projection).limit(limit):
        dt = _parse_trade_date_flexible(doc.get("trade_date"))
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_doc = doc
    return best_doc


def _find_quote_gaps(
    store: MongoFactorDataStore,
    stocks: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
) -> Dict[str, Dict[str, Any]]:
    """Find per-code quote gaps against the observed Tushare trading days."""
    quotes_coll = store.db[store.collections["daily_quotes"]]
    date_rows = quotes_coll.aggregate([
        {"$match": {
            "data_source": "tushare",
            "period": "daily",
            "trade_date": {"$gte": start_date, "$lte": end_date},
        }},
        {"$group": {"_id": "$trade_date"}},
        {"$sort": {"_id": 1}},
    ], allowDiskUse=True)
    expected_dates = [row["_id"] for row in date_rows]
    if not expected_dates:
        return {}
    expected_positions = {day: idx for idx, day in enumerate(expected_dates)}

    code_to_stock = {
        str(stock.get("code") or "").zfill(6): stock
        for stock in stocks
        if stock.get("code")
    }
    codes = list(code_to_stock)
    docs = quotes_coll.find(
        {
            "code": {"$in": codes},
            "data_source": "tushare",
            "period": "daily",
            "trade_date": {"$gte": start_date, "$lte": end_date},
        },
        {"code": 1, "trade_date": 1, "_id": 0},
    )

    dates_by_code: Dict[str, set] = {code: set() for code in codes}
    for doc in docs:
        code = str(doc.get("code") or "").zfill(6)
        trade_date = _normalize_trade_date(doc.get("trade_date"))
        if code and trade_date:
            dates_by_code.setdefault(code, set()).add(trade_date)

    gap_plan: Dict[str, Dict[str, Any]] = {}
    for code, stock in code_to_stock.items():
        seen_dates = dates_by_code.get(code, set())
        missing_dates = [day for day in expected_dates if day not in seen_dates]
        if not missing_dates:
            continue
        ranges: List[Dict[str, str]] = []
        range_start = missing_dates[0]
        prev_day = missing_dates[0]
        for day in missing_dates[1:]:
            if expected_positions[day] != expected_positions[prev_day] + 1:
                ranges.append({
                    "start_date": range_start.replace("-", ""),
                    "end_date": prev_day.replace("-", ""),
                })
                range_start = day
            prev_day = day
        ranges.append({
            "start_date": range_start.replace("-", ""),
            "end_date": prev_day.replace("-", ""),
        })
        gap_plan[code] = {
            "code": code,
            "name": stock.get("name"),
            "list_date": stock.get("list_date"),
            "start_date": missing_dates[0].replace("-", ""),
            "missing_dates": missing_dates,
            "ranges": ranges,
        }
    return gap_plan


def _remaining_missing_dates(quotes_coll, code: str, missing_dates: List[str]) -> List[str]:
    if not missing_dates:
        return []
    existing_dates = {
        doc["trade_date"]
        for doc in quotes_coll.find(
            {
                "code": code,
                "data_source": "tushare",
                "period": "daily",
                "trade_date": {"$in": missing_dates},
            },
            {"trade_date": 1, "_id": 0},
        )
    }
    return [day for day in missing_dates if day not in existing_dates]


def _summarize_quote_coverage(
    store: MongoFactorDataStore,
    quotes_coll,
    recent_dates: List[str],
    expected_codes: set,
    latest_trade_date: str,
    label: str,
) -> Optional[Dict[str, Any]]:
    if not expected_codes or not recent_dates:
        return None

    docs = quotes_coll.find(
        {
            "period": "daily",
            "data_source": store.quote_source,
            "trade_date": {"$in": recent_dates},
            "code": {"$in": list(expected_codes)},
        },
        {"code": 1, "trade_date": 1, "_id": 0},
    )
    seen_by_code: Dict[str, set] = {code: set() for code in expected_codes}
    for doc in docs:
        code = str(doc.get("code") or "").zfill(6)
        trade_date = doc.get("trade_date")
        if code and trade_date:
            seen_by_code.setdefault(code, set()).add(trade_date)

    latest_day = recent_dates[-1]
    late_start = []
    stale_tail = []
    sporadic = []
    latest_only = []

    for code in sorted(expected_codes):
        seen = seen_by_code.get(code, set())
        missing = [day for day in recent_dates if day not in seen]
        if not missing:
            continue
        first_seen = min(seen) if seen else None
        last_seen = max(seen) if seen else None
        basic = store.db[store.collections["basic_info"]].find_one(
            {"code": code},
            {"name": 1, "_id": 0},
        ) or {}
        item = {
            "code": code,
            "name": basic.get("name", ""),
            "missing_days": len(missing),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "missing_sample": missing[:5],
        }

        if first_seen and all(day < first_seen for day in missing):
            late_start.append(item)
            continue
        if last_seen and all(day > last_seen for day in missing):
            if len(missing) == 1 and missing[0] == latest_day:
                latest_only.append(item)
            else:
                stale_tail.append(item)
            continue
        sporadic.append(item)

    latest_codes = seen_by_code
    latest_count = sum(1 for code in expected_codes if latest_trade_date in latest_codes.get(code, set()))
    coverage_summary = {
        "label": label,
        "window_start": recent_dates[0],
        "window_end": recent_dates[-1],
        "pool_size": len(expected_codes),
        "latest_count": latest_count,
        "latest_missing": len(expected_codes) - latest_count,
        "late_start_count": len(late_start),
        "stale_tail_count": len(stale_tail),
        "latest_only_count": len(latest_only),
        "sporadic_count": len(sporadic),
        "late_start_sample": late_start[:5],
        "stale_tail_sample": stale_tail[:5],
        "latest_only_sample": latest_only[:5],
        "sporadic_sample": sporadic[:5],
    }
    status = "✅" if not sporadic and not latest_only else "⚠️"
    if coverage_summary["latest_missing"] > max(5, len(expected_codes) // 20):
        status = "⚠️"
    return {
        "name": f"A股日线覆盖分类({label})",
        "latest": (
            f"latest_missing={coverage_summary['latest_missing']}, "
            f"late_start={coverage_summary['late_start_count']}, "
            f"stale_tail={coverage_summary['stale_tail_count']}, "
            f"latest_only={coverage_summary['latest_only_count']}, "
            f"sporadic={coverage_summary['sporadic_count']}"
        ),
        "count": len(expected_codes),
        "age_days": "—",
        "status": status,
        "coverage_summary": coverage_summary,
    }


def import_quotes(
    store: MongoFactorDataStore,
    limit: int = 5000,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sleep_seconds: float = 0.12,
    rate_limit_per_minute: int = 160,
    repair_missing: bool = False,
    scope: str = "pool",
) -> Dict[str, Any]:
    """导入 canonical Tushare 前复权日线。

    使用 ts.pro_bar(adj="qfq")，保持与旧 TradingAgents 历史全量源一致。
    """
    import tushare as ts

    pro = _get_tushare_pro()
    if pro is None:
        return {"ok": False, "error": "Tushare token 未配置或 tushare 不可用"}

    pool = _get_pool_filters()
    query = _quote_scope_query(scope, pool)
    stocks = list(store.db[store.collections["basic_info"]].find(
        query,
        {"code": 1, "name": 1, "list_date": 1, "_id": 0},
    ).sort("latest_amount", -1).limit(limit))

    quotes_coll = store.db[store.collections["daily_quotes"]]
    imported = 0
    failed = 0
    skipped = 0
    fallback_used = 0
    latest_date = None
    scanned_gap_codes = 0
    unresolved_gap_codes = 0
    unresolved_gap_dates = 0
    end = _normalize_tushare_date(end_date) or datetime.now().strftime("%Y%m%d")
    normalized_end = _normalize_trade_date(end) or end
    limiter = SlidingWindowRateLimiter(rate_limit_per_minute)

    gap_plan: Dict[str, Dict[str, Any]] = {}
    if repair_missing:
        normalized_start = _normalize_trade_date(start_date) or "1990-01-01"
        gap_plan = _find_quote_gaps(store, stocks, normalized_start, normalized_end)
        scanned_gap_codes = len(gap_plan)
        stocks = [stock for stock in stocks if str(stock.get("code") or "").zfill(6) in gap_plan]
        print(
            f"扫描缺口完成: range={normalized_start}..{normalized_end}, "
            f"gap_codes={scanned_gap_codes}, import_targets={len(stocks)}, scope={scope}"
        )

    print(
        f"导入 Tushare 前复权日线: stocks={len(stocks)}, end={end}, "
        f"rate_limit={rate_limit_per_minute}/min, repair_missing={repair_missing}, scope={scope}"
    )
    for i, stock in enumerate(stocks, start=1):
        code = str(stock.get("code") or "").zfill(6)
        if not code:
            skipped += 1
            continue

        fetch_ranges: List[Dict[str, str]] = []
        if repair_missing and code in gap_plan:
            fetch_ranges = gap_plan[code].get("ranges") or []
        else:
            start = _normalize_tushare_date(start_date)
            if not start:
                latest = quotes_coll.find_one(
                    {"code": code, "period": "daily", "data_source": "tushare"},
                    {"trade_date": 1, "_id": 0},
                    sort=[("trade_date", -1)],
                )
                if latest and latest.get("trade_date"):
                    start = _next_date(str(latest["trade_date"]))
                else:
                    list_date = str(stock.get("list_date") or "").replace("-", "")
                    start = list_date if len(list_date) == 8 else "19900101"
            if start <= end:
                fetch_ranges = [{"start_date": start, "end_date": end}]

        if not fetch_ranges:
            skipped += 1
            continue

        imported_this_code = 0
        try:
            for fetch_range in fetch_ranges:
                start = fetch_range["start_date"]
                range_end = fetch_range["end_date"]
                if start > range_end:
                    continue
                limiter.acquire()
                df, fallback_reason = _fetch_tushare_qfq_bar(ts, pro, _ts_code(code), start, range_end)
                if fallback_reason:
                    fallback_used += 1
                if df is None or df.empty:
                    continue

                docs = []
                for _, row in df.iterrows():
                    trade_date = str(row.get("trade_date") or "")
                    if len(trade_date) == 8:
                        trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
                    close = _safe_float(row.get("close"))
                    if not trade_date or not close:
                        continue
                    docs.append({
                        "code": code,
                        "symbol": code,
                        "market": "A股",
                        "currency": "CNY",
                        "trade_date": trade_date,
                        "open": _safe_float(row.get("open")),
                        "high": _safe_float(row.get("high")),
                        "low": _safe_float(row.get("low")),
                        "close": close,
                        "pre_close": _safe_float(row.get("pre_close"), None),
                        "change": _safe_float(row.get("change"), None),
                        "pct_chg": _safe_float(row.get("pct_chg"), None),
                        "volume": _safe_float(row.get("vol"), 0) * 100,
                        "amount": _safe_float(row.get("amount"), 0) * 1000,
                        "data_source": "tushare",
                        "period": "daily",
                        "adjust": "qfq",
                    })
                if not docs:
                    continue
                count = store.upsert_quotes(docs)
                imported += count
                imported_this_code += count
                doc_latest = max(d["trade_date"] for d in docs)
                latest_date = max(latest_date or doc_latest, doc_latest)

            if imported_this_code == 0:
                skipped += 1
                continue
        except Exception as exc:
            failed += 1
            print(f"  {code} Tushare 日线失败: {exc}")
            continue

        if repair_missing and code in gap_plan:
            remaining = _remaining_missing_dates(quotes_coll, code, gap_plan[code].get("missing_dates") or [])
            if remaining:
                unresolved_gap_codes += 1
                unresolved_gap_dates += len(remaining)
                if unresolved_gap_codes <= 20:
                    print(
                        f"  未补齐 {code} {stock.get('name','')}: "
                        f"{len(remaining)} 天, sample={remaining[:5]}"
                    )

        if i % 100 == 0:
            print(f"  进度: {i}/{len(stocks)} imported={imported}, failed={failed}, skipped={skipped}, fallback={fallback_used}")
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return {
        "ok": failed == 0,
        "source": "tushare",
        "adjust": "qfq",
        "scope": scope,
        "stocks": len(stocks),
        "imported": imported,
        "failed": failed,
        "skipped": skipped,
        "fallback": fallback_used,
        "latest": latest_date,
        "repair_missing": repair_missing,
        "gap_codes": scanned_gap_codes,
        "unresolved_gap_codes": unresolved_gap_codes,
        "unresolved_gap_dates": unresolved_gap_dates,
        "rate_limit_per_minute": rate_limit_per_minute,
    }

def _load_portfolio_hk_stocks() -> List[Dict[str, Any]]:
    """从 portfolio.yaml 提取港股代码。"""
    portfolio_path = os.path.join(PROJECT_ROOT, "data", "portfolio.yaml")
    try:
        with open(portfolio_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        hk = [p for p in (data.get("positions") or []) if p.get("market") == "港股"]
        hk += [w for w in (data.get("watchlist") or []) if w.get("market") == "港股"]
        return hk
    except Exception:
        return []


# ================================================================
# 信号缓存 — 预计算趋势+因子，buy_plan 直接读
# ================================================================

def import_stock_moneyflow(store: MongoFactorDataStore, limit: int = 3000,
                           sleep_ms: int = 600) -> Dict[str, Any]:
    """批量导入个股资金流（带限速），存到 stock_signals.moneyflow_net。

    Tushare moneyflow API，2000积分用户每分钟可调 ~100-200次。
    默认间隔 600ms，约 100次/分，安全不触发限频。
    3000只约需 30 分钟。
    """
    ts_pro = _get_tushare_pro()
    if not ts_pro:
        return {"ok": False, "error": "Tushare 不可用"}

    # 获取可投池列表
    coll = store.db[store.collections["basic_info"]]
    stocks = list(coll.find(
        {"display_market": "A股", "market": {"$in": ["主板", "创业板", "科创板"]},
         "latest_amount": {"$gte": 5_000}, "total_mv": {"$gte": 5_000_000_000},
         "pe": {"$gt": 0, "$lt": 50}},
        {"code": 1, "name": 1, "_id": 0},
    ).sort("latest_amount", -1).limit(limit))

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    start = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y%m%d")
    imported = 0
    errors = 0

    for i, s in enumerate(stocks):
        code = s["code"]
        ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
        try:
            df = ts_pro.moneyflow(ts_code=ts_code, start_date=start, end_date=today)
            if df is not None and not df.empty:
                r = df.iloc[0]
                net = _safe_float(r.get("buy_lg_amount"), 0) - _safe_float(r.get("sell_lg_amount"), 0)
                store.db["stock_signals"].update_one(
                    {"code": code},
                    {"$set": {"moneyflow_net": net, "moneyflow_date": str(r.get("trade_date", ""))}},
                    upsert=True,
                )
                imported += 1
        except Exception:
            errors += 1

        if (i + 1) % 200 == 0:
            print(f"  资金流: {i+1}/{len(stocks)} (成功 {imported}, 失败 {errors})")
        time.sleep(sleep_ms / 1000.0)

    print(f"个股资金流完成: {imported} 成功, {errors} 失败")
    return {"ok": True, "imported": imported, "errors": errors}


def import_stock_forecast(store: MongoFactorDataStore, limit: int = 3000,
                          sleep_ms: int = 800) -> Dict[str, Any]:
    """批量导入一致预期（带限速），存到 stock_signals。

    Tushare forecast API，限频较严，默认间隔 800ms。
    """
    ts_pro = _get_tushare_pro()
    if not ts_pro:
        return {"ok": False, "error": "Tushare 不可用"}

    coll = store.db[store.collections["basic_info"]]
    stocks = list(coll.find(
        {"display_market": "A股", "market": {"$in": ["主板", "创业板", "科创板"]},
         "latest_amount": {"$gte": 5_000}, "total_mv": {"$gte": 5_000_000_000},
         "pe": {"$gt": 0, "$lt": 50}},
        {"code": 1, "name": 1, "_id": 0},
    ).sort("latest_amount", -1).limit(limit))

    imported = 0
    errors = 0

    for i, s in enumerate(stocks):
        code = s["code"]
        ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
        try:
            fc = ts_pro.forecast(ts_code=ts_code)
            if fc is not None and not fc.empty:
                fc = fc.sort_values("end_date", ascending=False)
                r = fc.iloc[0]
                store.db["stock_signals"].update_one(
                    {"code": code},
                    {"$set": {
                        "forecast_min": _safe_float(r.get("p_change_min"), None),
                        "forecast_max": _safe_float(r.get("p_change_max"), None),
                    }},
                    upsert=True,
                )
                imported += 1
        except Exception:
            errors += 1

        if (i + 1) % 200 == 0:
            print(f"  预期: {i+1}/{len(stocks)} (成功 {imported}, 失败 {errors})")
        time.sleep(sleep_ms / 1000.0)

    print(f"一致预期完成: {imported} 成功, {errors} 失败")
    return {"ok": True, "imported": imported, "errors": errors}

def _get_tushare_pro():
    """获取 Tushare pro 接口（从 config_complete.yaml 读取 token）。"""
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "config", "config_complete.yaml")
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
        if not token:
            return None
        import tushare as ts
        ts.set_token(token)
        return ts.pro_api()
    except Exception:
        return None


def _get_pool_filters() -> Dict[str, Any]:
    """从 config 读取股票池生存过滤器阈值。

    生意模式分组后，pool 层用全局最宽的 PE（覆盖所有组的 pe_max），
    各组的精细化 PE 阈值在下游（因子/策略层）生效。
    """
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "config", "config_complete.yaml")
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        pool_cfg = cfg.get("data_sources", {}).get("mongodb", {}).get("stock_pool", {})

        # 取所有 business_group 中最宽的 pe_max（确保不遗漏任一组）
        groups = pool_cfg.get("business_groups", {})
        pe_max = max(
            (g.get("pe_max", 50) for g in groups.values()),
            default=50
        )

        return {
            "min_amount": pool_cfg.get("min_amount", 50000000),
            "min_market_cap": pool_cfg.get("min_market_cap", 5000000000),
            "pe_min": 0,
            "pe_max": pe_max,  # ← 取各组 PE 上限的最大值
            "markets": pool_cfg.get("market_allowlist", ["主板", "创业板", "科创板"]),
            "business_groups": groups,
        }
    except Exception:
        return {
            "min_amount": 50000000, "min_market_cap": 5000000000,
            "pe_min": 0, "pe_max": 200,
            "markets": ["主板", "创业板", "科创板"],
            "business_groups": {},
        }


def _get_pe_percentile(pro, code: str) -> Optional[float]:
    """从 Tushare daily_basic 获取 PE_TTM 历史分位。"""
    ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
    try:
        df = pro.daily_basic(ts_code=ts_code, start_date="20240101",
                             end_date=datetime.now(timezone.utc).strftime("%Y%m%d"),
                             fields="pe_ttm")
        if df is None or df.empty or len(df) < 30:
            return None
        pe_vals = df["pe_ttm"].dropna()
        if len(pe_vals) < 30:
            return None
        current = pe_vals.iloc[0]
        pct = (pe_vals <= current).sum() / len(pe_vals) * 100
        return round(pct, 1)
    except Exception:
        return None


# ================================================================
# 股息率 — Tushare daily_basic 入 MongoDB（仅A股）
# ================================================================

def import_dividend_yield(store: MongoFactorDataStore, portfolio_path: str) -> Dict[str, Any]:
    """拉取组合中所有A股的股息率(dv_ttm/dv_ratio)，存入 stock_dividend 集合。"""
    import requests

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
    if not token:
        return {"ok": False, "error": "Tushare token 未配置"}

    with open(portfolio_path, encoding="utf-8") as f:
        pf = yaml.safe_load(f)
    a_codes: List[str] = []
    for pos in pf.get("positions", []) or []:
        if pos.get("market") == "A股" and pos.get("asset_type") == "stock":
            a_codes.append(pos["code"])
    for wl in pf.get("watchlist", []) or []:
        if wl.get("market") == "A股":
            a_codes.append(wl["code"])
    if not a_codes:
        return {"ok": True, "message": "无A股持仓/观察，跳过"}

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    results: Dict[str, Any] = {}
    for code in a_codes:
        ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
        try:
            resp = requests.post(
                "https://api.tushare.pro",
                json={
                    "api_name": "daily_basic",
                    "token": token,
                    "params": {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
                    "fields": "ts_code,trade_date,dv_ratio,dv_ttm",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                results[code] = f"API错误: {data.get('msg')}"
                continue
            items = data["data"]["items"]
            count = 0
            for item in items:
                doc = {
                    "code": code,
                    "ts_code": item[0],
                    "trade_date": item[1],
                    "dv_ratio": float(item[2]) if item[2] else None,
                    "dv_ttm": float(item[3]) if item[3] else None,
                    "source": "tushare_daily_basic",
                    "updated_at": datetime.now(),
                }
                store.db["stock_dividend"].update_one(
                    {"code": code, "trade_date": item[1]},
                    {"$set": doc},
                    upsert=True,
                )
                count += 1
            results[code] = f"写入{count}条"
        except Exception as e:
            results[code] = str(e)
        time.sleep(0.3)

    # 索引
    store.db["stock_dividend"].create_index(
        [("code", ASCENDING), ("trade_date", ASCENDING)], unique=True, background=True)
    return {"ok": True, "results": results}


# ================================================================
# 港股南向持仓 — Tushare hk_hold 入 MongoDB
# ================================================================

def import_hk_southbound(store: MongoFactorDataStore, portfolio_path: str) -> Dict[str, Any]:
    """拉取组合中所有港股的南向持股数据，存入 stock_southbound 集合。"""
    import requests

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
    if not token:
        return {"ok": False, "error": "Tushare token 未配置"}

    # 从 portfolio 中提取港股代码
    with open(portfolio_path, encoding="utf-8") as f:
        pf = yaml.safe_load(f)
    hk_codes: List[str] = []
    for pos in pf.get("positions", []) or []:
        if pos.get("market") == "港股":
            hk_codes.append(pos["code"])
    for wl in pf.get("watchlist", []) or []:
        if wl.get("market") == "港股":
            hk_codes.append(wl["code"])
    if not hk_codes:
        return {"ok": True, "message": "无港股持仓/观察，跳过"}

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    results: Dict[str, Any] = {}
    for code in hk_codes:
        ts_code = f"{code}.HK"
        try:
            resp = requests.post(
                "https://api.tushare.pro",
                json={
                    "api_name": "hk_hold",
                    "token": token,
                    "params": {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
                    "fields": "trade_date,ts_code,name,ratio,vol",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                results[code] = f"API错误: {data.get('msg')}"
                continue
            items = data["data"]["items"]
            count = 0
            for item in items:
                doc = {
                    "code": code,
                    "ts_code": item[1],
                    "name": item[2],
                    "trade_date": item[0],
                    "ratio": float(item[3]) if item[3] else 0,
                    "vol": int(item[4]) if item[4] else 0,
                    "source": "tushare_hk_hold",
                    "updated_at": datetime.now(),
                }
                store.db["stock_southbound"].update_one(
                    {"code": code, "trade_date": item[0]},
                    {"$set": doc},
                    upsert=True,
                )
                count += 1
            results[code] = f"写入{count}条"
        except Exception as e:
            results[code] = str(e)
        import time
        time.sleep(0.3)

    return {"ok": True, "results": results}


# ================================================================
# 港股做空数据 — HKEX 免费公开数据
# ================================================================

def import_hk_shortsell(store: MongoFactorDataStore) -> Dict[str, Any]:
    """从 HKEX 抓取港股做空数据（早盘+全天），存入 stock_shortsell 集合。"""
    import urllib.request as _req
    import re

    results = {}
    urls = {
        "morning": "https://www.hkex.com.hk/eng/stat/smstat/ssturnover/ncms/mshtmain.htm",
        "day": "https://www.hkex.com.hk/eng/stat/smstat/ssturnover/ncms/ashtmain.htm",
    }
    for period, url in urls.items():
        try:
            req = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _req.urlopen(req, timeout=10) as r:
                html = r.read().decode("iso-8859-1", errors="ignore")
            pre = re.findall(r"<pre>(.*?)</pre>", html, re.DOTALL)
            if not pre:
                results[period] = "无数据(可能未收盘)"
                continue
            lines = pre[0].strip().split("\n")
            # 提取交易日期
            date_match = re.search(r"TRADING DATE\s*:\s*(\d{1,2}\s+\w+\s+\d{4})", pre[0])
            trade_date = date_match.group(1) if date_match else ""
            count = 0
            for line in lines:
                # 匹配: 空格+数字+空格+名称...
                m = re.match(r"\s+(\d{1,5})\s{2,}([A-Z][A-Za-z .&,-]+?)\s{2,}([\d,]+)\s+([\d,]+)", line)
                if not m:
                    continue
                code_num = m.group(1).zfill(5)  # 700 → 00700
                name = m.group(2).strip()
                shares = int(m.group(3).replace(",", ""))
                amount = int(m.group(4).replace(",", ""))
                doc = {
                    "code": code_num,
                    "name": name,
                    "shares": shares,
                    "amount": amount,
                    "trade_date": trade_date,
                    "period": period,
                    "source": "hkex",
                    "updated_at": _utc_now(),
                }
                store.db["stock_shortsell"].update_one(
                    {"code": code_num, "trade_date": trade_date, "period": period},
                    {"$set": doc}, upsert=True)
                count += 1
            results[period] = f"导入{count}条 ({trade_date})"
        except Exception as e:
            results[period] = str(e)

    return {"ok": True, "results": results}


# ================================================================
# PE 修复 — 只补缺失的 PE/PB（腾讯接口），不动日线
# ================================================================

def repair_pe(store: MongoFactorDataStore) -> Dict[str, Any]:
    from factor_data_import_service import FactorDataImporter
    missing = list(store.db[store.collections["basic_info"]].find(
        {"display_market": "A股", "market": {"$in": ["主板", "创业板", "科创板"]},
         "latest_amount": {"$gte": 5_000},  # 万元单位，5000万
         "$or": [{"pe": None}, {"pe": 0}, {"pe": {"$exists": False}}]},
        {"code": 1, "name": 1, "_id": 0},
    ).limit(2000))
    if not missing:
        print("无 PE 缺失的股票")
        return {"ok": True, "repaired": 0}
    print(f"PE 缺失: {len(missing)} 只，从腾讯接口补...")
    repaired = 0
    for i, s in enumerate(missing):
        if (i + 1) % 200 == 0:
            print(f"  进度: {i+1}/{len(missing)}")
        try:
            fetched = FactorDataImporter._fetch_basic_via_tencent_a(s["code"])
            if fetched and fetched.get("pe") and fetched["pe"] > 0:
                store.db[store.collections["basic_info"]].update_one(
                    {"code": s["code"]},
                    {"$set": {"pe": fetched["pe"], "pb": fetched.get("pb"),
                              "total_mv": fetched.get("total_mv"),
                              "close": fetched.get("close"),
                              "latest_amount": fetched.get("latest_amount"),
                              "valuation_source": "tencent_repair"}})
                repaired += 1
        except Exception:
            pass
        time.sleep(0.1)
    print(f"PE 修复完成: {repaired}/{len(missing)}")
    return {"ok": True, "repaired": repaired}


# ================================================================
# 全市场主力资金流向导入 — Tushare moneyflow_mkt_dc（2次/小时限制）
# ================================================================

def import_market_moneyflow(store: MongoFactorDataStore) -> Dict[str, Any]:
    """导入全市场主力资金流向（HTTP API，避免 tushare 包 numpy 兼容问题）。"""
    import requests

    def _import_market_moneyflow_akshare(reason: str) -> Dict[str, Any]:
        try:
            import akshare as ak

            df = ak.stock_market_fund_flow()
        except Exception as exc:
            return {"ok": False, "error": f"Tushare不可用({reason}); AKShare失败: {exc}"}
        if df is None or df.empty:
            return {"ok": False, "error": f"Tushare不可用({reason}); AKShare返回空数据"}

        count = 0
        for _, row in df.tail(120).iterrows():
            trade_date = str(row.get("日期", "")).replace("-", "")[:8]
            if not trade_date:
                continue
            doc = {
                "trade_date": trade_date,
                "net_amount": _safe_float(row.get("主力净流入-净额"), 0),
                "buy_elg_amount": _safe_float(row.get("超大单净流入-净额"), 0),
                "buy_lg_amount": _safe_float(row.get("大单净流入-净额"), 0),
                "buy_md_amount": _safe_float(row.get("中单净流入-净额"), 0),
                "buy_sm_amount": _safe_float(row.get("小单净流入-净额"), 0),
                "close_sh": _safe_float(row.get("上证-收盘价"), 0),
                "pct_change_sh": _safe_float(row.get("上证-涨跌幅"), 0),
                "close_sz": _safe_float(row.get("深证-收盘价"), 0),
                "pct_change_sz": _safe_float(row.get("深证-涨跌幅"), 0),
                "source": "akshare_stock_market_fund_flow",
                "fallback_reason": reason,
                "updated_at": datetime.now(),
            }
            store.db["market_moneyflow"].update_one(
                {"trade_date": trade_date}, {"$set": doc}, upsert=True)
            count += 1

        latest = str(df.iloc[-1].get("日期", "")).replace("-", "")[:8]
        return {
            "ok": True,
            "imported": count,
            "latest": latest,
            "source": "akshare_stock_market_fund_flow",
            "fallback_reason": reason,
        }

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
    if not token:
        return _import_market_moneyflow_akshare("缺少 Tushare token")

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    try:
        resp = requests.post(
            "https://api.tushare.pro",
            json={
                "api_name": "moneyflow_mkt_dc",
                "token": token,
                "params": {"start_date": start, "end_date": end},
                "fields": "trade_date,net_amount,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            return _import_market_moneyflow_akshare(data.get("msg", "API错误"))
        items = data["data"]["items"]
    except Exception as e:
        return _import_market_moneyflow_akshare(str(e))

    count = 0
    for item in items:
        if not item or len(item) < 6:
            continue
        doc = {
            "trade_date": item[0],
            "net_amount": float(item[1]) if len(item) > 1 and item[1] else 0,
            "buy_elg_amount": float(item[2]) if len(item) > 2 and item[2] else 0,
            "buy_lg_amount": float(item[3]) if len(item) > 3 and item[3] else 0,
            "buy_md_amount": float(item[4]) if len(item) > 4 and item[4] else 0,
            "buy_sm_amount": float(item[5]) if len(item) > 5 and item[5] else 0,
            "source": "tushare",
            "updated_at": datetime.now(),
        }
        store.db["market_moneyflow"].update_one(
            {"trade_date": item[0]}, {"$set": doc}, upsert=True)
        count += 1

    return {"ok": True, "imported": count, "range": f"{start}-{end}"}


# ================================================================
# 行业资金流向导入 — Tushare moneyflow_ind_dc
# ================================================================

def import_industry_moneyflow(store: MongoFactorDataStore) -> Dict[str, Any]:
    """导入行业资金流向：批量取个股 moneyflow，按申万行业聚合主力净额。

    替代已失效的 moneyflow_ind_dc。每次取 100 只，聚合后写入 industry_moneyflow。
    """
    ts_pro = _get_tushare_pro()
    if not ts_pro:
        return {"ok": False, "error": "Tushare 不可用"}

    # 取 A 股可投池（有行业编码的股票）
    pool = list(store.db[store.collections["basic_info"]].find(
        {"display_market": "A股", "industry_code": {"$exists": True, "$ne": None}},
        {"code": 1, "industry": 1, "_id": 0}
    ))
    if not pool:
        return {"ok": False, "error": "无可投池"}

    # Build ts_code → industry mapping
    code_to_ind = {}
    ts_codes = []
    for s in pool:
        code = str(s["code"])
        ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
        ts_codes.append(ts_code)
        code_to_ind[ts_code] = s.get("industry", "")

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")

    # 分批取（每次 100 只，限速 0.3s）
    batch_size = 100
    all_rows = []
    for i in range(0, len(ts_codes), batch_size):
        batch = ts_codes[i:i+batch_size]
        try:
            df = ts_pro.moneyflow(ts_code=','.join(batch), start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                all_rows.append(df)
        except Exception as e:
            print(f"  moneyflow 批次 {i//batch_size} 失败: {e}")
        time.sleep(0.3)

    if not all_rows:
        return {"ok": False, "error": "无数据返回"}

    import pandas as pd
    df_all = pd.concat(all_rows, ignore_index=True)

    # 按 trade_date + industry 聚合
    df_all['industry'] = df_all['ts_code'].map(code_to_ind)
    df_all = df_all[df_all['industry'] != '']
    df_all['net_mf_amount'] = pd.to_numeric(df_all['net_mf_amount'], errors='coerce')

    grouped = df_all.groupby(['trade_date', 'industry'], as_index=False).agg(
        net_amount=('net_mf_amount', 'sum'),
        stock_count=('ts_code', 'nunique'),
    )

    # 写入 MongoDB
    count = 0
    for _, row in grouped.iterrows():
        doc = {
            "trade_date": str(row['trade_date']),
            "industry_name": row['industry'],
            "net_amount": float(row['net_amount']),
            "stock_count": int(row['stock_count']),
            "source": "tushare_moneyflow_agg",
            "updated_at": datetime.now(),
        }
        store.db["industry_moneyflow"].update_one(
            {"trade_date": doc["trade_date"], "industry_name": doc["industry_name"]},
            {"$set": doc}, upsert=True)
        count += 1

    return {"ok": True, "imported": count, "range": f"{start}-{end}"}


# ================================================================
# 宏观新闻导入 — Tushare major_news 华尔街见闻（4次/小时限制）
# ================================================================

def import_macro_news(store: MongoFactorDataStore) -> Dict[str, Any]:
    """导入宏观新闻，Tushare 华尔街见闻优先，失败降级到 AKShare 东方财富。"""
    result = _import_macro_news_tushare(store)
    if result.get("ok"):
        return result

    print(f"  Tushare 失败: {result.get('error', '未知')}，降级到 AKShare...")
    return _import_macro_news_akshare(store)


def _import_macro_news_tushare(store: MongoFactorDataStore) -> Dict[str, Any]:
    """Tushare major_news 华尔街见闻（优先）。"""
    import requests

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
    if not token:
        return {"ok": False, "error": "缺少 Tushare token"}

    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        resp = requests.post(
            "https://api.tushare.pro",
            json={
                "api_name": "major_news",
                "token": token,
                "params": {
                    "src": "华尔街见闻",
                    "start_date": f"{today_str} 00:00:00",
                    "end_date": f"{today_str} 23:59:59",
                },
                "fields": "title,pub_time",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            return {"ok": False, "error": data.get("msg", "API错误")}
        items = data["data"]["items"]
    except Exception as e:
        return {"ok": False, "error": str(e)}

    count = _persist_news(items, store)
    return {"ok": True, "imported": count, "date": today_str, "source": "tushare_wallstreetcn"}


def _import_macro_news_akshare(store: MongoFactorDataStore) -> Dict[str, Any]:
    """AKShare 东方财富全球财经新闻（降级）。"""
    import akshare as ak

    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        df = ak.stock_info_global_em()
        if df is None or df.empty:
            return {"ok": False, "error": "AKShare 返回空数据"}
        # 只取今天发布的
        items = []
        for _, row in df.iterrows():
            pub_time = str(row.get("发布时间", ""))
            if today_str not in pub_time:
                continue
            title = str(row.get("标题", "")).strip()
            if not title:
                continue
            items.append([title, pub_time])
    except Exception as e:
        return {"ok": False, "error": f"AKShare 降级失败: {e}"}

    count = _persist_news(items, store)
    return {"ok": True, "imported": count, "date": today_str, "source": "akshare_eastmoney"}


def _persist_news(items: list, store: MongoFactorDataStore) -> int:
    """去重写入 market_news 集合，返回实际写入数。"""
    seen = set()
    count = 0
    for item in items:
        title = str(item[0]).strip() if item[0] else ""
        if not title or title in seen:
            continue
        seen.add(title)
        doc = {
            "title": title,
            "pub_time": str(item[1]) if len(item) > 1 and item[1] else "",
            "source": "华尔街见闻" if len(item) == 2 else str(item[2]) if len(item) > 2 else "",
            "fetched_date": datetime.now().strftime("%Y%m%d"),
            "updated_at": datetime.now(),
        }
        store.db["market_news"].update_one(
            {"title": title, "fetched_date": doc["fetched_date"]},
            {"$set": doc}, upsert=True)
        count += 1
    return count


# ================================================================
# 龙虎榜导入 — top_list（2000积分）
# ================================================================

def import_top_list(store: MongoFactorDataStore) -> Dict[str, Any]:
    """导入最近5个交易日的龙虎榜数据，存入 top_list 集合。"""
    import requests

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
    if not token:
        return {"ok": False, "error": "缺少 Tushare token"}

    total = 0
    for i in range(5):
        dt = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            resp = requests.post(
                "https://api.tushare.pro",
                json={
                    "api_name": "top_list",
                    "token": token,
                    "params": {"trade_date": dt},
                    "fields": "trade_date,ts_code,name,close,pct_change,l_buy,l_sell,net_amount,reason",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                continue
            items = data["data"]["items"]
            for item in items:
                doc = {
                    "trade_date": item[0],
                    "ts_code": item[1],
                    "name": item[2],
                    "close": float(item[3]) if item[3] else 0,
                    "pct_change": float(item[4]) if item[4] else 0,
                    "l_buy": float(item[5]) if item[5] else 0,
                    "l_sell": float(item[6]) if item[6] else 0,
                    "net_amount": float(item[7]) if item[7] else 0,
                    "reason": item[8] if len(item) > 8 and item[8] else "",
                    "updated_at": datetime.now(),
                }
                store.db["top_list"].update_one(
                    {"ts_code": doc["ts_code"], "trade_date": doc["trade_date"]},
                    {"$set": doc}, upsert=True)
                total += 1
            import time; time.sleep(0.3)
        except Exception:
            pass

    return {"ok": True, "imported": total}


# ================================================================
# 全市场轻量基础导入 — 代码+名称+价格+涨跌幅+成交额（Sina全量）
# ============================================================

def _a_board(code: str) -> str:
    if code.startswith("688"):
        return "科创板"
    if code.startswith("300"):
        return "创业板"
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "主板"
    if code.startswith(("8", "4")):
        return "北交所"
    return "A股"


def import_universe_light(store: MongoFactorDataStore) -> Dict[str, Any]:
    """导入全市场A股基础：代码+名称+价格+涨跌幅+成交量+申万行业编码。

    Sina 全量行情 + 申万股票→行业编码映射，1-2次 API 调用。
    """
    import akshare as ak
    import re

    print("获取全市场 A 股行情 (Sina 源)...")
    try:
        df = ak.stock_zh_a_spot()
    except Exception as e:
        return {"ok": False, "error": f"获取行情失败: {e}"}
    if df is None or df.empty:
        return {"ok": False, "error": "返回空数据"}

    # 申万行业编码映射（通过 tushare stock_basic）
    print("获取申万行业编码映射...")
    sw_map = _get_sw_industry_map()
    if sw_map:
        print(f"  申万覆盖: {len(sw_map)} 只")
    else:
        print(f"  申万映射获取失败，行业字段将留空")

    updated = 0
    failed = 0
    now = datetime.now(timezone.utc)

    for _, row in df.iterrows():
        raw_code = str(row.get("代码", ""))
        digits = "".join(re.findall(r"\d", raw_code))
        code = digits[-6:] if len(digits) >= 6 else digits.zfill(6)
        name = str(row.get("名称", "")).replace(" ", "")
        if not code or code == "000000":
            failed += 1
            continue
        try:
            industry_code = sw_map.get(code, "")
            doc = {
                "code": code,
                "symbol": code,
                "name": name,
                "market": _a_board(code),
                "display_market": "A股",
                "market_info": {"market": "CN"},
                "currency": "CNY",
                "asset_type": "stock",
                "close": _safe_float(row.get("最新价")),
                "pct_chg": _safe_float(row.get("涨跌幅"), None),
                "latest_amount": _safe_float(row.get("成交额"), None),
                "turnover_rate": _safe_float(row.get("换手率"), None),
                "industry_code": industry_code if industry_code else None,
                "industry": industry_code if industry_code else None,
                "sync_source": "universe_light_sina",
                "data_version": "1.0",
                "updated_at": now,
            }
            store.upsert_basic(doc)
            updated += 1
        except Exception:
            failed += 1

    ind_count = sum(1 for v in sw_map.values() if v in set(
        d.get("industry", "") for d in [{}]
    )) if sw_map else 0
    print(f"全市场轻量导入完成: {updated} 只, 失败 {failed}, 含行业编码 {len(sw_map)} 只")
    return {"ok": True, "updated": updated, "failed": failed, "with_industry": len(sw_map)}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "" or value == "--":
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


# ============================================================
# 全市场重基础信息导入（PE/PB/市值 — 走 AKShare Spot，不稳定）
# ============================================================

# ============================================================
# 全量导入
# ============================================================

def data_check(store: MongoFactorDataStore) -> Dict[str, Any]:
    """数据健康检查 — 检查各数据集新鲜度。"""
    from collections import Counter

    result = {"ok": True, "checks": [], "issues": [], "notes": []}
    now = _utc_now()

    # 1. A股日线
    qc = store.db[store.collections["daily_quotes"]]
    a_dates = Counter({
        row["_id"]: row["count"]
        for row in qc.aggregate([
            {"$match": {"period": "daily", "data_source": store.quote_source}},
            {"$group": {"_id": "$trade_date", "count": {"$sum": 1}}},
            {"$sort": {"_id": -1}},
            {"$limit": 120},
        ], allowDiskUse=True)
    })
    a_latest = max(a_dates.keys()) if a_dates else "无"
    a_age = (_utc_now() - datetime.strptime(str(a_latest), "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
    a_status = "✅" if a_age <= 2 else ("⚠️" if a_age <= 4 else "🔴")
    result["checks"].append({
        "name": "A股日线", "latest": a_latest, "count": a_dates.get(a_latest, 0),
        "age_days": a_age, "status": a_status,
    })
    if a_age > 2:
        result["issues"].append(f"A股日线 {a_age} 天未更新（最新 {a_latest}）")
    else:
        recent_dates = sorted(a_dates.keys())[-60:]

        pool = _get_pool_filters()
        pool_query = _quote_pool_query(pool)
        pool_codes = {
            str(doc.get("code") or "").zfill(6)
            for doc in store.db[store.collections["basic_info"]].find(
                pool_query,
                {"code": 1, "_id": 0},
            )
            if doc.get("code")
        }
        universe_codes = {
            str(doc.get("code") or "").zfill(6)
            for doc in store.db[store.collections["basic_info"]].find(
                _a_share_universe_query(),
                {"code": 1, "_id": 0},
            )
            if doc.get("code")
        }

        pool_check = _summarize_quote_coverage(
            store, qc, recent_dates, pool_codes, a_latest, "策略池"
        )
        universe_check = _summarize_quote_coverage(
            store, qc, recent_dates, universe_codes, a_latest, "全A股"
        )
        if pool_check:
            result["checks"].append(pool_check)
            summary = pool_check["coverage_summary"]
            if summary.get("late_start_count"):
                result["notes"].append(
                    "策略池 A股日线存在历史起点较晚股票，通常更像新近纳入股票池或源端历史可得性问题"
                )
            if summary.get("stale_tail_count"):
                result["notes"].append(
                    "策略池 A股日线存在尾部停更股票，通常更像停牌、长期异常或源端尾部缺失"
                )
            if summary.get("latest_only_count"):
                result["issues"].append(
                    f"策略池 A股日线最新交易日仍有 {summary['latest_only_count']} 只单日缺口"
                    f"（如 {', '.join(i['code'] for i in summary['latest_only_sample'][:5])}）"
                )
            if summary.get("sporadic_count"):
                result["issues"].append(
                    f"策略池 A股日线存在 {summary['sporadic_count']} 只零星断点股票"
                    f"（如 {', '.join(i['code'] for i in summary['sporadic_sample'][:5])}）"
                )
        if universe_check:
            result["checks"].append(universe_check)
            u = universe_check["coverage_summary"]
            if u["latest_missing"] > len(pool_codes):
                result["notes"].append(
                    "全A股覆盖明显低于基础信息股票总数，说明当前行情层仍是策略池优先，并非全市场全量入库"
                )

    # 2. 港股日线（持仓）
    hk_codes = [p["code"] for p in _load_portfolio_hk_stocks()]
    for code in hk_codes:
        quotes = store.get_recent_quotes(code, 3, market="港股")
        if quotes:
            latest = quotes[0].get("trade_date", "?")
            latest_dt = _parse_trade_date_flexible(latest)
            age = (_utc_now() - latest_dt).days if latest_dt else "?"
            status = "✅" if isinstance(age, int) and age <= 2 else ("⚠️" if isinstance(age, int) and age <= 4 else "🔴")
            result["checks"].append({
                "name": f"港股日线({code})", "latest": latest, "age_days": age, "status": status,
            })
            if isinstance(age, int) and age > 2:
                result["issues"].append(f"港股{code}日线 {age} 天未更新（最新 {latest}）")
        else:
            result["checks"].append({
                "name": f"港股日线({code})", "latest": "无", "age_days": "?", "status": "🔴",
            })
            result["issues"].append(f"港股{code}日线: 无数据")

        sb_doc = _latest_doc_by_trade_date(
            store.db["stock_southbound"],
            {"code": code},
            {"trade_date": 1, "_id": 0},
        )
        if sb_doc and sb_doc.get("trade_date"):
            sb_latest = sb_doc["trade_date"]
            sb_dt = _parse_trade_date_flexible(sb_latest)
            sb_age = (_utc_now() - sb_dt).days if sb_dt else "?"
            sb_status = "✅" if isinstance(sb_age, int) and sb_age <= 2 else ("⚠️" if isinstance(sb_age, int) and sb_age <= 4 else "🔴")
            result["checks"].append({
                "name": f"港股南向({code})", "latest": sb_latest, "age_days": sb_age, "status": sb_status,
            })
            if isinstance(sb_age, int) and sb_age > 2:
                result["issues"].append(f"港股{code}南向数据 {sb_age} 天未更新（最新 {sb_latest}）")
        else:
            result["checks"].append({
                "name": f"港股南向({code})", "latest": "无", "age_days": "?", "status": "⚠️",
            })

        ss_doc = _latest_doc_by_trade_date(
            store.db["stock_shortsell"],
            {"code": code},
            {"trade_date": 1, "period": 1, "_id": 0},
        )
        if ss_doc and ss_doc.get("trade_date"):
            ss_latest = ss_doc["trade_date"]
            ss_dt = _parse_trade_date_flexible(ss_latest)
            ss_age = (_utc_now() - ss_dt).days if ss_dt else "?"
            ss_status = "✅" if isinstance(ss_age, int) and ss_age <= 2 else ("⚠️" if isinstance(ss_age, int) and ss_age <= 4 else "🔴")
            label = f"{ss_latest} ({ss_doc.get('period', '?')})"
            result["checks"].append({
                "name": f"港股做空({code})", "latest": label, "age_days": ss_age, "status": ss_status,
            })
            if isinstance(ss_age, int) and ss_age > 2:
                result["issues"].append(f"港股{code}做空数据 {ss_age} 天未更新（最新 {label}）")
        else:
            result["checks"].append({
                "name": f"港股做空({code})", "latest": "无", "age_days": "?", "status": "⚠️",
            })

    # 3. 新架构预计算缓存：趋势 + 因子
    for coll_name, label in [("stock_trends", "趋势缓存"), ("stock_factors", "因子缓存")]:
        coll = store.db[coll_name]
        dates = Counter()
        for d in coll.find({}, {"trade_date": 1}).sort("trade_date", -1).limit(5000):
            if d.get("trade_date"):
                dates[d["trade_date"]] += 1
        latest = max(dates.keys()) if dates else "无"
        if latest == "无":
            result["checks"].append({
                "name": label, "latest": latest, "count": 0,
                "age_days": "?", "status": "🔴",
            })
            result["issues"].append(f"{label}: 无数据")
            continue
        age = (_utc_now() - datetime.strptime(str(latest), "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
        status = "✅" if age <= 2 else ("⚠️" if age <= 4 else "🔴")
        result["checks"].append({
            "name": label, "latest": latest, "count": dates.get(latest, 0),
            "age_days": age, "status": status,
        })
        if age > 2:
            result["issues"].append(f"{label} {age} 天未更新")

    # 4. 全市场资金流
    mf = store.db["market_moneyflow"]
    mf_latest_doc = list(mf.find({}, {"trade_date": 1}).sort("trade_date", -1).limit(1))
    if mf_latest_doc:
        mf_latest = mf_latest_doc[0]["trade_date"]
        mf_age = (_utc_now() - datetime.strptime(str(mf_latest), "%Y%m%d").replace(tzinfo=timezone.utc)).days
        mf_status = "✅" if mf_age <= 2 else ("⚠️" if mf_age <= 4 else "🔴")
        result["checks"].append({
            "name": "全市场资金流", "latest": mf_latest, "age_days": mf_age, "status": mf_status,
        })
        if mf_age > 2:
            result["issues"].append(
                f"全市场资金流 {mf_age} 天未更新（运行 import-market-moneyflow；"
                "Tushare无权限时会尝试AKShare/Eastmoney兜底）"
            )
    else:
        result["checks"].append({
            "name": "全市场资金流", "latest": "无", "age_days": "?", "status": "⚠️",
        })

    # 5. 行业分类覆盖
    total = store.db[store.collections["basic_info"]].count_documents({"display_market": "A股"})
    has_ind = store.db[store.collections["basic_info"]].count_documents(
        {"display_market": "A股", "industry": {"$ne": None, "$ne": ""}})
    ind_pct = round(has_ind / total * 100, 1) if total > 0 else 0
    ind_status = "✅" if ind_pct >= 95 else "⚠️"
    result["checks"].append({
        "name": "行业分类覆盖", "latest": f"{has_ind}/{total} ({ind_pct}%)", "age_days": "—", "status": ind_status,
    })
    if ind_pct < 95:
        result["issues"].append(f"行业分类覆盖仅 {ind_pct}%，建议运行 import-universe-light")

    result["ok"] = len(result["issues"]) == 0
    return result


def import_hsgt_flow(store: MongoFactorDataStore) -> Dict[str, Any]:
    """从东方财富 RPT_MUTUAL_QUOTA 抓取当日北向/南向成交净买额，存入 stock_hsgt_flow。

    注意：北向数据在 A 股15:00收盘后清零，必须在收盘前（建议14:55）执行。
    南向数据在港股16:00收盘前均可获取。
    """
    import requests as req

    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_MUTUAL_QUOTA",
        "columns": "TRADE_DATE,MUTUAL_TYPE,BOARD_TYPE,MUTUAL_TYPE_NAME,FUNDS_DIRECTION,INDEX_CODE,INDEX_NAME,BOARD_CODE",
        "quoteColumns": "netBuyAmt~07~BOARD_CODE,status~07~BOARD_CODE",
        "quoteType": "0",
        "pageNumber": "1",
        "pageSize": "200",
        "sortTypes": "1",
        "sortColumns": "MUTUAL_TYPE",
        "source": "WEB",
        "client": "WEB",
    }

    try:
        r = req.get(url, params=params, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://data.eastmoney.com/hsgt/index.html",
        }, timeout=15)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"东方财富API请求失败: {e}"}

    if not data.get("success") or not data.get("result"):
        return {"ok": False, "error": f"API返回失败: {data.get('message', 'unknown')}"}

    rows = data["result"]["data"]
    if not rows:
        return {"ok": False, "error": "API返回空数据"}

    trade_date = rows[0].get("TRADE_DATE", "")[:10]
    coll = store.db[store.collections.get("hsgt_flow", "stock_hsgt_flow")]
    saved = 0
    skipped_north_closed = 0

    for row in rows:
        direction = row.get("FUNDS_DIRECTION", "")
        board = row.get("BOARD_TYPE", "")
        mutual_type = row.get("MUTUAL_TYPE", "")
        status = row.get("status")
        net_buy_raw = row.get("netBuyAmt")

        # 北向收盘后(status=3 或 None)数据不可用，跳过
        if direction == "北向" and (status is None or status == 3):
            skipped_north_closed += 1
            continue

        if net_buy_raw is None:
            continue

        # 万元 → 亿
        net_buy_amt = round(float(net_buy_raw) / 10000, 4)

        doc = {
            "trade_date": trade_date,
            "mutual_type": mutual_type,
            "board_type": board,
            "direction": direction,
            "net_buy_amt": net_buy_amt,  # 亿
            "status": status,
            "updated_at": _utc_now().isoformat(),
        }

        coll.update_one(
            {"trade_date": trade_date, "mutual_type": mutual_type},
            {"$set": doc},
            upsert=True,
        )
        saved += 1

    # 确保有日期索引
    coll.create_index([("trade_date", 1), ("mutual_type", 1)], unique=True)

    return {
        "ok": True,
        "trade_date": trade_date,
        "saved": saved,
        "skipped_north_closed": skipped_north_closed,
    }


def import_all(config_path: str, portfolio_path: str) -> Dict[str, Any]:
    """全量数据导入（周日跑）。"""
    mongodb_config = _load_mongodb_config(config_path)
    store = MongoFactorDataStore(mongodb_config)
    results = {}

    # 1. A股全市场代码+名称（轻量）
    print("\n" + "=" * 60)
    print("1/5 全市场 A股 基础信息（轻量）")
    print("=" * 60)
    results["universe_light"] = import_universe_light(store)

    # 2. 持仓+观察池 K线+财务
    print("\n" + "=" * 60)
    print("2/5 持仓+观察池 K线 & 财务")
    print("=" * 60)
    results["portfolio"] = import_portfolio(portfolio_path, store)

    # 3. 港股南向持仓
    print("\n" + "=" * 60)
    print("3/5 港股南向持仓")
    print("=" * 60)
    results["hk_southbound"] = import_hk_southbound(store, portfolio_path)

    # 4. A股股息率
    print("\n" + "=" * 60)
    print("4/5 A股股息率")
    print("=" * 60)
    results["dividend_yield"] = import_dividend_yield(store, portfolio_path)

    # 5. 行业资金流向
    print("\n" + "=" * 60)
    print("5/6 行业资金流向")
    print("=" * 60)
    results["industry_moneyflow"] = import_industry_moneyflow(store)

    # 6. 北向南向净买额（东方财富，需在收盘前运行才能拿到北向数据）
    print("\n" + "=" * 60)
    print("6/6 沪深港通净买额")
    print("=" * 60)
    results["hsgt_flow"] = import_hsgt_flow(store)

    # 汇总
    print("\n" + "=" * 60)
    print("全量导入完成")
    for key, r in results.items():
        status = "✓" if r.get("ok") else "✗"
        print(f"  {status} {key}: {r}")
    return results


def import_sentiment_cache(portfolio_path: str, config_path: str) -> Dict[str, Any]:
    """预缓存持仓+观察池的新闻情绪分析结果到 MongoDB。

    用法:
        python3 data_import_pipeline.py import-sentiment

    流程：
        1. 读取 portfolio.yaml 获取所有持仓和观察池股票
        2. 逐只拉取新闻（最多10条）
        3. 合并所有新闻，批量 LLM 情绪分析
        4. 结果自动持久化到 MongoDB sentiment_cache（7天TTL）
        5. portfolio review 和 buy_plan 下次运行时直接命中缓存，不调 LLM
    """
    from event_driven_strategy import NewsDataProvider, NewsSentimentAnalyzer

    positions = _load_portfolio_positions(portfolio_path, include_watchlist=True)
    if not positions:
        return {"ok": False, "error": "无持仓数据"}

    news_provider = NewsDataProvider()
    sentiment_analyzer = NewsSentimentAnalyzer(config_path)

    print(f"预缓存 {len(positions)} 只股票的新闻情绪...")

    all_texts: list = []
    stock_text_map: dict = {}  # code -> [text indices]

    for pos in positions:
        code = str(pos.get("code", "")).strip()
        name = pos.get("name", code)
        market = pos.get("market", "A股")
        if pos.get("asset_type") == "etf":
            continue
        try:
            news_list = news_provider.get_stock_news(code, limit=10)
        except Exception:
            continue
        if not news_list:
            continue

        indices = []
        for item in news_list:
            text = f"{item.get('title', '')} {item.get('content', '')}"
            if text.strip():
                indices.append(len(all_texts))
                all_texts.append(text)
        if indices:
            stock_text_map[code] = {"name": name, "indices": indices, "count": len(indices)}
            print(f"  {code} {name}: {len(indices)} 条新闻")

    if not all_texts:
        return {"ok": True, "cached": 0, "message": "无新闻可缓存"}

    print(f"\n批量情绪分析 {len(all_texts)} 条新闻...")
    t0 = time.time()
    results = sentiment_analyzer.analyze_sentiment_batch(all_texts)
    elapsed = time.time() - t0

    llm_calls = sum(1 for r in results if r.get("method") == "llm")
    cached = sum(1 for r in results if r.get("method") in ("llm_cached",))
    print(f"完成: {len(results)} 条, LLM新分析 {llm_calls} 条, 缓存命中 {cached} 条, 耗时 {elapsed:.1f}s")

    # 汇总各股票情绪（预缓存任务用简单等权平均，查询时用时间衰减）
    stock_summary = {}
    for code, info in stock_text_map.items():
        scores = [float(results[i].get("score", 0)) for i in info["indices"]]
        avg = sum(scores) / len(scores) if scores else 0
        stock_summary[code] = {
            "name": info["name"],
            "news_count": info["count"],
            "avg_sentiment": round(avg, 3),
        }

    print("\n各股票情绪汇总:")
    for code, s in stock_summary.items():
        label = "正面" if s["avg_sentiment"] > 0.15 else ("负面" if s["avg_sentiment"] < -0.15 else "中性")
        print(f"  {code} {s['name']}: {s['avg_sentiment']} ({label}, {s['news_count']}条)")

    return {"ok": True, "total_texts": len(all_texts), "llm_calls": llm_calls, "cached_hits": cached,
            "stocks": list(stock_summary.keys()), "elapsed_seconds": round(elapsed, 1)}


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="统一数据导入管道")
    parser.add_argument("command", choices=[
        "import-portfolio", "import-stocks", "import-universe-light",
        "import-quotes", "import-hk-shortsell",
        "import-hk-southbound", "import-dividend-yield", "repair-pe",
        "import-industry-moneyflow", "import-market-moneyflow", "import-macro-news",
        "import-top-list", "import-stock-moneyflow", "import-stock-forecast",
        "import-hsgt-flow", "import-sentiment", "import-all", "data-check",
        "config-check", "repair-quote-schema",
    ])
    parser.add_argument("--codes", default="", help="股票代码，逗号分隔（import-stocks 使用）")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "config", "config_complete.yaml"))
    parser.add_argument("--portfolio", default=os.path.join(PROJECT_ROOT, "data", "portfolio.yaml"))
    parser.add_argument("--market", default="A股")
    parser.add_argument("--quote-limit", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=0.6)
    parser.add_argument("--start-date", default=None,
                        help="import-quotes 起始日期 YYYY-MM-DD 或 YYYYMMDD")
    parser.add_argument("--end-date", default=None,
                        help="import-quotes 结束日期 YYYY-MM-DD 或 YYYYMMDD")
    parser.add_argument("--rate-limit-per-minute", type=int, default=160,
                        help="import-quotes 每分钟最大 Tushare 调用数，默认 160")
    parser.add_argument("--repair-missing", action="store_true",
                        help="import-quotes 先扫描 start/end 区间缺口，再只补缺口股票")
    parser.add_argument("--scope", choices=["pool", "universe"], default="pool",
                        help="import-quotes 覆盖范围：pool=策略池，universe=全A股")
    args = parser.parse_args()

    if args.command == "config-check":
        try:
            validate_system_config(_load_yaml(args.config), args.config)
        except ConfigError as exc:
            print(f"CONFIG_ERROR: {exc}")
            raise SystemExit(2)
        print(f"CONFIG_OK: {args.config}")
        return

    mongodb_config = _load_mongodb_config(args.config)
    store = MongoFactorDataStore(mongodb_config)

    if args.command == "repair-quote-schema":
        print(store.repair_daily_quote_schema())

    elif args.command == "import-portfolio":
        import_portfolio(args.portfolio, store, quote_limit=args.quote_limit or 180, sleep_seconds=args.sleep)

    elif args.command == "import-stocks":
        if not args.codes:
            print("错误: import-stocks 需要 --codes 参数 (逗号分隔，如 600941,000001)")
            return
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        import_stocks(codes, store, quote_limit=args.quote_limit or 180, sleep_seconds=args.sleep)

    elif args.command == "import-universe-light":
        import_universe_light(store)

    elif args.command == "import-quotes":
        result = import_quotes(
            store,
            limit=args.quote_limit or 5000,
            start_date=args.start_date,
            end_date=args.end_date,
            sleep_seconds=args.sleep,
            rate_limit_per_minute=args.rate_limit_per_minute,
            repair_missing=args.repair_missing,
            scope=args.scope,
        )
        print(result)

    elif args.command == "import-hk-shortsell":
        import_hk_shortsell(store)

    elif args.command == "import-market-moneyflow":
        result = import_market_moneyflow(store)
        print(result)

    elif args.command == "import-macro-news":
        result = import_macro_news(store)
        print(result)

    elif args.command == "import-top-list":
        result = import_top_list(store)
        print(result)

    elif args.command == "import-hk-southbound":
        import_hk_southbound(store, args.portfolio)

    elif args.command == "import-dividend-yield":
        import_dividend_yield(store, args.portfolio)

    elif args.command == "repair-pe":
        repair_pe(store)

    elif args.command == "import-industry-moneyflow":
        result = import_industry_moneyflow(store)
        print(result)

    elif args.command == "import-sentiment":
        import_sentiment_cache(args.portfolio, args.config)

    elif args.command == "import-all":
        import_all(args.config, args.portfolio)

    elif args.command == "import-stock-moneyflow":
        limit = args.quote_limit or 3000
        result = import_stock_moneyflow(store, limit=limit)
        print(result)

    elif args.command == "import-stock-forecast":
        limit = args.quote_limit or 3000
        result = import_stock_forecast(store, limit=limit)
        print(result)

    elif args.command == "import-hsgt-flow":
        result = import_hsgt_flow(store)
        print(result)

    elif args.command == "data-check":
        result = data_check(store)
        print("=" * 50)
        print("数据健康检查")
        print("=" * 50)
        for c in result["checks"]:
            print(f"  {c['status']} {c['name']}: {c['latest']} ({c['age_days']}天前, {c.get('count', '?')}条)")
            summary = c.get("coverage_summary")
            if summary:
                if summary.get("late_start_sample"):
                    sample = ", ".join(
                        f"{item['code']}({item['first_seen']})"
                        for item in summary["late_start_sample"]
                    )
                    print(f"      历史起点晚 sample: {sample}")
                if summary.get("stale_tail_sample"):
                    sample = ", ".join(
                        f"{item['code']}({item['last_seen']})"
                        for item in summary["stale_tail_sample"]
                    )
                    print(f"      尾部停更 sample: {sample}")
                if summary.get("latest_only_sample"):
                    sample = ", ".join(
                        f"{item['code']}({item['last_seen']})"
                        for item in summary["latest_only_sample"]
                    )
                    print(f"      最新单日缺口 sample: {sample}")
                if summary.get("sporadic_sample"):
                    sample = ", ".join(
                        f"{item['code']}({item['missing_days']}天)"
                        for item in summary["sporadic_sample"]
                    )
                    print(f"      零星断点 sample: {sample}")
        if result.get("notes"):
            print("\n说明:")
            for note in result["notes"]:
                print(f"  - {note}")
        if result["issues"]:
            print(f"\n⚠️ 发现问题:")
            for i in result["issues"]:
                print(f"  - {i}")
        else:
            print("\n✅ 所有数据正常")
        if not result["ok"]:
            print(
                "\n建议: A股日线运行 python3 scripts/data_import_pipeline.py import-quotes --quote-limit 5000；"
                "港股持仓日线运行 python3 scripts/data_import_pipeline.py import-portfolio；"
                "随后运行 python3 scripts/precompute_history.py --date today"
            )


if __name__ == "__main__":
    main()
