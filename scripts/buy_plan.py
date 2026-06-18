#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buy Plan — 五层漏斗买入候选筛选

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


def get_layer0_filter(industries: Optional[List[str]] = None) -> tuple:
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
# Buy Plan Engine — 五层漏斗
# ============================================================

class BuyPlanEngine:
    """五层漏斗买入候选筛选引擎。"""

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

    def _broad_screen(self, industries: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Layer 0: 生存过滤 — MongoDB 直接查询，和 import_universe_quotes 统一。"""
        if self._hist:
            candidates = self._hist["candidates"]
            if industries:
                candidates = [c for c in candidates if c.get("industry") in industries]
            print(f"Layer0 历史快照: {len(candidates)} 只")
            return candidates
        coll = self.mongo.db[self.mongo.collections["basic_info"]]
        query, projection = get_layer0_filter(industries)
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
        trend_cache = {}
        for doc in trends_coll.find(
            {"code": {"$in": codes_with_factor}, "trade_date": trend_date},
            {"code": 1, "available": 1, "status": 1, "ma": 1, "returns": 1,
             "volatility_20d": 1, "rsi14": 1, "kdj": 1, "macd": 1,
             "boll_percent_b": 1, "volume_price_signal": 1, "_id": 0}
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
                            "pe_percentile": None, "forecast_min": None,
                            "forecast_max": None, "moneyflow_net": None})

        return results

    def _layer1_trend_filter(self, stocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """策略感知的趋势过滤：不同策略不同趋势容忍度。

        动量突破 → 必须"趋势较强"（在跑着）
        周期共振 → "修复中"或"震荡分歧"（周期过渡）
        低位拐点 → 接受"趋势偏弱"（就是来抄底的）
        """
        TREND_MAP = {
            "动量突破": ("趋势较强",),
            "周期共振": ("趋势较强", "修复中", "震荡分歧", "趋势偏弱"),
            "低位拐点": ("趋势较强", "修复中", "震荡分歧", "趋势偏弱"),
        }

        passed = []
        for s in stocks:
            trend = s.get("trend", {})
            if not trend.get("available"):
                continue

            # 按策略选趋势门槛
            tags = s.get("strategy_tags", [])
            allowed = ("趋势较强", "修复中")  # fallback
            for tag in tags:
                if tag in TREND_MAP:
                    allowed = TREND_MAP[tag]
                    break
            trend_status = trend.get("status", "")
            trend_ok = trend_status in allowed

            profit_crash = False
            if self._hist:
                profit_g = (s.get("factor", {}).get("profit_growth") or
                            (s.get("factor", {}).get("factor_details") or {})
                            .get("growth", {}).get("profit_growth"))
                profit_crash = profit_g is not None and _to_float(profit_g, 0) < -30
            else:
                fin_data = self.data.get_financial(s["code"]).get("data") or {}
                profit_g = _to_float(fin_data.get("profit_growth"), None)
                profit_crash = profit_g is not None and profit_g < -30

            if trend_ok and not profit_crash:
                s["layer1"] = True
                passed.append(s)

        no_trend = sum(1 for s in stocks if not s.get("trend", {}).get("available"))
        tags = {}
        for s in passed:
            for t in s.get("strategy_tags", []):
                tags[t] = tags.get(t, 0) + 1
        print(f"趋势过滤: {len(stocks)} → {len(passed)} 只 (趋势不可用:{no_trend}) {tags}")
        return passed

    # ================================================================
    # Layer 2: 策略匹配
    # ================================================================

    def _layer2_strategy_match(self, stocks: List[Dict[str, Any]],
                                cycle_state: Dict[str, Any],
                                target_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """三策略并行为每只票打标签。"""
        # 行业 PE 中位数
        peer_stats = self._build_peer_stats(stocks)

        # 行业数据（苏醒分+广度 + 热力 + 拐点）
        sector_data = self._get_sector_wake_scores()
        sector_wake = {k: v.get("wake", 0) for k, v in sector_data.items()}
        sector_breadth = {k: v.get("breadth", 0) for k, v in sector_data.items()}

        # 热力行业 + 拐点行业 + 行业状态（from sector_radar）
        hot_sectors = set()
        inflection_map = {}
        sector_status = {}
        try:
            from sector_radar import SectorRadarEngine
            radar = SectorRadarEngine()
            hot_sectors = set(radar.get_hot_sectors(10, target_date=target_date))
            inflection_map = radar.get_inflection_map(target_date=target_date)
            sector_status = radar.get_sector_status_map(target_date=target_date)
        except Exception:
            pass

        for s in stocks:
            trend = s.get("trend", {})
            factor = s.get("factor", {})
            ti = trend.get("technical_indicators", {}) or {}
            ma = trend.get("ma", {}) or {}
            rsi = ti.get("rsi14")
            ret_5d = (trend.get("returns") or {}).get("return_5d")
            ret_20d = (trend.get("returns") or {}).get("return_20d")
            momentum = (factor.get("factor_scores") or {}).get("momentum", 0)
            quality = (factor.get("factor_scores") or {}).get("quality", 0)
            scores = factor.get("factor_scores") or {}
            weights = factor.get("factor_weights") or {}

            ind_code = s.get("industry_code", "")
            peers = peer_stats.get(ind_code, {})
            pe_med = peers.get("pe_median")
            pb_med = peers.get("pb_median")
            pe = s.get("pe")
            pb = s.get("pb")

            # --- 动量突破（放宽条件，适配震荡市） ---
            turnover = s.get("turnover_rate")
            ma_bull = (ma.get("ma5") and ma.get("ma20") and ma.get("ma60")
                       and ma["ma5"] > ma["ma20"] > ma["ma60"])
            group = s.get("group", "")
            funnel = BusinessGroupLoader().get_funnel(group) if group else {}
            mom_cfg = funnel.get("layer2_momentum", {})
            momentum_ok = momentum >= mom_cfg.get("momentum_min", 0.50)
            rsi_lo = mom_cfg.get("rsi_min", 40)
            rsi_hi = mom_cfg.get("rsi_max", 70)
            rsi_momentum_ok = rsi is not None and rsi_lo <= rsi <= rsi_hi
            ret20_ok = ret_20d is not None and ret_20d > 0
            # PE 硬顶：不买离谱估值的动量票（PE=None 放行，历史数据缺失不惩罚）
            pe_ceiling = min(pe_med * 1.2, 80) if pe_med else 80
            pe_momentum_ok = pe is None or pe < pe_ceiling
            # 换手底线：过滤僵尸股（数据缺失时默认放行）
            turnover_ok = turnover is None or turnover <= 0 or turnover >= 0.005

            ind = s.get("industry", "")
            ind_in_hot = ind in hot_sectors
            ind_mf = radar.get_sector_moneyflow(ind, target_date=target_date) if ind else 0
            ind_mf_ok = ind_mf > 0
            vol_sig = (trend.get("volume_price_signal") or "")
            vol_ok = "放量上涨" in str(vol_sig)

            s["tag_momentum"] = True  # v2: label only, no gate
            s["momentum_checks"] = {
                "ma_bull": ma_bull,
                f"mom≥{mom_cfg.get('momentum_min', 0.5):.1f}": momentum_ok,
                f"RSI{rsi_lo}-{rsi_hi}": rsi_momentum_ok, "20d>0": ret20_ok,
                f"PE<{pe_ceiling:.0f}": pe_momentum_ok, "换手≥0.5%": turnover_ok,
                "行业热力Top10": ind_in_hot, "主力流入": ind_mf_ok, "放量上涨": vol_ok,
                "预期向上": fc_momentum_ok,
            }

            # --- 周期共振（行业周期得分，precompute 已算好） ---
            cycle_score = s.get("cycle_score", 0.5)
            s["tag_cycle"] = cycle_score >= 0.20  # 行业前 45% 视为周期顺风
            s["cycle_checks"] = {
                f"周期得分{cycle_score:.2f}": cycle_score >= 0.20,
            }

            # --- 低位拐点（阈值从 config 读取） ---
            ta_cfg = _get_buy_plan_config().get("turnaround", {})
            ta_threshold = ta_cfg.get("tag_threshold", 0.65)
            ta_score = s.get("turnaround_score", 0.5)
            s["tag_turnaround"] = ta_score >= ta_threshold
            s["turnaround_checks"] = {
                f"拐点得分{ta_score:.2f}": ta_score >= ta_threshold,
            }

            # 综合得分（策略命中越多越高）
            tags = [s["tag_momentum"], s["tag_cycle"], s["tag_turnaround"]]
            s["strategy_count"] = sum(tags)
            s["strategy_tags"] = []
            if s["tag_momentum"]:
                s["strategy_tags"].append("动量突破")
            if s["tag_cycle"]:
                s["strategy_tags"].append("周期共振")
            if s["tag_turnaround"]:
                s["strategy_tags"].append("低位拐点")

        # 至少命中一个策略
        matched = [s for s in stocks if s["strategy_count"] > 0]
        counts = {
            "动量突破": sum(1 for s in matched if s["tag_momentum"]),
            "周期共振": sum(1 for s in matched if s["tag_cycle"]),
            "低位拐点": sum(1 for s in matched if s["tag_turnaround"]),
        }
        print(f"Layer2 策略匹配: {len(stocks)} → {len(matched)} 只 {counts}")
        return matched

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

    def _get_sector_wake_scores(self) -> Dict[str, float]:
        """获取行业苏醒分和广度映射，来自 sector_radar。

        如果 _cached_sector_data 已注入（回测模式），直接使用。
        """
        if hasattr(self, "_cached_sector_data") and self._cached_sector_data:
            return self._cached_sector_data
        result = {}
        try:
            from sector_radar import SectorRadarEngine
            engine = SectorRadarEngine()
            groups, _, _ = engine.load_all_industry_data()
            metrics = [engine._compute_industry_metrics(g) for g in groups]
            valid = [m for m in metrics if m.get("available")]
            # 热力图（含广度数据）
            heatmap = engine.compute_heatmap(valid)
            for m in heatmap:
                result[m["industry"]] = {
                    "wake": m.get("wake_up_score", 0),
                    "breadth": m.get("breadth_pct", 0),
                }
            # 雷达苏醒分覆盖
            radar = engine.compute_radar(heatmap)
            for m in radar:
                if m["industry"] not in result:
                    result[m["industry"]] = {"breadth": 0}
                result[m["industry"]]["wake"] = m.get("wake_up_score", 0)
        except Exception:
            pass
        return result

    # ================================================================
    # Layer 3: 入场时机 — RSI不极端 + 近均线 + 短期不过热
    # ================================================================

    def _layer3_entry_timing(self, stocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤 + 评分：RSI 40-75 + 距MA20 ≤5% + 5d收益 ±5%；额外计算入场时机质量分。"""
        passed = []
        for s in stocks:
            trend = s.get("trend", {})
            ti = trend.get("technical_indicators", {}) or {}
            ma = trend.get("ma", {}) or {}
            ret_5d = (trend.get("returns") or {}).get("return_5d")

            rsi = ti.get("rsi14")
            kdj = ti.get("kdj", {}) or {}
            kdj_k = kdj.get("k")
            kdj_j = kdj.get("j")
            price = s["close"]
            ma20 = ma.get("ma20")

            rsi_ok = rsi is not None and 40 <= rsi <= 75
            near_ma20 = ma20 and abs(price - ma20) / ma20 <= 0.05
            ret5_ok = ret_5d is not None and -5 <= ret_5d <= 5

            # ── 入场时机质量评分 (0-1) ──
            entry_score = 0.5  # 中性基准

            # MA20 距离：越近越好，10%以上距离为0分
            if ma20 and ma20 > 0:
                ma20_dist = abs(price - ma20) / ma20
                # 在MA20附近(±2%) = 满分；>5% = 0分
                entry_score += 0.35 * max(0, 1 - ma20_dist / 0.05)
                # 略低于MA20（回踩）小幅加分
                if -0.02 <= (price - ma20) / ma20 <= 0:
                    entry_score += 0.10

            # RSI 区间：40-55最优，>65或<30扣分
            if rsi is not None:
                if 40 <= rsi <= 55:
                    entry_score += 0.25
                elif 55 < rsi <= 65:
                    entry_score += 0.15
                elif 30 <= rsi < 40:
                    entry_score += 0.10
                elif rsi > 70:
                    entry_score -= 0.20  # 追高警告
                elif rsi < 25:
                    entry_score -= 0.15  # 接飞刀警告

            # KDJ-J：低位最佳买点
            if kdj_j is not None:
                if kdj_j < 30:
                    entry_score += 0.20  # 超卖区=好买点
                elif kdj_j < 50:
                    entry_score += 0.10
                elif kdj_j > 80:
                    entry_score -= 0.15  # 超买区=差买点

            # 近5日走势：微跌/横盘最优，连续大涨追高扣分
            if ret_5d is not None:
                if -2 <= ret_5d <= 0.5:
                    entry_score += 0.10  # 回调到位
                elif 0.5 < ret_5d <= 3:
                    entry_score += 0.05
                elif ret_5d > 5:
                    entry_score -= 0.15  # 短期追高
                elif ret_5d < -5:
                    entry_score -= 0.05

            s["entry_score"] = round(max(0, min(entry_score, 1.0)), 3)
            group = s.get("group", "")
            funnel = BusinessGroupLoader().get_funnel(group) if group else {}
            entry_cfg = funnel.get("layer3_entry", {})
            s["entry_label"] = "🟢" if s["entry_score"] >= entry_cfg.get("entry_green", 0.7) else (
                "🟡" if s["entry_score"] >= entry_cfg.get("entry_yellow", 0.45) else "🔴")

            if rsi_ok and near_ma20 and ret5_ok:
                s["entry_ok"] = True
                passed.append(s)
            else:
                s["entry_ok"] = False
                s["entry_blocked"] = [k for k, v in
                    {"RSI": rsi_ok, "近MA20": near_ma20, "5d温和": ret5_ok}.items()
                    if not v]

        print(f"Layer3 入场时机: {len(stocks)} → {len(passed)} 只")
        return passed

    # ================================================================
    # Layer 4: 催化剂
    # ================================================================

    def _layer4_catalyst(self, stocks: List[Dict[str, Any]],
                         target_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """催化剂加分：资金流 + 情绪 + 业绩。"""
        # 预加载个股资金流
        mf_data = {}
        if self._hist:
            # 历史模式：从快照中的 moneyflow 映射获取
            mf_map = self._hist.get("moneyflow", {})
            for code in [s["code"] for s in stocks]:
                net = mf_map.get(code)
                if net is not None:
                    mf_data[code] = {"moneyflow_net": net}
        else:
            try:
                for doc in self.mongo.db["stock_signals"].find(
                    {"code": {"$in": [s["code"] for s in stocks]}, "moneyflow_net": {"$exists": True}},
                    {"code": 1, "moneyflow_net": 1, "moneyflow_date": 1},
                ):
                    mf_data[doc["code"]] = doc
            except Exception:
                pass

        for s in stocks:
            score = 0
            details = {}

            # 1. 资金流确认（融资净买 + 主力资金）
            code = s["code"]
            mf_doc = mf_data.get(code)
            if mf_doc:
                mf_net = _to_float(mf_doc.get("moneyflow_net"), 0)
                if mf_net > 0:
                    score += 2  # 权重翻倍：资金确认比新闻/业绩更重要
                    details["flow"] = f"主力净流入{mf_net/1e4:.0f}万"
                else:
                    details["flow"] = f"主力净流出{abs(mf_net)/1e4:.0f}万"
            elif not self._hist:
                # 兜底：用旧的数据源（仅实时模式）
                try:
                    mf = self.data.get_moneyflow(code)
                    d = mf.get("data") or {}
                    if d:
                        buy = _to_float(d.get("buy_lg_amount"), 0)
                        sell = _to_float(d.get("sell_lg_amount"), 0)
                        if buy > sell and buy > 0:
                            score += 1
                            details["flow"] = "主力净流入"
                except Exception:
                    pass

            # 2. 新闻情绪（回测/历史模式跳过）
            if not self._hist and target_date is None:
                try:
                    news = self.data.get_news(s["code"], limit=10)
                    sentiments = [self.data.analyze_sentiment(
                        f"{n.get('title','')} {n.get('content','')}").get("score", 0)
                        for n in news[:5]]
                    if sentiments:
                        avg = sum(sentiments) / len(sentiments)
                        if avg > 0.2:
                            score += 1
                            details["sentiment"] = round(avg, 2)
                except Exception:
                    pass

            # 3. 业绩超预期（利润增长 > 15%）
            if not self._hist:
                try:
                    fin = self.data.get_financial(s["code"])
                    fd = fin.get("data") or {}
                    profit_g = _to_float(fd.get("profit_growth"), 0)
                    if profit_g > 15:
                        score += 1
                        details["earnings"] = f"+{profit_g:.1f}%"
                except Exception:
                    pass

            s["catalyst_score"] = score
            s["catalyst_details"] = details

        print(f"Layer4 催化剂: 有催化 {sum(1 for s in stocks if s['catalyst_score'] > 0)}/{len(stocks)} 只")
        return stocks

    # ================================================================
    # Layer 4.5: 精准补财务数据
    # ================================================================

    def _enrich_financial(self, stocks: List[Dict[str, Any]]) -> None:
        """对通过全部筛选的候选股，精准拉取 tushare 财务数据，修正质量因子。

        只在候选股已有数据的质量分看起来像默认值(≈0.5)时才触发。
        调用量 = 候选股数 × 1次API，远小于全量导入。
        """
        if not stocks:
            return

        if self._hist:
            return  # 历史模式：快照中已有预计算的因子分

        # 找出需要补数据的：质量分在 0.45-0.55 之间（可能是默认值）
        need_enrich = []
        for s in stocks:
            scores = (s.get("factor", {}).get("factor_scores") or {})
            quality = scores.get("quality", 0)
            if abs(quality - 0.50) < 0.10:  # 接近默认值
                need_enrich.append(s)

        if not need_enrich:
            return

        try:
            import yaml, requests, os
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "config", "config_complete.yaml")
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            token = (cfg.get("data_sources") or {}).get("tushare", {}).get("token", "")
            if not token:
                return
        except Exception:
            return

        enriched = 0
        for s in need_enrich:
            code = s["code"]
            ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
            try:
                resp = requests.post(
                    "https://api.tushare.pro",
                    json={
                        "api_name": "fina_indicator",
                        "token": token,
                        "params": {"ts_code": ts_code},
                        "fields": "ts_code,roe,grossprofit_margin,debt_to_assets",
                    },
                    timeout=10,
                )
                data = resp.json()
                items = data.get("data", {}).get("items", [])
                if items:
                    r = items[0]  # 最新一季
                    roe = _to_float(r[1], None) if len(r) > 1 else None
                    gm = _to_float(r[2], None) if len(r) > 2 else None
                    dr = _to_float(r[3], None) if len(r) > 3 else None

                    # 重新计算质量分
                    from pyramid_multifactor_strategy import PyramidMultifactorStrategy
                    fin = {}
                    if roe is not None:
                        fin["roe"] = roe
                    if gm is not None:
                        fin["gross_margin"] = gm
                    if dr is not None:
                        fin["debt_to_assets"] = dr

                    if fin:
                        fin["report_period"] = items[0][0] if len(items[0]) > 0 else "?"
                        quality_score, _ = self.pyramid.calculate_quality_score(s, fin)
                        scores = s.get("factor", {}).get("factor_scores", {})
                        old_quality = scores.get("quality", 0)
                        scores["quality"] = round(quality_score, 3)

                        # 更新综合分
                        weights = s.get("factor", {}).get("factor_weights", {})
                        composite = round(
                            sum(scores.get(k, 0) * weights.get(k, 0)
                                for k in ["value", "growth", "quality", "momentum"]), 3)
                        s["factor"]["composite_score"] = composite
                        s["factor"]["factor_scores"] = scores
                        enriched += 1
            except Exception:
                pass
            time.sleep(0.3)  # 节流

        if enriched > 0:
            print(f"Layer4.5 财务补全: {enriched}/{len(need_enrich)} 只")

    # ================================================================
    # Layer 5: 排序输出
    # ================================================================

    def _layer5_rank(self, stocks: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
        """综合打分排序：策略匹配 > 入场时机 > 催化剂 > 趋势强度 > 估值。"""
        for s in stocks:
            factor = s.get("factor", {})
            trend = s.get("trend", {})
            ret_20d = (trend.get("returns") or {}).get("return_20d") or 0
            quality = (factor.get("factor_scores") or {}).get("quality", 0)
            composite = factor.get("composite_score", 0)
            pe = s.get("pe") or 999
            entry = s.get("entry_score", 0.5)

            # 无催化全局降权
            cat_penalty = 0.08 if s["catalyst_score"] == 0 else 0.0

            # v2 因子加权排序：动量 50% + 周期 30% + 入场 10% + 催化 10%
            momentum = (factor.get("factor_scores") or {}).get("momentum", 0.5)
            cycle = s.get("cycle_score", 0.5)
            raw = (momentum * 0.50 + cycle * 0.30 +
                   entry * 0.10 + s["catalyst_score"] * 0.10)
            s["final_score"] = round(min(raw, 1.0), 3)

        stocks.sort(key=lambda x: x["final_score"], reverse=True)

        # 持仓去重 + 行业重叠
        portfolio_codes = self._get_portfolio_codes()
        portfolio_inds = self._get_portfolio_industries()
        for s in stocks:
            if s["code"] in portfolio_codes:
                s["in_portfolio"] = True
                s["final_score"] = round(s["final_score"] * 0.5, 3)  # 已持仓降权
            # 行业重叠检测
            ind_code = s.get("industry_code") or ""
            for pf_code, pf_name in portfolio_inds.items():
                if pf_code == ind_code:
                    s["industry_overlap"] = pf_name
                    break

        final = [s for s in stocks if not s.get("in_portfolio")][:top_n]
        return final

    def _get_portfolio_codes(self) -> set:
        try:
            from portfolio_strategy import PortfolioStrategy
            ps = PortfolioStrategy()
            return {p["code"] for p in (ps.data.get("positions") or [])}
        except Exception:
            return set()

    def _get_portfolio_industries(self) -> Dict[str, str]:
        """获取持仓行业映射 {industry_code: name}。"""
        try:
            result = {}
            for p in self.mongo.db[self.mongo.collections["basic_info"]].find(
                {"code": {"$in": list(self._get_portfolio_codes())}},
                {"code": 1, "name": 1, "industry_code": 1, "_id": 0}
            ):
                ic = p.get("industry_code", "")
                if ic:
                    result[ic] = p.get("name", "")
            return result
        except Exception:
            return {}

    # ================================================================
    # 辅助
    # ================================================================

    def _build_peer_stats(self, stocks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """行业 PE/PB 中位数（过滤极端值）。"""
        if self._hist:
            # 历史模式：从候选池中按行业聚合 PE/PB
            from collections import defaultdict
            ind_vals = defaultdict(lambda: {"pe": [], "pb": []})
            for s in stocks:
                ic = s.get("industry_code", "")
                if not ic:
                    continue
                pe = s.get("pe")
                pb = s.get("pb")
                if pe and 0 < pe < 500:
                    ind_vals[ic]["pe"].append(pe)
                if pb and 0 < pb < 50:
                    ind_vals[ic]["pb"].append(pb)
            stats = {}
            for ic, vals in ind_vals.items():
                pe_vals = sorted(vals["pe"])
                pb_vals = sorted(vals["pb"])
                if not pe_vals:
                    continue
                n = len(pe_vals); mid = n // 2
                stats[ic] = {
                    "pe_median": round(pe_vals[mid] if n % 2 else (pe_vals[mid-1] + pe_vals[mid]) / 2, 2),
                    "pb_median": round(pb_vals[mid] if n % 2 and len(pb_vals) > mid else (
                        (pb_vals[mid-1] + pb_vals[mid]) / 2 if len(pb_vals) > mid and mid > 0
                        else pb_vals[0] if pb_vals else None), 2) if pb_vals else None,
                }
            return stats

        ind_codes = set(s.get("industry_code", "") for s in stocks if s.get("industry_code"))
        stats = {}
        for ic in ind_codes:
            peers = list(self.mongo.db[self.mongo.collections["basic_info"]].find(
                {"industry_code": ic, "pe": {"$gt": 0, "$lt": 500}},
                {"pe": 1, "pb": 1, "_id": 0}))
            pe_vals = sorted(p["pe"] for p in peers if p.get("pe") and 0 < p["pe"] < 500)
            pb_vals = sorted(p["pb"] for p in peers if p.get("pb") and 0 < p["pb"] < 50)
            if not pe_vals:
                continue
            n = len(pe_vals); mid = n // 2
            stats[ic] = {
                "pe_median": round(pe_vals[mid] if n % 2 else (pe_vals[mid-1] + pe_vals[mid]) / 2, 2),
                "pb_median": round(pb_vals[mid] if n % 2 and len(pb_vals) > mid else (
                    (pb_vals[mid-1] + pb_vals[mid]) / 2 if len(pb_vals) > mid and mid > 0
                    else pb_vals[0] if pb_vals else None), 2) if pb_vals else None,
            }
        return stats

    # ================================================================
    # 主流程
    # ================================================================

    def run(self, top_n: int = 10, initial_limit: int = 500, enrich_limit: int = 200,
            industries: Optional[List[str]] = None,
            target_date: Optional[str] = None) -> Dict[str, Any]:
        # 解析 "today" → 实际日期
        if target_date == "today":
            target_date = _utc_now().strftime("%Y-%m-%d")
        date_label = target_date or "最新"
        print("=" * 60)
        print(f"Buy Plan — 五层漏斗 ({date_label})")
        if industries:
            print(f"  行业范围: {', '.join(industries)}")
        print("=" * 60)

        # Layer 0: 生存过滤（全市场，不抽样）
        candidates = self._broad_screen(industries=industries)
        if not candidates:
            return {"error": "初筛无结果", "candidates": []}

        # 市场宽度 + 牛熊状态
        breadth_pct = self._get_market_breadth(target_date)
        halt_pct = _get_buy_plan_config().get("market_breadth", {}).get("halt_threshold", 15)
        if breadth_pct < halt_pct:
            print(f"⚠ 市场宽度 {breadth_pct:.1f}% < {halt_pct}%，buy_plan 停摆（熊市不做多）")
            return {"error": f"市场宽度不足({breadth_pct:.1f}%)，暂停推荐",
                    "breadth_pct": breadth_pct}

        # 短周期市场状态 → 动态 alpha 权重
        regime_cfg = _get_buy_plan_config().get("market_regime", {})
        strong_line = regime_cfg.get("strong_breadth", 60)
        weak_line = regime_cfg.get("weak_breadth", 35)
        if breadth_pct >= strong_line:
            regime = "strong"
        elif breadth_pct >= weak_line:
            regime = "neutral"
        else:
            regime = "weak"
        alpha_w = regime_cfg.get("alpha_weights", {}).get(regime, {
            "momentum": 0.45, "cycle": 0.35, "turnaround": 0.20})
        weak_min_cycle = regime_cfg.get("weak_min_cycle", 0.50)
        print(f"市场宽度 {breadth_pct:.1f}% → {regime} 权重 "
              f"m={alpha_w['momentum']:.0%} c={alpha_w['cycle']:.0%} t={alpha_w['turnaround']:.0%}"
              + (f" 弱市保护 cycle>{weak_min_cycle:.0%}" if regime == "weak" else ""))

        # 补齐因子数据
        eff_limit = enrich_limit if enrich_limit > 0 else len(candidates)
        scored = self._enrich(candidates, limit=min(len(candidates), eff_limit),
                              target_date=target_date)

        # ── v3 候选池生成：因子评分 + 预过滤 + 排序 ──

        # 计算 alpha_score + 打标签
        passed = []
        for s in scored:
            factor = s.get("factor", {})
            momentum = (factor.get("factor_scores") or {}).get("momentum", 0.5)
            cycle = s.get("cycle_score", 0.5)
            turnaround = s.get("turnaround_score", 0.5)

            # alpha = 动量 + 周期 + 拐点（权重按牛熊动态调整）
            s["alpha_score"] = round(
                momentum * alpha_w["momentum"]
                + cycle * alpha_w["cycle"]
                + turnaround * alpha_w["turnaround"], 3)

            # 预过滤：行业极弱不做；弱势市场须 cycle 确认
            if cycle < 0.20:
                continue
            if regime == "weak" and cycle < weak_min_cycle:
                continue

            # 纯标签，不参与过滤
            s["strategy_tags"] = []
            s["tag_momentum"] = True
            s["strategy_tags"].append("动量突破")
            if cycle >= 0.20:
                s["tag_cycle"] = True
                s["strategy_tags"].append("周期共振")
            if turnaround >= 0.7:
                s["tag_turnaround"] = True
                s["strategy_tags"].append("低位拐点")
            s["strategy_count"] = len(s["strategy_tags"])
            passed.append(s)

        print(f"候选池: {len(scored)} → {len(passed)} 只 (cycle<0.20过滤)")

        # 排序输出
        passed.sort(key=lambda x: x["alpha_score"], reverse=True)
        # 已持仓降权
        portfolio_codes = self._get_portfolio_codes()
        for s in passed:
            if s["code"] in portfolio_codes:
                s["alpha_score"] = round(s["alpha_score"] * 0.5, 3)
        final = passed[:top_n]

        report = {
            "generated_at": _utc_now().isoformat(),
            "pipeline": {
                "candidates": len(candidates),
                "scored": len(scored),
                "passed": len(passed),
                "final": len(final),
            },
            "recommendations": final,
        }
        return report

    def _get_cycle_state(self) -> Dict[str, Any]:
        if self._hist:
            return {"styles": []}
        try:
            from portfolio_strategy import PortfolioStrategy
            return PortfolioStrategy().review().get("cycle_summary", {"styles": []})
        except Exception:
            return {"styles": []}


# ================================================================
# 风格-周期映射（供 Layer2 周期共振使用）
# ================================================================

def _map_style_strength(cycle_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """从 cycle_summary 提取风格→强度映射。"""
    result = {}
    for s in cycle_state.get("styles", []):
        result[s.get("style", "")] = {
            "score": s.get("score", 0),
            "strength": s.get("strength", "未判断"),
        }
    return result


def _best_matching_style(stock: Dict[str, Any],
                          style_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """找该票匹配的最佳周期强度（宽松匹配）。"""
    name = stock.get("name", "")
    ind = stock.get("industry", "")
    ind_code = stock.get("industry_code", "")
    for style, info in style_map.items():
        keywords = style_to_keywords(style)
        for kw in keywords:
            if kw in name or kw in ind or kw in ind_code:
                return info
    return None


def style_to_keywords(style: str) -> List[str]:
    maps = {
        "红利防御": ["红利", "防御", "电力", "铁路", "通信", "运营商", "能源", "石化"],
        "科技成长": ["科技", "成长", "电子", "半导体", "芯片", "AI", "软件"],
        "资源周期": ["有色", "资源", "周期", "钢铁", "煤炭", "化工"],
        "港股核心资产": ["互联网", "平台"],
    }
    return maps.get(style, [style])


# ================================================================
# 格式化输出
# ================================================================

def format_buy_plan(report: Dict[str, Any]) -> str:
    pipe = report.get("pipeline", {})
    lines = [
        "=" * 90,
        f"Buy Plan — 候选池 ({report.get('generated_at', '')})",
        f"候选 {pipe.get('candidates','?')} → 有因子 {pipe.get('scored','?')} → "
        f"通过 {pipe.get('passed','?')} → 最终 {pipe.get('final','?')}",
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
    parser = argparse.ArgumentParser(description="Buy Plan — 五层漏斗买入候选")
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
