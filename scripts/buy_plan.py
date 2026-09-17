#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""

Buy Plan — Alpha Rank 候选池生成器



Layer 1: 趋势过滤 — 中期向上 + 质量不差

Layer 2: 策略匹配 — 动量突破 / 周期共振 / 低位拐点

Layer 3: 入场时机 — RSI不极端 + 近均线 + 短期未过热

Layer 4: 催化剂 — 资金流 + 情绪 + 业绩

Layer 5: 合并输出 Top N



用法:

    python3 buy_plan.py --limit 500 --top 10

"""



import argparse

import json

import os

import sys

import time

from datetime import datetime, timezone

from typing import Any, Dict, List, Optional



PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, PROJECT_ROOT)

sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))





def _get_buy_plan_config() -> Dict[str, Any]:

    """从 config_complete.yaml 读取 buy_plan 阈值，带默认值兜底。"""

    import yaml

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")

    try:

        with open(config_path) as f:

            cfg = yaml.safe_load(f)

        return (cfg.get("pyramid_middle_layer", {}).get("buy_plan", {}))

    except Exception:

        pass

    return {}



def _get_market_regime_config() -> Dict[str, Any]:
    """从 config_complete.yaml 读 market_regime（在 pyramid_middle_layer 下，与 buy_plan 同级）。

    注意：不能用 _get_buy_plan_config()——它只返回 pyramid_middle_layer.buy_plan，
    而 market_regime 在 buy_plan 之外，旧代码因此一直读不到、走了 fallback 默认值。
    """
    import yaml

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return ((cfg.get("pyramid_middle_layer") or {}).get("market_regime") or {})
    except Exception:
        return {}


from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore

from market_data_provider import MarketDataProvider

from pyramid_multifactor_strategy import PyramidMultifactorStrategy

from business_group_loader import BusinessGroupLoader

def _utc_now() -> datetime:

    return datetime.now(timezone.utc)





def _to_float(v: Any, default: float = 0.0) -> float:

    try:

        return float(v) if v is not None else default

    except (TypeError, ValueError):

        return default


def _is_dip_candidate(stock: Dict[str, Any]) -> bool:
    """低吸：价格未明显过热，且已有短线企稳证据。"""
    trend = stock.get("trend", {}) or {}
    ti = trend.get("technical_indicators", {}) or {}
    bias = ti.get("bias", {}) or {}
    rsi = _to_float(ti.get("rsi14"), None)
    bias20 = _to_float(bias.get("ma20"), None)
    boll_b = _to_float((ti.get("bollinger") or {}).get("percent_b"), None)
    ret5 = _to_float((trend.get("returns") or {}).get("return_5d"), None)
    kdj_j = _to_float((ti.get("kdj") or {}).get("j"), None)
    if ti.get("ma_alignment") == "bearish":
        return False
    below_or_near_ma = ((bias20 is not None and bias20 <= 0)
                        or (boll_b is not None and boll_b <= 0.35)
                        or (rsi is not None and rsi <= 45))
    stabilizing = ((ret5 is not None and ret5 >= 0)
                   or (kdj_j is not None and 0 <= kdj_j <= 30))
    return below_or_near_ma and stabilizing





def get_layer0_filter(industries: Optional[List[str]] = None, overrides: Optional[Dict[str, Any]] = None) -> tuple:

    """Layer 0 生存过滤 — 阈值从 config_complete.yaml 读取，不再一刀切。



    PE 过滤器不在此处限制——pool 阶段用最宽 PE（200），

    各组的 pe_max 在因子层通过 group 归属自然区分。

    """

    import yaml

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")

    try:

        with open(config_path) as f:

            cfg = yaml.safe_load(f)

        pool_cfg = cfg.get("data_sources", {}).get("mongodb", {}).get("stock_pool", {})

        min_amount = pool_cfg.get("min_amount", 50000000)

        min_mv = pool_cfg.get("min_market_cap", 5000000000)

        markets = pool_cfg.get("market_allowlist", ["主板", "创业板", "科创板"])

        # 取所有组中最宽的 pe_max，确保不遗漏

        groups = pool_cfg.get("business_groups", {})

        pe_max = 200  # 默认最宽

        if groups:

            pe_max = max((g.get("pe_max", 200) for g in groups.values()), default=200)

    except Exception:

        min_amount = 50000000

        min_mv = 5000000000

        markets = ["主板", "创业板", "科创板"]

        pe_max = 200



    overrides = overrides or {}
    if "pe_max" in overrides:
        pe_max = overrides["pe_max"]
    if "min_market_cap" in overrides:
        min_mv = overrides["min_market_cap"]
    if "min_amount" in overrides:
        min_amount = overrides["min_amount"]


    query: Dict[str, Any] = {

        "display_market": "A股",

        "market": {"$in": markets},

        "latest_amount": {"$gte": min_amount / 10000},   # 元→万元

        "total_mv": {"$gte": min_mv},

        "pe": {"$gt": 0, "$lt": pe_max},

        "industry_code": {"$exists": True, "$ne": ""},

    }

    if industries:

        query["industry"] = {"$in": industries}

    projection = {

        "code": 1, "name": 1, "close": 1, "pe": 1, "pb": 1,

        "total_mv": 1, "latest_amount": 1, "industry_code": 1,

        "industry": 1, "_id": 0,

    }

    return query, projection





# ============================================================

# Buy Plan Engine — 候选池生成器（因子评分 + 排序）

# ============================================================



class BuyPlanEngine:

    """Alpha Rank 候选池生成引擎。



    因子评分 + 动态权重 + 排序输出。

    输出候选池供 portfolio_controller 消费，不做买卖决策。"""



    def __init__(self, historical_snapshot: Optional[Dict[str, Any]] = None):

        self._hist = historical_snapshot  # 回测模式：预计算的当日快照

        if historical_snapshot:

            # 历史模式下跳过 MongoDB 初始化（数据由快照提供）

            self.mongo = None

            self.data = None

            self.pyramid = None

            self._cached_sector_data = historical_snapshot.get("sector_data", {})

        else:

            config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")

            self.mongo = MongoFactorDataStore(_load_mongodb_config(config_path))

            self.data = MarketDataProvider(self.mongo)

            self.pyramid = PyramidMultifactorStrategy(config_path)



    # ================================================================

    # 候选池：MongoDB 初筛（市值 + 流动性）

    # ================================================================



    def _broad_screen(self, industries: Optional[List[str]] = None, thresholds: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:

        """Layer 0: 生存过滤 — MongoDB 直接查询，和 import_universe_quotes 统一。"""

        if self._hist:

            candidates = self._hist["candidates"]

            if industries:

                candidates = [c for c in candidates if c.get("industry") in industries]

            print(f"Layer0 历史快照: {len(candidates)} 只")

            return candidates

        coll = self.mongo.db[self.mongo.collections["basic_info"]]

        query, projection = get_layer0_filter(industries, overrides=thresholds)

        raw = list(coll.find(query, projection).sort("latest_amount", -1))

        if industries:

            print(f"Layer0 行业范围: {', '.join(industries)} → {len(raw)} 只")



        candidates = []

        for s in raw:

            name = str(s.get("name", ""))

            if "ST" in name.upper():

                continue

            price = _to_float(s.get("close"), 0)

            if price <= 0 or price > 200:

                continue

            candidates.append({

                "code": str(s["code"]), "name": name, "market": "A股",

                "close": price,

                "pe": _to_float(s.get("pe"), None),

                "pb": _to_float(s.get("pb"), None),

                "total_mv": _to_float(s.get("total_mv"), 0),

                "latest_amount": _to_float(s.get("latest_amount"), 0),

                "industry_code": str(s.get("industry_code", "")),

                "industry": str(s.get("industry", "")),

            })

        print(f"Layer0 生存过滤: {len(raw)} → {len(candidates)} 只")

        return candidates



    def _get_universe(self) -> List[Dict[str, Any]]:

        """全市场基础信息（供前端初筛漏斗做可配置筛选）。

        与 _broad_screen 的差异：不套 Layer0 硬编码阈值（市值/成交额/PE/板块），

        返回全部 active 股票的基础字段，让初筛的每个阈值都由用户在前端调整。
        """

        if self._hist:

            return self._hist.get("universe", [])

        coll = self.mongo.db[self.mongo.collections["basic_info"]]

        projection = {

            "code": 1, "name": 1, "close": 1, "pe": 1, "pb": 1,

            "total_mv": 1, "latest_amount": 1, "market": 1,

            "display_market": 1, "industry": 1, "industry_code": 1, "_id": 0,

        }

        raw = list(coll.find({"active": {"$ne": False}}, projection).sort("code", 1))

        out = []

        for s in raw:

            out.append({

                "code": str(s.get("code", "")),

                "name": str(s.get("name", "")),

                "close": _to_float(s.get("close"), None),

                "pe": _to_float(s.get("pe"), None),

                "pb": _to_float(s.get("pb"), None),

                "total_mv": _to_float(s.get("total_mv"), None),

                "latest_amount": _to_float(s.get("latest_amount"), None),

                "market": str(s.get("market", "")),

                "display_market": str(s.get("display_market", "")),

                "industry": str(s.get("industry", "")),

                "industry_code": str(s.get("industry_code", "")),

            })

        return out



    # ================================================================

    # 批量补齐数据 + 趋势/因子打分

    # ================================================================



    def _enrich(self, candidates: List[Dict[str, Any]], limit: int = 200,

                target_date: Optional[str] = None) -> List[Dict[str, Any]]:

        """v3：③因子优先 → ②趋势缓存 + fallback。"""

        if self._hist:

            if len(candidates) > limit:

                candidates = candidates[:limit]

            print(f"  历史模式: {len(candidates)} 只已预计算信号")

            return candidates

        if len(candidates) > limit:

            candidates = candidates[:limit]



        from datetime import timezone as _tz, timedelta as _td



        # ── ③ 因子层（先加载，确定有效候选）──

        factors_coll = self.mongo.db["stock_factors"]

        factor_date = target_date

        factor_dates = sorted(factors_coll.distinct("trade_date"), reverse=True)

        if not factor_dates:

            print("⚠️ stock_factors 无数据，请先运行 precompute_history")

            return []

        # 指定日期无数据 → 自动回退到最新

        if factor_date and factor_date not in factor_dates:

            print(f"⚠️ {factor_date} 无数据，回退到最新 {factor_dates[0]}")

            factor_date = factor_dates[0]

        elif not factor_date:

            factor_date = factor_dates[0]



        candidate_codes = [c["code"] for c in candidates]

        factor_map = {}     # code → factor scores

        group_map = {}       # code → group name

        cycle_map = {}       # code → {trend, breadth, flow, cycle}

        turnaround_map = {}  # code → turnaround_score

        for doc in factors_coll.find(

            {"code": {"$in": candidate_codes}, "trade_date": factor_date},

            {"code": 1, "group": 1, "factors": 1,

             "industry_trend": 1, "industry_breadth": 1,

             "industry_flow": 1, "cycle_score": 1,

             "turnaround_score": 1, "_id": 0}

        ):

            group = doc.get("group", "")

            factors = doc.get("factors", {})

            factor_map[doc["code"]] = factors.get(group, {})

            group_map[doc["code"]] = group

            cycle_map[doc["code"]] = {

                "trend": doc.get("industry_trend", 0.5),

                "breadth": doc.get("industry_breadth", 0.5),

                "flow": doc.get("industry_flow", 0.5),

                "cycle": doc.get("cycle_score", 0.5),

            }

            turnaround_map[doc["code"]] = doc.get("turnaround_score", 0.5)



        candidates_with_factor = [c for c in candidates if c["code"] in factor_map]

        print(f"③ 因子: {len(candidates)} → {len(candidates_with_factor)} 只 (日期={factor_date})")

        if not candidates_with_factor:

            return []



        # ── ② 趋势层（缓存优先，缺的实时算）──

        trends_coll = self.mongo.db["stock_trends"]

        trend_date = target_date if (target_date and target_date in trends_coll.distinct("trade_date")) else factor_date

        cache_age = (_utc_now() - datetime.strptime(trend_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days

        if cache_age >= 2:

            print(f"⚠️ 趋势缓存 {cache_age} 天未更新（最新 {trend_date}），建议运行 precompute_history")



        codes_with_factor = [c["code"] for c in candidates_with_factor]

        # ── 财务层：最新报告期 ROE（弱势套件质量门槛用）──

        fin_coll = self.mongo.db["stock_financial_data"]

        roe_map = {}

        for doc in fin_coll.find(

            {"code": {"$in": codes_with_factor}, "roe": {"$exists": True, "$ne": None}},

            {"code": 1, "report_period": 1, "roe": 1, "_id": 0}

        ).sort("report_period", -1):

            if doc["code"] not in roe_map:  # report_period 倒序，首条即最新期

                roe_map[doc["code"]] = doc["roe"]

        trend_cache = {}

        for doc in trends_coll.find(

            {"code": {"$in": codes_with_factor}, "trade_date": trend_date},

            {"code": 1, "available": 1, "status": 1, "ma": 1, "returns": 1,

             "volatility_20d": 1, "rsi14": 1, "kdj": 1, "macd": 1,

             "boll_percent_b": 1, "volume_price_signal": 1,

             # P0/P1 新增指标
             "atr_20_pct": 1, "bias_ma5": 1, "bias_ma10": 1, "bias_ma20": 1,

             "volume_ratio": 1, "max_drawdown_20d_pct": 1, "cci14": 1,

             "ma_alignment": 1, "daily_quality_score": 1, "_id": 0}

        ):

            trend_cache[doc["code"]] = doc



        hit = len(trend_cache)

        miss = len(codes_with_factor) - hit

        print(f"② 趋势: {hit} 只缓存命中, {miss} 只实时算")



        # ── 组装 ──

        results = []

        for c in candidates_with_factor:

            code = c["code"]

            trend_doc = trend_cache.get(code)

            if trend_doc:

                trend = {

                    "available": trend_doc.get("available", False),

                    "status": trend_doc.get("status", ""),

                    "ma": trend_doc.get("ma", {}),

                    "returns": trend_doc.get("returns", {}),

                    "volatility_20d": trend_doc.get("volatility_20d"),

                    "technical_indicators": {

                        "rsi14": trend_doc.get("rsi14"),

                        "kdj": trend_doc.get("kdj", {}),

                        "macd": trend_doc.get("macd", {}),

                        "bollinger": {"percent_b": trend_doc.get("boll_percent_b")},

                        "volume_price_signal": trend_doc.get("volume_price_signal"),

                        # P0/P1 新增指标（旧文档缺失时为 None，下游已做容错）
                        "atr_20_pct": trend_doc.get("atr_20_pct"),

                        "bias": {

                            "ma5": trend_doc.get("bias_ma5"),

                            "ma10": trend_doc.get("bias_ma10"),

                            "ma20": trend_doc.get("bias_ma20"),

                        },

                        "volume_ratio": trend_doc.get("volume_ratio"),

                        "max_drawdown_20d_pct": trend_doc.get("max_drawdown_20d_pct"),

                        "cci14": trend_doc.get("cci14"),

                        "ma_alignment": trend_doc.get("ma_alignment"),

                        "daily_quality_score": trend_doc.get("daily_quality_score"),

                    },

                }

            else:

                try:

                    trend = self.data.get_trend_signal(code, "A股")

                except Exception:

                    trend = {"available": False}



            factor = factor_map[code]

            results.append({**c, "trend": trend, "factor": factor,

                            "group": group_map.get(code, ""),

                            "cycle_score": cycle_map.get(code, {}).get("cycle", 0.5),

                            "turnaround_score": turnaround_map.get(code, 0.5),

                            "roe": roe_map.get(code),

                            "pe_percentile": None, "forecast_min": None,

                            "forecast_max": None, "moneyflow_net": None})



        return results



    def _get_market_breadth(self, target_date: Optional[str] = None) -> float:

        """全市场站上MA20的股票比例（%）。从 stock_trends 读取。"""

        if self._hist:

            return self._hist.get("market_breadth", 50.0)

        try:

            trends_coll = self.mongo.db["stock_trends"]

            dates = sorted(trends_coll.distinct("trade_date"), reverse=True)

            if not dates:

                return 50.0

            td = target_date or dates[0]



            # 批量加载收盘价（避免逐只 find_one）

            price_coll = self.mongo.db[self.mongo.collections["daily_quotes"]]

            price_map = {}

            for pdoc in price_coll.find(

                {"trade_date": td, "period": "daily"},

                {"code": 1, "close": 1, "_id": 0}

            ).batch_size(5000):

                price_map[pdoc["code"]] = pdoc.get("close", 0)



            above = 0

            valid = 0

            for doc in trends_coll.find(

                {"trade_date": td, "available": True},

                {"code": 1, "ma.ma20": 1, "_id": 0}

            ).batch_size(2000):

                ma20 = (doc.get("ma") or {}).get("ma20", 0)

                if ma20 <= 0:

                    continue

                close = price_map.get(doc["code"], 0)

                if close > 0 and close > ma20:

                    above += 1

                valid += 1



            return round(above / valid * 100, 1) if valid > 0 else 50.0

        except Exception:

            return 50.0



    def _resolve_regime(self, breadth_pct: float, target_date: Optional[str] = None) -> str:
        """即时市场状态 + 防抖：新档位连续维持 regime_confirm_days 个交易日才切换。

        历史回测（target_date 指定 / self._hist）不防抖，直接即时判定。
        状态持久化在 market_regime_state（单文档 upsert，只增不改，遵守禁删）。
        """
        regime_cfg = _get_market_regime_config()
        strong_line = regime_cfg.get("strong_breadth", 60)
        weak_line = regime_cfg.get("weak_breadth", 35)
        if breadth_pct >= strong_line:
            instant = "strong"
        elif breadth_pct >= weak_line:
            instant = "neutral"
        else:
            instant = "weak"

        confirm_days = int(regime_cfg.get("regime_confirm_days", 3) or 3)

        if self._hist or target_date or self.mongo is None:
            return instant

        col = self.mongo.db["market_regime_state"]
        state = col.find_one({"key": "current"})
        active = (state or {}).get("active_regime")
        pending = (state or {}).get("pending_regime")
        pending_days = int((state or {}).get("pending_days") or 0)

        if active is None:
            active = instant
            pending = instant
            pending_days = confirm_days
        elif instant == pending:
            pending_days += 1
            if pending_days >= confirm_days and instant != active:
                active = instant
        else:
            pending = instant
            pending_days = 1

        col.update_one(
            {"key": "current"},
            {"$set": {
                "active_regime": active,
                "pending_regime": pending,
                "pending_days": pending_days,
                "instant_regime": instant,
                "breadth_pct": breadth_pct,
                "updated_at": _utc_now().isoformat(),
            }},
            upsert=True,
        )
        return active

    def _get_market_phase(self, breadth_pct: float) -> Dict[str, Any]:
        """市场阶段判定。数据源不可用时降级为中性，不阻断主流程。"""
        from market_phase import determine_market_phase, extract_phase_inputs

        market_sentiment = None
        if not self._hist and self.data is not None:
            try:
                market_sentiment = self.data.get_market_sentiment()
            except Exception:
                market_sentiment = None

        inputs = extract_phase_inputs(market_sentiment, breadth_pct=breadth_pct)
        return determine_market_phase(**inputs)

    def _get_portfolio_codes(self) -> set:

        try:

            from portfolio_strategy import PortfolioStrategy

            ps = PortfolioStrategy()

            return {p["code"] for p in (ps.data.get("positions") or [])}

        except Exception:

            return set()



    def run(self, top_n: int = 10, initial_limit: int = 500, enrich_limit: int = 200,
            industries: Optional[List[str]] = None,
            target_date: Optional[str] = None) -> Dict[str, Any]:
        if target_date == "today":
            target_date = _utc_now().strftime("%Y-%m-%d")
        date_label = target_date or "最新"
        print("=" * 60)
        print(f"Buy Plan — Alpha Rank 候选池 ({date_label})")
        if industries:
            print(f"  行业范围: {', '.join(industries)}")
        print("=" * 60)

        # 市场宽度 + 牛熊状态（先算，决定 Layer0 用哪套阈值）
        breadth_pct = self._get_market_breadth(target_date)
        halt_pct = _get_buy_plan_config().get("market_breadth", {}).get("halt_threshold", 15)
        if breadth_pct < halt_pct:
            print(f"⚠ 市场宽度 {breadth_pct:.1f}% < {halt_pct}%，buy_plan 停摆（熊市不做多）")
            return {"error": f"市场宽度不足({breadth_pct:.1f}%)，暂停推荐",
                    "breadth_pct": breadth_pct}

        # 市场状态（防抖）→ 阈值套件 + 动态 alpha 权重
        regime = self._resolve_regime(breadth_pct, target_date)
        regime_cfg = _get_market_regime_config()
        _th_all = regime_cfg.get("thresholds") or {}
        thresholds = _th_all.get(regime) or _th_all.get("neutral") or {}
        alpha_w = regime_cfg.get("alpha_weights", {}).get(regime, {
            "momentum": 0.45, "cycle": 0.35, "turnaround": 0.20})
        weak_min_cycle = regime_cfg.get("weak_min_cycle", 0.50)
        roe_min = thresholds.get("roe_min")

        print(f"市场宽度 {breadth_pct:.1f}% → {regime} | "
              f"阈值 pe_max={thresholds.get('pe_max', '默认')} "
              f"市值≥{thresholds.get('min_market_cap', 0) / 1e8:.0f}亿 "
              f"成交额≥{thresholds.get('min_amount', 0) / 1e8:.1f}亿 | "
              f"权重 m={alpha_w['momentum']:.0%} c={alpha_w['cycle']:.0%} t={alpha_w['turnaround']:.0%}"
              + (f" 弱市保护 cycle>{weak_min_cycle:.0%}" if regime == "weak" else ""))

        # 市场阶段判定（宽度 + 指数动量 + 北向 + 两融）→ 顶层熊市熔断（先于初筛，decline 直接不推票）
        # 注：market_phase 的强/弱线（55/35，硬编码于 market_phase.py）与上面 _resolve_regime 的
        # 档位线（60/35，config_complete.yaml）口径不同、职责不同——regime 决定「用哪套阈值/权重」，
        # market_phase 决定「能不能买（是否熔断）」。两条线刻意独立，同一天给出不一致的强/弱观感属预期，
        # 勿强行统一。
        from market_phase import format_market_phase

        market_phase = self._get_market_phase(breadth_pct)
        print(format_market_phase(market_phase))
        for ev in market_phase.get("evidence", []):
            print(f"  · {ev}")

        # 下跌期：暂停生成新买入候选（已有持仓由 portfolio_strategy 独立管理）
        if not market_phase.get("allow_new_buy", True):
            print(f"⚠ 市场阶段为{market_phase.get('label')}，暂停生成买入候选")
            return {"error": f"市场阶段{market_phase.get('label')}，暂停推荐",
                    "breadth_pct": breadth_pct,
                    "market_phase": market_phase,
                    "candidates": []}

        # Layer 0: 生存过滤（用当前 regime 的阈值套件）
        candidates = self._broad_screen(industries=industries, thresholds=thresholds)

        # 全市场基础信息（供前端初筛漏斗做可配置筛选，如实展示漏斗头部）
        universe = self._get_universe()

        if not candidates:
            return {"error": "初筛无结果", "candidates": []}

        # 补齐因子数据
        eff_limit = enrich_limit if enrich_limit > 0 else len(candidates)
        scored = self._enrich(candidates, limit=min(len(candidates), eff_limit),
                              target_date=target_date)

        # ── v3 候选池生成：因子评分 + 打标签（全量，供前端漏斗筛选）──
        portfolio_codes = self._get_portfolio_codes()
        for s in scored:
            factor = s.get("factor", {})
            momentum = (factor.get("factor_scores") or {}).get("momentum", 0.5)
            cycle = s.get("cycle_score", 0.5)
            turnaround = s.get("turnaround_score", 0.5)

            s["alpha_score"] = round(
                momentum * alpha_w["momentum"]
                + cycle * alpha_w["cycle"]
                + turnaround * alpha_w["turnaround"], 3)
            s["in_portfolio"] = s["code"] in portfolio_codes

            s["strategy_tags"] = ["动量突破"]
            s["tag_momentum"] = True
            s["tag_cycle"] = cycle >= 0.20
            s["tag_turnaround"] = turnaround >= 0.7
            if cycle >= 0.20:
                s["strategy_tags"].append("周期共振")
            if turnaround >= 0.7:
                s["strategy_tags"].append("低位拐点")
            s["strategy_count"] = len(s["strategy_tags"])

        pool = sorted(scored, key=lambda x: x["alpha_score"], reverse=True)

        # 弱势质量门槛：ROE≥roe_min（仅实时模式有财务数据；历史快照缺 roe 时跳过）
        has_roe = any(s.get("roe") is not None for s in pool)

        passed = []
        for s in pool:
            if s["cycle_score"] < 0.20:
                continue
            if regime == "weak":
                if s["cycle_score"] < weak_min_cycle:
                    continue
                if roe_min and has_roe and (s.get("roe") is None or s["roe"] < roe_min):
                    continue
            # 筑底/分配期仅允许低吸候选（market_phase 的 dip_only 约束）
            if market_phase.get("dip_only") and not _is_dip_candidate(s):
                continue
            # 趋势质量门槛：日线质量分 < 60 过滤（防追高劣质形态）
            quality = s.get("trend", {}).get("technical_indicators", {}).get(
                "daily_quality_score")
            if quality is not None and quality < 60:
                continue
            passed.append(s)

        roe_note = f" + ROE≥{roe_min}%" if (regime == "weak" and roe_min and has_roe) else ""
        print(f"候选池: {len(scored)} → {len(passed)} 只 (cycle<0.20过滤{roe_note})")

        final = []
        for s in passed[:top_n]:
            item = dict(s)
            if item["code"] in portfolio_codes:
                item["alpha_score"] = round(item["alpha_score"] * 0.5, 3)
            final.append(item)

        report = {
            "generated_at": _utc_now().isoformat(),
            "pipeline": {
                "candidates": len(candidates),
                "scored": len(scored),
                "passed": len(passed),
                "final": len(final),
            },
            "universe": universe,
            "pool": pool,
            "recommendations": final,
            "market": {
                "breadth_pct": breadth_pct,
                "regime": regime,
                "alpha_weights": alpha_w,
                "thresholds": thresholds,
                "phase": market_phase,
            },
        }
        return report




def format_buy_plan(report: Dict[str, Any]) -> str:

    pipe = report.get("pipeline", {})

    lines = [

        "=" * 90,

        f"Buy Plan — 候选池 ({report.get('generated_at', '')})",

        f"候选 {pipe.get('candidates','?')} → 有因子 {pipe.get('scored','?')} → "

        f"通过 {pipe.get('passed','?')} → 最终 {pipe.get('final','?')}",

    ]

    # 市场阶段（旧报告无此字段时跳过，保持向后兼容）
    phase = (report.get("market") or {}).get("phase")

    if phase:

        from market_phase import format_market_phase

        lines.append(format_market_phase(phase))

    lines += [

        "=" * 90,

        "",

        f"{'':>3} {'代码':<8} {'名称':<10} {'行业':<10} {'策略':<18} {'alpha':>6} {'动量':>6} {'周期':>6} {'拐点':>6} {'PE':>6}",

        "-" * 90,

    ]



    for i, r in enumerate(report.get("recommendations", []), 1):

        tags = "+".join(r.get("strategy_tags", []))

        momentum = (r.get("factor", {}).get("factor_scores") or {}).get("momentum", 0)

        in_pf = "[已持仓]" if r.get("in_portfolio") else ""

        lines.append(

            f"{i:>3} {r['code']:<8} {r.get('name','')[:8]:<10} "

            f"{r.get('industry','')[:10]:<10} "

            f"{tags:<18} {r.get('alpha_score',0):>5.3f}  "

            f"{momentum:>5.3f}  {r.get('cycle_score',0):>5.3f}  "

            f"{r.get('turnaround_score',0):>5.3f}  "

            f"{r.get('pe') or '-':>6} {in_pf}"

        )

        # PE分位 + 一致预期 + 资金

        extra = []

        pe_pct = r.get("pe_percentile")

        if pe_pct is not None:

            extra.append(f"PE分位{pe_pct}%")

        fc_min = r.get("forecast_min")

        if fc_min is not None:

            extra.append(f"预期{fc_min}~{r.get('forecast_max','?')}%")

        mf_net = r.get("moneyflow_net")

        if mf_net is not None and mf_net != 0:

            direction = "流入" if mf_net > 0 else "流出"

            extra.append(f"大单{direction}{abs(mf_net/10000):.0f}万")

        turnover = r.get("turnover_rate")

    lines.extend(["", "=" * 90])

    return "\n".join(lines)





# ================================================================

# CLI

# ================================================================



def main():

    parser = argparse.ArgumentParser(description="Buy Plan — Alpha Rank 候选池生成器")

    parser.add_argument("--limit", "-l", type=int, default=500, help="初筛数")

    parser.add_argument("--top", "-n", type=int, default=10)

    parser.add_argument("--enrich", type=int, default=0, help="补齐数据上限，0=不限制")

    parser.add_argument("--date", "-d", type=str, default=None,

                       help="指定日期 YYYY-MM-DD（回测用，默认最新）")

    parser.add_argument("--industries", "-i", type=str, default=None,

                       help="限定行业范围，逗号分隔，如'煤炭开采,白酒,水力发电'（配合 sector_radar 使用）")

    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()



    industries = None

    if args.industries:

        industries = [i.strip() for i in args.industries.split(",") if i.strip()]

    engine = BuyPlanEngine()

    report = engine.run(top_n=args.top, initial_limit=args.limit,

                        enrich_limit=args.enrich, industries=industries,

                        target_date=args.date)



    if args.json:

        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    else:

        print(format_buy_plan(report))





if __name__ == "__main__":

    main()
