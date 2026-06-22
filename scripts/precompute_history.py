#!/usr/bin/env python3
"""
信号预计算 v3 — 统一实时/历史，trend/factor 拆层，4 套因子并存。

三张表:
  stock_meta    — 日级基础信息（code+trade_date 主键）
  stock_trends  — 趋势指标（RSI/KDJ/MACD/MA，跟阈值无关，极少重算）
  stock_factors — 因子得分（4 组权重各算一份，改阈值后重算）

用法:
  python3 precompute_history.py --date 2026-06-16            # 实时（当天）
  python3 precompute_history.py --date 2026-03-02            # 指定历史日期
  python3 precompute_history.py --start 2026-03-02 --end 2026-06-11  # 批量
  python3 precompute_history.py --date today --trends-only   # 只跑趋势
  python3 precompute_history.py --date today --factors-only  # 只跑因子
  python3 precompute_history.py --date today --meta-only     # 只跑元数据
"""

import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import yaml as _yaml_lib

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config

_UTC = timezone.utc
_CN_TZ = timezone(timedelta(hours=8))
MIN_DAILY_QUOTES_FOR_PRECOMPUTE = 50


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _resolve_today_trade_date(
    store: MongoFactorDataStore,
    min_quotes: int = MIN_DAILY_QUOTES_FOR_PRECOMPUTE,
) -> Optional[str]:
    """Resolve CLI `today` to the latest available trading day in MongoDB.

    Natural today is not always a computable trade date: A shares may be closed,
    the market may not have finished, or quotes may not have been imported yet.
    Use the newest daily quote date no later than China-local today with enough
    coverage, and print the resolution explicitly to avoid silent fallback.
    """
    today_cn = datetime.now(_CN_TZ).strftime("%Y-%m-%d")
    coll = store.db[store.collections["daily_quotes"]]
    pipeline = [
        {"$match": {"period": "daily", "trade_date": {"$lte": today_cn}}},
        {"$group": {"_id": "$trade_date", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gte": min_quotes}}},
        {"$sort": {"_id": -1}},
        {"$limit": 1},
    ]
    rows = list(coll.aggregate(pipeline))
    if not rows:
        print(f"today={today_cn} 没有找到覆盖数 >= {min_quotes} 的可预计算交易日")
        return None

    resolved = rows[0]["_id"]
    count = rows[0]["count"]
    if resolved == today_cn:
        print(f"today={today_cn} 已有 {count} 只日线，按今日交易日预计算")
    else:
        print(f"today={today_cn} 暂无足够日线，回退到最新可用交易日 {resolved} ({count} 只)")
    return resolved


# ================================================================
# 单日计算核心
# ================================================================

def _compute_date(store: MongoFactorDataStore, target_date: str,
                  run_meta: bool = True, run_trends: bool = True,
                  run_factors: bool = True) -> Dict[str, Any]:
    """为单个日期计算信号，写入三张表。"""
    t0 = time.time()
    print(f"  [{target_date}] 开始...", flush=True)

    daily = store.db[store.collections["daily_quotes"]]
    basic = store.db[store.collections["basic_info"]]
    fin_coll = store.db[store.collections["financial_data"]]

    # ── 1. 当日有成交的 A 股 ──
    price_docs = list(daily.find(
        {"trade_date": target_date, "period": "daily"},
        {"code": 1, "close": 1, "volume": 1, "amount": 1, "_id": 0},
    ))
    if len(price_docs) < 50:
        return {"ok": False, "error": f"{target_date} 仅 {len(price_docs)} 只股票有行情，请先运行 data_import_pipeline.py import-quotes"}

    price_map: Dict[str, Dict[str, float]] = {}
    for doc in price_docs:
        code = doc["code"]
        if not (code.startswith("6") or code.startswith("0") or code.startswith("3")):
            continue
        close = _to_float(doc.get("close"), 0)
        if close <= 0:
            continue
        price_map[code] = {
            "close": close,
            "volume": _to_float(doc.get("volume"), 0),
            "amount": _to_float(doc.get("amount"), 0),
        }
    codes = list(price_map.keys())
    if len(codes) < 50:
        return {"ok": False, "error": f"过滤后仅 {len(codes)} 只"}

    # ── 2. 基础信息（仅静态字段）──
    info_map: Dict[str, Dict] = {}
    for doc in basic.find(
        {"code": {"$in": codes}},
        {"code": 1, "name": 1, "industry": 1, "industry_code": 1,
         "market": 1, "display_market": 1, "dividend_yield": 1,
         "pe": 1, "pb": 1, "_id": 0},
    ):
        name = str(doc.get("name", ""))
        if "ST" in name.upper():
            continue
        info_map[doc["code"]] = doc

    # ── 3. 业务组加载 ──
    from business_group_loader import BusinessGroupLoader
    loader = BusinessGroupLoader()
    all_groups = loader.all_group_names()

    # ── 4. 写 stock_meta ──
    meta_count = 0
    if run_meta:
        from market_data_provider import MarketDataProvider
        data = MarketDataProvider(store)
        meta_coll = store.db["stock_meta"]
        for code in codes:
            info = info_map.get(code)
            if not info:
                continue
            doc = {
                "code": code,
                "trade_date": target_date,
                "name": info.get("name", ""),
                "industry": info.get("industry", ""),
                "industry_code": info.get("industry_code", ""),
                "market": info.get("display_market", "A股"),
                "close": price_map[code]["close"],
                "amount": price_map[code]["amount"],
                "volume": price_map[code]["volume"],
                "pe": info.get("pe"),
                "pb": info.get("pb"),
                "dividend_yield": info.get("dividend_yield"),
                "group": loader.get_group(info.get("industry", "")),
                "updated_at": datetime.now(_UTC),
            }
            # 补股息率 TTM + 一致预期
            try:
                dv = store.db["stock_dividend"].find_one({"code": code})
                if dv:
                    doc["dividend_yield"] = dv.get("dv_ttm") or dv.get("dv_ratio") or doc["dividend_yield"]
            except Exception:
                pass
            try:
                fc = store.db["stock_signals"].find_one(
                    {"code": code}, {"forecast_min": 1, "forecast_max": 1})
                if fc:
                    doc["forecast_min"] = fc.get("forecast_min")
                    doc["forecast_max"] = fc.get("forecast_max")
            except Exception:
                pass
            meta_coll.update_one(
                {"code": code, "trade_date": target_date},
                {"$set": doc}, upsert=True)
            meta_count += 1

    # ── 5. 写 stock_trends ──
    trend_count = 0
    if run_trends:
        from market_data_provider import MarketDataProvider
        data = MarketDataProvider(store)
        trend_coll = store.db["stock_trends"]
        for code in codes:
            info = info_map.get(code)
            if not info:
                continue
            try:
                trend = data.get_trend_signal(
                    code, info.get("display_market", "A股"), as_of_date=target_date)
            except Exception:
                trend = {"available": False}

            ti = trend.get("technical_indicators", {}) or {}
            doc = {
                "code": code,
                "trade_date": target_date,
                "available": trend.get("available", False),
                "status": trend.get("status", ""),
                "ma": trend.get("ma", {}),
                "returns": trend.get("returns", {}),
                "volatility_20d": trend.get("volatility_20d"),
                "rsi14": ti.get("rsi14"),
                "kdj": ti.get("kdj", {}),
                "macd": ti.get("macd", {}),
                "boll_percent_b": (ti.get("bollinger") or {}).get("percent_b"),
                "volume_price_signal": ti.get("volume_price_signal"),
            }
            trend_coll.update_one(
                {"code": code, "trade_date": target_date},
                {"$set": doc}, upsert=True)
            trend_count += 1
            if trend_count % 100 == 0:
                print(f"  trend: {trend_count}/{len(codes)}")

    # ── 6. 写 stock_factors（4 组各算一份）──
    factor_count = 0
    if run_factors:
        from market_data_provider import MarketDataProvider
        from pyramid_multifactor_strategy import PyramidMultifactorStrategy

        data = MarketDataProvider(store)
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "config", "config_complete.yaml")
        pyramid = PyramidMultifactorStrategy(config_path)
        factor_coll = store.db["stock_factors"]

        # ── V2 动量：全市场百分位排名 ──
        # 批量加载当日所有趋势数据，算 ret5d/20d/60d 百分位 + MA 多头
        trend_docs_all = {}
        ret5_all, ret20_all, ret60_all = [], [], []
        for tdoc in store.db["stock_trends"].find(
            {"trade_date": target_date, "available": True},
            {"code": 1, "returns": 1, "ma": 1}
        ):
            trend_docs_all[tdoc["code"]] = tdoc
            rets = tdoc.get("returns", {})
            ret5_all.append(rets.get("return_5d", 0) or 0)
            ret20_all.append(rets.get("return_20d", 0) or 0)
            ret60_all.append(rets.get("return_60d", 0) or 0)

        ret5_arr = sorted(ret5_all)
        ret20_arr = sorted(ret20_all)
        ret60_arr = sorted(ret60_all)
        n_all = len(ret5_arr)

        def _pct_rank(val, arr):
            """值在已排序数组中的百分位 [0, 1]"""
            if not arr: return 0.5
            import bisect
            return bisect.bisect_left(arr, val) / len(arr)

        # ── 行业周期得分（日级缓存，该日期所有股票共享）──
        cycle_cache = {}
        turnaround_cache = {}
        try:
            from sector_radar import SectorRadarEngine
            radar = SectorRadarEngine()
            cycle_cache = radar.get_cycle_scores(target_date=target_date)
            inf_map = radar.get_inflection_map(target_date=target_date)
            # 行业拐点分：inflection hits / 3
            for ind, v in inf_map.items():
                turnaround_cache[ind] = v.get("hits", 0) / 3
        except Exception:
            pass

        # ── PE 分行业百分位（估值分）──
        ind_pe_map = {}  # industry → [pe values]
        for code in codes:
            info = info_map.get(code)
            if not info: continue
            ind = info.get("industry", "")
            pe_val = None
            fin = fin_coll.find_one({"code": code, "estimated_disclosure": {"$lte": target_date}},
                                    sort=[("report_period", -1)])
            if fin:
                eps = fin.get("eps")
                if eps and eps > 0:
                    pe_val = price_map[code]["close"] / eps
            if pe_val and pe_val > 0 and pe_val < 500:
                if ind not in ind_pe_map:
                    ind_pe_map[ind] = []
                ind_pe_map[ind].append(pe_val)

        # 每个行业的 PE 排序数组
        ind_pe_sorted = {ind: sorted(vals) for ind, vals in ind_pe_map.items()}

        # turnaround 权重（从 config 读取一次）
        try:
            with open(config_path) as _f:
                _cfg = _yaml_lib.safe_load(_f)
            ta_cfg = _cfg.get("pyramid_middle_layer", {}).get("turnaround", {})
        except Exception:
            ta_cfg = {}
        ta_w = ta_cfg.get("weights", {"valuation": 0.25, "price_recovery": 0.45, "sector_inflection": 0.30})
        ta_w_fc = ta_cfg.get("forecast_weight", {"valuation": 0.25, "forecast": 0.30,
                                                   "price_recovery": 0.25, "sector_inflection": 0.20})

        for code in codes:
            info = info_map.get(code)
            if not info:
                continue
            industry = info.get("industry", "")
            market = info.get("display_market", "A股")
            name = info.get("name", code)
            close = price_map[code]["close"]

            # 历史 PE/PB（从财报反算）
            pe = None
            pb = None
            fin = fin_coll.find_one(
                {"code": code, "estimated_disclosure": {"$lte": target_date}},
                sort=[("report_period", -1)],
            )
            fin_eps = fin.get("eps") if fin else None
            fin_bps = fin.get("bps") if fin else None
            if fin_eps and fin_eps > 0:
                pe = round(close / fin_eps, 2)
            if fin_bps and fin_bps > 0:
                pb = round(close / fin_bps, 2)

            # 趋势缓存（如果有的话，加速因子计算）
            try:
                quotes = store.get_recent_quotes(code, 300, as_of_date=target_date)
            except Exception:
                quotes = []

            # 4 组各算一份
            factors = {}
            for group_name in all_groups:
                w = loader.get_weights(group_name)
                s = loader.get_scoring(group_name)
                if not w:
                    continue
                try:
                    context = {"industry": industry, "market": market}
                    stock_dict = {
                        "code": code, "name": name,
                        "pe": pe, "pb": pb,
                        "dividend_yield": info.get("dividend_yield"),
                        "industry_code": info.get("industry_code"),
                        "industry": industry, "market": market,
                    }
                    factor = pyramid.calculate_composite_score(
                        stock_dict, quotes, fin, context=context,
                        override_weights=w, override_scoring=s)
                    factors[group_name] = {
                        "composite_score": factor.get("composite_score", 0),
                        "factor_scores": factor.get("factor_scores", {}),
                    }
                except Exception:
                    factors[group_name] = {
                        "composite_score": 0,
                        "factor_scores": {},
                    }

            # ── V2 动量：百分位排名 + MA结构，替换各组的旧 momentum 分 ──
            tdoc = trend_docs_all.get(code)
            if tdoc:
                rets = tdoc.get("returns", {})
                ma = tdoc.get("ma", {})
                r5 = rets.get("return_5d", 0) or 0
                r20 = rets.get("return_20d", 0) or 0
                r60 = rets.get("return_60d", 0) or 0
                p5  = _pct_rank(r5, ret5_arr)
                p20 = _pct_rank(r20, ret20_arr)
                p60 = _pct_rank(r60, ret60_arr)
                ma_bull = 1 if (ma.get("ma5",0) or 0) > (ma.get("ma20",0) or 0) > (ma.get("ma60",0) or 0) else 0
                v2 = (0.4*p60*100 + 0.4*p20*100 + 0.2*p5*100 + 5*ma_bull) / 105  # 归一化 0~1
            else:
                v2 = 0.5

            for g in factors:
                if "factor_scores" in factors[g]:
                    factors[g]["factor_scores"]["momentum"] = round(v2, 3)

            # ── turnaround_score（估值+盈利+价格+拐点）──
            ta_score = 0.5  # 默认
            tdoc2 = trend_docs_all.get(code)
            if tdoc2 and info_map.get(code):
                ind = info_map[code].get("industry", "")
                # ① 估值分 V3：行业内PE排名 + 自身历史PE分位（双重交叉）
                pe_vals_sorted = ind_pe_sorted.get(ind, [])
                pe_val = round(price_map[code]["close"] / fin_eps, 2) if fin_eps and fin_eps > 0 else None
                if pe_val and pe_val > 0 and pe_vals_sorted:
                    cross_val = 1.0 - _pct_rank(pe_val, pe_vals_sorted)  # 行业内排名
                else:
                    cross_val = 0.5
                # 自身PE历史分位近似：用60日收益代表估值变化方向
                rets = tdoc2.get("returns", {})
                r60 = rets.get("return_60d", 0) or 0
                hist_val = _pct_rank(r60, ret60_arr)  # 跌得多 = 估值压缩多 = 便宜
                val_score = 0.5 * cross_val + 0.5 * hist_val

                # ② 价格企稳 V3：改善速度 + 成交量确认
                r5  = rets.get("return_5d", 0) or 0
                r20 = rets.get("return_20d", 0) or 0
                # 改善速度：ret20 - ret60 的百分位（正=加速改善）
                improve_speed_arr = [ret20_arr[i] - ret60_arr[i] for i in range(len(ret20_arr))]
                improve = r20 - r60
                speed_score = _pct_rank(improve, improve_speed_arr)
                # MA趋势改善：ret20 百分位
                ma_fix = _pct_rank(r20, ret20_arr)
                # 成交量确认：量比（当期量/20日均量）百分位
                vol_today = price_map.get(code, {}).get("volume", 0)
                vol_avg = 0
                quote_query = {"code": code, "trade_date": {"$lte": target_date}, "data_source": store.quote_source}
                quotes = store.db[store.collections["daily_quotes"]].find(
                    quote_query,
                    {"volume": 1}
                ).sort("trade_date", -1).limit(20)
                vol_list = [q.get("volume", 0) for q in quotes]
                if len(vol_list) >= 5:
                    vol_avg = sum(vol_list[1:]) / (len(vol_list) - 1) if len(vol_list) > 1 else vol_list[0]
                vol_ratio = vol_today / vol_avg if vol_avg > 0 else 1.0
                # 量比百分位（全市场）
                vol_ratios_all = []
                for c2 in codes[:200]:  # 采样200只算分布
                    sample_query = {"code": c2, "trade_date": {"$lte": target_date}, "data_source": store.quote_source}
                    qq = list(store.db[store.collections["daily_quotes"]].find(
                        sample_query,
                        {"volume": 1}
                    ).sort("trade_date", -1).limit(20))
                    if len(qq) >= 5:
                        va = sum(q.get("volume", 0) for q in qq[1:]) / (len(qq)-1)
                        vt = qq[0].get("volume", 0)
                        vol_ratios_all.append(vt / va if va > 0 else 1.0)
                vol_score = _pct_rank(vol_ratio, sorted(vol_ratios_all)) if vol_ratios_all else 0.5
                recovery = min(1.0, 0.40 * speed_score + 0.30 * ma_fix + 0.30 * vol_score)

                # ③ 行业拐点 V3：连续分（不用离散档位）
                import bisect
                turn_scores_all = sorted(turnaround_cache.values())
                turn_score = _pct_rank(turnaround_cache.get(ind, 0.33), turn_scores_all)

                # ④ 盈利修正分（forecast 就绪后启用）
                fc_score = 0.0
                has_forecast = False
                info_meta = info_map.get(code, {})
                fc_min = info_meta.get("forecast_min")
                if fc_min is not None:
                    has_forecast = True
                    fc_score = min(1.0, max(0, fc_min / 30))

                # ⑤ 合成（权重从 config 预加载）
                if has_forecast:
                    ta_score = round(ta_w_fc["valuation"] * val_score + ta_w_fc["forecast"] * fc_score
                                     + ta_w_fc["price_recovery"] * recovery + ta_w_fc["sector_inflection"] * turn_score, 3)
                else:
                    ta_score = round(ta_w["valuation"] * val_score + ta_w["price_recovery"] * recovery
                                     + ta_w["sector_inflection"] * turn_score, 3)

            # ── 行业周期得分 ──
            cs = cycle_cache.get(industry, {})
            doc = {
                "code": code,
                "trade_date": target_date,
                "group": loader.get_group(industry),
                "pe": pe,
                "pb": pb,
                "fin_report_period": fin.get("report_period") if fin else None,
                "factors": factors,
                "industry_trend": cs.get("trend", 0.5),
                "industry_breadth": cs.get("breadth", 0.5),
                "industry_flow": cs.get("flow", 0.5),
                "cycle_score": cs.get("cycle", 0.5),
                "turnaround_score": ta_score,
            }
            factor_coll.update_one(
                {"code": code, "trade_date": target_date},
                {"$set": doc}, upsert=True)
            factor_count += 1
            if factor_count % 100 == 0:
                print(f"  factor: {factor_count}/{len(codes)}")

    elapsed = time.time() - t0
    result = {
        "ok": True, "date": target_date,
        "meta": meta_count, "trends": trend_count, "factors": factor_count,
        "total_candidates": len(codes), "time": round(elapsed),
    }
    print(f"  {target_date}: meta={meta_count} trend={trend_count} factor={factor_count} "
          f"({len(codes)} 候选, {elapsed:.0f}s)")
    return result


def _compute_one_date(mongodb_config: Dict[str, Any], target_date: str,
                      run_meta: bool, run_trends: bool, run_factors: bool
                      ) -> Dict[str, Any]:
    """子进程入口，带连接重试。"""
    from pymongo.errors import AutoReconnect
    last_err = None
    for attempt in range(3):
        try:
            store = MongoFactorDataStore(mongodb_config)
            return _compute_date(store, target_date,
                                 run_meta=run_meta, run_trends=run_trends,
                                 run_factors=run_factors)
        except AutoReconnect as e:
            last_err = e
            delay = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)
    raise last_err


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="信号预计算 v3")
    parser.add_argument("--date", help="单个日期 YYYY-MM-DD 或 'today'")
    parser.add_argument("--start", help="批量开始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="批量结束日期 YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数")
    parser.add_argument("--meta-only", action="store_true", help="仅元数据")
    parser.add_argument("--trends-only", action="store_true", help="仅趋势")
    parser.add_argument("--factors-only", action="store_true", help="仅因子")
    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    mongodb_config = _load_mongodb_config(config_path)
    store = MongoFactorDataStore(mongodb_config)

    # 决定跑哪些
    run_meta = not (args.trends_only or args.factors_only)
    run_trends = not (args.meta_only or args.factors_only)
    run_factors = not (args.meta_only or args.trends_only)
    if args.meta_only:
        run_trends = run_factors = False
    elif args.trends_only:
        run_meta = run_factors = False
    elif args.factors_only:
        run_meta = run_trends = False

    # 获取日期列表
    if args.date:
        if args.date == "today":
            target_date = _resolve_today_trade_date(store)
            if not target_date:
                return
        else:
            target_date = args.date
        dates = [target_date]
    elif args.start:
        coll = store.db[store.collections["daily_quotes"]]
        all_dates = sorted(coll.distinct("trade_date"))
        dates = [d for d in all_dates if args.start <= d <= (args.end or "2099-01-01")]
    else:
        parser.print_help()
        return

    if not dates:
        print("无可用日期")
        return

    mode_parts = []
    if run_meta: mode_parts.append("meta")
    if run_trends: mode_parts.append("trends")
    if run_factors: mode_parts.append("factors")
    print(f"预计算 v3: {dates[0]} → {dates[-1]} ({len(dates)} 天) "
          f"| {'+'.join(mode_parts)} | workers={args.workers}")

    if args.workers > 1 and len(dates) > 1:
        from multiprocessing import Pool
        tasks = [(mongodb_config, d, run_meta, run_trends, run_factors) for d in dates]
        with Pool(args.workers) as pool:
            results = pool.starmap(_compute_one_date, tasks)
    else:
        results = []
        for i, date in enumerate(dates):
            r = _compute_date(store, date, run_meta=run_meta,
                              run_trends=run_trends, run_factors=run_factors)
            if not r.get("ok"):
                print(f"  ❌ {date}: {r.get('error', '未知错误')}")
            results.append(r)

    ok = sum(1 for r in results if r.get("ok"))
    total_meta = sum(r.get("meta", 0) for r in results)
    total_trends = sum(r.get("trends", 0) for r in results)
    total_factors = sum(r.get("factors", 0) for r in results)
    print(f"\n完成: {ok}/{len(dates)} 天, "
          f"meta={total_meta} trends={total_trends} factors={total_factors}")


if __name__ == "__main__":
    main()
