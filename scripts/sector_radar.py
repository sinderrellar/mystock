#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业轮动雷达 — Sector Momentum & Rotation Detection

两个视角:
  View 1: 动量热力图 — 当前哪些行业在风口
  View 2: 轮动雷达 — 哪些冷门行业正在苏醒，可能是下一个轮动目标

用法:
  python3 sector_radar.py                       # 默认: 热力图 + 雷达
  python3 sector_radar.py --heatmap              # 仅热力图
  python3 sector_radar.py --radar                # 仅雷达
  python3 sector_radar.py --industry "半导体"     # 深入单行业
  python3 sector_radar.py --backfill             # 回填缺失行业分类
  python3 sector_radar.py --top 30 --json        # JSON 输出
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _get_radar_config() -> Dict[str, Any]:
    """从 config 读取 sector_radar 阈值，带默认值兜底。"""
    import yaml
    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return (cfg.get("pyramid_middle_layer", {}).get("sector_radar", {}))
    except Exception:
        return {}

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config


# ================================================================
# Utilities
# ================================================================

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _safe_mean(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    return sum(vals) / len(vals)


def _norm(v: float, lo: float, hi: float) -> float:
    """Min-max normalize to [0,1], clamping at boundaries."""
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


# ================================================================
# SectorRadarEngine
# ================================================================

class SectorRadarEngine:
    """行业轮动雷达引擎。"""

    MIN_STOCKS = 5  # 行业最少股票数才纳入分析

    def __init__(self):
        config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
        config = _load_mongodb_config(config_path)
        self.mongo = MongoFactorDataStore(config)
        self.config_path = config_path

    # ================================================================
    # Data Loading
    # ================================================================

    def load_all_industry_data(self, as_of_date: Optional[str] = None) -> Tuple[List[Dict], List[Dict], Dict]:
        """从 v3 三张表加载行业数据。

        stock_meta → 基础信息(代码/名称/行业/PE/PB/市值/成交额)
        stock_trends → 趋势(RSI/KDJ/MACD/MA/量价)
        stock_factors → 因子(按归属组取 factor_scores)
        """
        trends_coll = self.mongo.db["stock_trends"]
        factors_coll = self.mongo.db["stock_factors"]

        # 最新可用日期（trends 覆盖最少，以它为准）
        latest_date = as_of_date
        if not latest_date:
            trends_dates = sorted(trends_coll.distinct("trade_date"), reverse=True)
            if trends_dates:
                latest_date = trends_dates[0]
            else:
                return [], [], {"error": "stock_trends 无数据"}

        # ── 1. 加载 basic_info（元数据层，日级不变）──
        industry_map = defaultdict(list)
        null_industry = 0
        for doc in self.mongo.db[self.mongo.collections["basic_info"]].find(
            {"display_market": "A股"},
            {"code": 1, "name": 1, "close": 1, "pe": 1, "pb": 1,
             "total_mv": 1, "latest_amount": 1, "turnover_rate": 1,
             "industry": 1, "_id": 0}
        ):
            ind = (doc.get("industry") or "").strip()
            if not ind:
                null_industry += 1
                continue
            industry_map[ind].append(doc)

        all_codes = []
        for stocks in industry_map.values():
            all_codes.extend(s["code"] for s in stocks)

        # ── 2. 批量加载 stock_trends（最新日期）──
        trend_docs = {}
        for doc in trends_coll.find(
            {"code": {"$in": all_codes}, "trade_date": latest_date},
            {"code": 1, "available": 1, "status": 1, "ma": 1, "returns": 1,
             "volatility_20d": 1, "rsi14": 1, "kdj": 1, "macd": 1,
             "boll_percent_b": 1, "volume_price_signal": 1, "_id": 0}
        ):
            trend_docs[doc["code"]] = doc

        # ── 3. 批量加载 stock_factors（最新日期，只取归属组的因子分）──
        factor_docs = {}
        for doc in factors_coll.find(
            {"code": {"$in": all_codes}, "trade_date": latest_date},
            {"code": 1, "group": 1, "factors": 1, "pe_percentile": 1, "_id": 0}
        ):
            group = doc.get("group", "")
            factors = doc.get("factors", {})
            group_factor = factors.get(group, {})
            factor_docs[doc["code"]] = {
                "composite_score": group_factor.get("composite_score", 0),
                "factor_scores": group_factor.get("factor_scores", {}),
            }

        # ── 4. 组装 ──
        result = []
        total_with_signal = 0
        total_stocks = 0
        stale_count = 0

        for ind_name, stocks in sorted(industry_map.items()):
            if len(stocks) < self.MIN_STOCKS:
                continue
            total_stocks += len(stocks)

            enriched = []
            for s in stocks:
                code = s["code"]
                trend = trend_docs.get(code)
                factor = factor_docs.get(code)
                if trend and trend.get("available"):
                    # 组装 signal 结构（兼容旧格式）
                    signal = {
                        "trend": trend,
                        "factor": factor or {"composite_score": 0, "factor_scores": {}},
                        "pe_percentile": s.get("pe_percentile"),
                        "forecast_min": s.get("forecast_min"),
                        "forecast_max": s.get("forecast_max"),
                        "moneyflow_net": s.get("moneyflow_net"),
                    }
                    enriched.append({**s, "signal": signal})
                elif trend:
                    stale_count += 1

            if len(enriched) < self.MIN_STOCKS:
                continue

            total_with_signal += len(enriched)
            result.append({
                "industry": ind_name,
                "stock_count_raw": len(stocks),
                "stock_count": len(enriched),
                "stocks": enriched,
            })

        stale_warnings = []
        if latest_date < (_utc_now() - timedelta(days=2)).strftime("%Y-%m-%d"):
            stale_warnings.append(f"信号缓存最新日期 {latest_date}，已超过2天，建议运行 precompute_history.py --date today")

        summary = {
            "total_industries_with_data": len(result),
            "total_industries_raw": len(industry_map),
            "total_stocks_with_signal": total_with_signal,
            "total_stocks": total_stocks,
            "null_industry_stocks": null_industry,
            "stale_stocks": stale_count,
            "signal_date": latest_date,
        }

        return result, stale_warnings, summary

    # ================================================================
    # Per-Industry Metrics
    # ================================================================

    def _compute_industry_metrics(self, industry_data: Dict) -> Dict[str, Any]:
        """Compute all aggregate metrics for one industry."""
        stocks = industry_data["stocks"]
        n = len(stocks)

        # Collect per-stock values
        close_vals = []
        pe_vals = []
        rsi_vals = []
        ret5_vals = []
        ret20_vals = []
        ret60_vals = []
        turnover_vals = []
        mv_vals = []
        amount_vals = []
        above_ma20 = 0
        above_ma60 = 0
        volume_bullish = 0
        volume_bearish = 0
        ma20_dists = []
        kdj_j_vals = []

        # For wake-up: track top-quartile metrics
        stock_ret20 = []  # (code, name, close, ret_20d, above_ma20, rsi)

        for s in stocks:
            sig = s.get("signal", {})
            trend = sig.get("trend", {})
            ma = trend.get("ma", {}) or {}
            rets = trend.get("returns", {}) or {}
            close = _to_float(s.get("close"), 0)

            if close <= 0:
                continue

            close_vals.append(close)
            ma20 = _to_float(ma.get("ma20"), 0)
            ma60 = _to_float(ma.get("ma60"), 0)
            rsi = _to_float(trend.get("rsi14"), None)
            ret5 = _to_float(rets.get("return_5d"), None)
            ret20 = _to_float(rets.get("return_20d"), None)
            ret60 = _to_float(rets.get("return_60d"), None)
            kdj = trend.get("kdj", {}) or {}
            kdj_j = _to_float(kdj.get("j"), None)

            if ma20 > 0:
                if close > ma20:
                    above_ma20 += 1
                ma20_dists.append((close - ma20) / ma20)
            if ma60 > 0 and close > ma60:
                above_ma60 += 1
            if rsi is not None:
                rsi_vals.append(rsi)
            if kdj_j is not None:
                kdj_j_vals.append(kdj_j)
            if ret5 is not None:
                ret5_vals.append(ret5)
            if ret20 is not None:
                ret20_vals.append(ret20)
            if ret60 is not None:
                ret60_vals.append(ret60)

            pe = _to_float(s.get("pe"), None)
            if pe is not None and 0 < pe < 500:
                pe_vals.append(pe)

            to = _to_float(s.get("turnover_rate"), None)
            if to is not None:
                turnover_vals.append(to)

            mv = _to_float(s.get("total_mv"), 0)
            if mv > 0:
                mv_vals.append(mv)

            amt = _to_float(s.get("latest_amount"), 0)
            if amt > 0:
                amount_vals.append(amt)

            vp = trend.get("volume_price_signal", "")
            if "放量" in str(vp) and ("涨" in str(vp) or "止跌" in str(vp)):
                volume_bullish += 1
            elif "放量" in str(vp):
                volume_bearish += 1

            stock_ret20.append({
                "code": s["code"],
                "name": s.get("name", ""),
                "close": close,
                "ret_20d": ret20 if ret20 is not None else -99,
                "ret_5d": ret5 if ret5 is not None else -99,
                "above_ma20": (close > ma20) if ma20 > 0 else False,
                "rsi": rsi,
                "kdj_j": kdj_j,
            })

        n_valid = len(close_vals)
        if n_valid == 0:
            return {"available": False, "industry": industry_data["industry"]}

        # Sort by ret_20d for leader detection
        stock_ret20.sort(key=lambda x: x["ret_20d"], reverse=True)
        top_q = max(1, n_valid // 4)
        leaders = stock_ret20[:top_q]
        laggards = stock_ret20[-top_q:]

        breadth_pct = (above_ma20 / n_valid * 100) if n_valid > 0 else 0
        breadth_ma60_pct = (above_ma60 / n_valid * 100) if n_valid > 0 else 0

        avg_ret5 = _safe_mean(ret5_vals)
        avg_ret20 = _safe_mean(ret20_vals)
        avg_ret60 = _safe_mean(ret60_vals)
        avg_rsi = _safe_mean(rsi_vals)
        avg_turnover = _safe_mean(turnover_vals)
        avg_ma20_dist = _safe_mean(ma20_dists) if ma20_dists else None
        pe_median = _safe_median(pe_vals)
        total_mv = sum(mv_vals)
        total_amount = sum(amount_vals)

        # RSI distribution
        rsi_oversold = sum(1 for r in rsi_vals if r < 30)
        rsi_weak = sum(1 for r in rsi_vals if 30 <= r < 40)
        rsi_neutral = sum(1 for r in rsi_vals if 40 <= r < 60)
        rsi_strong = sum(1 for r in rsi_vals if 60 <= r < 70)
        rsi_overbought = sum(1 for r in rsi_vals if r >= 70)

        # Leader metrics
        leader_above_ma20 = sum(1 for l in leaders if l["above_ma20"])
        leader_ret20_vals = [l["ret_20d"] for l in leaders if l["ret_20d"] > -99]
        leader_avg_ret20 = _safe_mean(leader_ret20_vals) if leader_ret20_vals else 0

        # Recovery signal: stocks where ret_5d > ret_20d (short-term improving)
        improving = sum(
            1 for s in stock_ret20
            if s["ret_5d"] > -99 and s["ret_20d"] > -99 and s["ret_5d"] > s["ret_20d"]
        )

        # RSI in recovery zone (30-50): oversold but potentially turning
        rsi_recovery = sum(1 for r in rsi_vals if 30 <= r <= 50)

        # Depth from peak: median decline for stocks in drawdown (超跌深度)
        depth_candidates = [s["ret_20d"] for s in stock_ret20
                           if s["ret_20d"] is not None and s["ret_20d"] > -99
                           and s["ret_20d"] < -3]
        depth_from_peak = round(abs(_safe_median(depth_candidates)), 1) if depth_candidates else 0

        # Leader lead: how much leaders outpace sector average (拐点先行信号)
        leader_lead = round(leader_avg_ret20 - avg_ret20, 1) \
            if leader_avg_ret20 is not None and avg_ret20 is not None else 0

        return {
            "available": True,
            "industry": industry_data["industry"],
            "stock_count": n_valid,
            "stock_count_raw": industry_data["stock_count_raw"],
            # Breadth
            "breadth_pct": round(breadth_pct, 1),
            "breadth_ma60_pct": round(breadth_ma60_pct, 1),
            # Returns
            "avg_ret_5d": round(avg_ret5, 2) if avg_ret5 is not None else None,
            "avg_ret_20d": round(avg_ret20, 2) if avg_ret20 is not None else None,
            "avg_ret_60d": round(avg_ret60, 2) if avg_ret60 is not None else None,
            # Technical
            "avg_rsi": round(avg_rsi, 1) if avg_rsi is not None else None,
            "avg_turnover": round(avg_turnover, 3) if avg_turnover is not None else None,
            "avg_ma20_dist_pct": round(avg_ma20_dist * 100, 1) if avg_ma20_dist is not None else None,
            # Valuation
            "pe_median": round(pe_median, 1) if pe_median is not None else None,
            "total_mv": total_mv,
            "total_amount": total_amount,
            # Distribution
            "rsi_dist": {
                "oversold": rsi_oversold, "weak": rsi_weak,
                "neutral": rsi_neutral, "strong": rsi_strong,
                "overbought": rsi_overbought,
            },
            # Volume signals
            "volume_bullish_pct": round(volume_bullish / n_valid * 100, 1) if n_valid > 0 else 0,
            # Recovery
            "improving_pct": round(improving / n_valid * 100, 1) if n_valid > 0 else 0,
            "rsi_recovery_pct": round(rsi_recovery / n_valid * 100, 1) if n_valid > 0 else 0,
            # Leaders
            "leader_above_ma20_pct": round(leader_above_ma20 / top_q * 100, 1) if top_q > 0 else 0,
            "leader_avg_ret_20d": round(leader_avg_ret20, 2) if leader_avg_ret20 is not None else None,
            # Leader lead + Depth from peak (computed above)
            "leader_lead_pct": leader_lead,
            "depth_from_peak_pct": depth_from_peak,
            # Raw data for drill-down
            "_leaders": leaders,
            "_laggards": laggards,
            "_all_stocks": stock_ret20,
            "_top_q_size": top_q,
            "_n_valid": n_valid,
        }

    # ================================================================
    # View 1: Heatmap
    # ================================================================

    def compute_heatmap(self, all_metrics: List[Dict]) -> List[Dict]:
        """Compute momentum scores and rank industries."""
        if not all_metrics:
            return []

        # Collect raw values for normalization
        breadth_vals = [m["breadth_pct"] for m in all_metrics]
        ret20_vals = [m["avg_ret_20d"] for m in all_metrics if m["avg_ret_20d"] is not None]
        rsi_vals = [m["avg_rsi"] for m in all_metrics if m["avg_rsi"] is not None]
        turnover_vals = [m["avg_turnover"] for m in all_metrics if m["avg_turnover"] is not None]
        ret5_vals = [m["avg_ret_5d"] for m in all_metrics if m["avg_ret_5d"] is not None]

        if not ret20_vals:
            return all_metrics

        lo_b, hi_b = min(breadth_vals, default=0), max(breadth_vals, default=100)
        lo_r20, hi_r20 = min(ret20_vals, default=-20), max(ret20_vals, default=20)
        lo_r5, hi_r5 = (min(ret5_vals, default=-10), max(ret5_vals, default=10)) if ret5_vals else (-10, 10)
        lo_rsi, hi_rsi = (min(rsi_vals, default=30), max(rsi_vals, default=70)) if rsi_vals else (30, 70)
        lo_to, hi_to = (min(turnover_vals, default=0), max(turnover_vals, default=5)) if turnover_vals else (0, 5)

        for m in all_metrics:
            b = _norm(m["breadth_pct"], lo_b, hi_b) if hi_b > lo_b else 0.5
            r20 = _norm(m["avg_ret_20d"] or 0, lo_r20, hi_r20) if hi_r20 > lo_r20 else 0.5
            rsi = _norm(m["avg_rsi"] or 50, lo_rsi, hi_rsi) if hi_rsi > lo_rsi else 0.5
            to = _norm(m["avg_turnover"] or 0, lo_to, hi_to) if hi_to > lo_to else 0.5
            r5 = _norm(m["avg_ret_5d"] or 0, lo_r5, hi_r5) if hi_r5 > lo_r5 else 0.5

            momentum_score = (
                b * 0.25 + r20 * 0.30 + rsi * 0.20 + to * 0.15 + r5 * 0.10
            )
            m["momentum_score"] = round(momentum_score, 3)

            hot_pct = _get_radar_config().get("momentum", {}).get("hot", 0.7)
            warm_pct = _get_radar_config().get("momentum", {}).get("warm", 0.4)
            if momentum_score >= hot_pct:
                m["status"] = "🔥领先"
            elif momentum_score >= warm_pct:
                m["status"] = "📈改善"
            else:
                m["status"] = "❄️滞后"

        all_metrics.sort(key=lambda x: x.get("momentum_score", 0), reverse=True)
        return all_metrics

    # ================================================================
    # View 2: Wake-up Radar
    # ================================================================

    def _check_volume_divergence(self, industry_metrics: Dict) -> float:
        """检测行业量价背离：放量滞涨 = 主力吸筹信号。

        取样行业内市值前 5 的股票，对比近 20 日 vs 前 20 日均量。
        Returns: divergence_score [0, 1], 0=无背离, 1=强背离。
        """
        stocks = industry_metrics.get("_all_stocks", [])
        if not stocks:
            return 0.0

        # 取 top 5 市值龙头（按 close 近似，_all_stocks 无 mv 字段，用 ret_20d 排序取代表）
        leaders = [s for s in stocks if s.get("ret_20d") is not None and s["ret_20d"] > -99][:20]
        if len(leaders) < 5:
            return 0.0

        codes = [s["code"] for s in leaders[:10]]
        daily = self.mongo.db[self.mongo.collections["daily_quotes"]]

        # 取最近 40 个交易日的量
        cutoff = (datetime.now(timezone.utc) - timedelta(days=80)).strftime("%Y-%m-%d")
        vols = defaultdict(list)
        for doc in daily.find(
            {"code": {"$in": codes}, "trade_date": {"$gte": cutoff}},
            {"code": 1, "trade_date": 1, "volume": 1}
        ).sort("trade_date", 1):
            vols[doc["code"]].append((doc["trade_date"], doc.get("volume", 0)))

        # 计算每只股票的 20 日量比
        ratios = []
        for code, data in vols.items():
            if len(data) < 30:
                continue
            # 最近 20 日 vs 前 20 日
            recent = [v for _, v in data[-20:] if v > 0]
            prior = [v for _, v in data[-40:-20] if v > 0]
            if len(recent) < 10 or len(prior) < 10:
                continue
            avg_recent = sum(recent) / len(recent)
            avg_prior = sum(prior) / len(prior)
            if avg_prior > 0:
                ratios.append(avg_recent / avg_prior)

        if not ratios:
            return 0.0

        vol_ratio = _safe_median(ratios) or 0

        # 量 > 1.2x 且价格稳定 → 放量滞涨
        avg_ret20 = industry_metrics.get("avg_ret_20d") or 0
        price_stable = abs(avg_ret20) < 3  # 20d 涨跌幅 < 3%

        if vol_ratio >= 1.5 and price_stable:
            return 1.0   # 强吸筹
        elif vol_ratio >= 1.2 and price_stable:
            return 0.7   # 吸筹中
        elif vol_ratio >= 1.1:
            return 0.4   # 轻度放量
        elif vol_ratio >= 0.9:
            return 0.2
        return 0.0

    def compute_radar(self, all_metrics: List[Dict]) -> List[Dict]:
        """Compute wake-up scores for cold/transitioning industries.

        2026-06-15 增强：新增量价背离(F)、超跌深度融入洗盘(A)、龙头先行强化(C)。
        检测范围从 breadth<40% 扩展到 <60% 以捕获放量吸筹的过渡板块。
        """
        # Consider industries with breadth < 60% (expanded from 40% to catch
        # transitioning sectors with volume-price divergence)
        cold = [m for m in all_metrics if m["breadth_pct"] < 60]
        if not cold:
            return []

        # Need top-industry 20d return for divergence score
        top_ret20 = max(
            (m["avg_ret_20d"] for m in all_metrics if m["avg_ret_20d"] is not None),
            default=0
        )

        # Market PE median for valuation
        all_pe = [m["pe_median"] for m in all_metrics
                  if m.get("pe_median") is not None and 0 < m["pe_median"] < 500]
        market_pe_med = _safe_median(all_pe) if all_pe else 30

        for m in cold:
            # A. Washout depth (15%) — breadth + ret_20d + depth_from_peak
            breadth_factor = 1.0 - m["breadth_pct"] / 60.0  # 放宽基准到60%
            ret = m["avg_ret_20d"] if m["avg_ret_20d"] is not None else 0
            ret_factor = min(max(-ret / 15.0, 0), 1.0) if ret < 0 else 0
            # 超跌深度：回撤 > 15% 满分
            depth = m.get("depth_from_peak_pct", 0)
            depth_factor = min(depth / 15.0, 1.0) if depth > 3 else 0
            washout = breadth_factor * 0.30 + ret_factor * 0.30 + depth_factor * 0.40

            # B. Reversal signals (25%) — RSI recovery + ST improvement + vol signals
            rsi_rec = m.get("rsi_recovery_pct", 0) / 100.0
            ret5 = m["avg_ret_5d"] if m["avg_ret_5d"] is not None else ret
            st_improve = min(max((ret5 - ret) / 15.0, 0), 1.0) if ret5 is not None else 0
            vol_bull = m.get("volume_bullish_pct", 0) / 100.0
            reversal = rsi_rec * 0.40 + st_improve * 0.35 + vol_bull * 0.25

            # C. Leader emergence (25%) — MA20 + return + lead_over_sector
            leader_above = m.get("leader_above_ma20_pct", 0) / 100.0
            leader_ret = m.get("leader_avg_ret_20d") or 0
            leader_ret_factor = min(max(leader_ret / 10.0, 0), 1.0)
            # 龙头领先板块幅度：>5% 即先行信号
            leader_lead = m.get("leader_lead_pct", 0)
            leader_lead_factor = min(max(leader_lead / 5.0, 0), 1.0) if leader_lead > 0 else 0
            leader = (leader_above * 0.35 + leader_ret_factor * 0.35 +
                      leader_lead_factor * 0.30)

            # D. Valuation support (10%)
            pe = m.get("pe_median")
            if pe is not None and pe > 0 and market_pe_med > 0:
                ratio = pe / market_pe_med
                if ratio < 0.7:
                    val = 1.0
                elif ratio < 1.0:
                    val = 0.7
                elif ratio < 1.3:
                    val = 0.4
                else:
                    val = 0.1
            else:
                val = 0.5

            # E. Divergence from hot sectors (10%)
            div = min(max((top_ret20 - ret) / 30.0, 0), 1.0) if ret is not None else 0.5

            # F. Volume-price divergence (15%) — 放量滞涨 = 吸筹
            vol_div = self._check_volume_divergence(m)

            wake_up = (
                washout * 0.15 + reversal * 0.25 + leader * 0.25 +
                val * 0.10 + div * 0.10 + vol_div * 0.15
            )
            m["wake_up_score"] = round(wake_up, 3)
            m["wake_up_detail"] = {
                "washout": round(washout, 2),
                "reversal": round(reversal, 2),
                "leader": round(leader, 2),
                "valuation": round(val, 2),
                "divergence": round(div, 2),
                "volume_divergence": round(vol_div, 2),
            }

            if wake_up >= 0.5:
                m["wake_label"] = "⚡关注"
            elif wake_up >= 0.4:
                m["wake_label"] = "👀观察"
            else:
                m["wake_label"] = "—"

        # Filter to only those worth showing (wake_up >= 0.35)
        radar = [m for m in cold if m["wake_up_score"] >= 0.35]
        radar.sort(key=lambda x: x["wake_up_score"], reverse=True)
        return radar

    # ================================================================
    # 拐点探测器 — 专门找"冻死→解冻"的行业拐点
    # ================================================================

    def _check_industry_moneyflow(self, industry_name: str, target_date: Optional[str] = None) -> float:
        """查询行业最近 3 个交易日累计主力净流入（亿元）。

        Returns: net_amount 亿元, 0=无数据/无流入
        """
        coll = self.mongo.db.get_collection("industry_moneyflow")
        if coll is None:
            return 0.0
        try:
            dates = sorted(coll.distinct("trade_date"), reverse=True)[:3]
            if not dates:
                return 0.0
            total = 0.0
            for d in dates:
                doc = coll.find_one({"industry_name": industry_name, "trade_date": d})
                if doc:
                    total += doc.get("net_amount", 0) or 0
            return total / 10000  # 万 → 亿
        except Exception:
            return 0.0

    def compute_inflection(self, all_metrics: List[Dict]) -> List[Dict]:
        """探测行业拐点：三个信号 + 肌肉记忆前提。

        三信号（同时命中 >=2 → 拐点候选）：
          ① 深度：60日高点回撤 > 15%（冻死了）
          ② 主力资金：行业 3 日累计净流入 > 0（有人在买），
             若 moneyflow 数据不可用则 fallback 到量价背离
          ③ 龙头先行：龙头 20日涨幅 > 行业均值 5%以上（有人先动了）
          ③ 龙头先行：龙头 20日涨幅 > 行业均值 5%以上（有人先动了）

        肌肉记忆前提：该行业过去6个月曾进入动量Top10（60日收益 > 全行业中位数）
        """
        median_ret60 = _safe_median(
            [m["avg_ret_60d"] for m in all_metrics if m.get("avg_ret_60d") is not None]) or 0

        candidates = []
        for m in all_metrics:
            # ── 前提：肌肉记忆（过去6个月有过强势表现）──
            ret60 = m.get("avg_ret_60d")
            has_memory = ret60 is not None and ret60 > median_ret60

            # ── 信号① 深度 ──
            depth = m.get("depth_from_peak_pct", 0)
            signal_depth = depth >= 15

            # ── 信号② 主力资金（优先）或量价背离（fallback）──
            mf_net = self._check_industry_moneyflow(m["industry"])
            vol_div = 0.0
            if abs(mf_net) > 0.01:  # 有资金流数据
                signal_money = mf_net > 0  # 主力 3 日累计净流入 > 0
                mf_str = f"主力 3日 +{mf_net:.1f}亿"
            else:  # 无资金流数据，fallback 到量价背离
                vol_div = self._check_volume_divergence(m)
                signal_money = vol_div >= 0.4
                mf_str = f"量价背离 {vol_div:.1f}{' ✓' if vol_div >= 0.4 else ''}"

            # ── 信号③ 龙头先行 ──
            lead = m.get("leader_lead_pct", 0)
            signal_leader = lead >= 5  # 龙头领先行业均值 5%以上

            signals_hit = sum([signal_depth, signal_money, signal_leader])

            if signals_hit >= 1:  # 至少亮一个灯才进入候选
                # 肌肉记忆生效时加分
                inflection_score = (signals_hit / 3) * (1.2 if has_memory else 0.8)

                m["inflection_score"] = round(inflection_score, 3)
                m["inflection_signals"] = {
                    "depth": ("🟢" if signal_depth else "🔴",
                              f"回撤 {depth}%{' ≥ 15%' if signal_depth else ' < 15%'}"),
                    "moneyflow": ("🟢" if signal_money else "🔴", mf_str),
                    "leader_lead": ("🟢" if signal_leader else "🔴",
                                    f"龙头领先 {lead}%{' ≥ 5%' if signal_leader else ' < 5%'}"),
                    "muscle_memory": ("🟢" if has_memory else "🔴",
                                      f"60日收益 {ret60:.1f}%{' > 中位数' if has_memory else ' ≤ 中位数'}"),
                }
                m["inflection_hits"] = signals_hit
                candidates.append(m)

        candidates.sort(key=lambda x: (x["inflection_hits"], x["inflection_score"]), reverse=True)
        return candidates

    # ================================================================
    # buy_plan 接口：热力行业 + 拐点行业 + 资金流
    # ================================================================

    def get_hot_sectors(self, top_n: int = 10,
                        target_date: Optional[str] = None) -> List[str]:
        """热力图 Top N 行业名列表，供 buy_plan 动量突破策略使用。"""
        groups, _, _ = self.load_all_industry_data(as_of_date=target_date)
        metrics = [self._compute_industry_metrics(g) for g in groups]
        valid = [m for m in metrics if m.get("available")]
        return [m["industry"] for m in self.compute_heatmap(valid)[:top_n]]

    def get_inflection_map(self,
                           target_date: Optional[str] = None) -> Dict[str, Dict]:
        """拐点行业映射 {行业: {hits, signals, score}}，供 低位拐点策略。"""
        groups, _, _ = self.load_all_industry_data(as_of_date=target_date)
        metrics = [self._compute_industry_metrics(g) for g in groups]
        valid = [m for m in metrics if m.get("available")]
        return {
            m["industry"]: {
                "hits": m.get("inflection_hits", 0),
                "signals": m.get("inflection_signals", {}),
                "score": m.get("inflection_score", 0),
            }
            for m in self.compute_inflection(valid)
        }

    def get_sector_status_map(self,
                              target_date: Optional[str] = None
                              ) -> Dict[str, str]:
        """行业状态映射 {行业: 🔥/📈/❄️}，供 cycle 策略判断广度方向。"""
        groups, _, _ = self.load_all_industry_data(as_of_date=target_date)
        metrics = [self._compute_industry_metrics(g) for g in groups]
        valid = [m for m in metrics if m.get("available")]
        result = {}
        for m in self.compute_heatmap(valid):
            result[m["industry"]] = m.get("status", "❄️滞后")
        return result

    def get_sector_moneyflow(self, industry: str,
                             target_date: Optional[str] = None) -> float:
        """行业 3 日主力净流入（亿元）。"""
        return self._check_industry_moneyflow(industry, target_date)

    def get_cycle_scores(self, target_date: Optional[str] = None
                         ) -> Dict[str, Dict[str, float]]:
        """行业周期得分 {行业: {trend, breadth, flow, cycle}}.

        trend    — 0.5×Rank(20d) + 0.3×Rank(60d) + 0.2×Rank(120d)
        breadth  — 0.7×Rank(站上MA20占比) + 0.3×Rank(广度变化)
        flow     — Rank(净流入/流通市值)
        cycle    — 0.45×trend + 0.35×breadth + 0.20×flow
        """
        groups, _, _ = self.load_all_industry_data(as_of_date=target_date)
        metrics = [self._compute_industry_metrics(g) for g in groups]
        valid = [m for m in metrics if m.get("available")]

        # 收集各行业原始值
        inds = []
        r20_vals, r60_vals, r120_vals = [], [], []
        breadth_vals, breadth_chg_vals = [], []
        flow_vals = []

        for m in valid:
            inds.append(m["industry"])
            r20_vals.append(m.get("avg_ret_20d", 0) or 0)
            r60_vals.append(m.get("avg_ret_60d", 0) or 0)
            r120_vals.append(m.get("avg_ret_120d", 0) or 0)
            breadth_vals.append(m.get("breadth_pct", 0) or 0)
            breadth_chg_vals.append(m.get("breadth_ma60_pct", 0) or 0)
            flow_vals.append(self._check_industry_moneyflow(m["industry"], target_date))

        # 百分位排名
        def _rank(val, arr):
            if not arr: return 0.5
            import bisect
            sorted_arr = sorted(arr)
            return bisect.bisect_left(sorted_arr, val) / len(arr)

        result = {}
        for i, ind in enumerate(inds):
            trend = 0.5*_rank(r20_vals[i], r20_vals) \
                  + 0.3*_rank(r60_vals[i], r60_vals) \
                  + 0.2*_rank(r120_vals[i], r120_vals)
            breadth = 0.7*_rank(breadth_vals[i], breadth_vals) \
                    + 0.3*_rank(breadth_chg_vals[i], breadth_chg_vals)
            flow = _rank(flow_vals[i], flow_vals)
            cycle = 0.45*trend + 0.35*breadth + 0.20*flow

            result[ind] = {
                "trend": round(trend, 3),
                "breadth": round(breadth, 3),
                "flow": round(flow, 3),
                "cycle": round(cycle, 3),
            }
        return result

    # ================================================================
    # Drill-down
    # ================================================================

    def drill_down_industry(self, industry_name: str) -> Optional[Dict]:
        """Detailed analysis for a single industry."""
        groups, _, _ = self.load_all_industry_data()
        target = None
        for g in groups:
            if g["industry"] == industry_name:
                target = g
                break

        if not target:
            return None

        metrics = self._compute_industry_metrics(target)
        if not metrics.get("available"):
            return None

        # Also compute wake-up score if cold
        all_metrics = [self._compute_industry_metrics(g) for g in groups]
        all_valid = [m for m in all_metrics if m.get("available")]
        heatmap = self.compute_heatmap(all_valid)
        if metrics["breadth_pct"] < 40:
            radar = self.compute_radar(all_valid)
            for r in radar:
                if r["industry"] == industry_name:
                    metrics["wake_up_score"] = r.get("wake_up_score")
                    metrics["wake_up_detail"] = r.get("wake_up_detail")
                    metrics["wake_label"] = r.get("wake_label")
                    break

        return metrics

    # ================================================================
    # Backfill null industries
    # ================================================================

    def backfill_null_industries(self) -> Dict[str, Any]:
        """Fix ~816 stocks with null industry using tushare stock_basic."""
        try:
            import tushare as ts
        except ImportError:
            return {"ok": False, "error": "tushare 未安装"}

        # Load token from config
        try:
            import yaml
            with open(self.config_path) as f:
                cfg = yaml.safe_load(f)
            token = cfg.get("data_sources", {}).get("tushare", {}).get("token", "")
            if not token:
                return {"ok": False, "error": "tushare token 未配置"}
        except Exception as e:
            return {"ok": False, "error": f"读取配置失败: {e}"}

        pro = ts.pro_api(token)

        # Get full industry mapping from tushare
        print("获取 tushare 行业分类...")
        try:
            df = pro.stock_basic(
                exchange='', list_status='L',
                fields='ts_code,symbol,name,industry'
            )
            # Build code -> industry map (strip .SZ/.SH suffix)
            ind_map = {}
            for _, row in df.iterrows():
                code = str(row["ts_code"]).split(".")[0]
                ind_map[code] = str(row.get("industry", "") or "")
            print(f"  获取到 {len(ind_map)} 只股票的行业映射")
        except Exception as e:
            return {"ok": False, "error": f"tushare stock_basic 失败: {e}"}

        # Find null-industry stocks
        null_stocks = list(self.mongo.db[self.mongo.collections["basic_info"]].find(
            {"$or": [
                {"industry": None},
                {"industry": ""},
                {"industry": {"$exists": False}},
            ], "display_market": "A股"},
            {"code": 1, "name": 1, "_id": 0}
        ))
        print(f"  发现 {len(null_stocks)} 只 industry 缺失的股票")

        backfilled = 0
        for i, s in enumerate(null_stocks):
            code = s["code"]
            ind = ind_map.get(code, "")
            if ind:
                self.mongo.db[self.mongo.collections["basic_info"]].update_one(
                    {"code": code},
                    {"$set": {"industry": ind}}
                )
                backfilled += 1
            if (i + 1) % 200 == 0:
                print(f"  进度: {i+1}/{len(null_stocks)}")
                time.sleep(0.5)

        return {
            "ok": True,
            "total_null": len(null_stocks),
            "backfilled": backfilled,
            "remaining": len(null_stocks) - backfilled,
        }

    # ================================================================
    # Main run
    # ================================================================

    def run(self, show_heatmap: bool = True, show_radar: bool = True,
            show_inflection: bool = False,
            top_n: int = 20, industry: Optional[str] = None) -> Dict[str, Any]:
        """Main execution."""
        # Load data
        groups, warnings, summary = self.load_all_industry_data()
        if not groups:
            return {
                "error": "无可用行业数据，请先运行 precompute_history.py --date today",
                "summary": summary,
                "warnings": warnings,
            }

        # 信号过期超过5天 → 拒绝执行（不给假数据比给过期数据好）
        signal_date = summary.get("signal_date", "")
        if signal_date:
            age = (datetime.now(timezone.utc) - datetime.strptime(signal_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
            if age > 90:  # TODO: 改回 5，等 trends 回填完成
                return {
                    "error": f"信号缓存过期 {age} 天（最新 {signal_date}），请先运行 precompute_history.py --date today 后再执行",
                    "summary": summary,
                    "warnings": warnings,
                }

        # Compute metrics for all industries
        all_metrics = [self._compute_industry_metrics(g) for g in groups]
        all_valid = [m for m in all_metrics if m.get("available")]

        result = {"summary": summary, "warnings": warnings}

        # View 1: Heatmap
        heatmap = []
        if show_heatmap:
            heatmap = self.compute_heatmap(all_valid)[:top_n]
            result["heatmap"] = heatmap

        # View 2: Radar
        radar = []
        if show_radar:
            radar = self.compute_radar(all_valid)[:top_n]
            result["radar"] = radar

        # View 3: Inflection (拐点探测器)
        inflection = []
        if show_inflection:
            inflection = self.compute_inflection(all_valid)[:top_n]
            result["inflection"] = inflection

        # Drill-down
        if industry:
            detail = self.drill_down_industry(industry)
            if detail:
                result["industry_detail"] = detail
            else:
                result["industry_detail"] = {"error": f"行业 '{industry}' 未找到或无可用数据"}

        return result


# ================================================================
# Formatters
# ================================================================

def _fmt(v: Any, suffix: str = "", decimals: int = 1) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{decimals}f}{suffix}"
    return f"{v}{suffix}"


def _status_icon(m: Dict) -> str:
    s = m.get("status", "")
    if "领先" in str(s):
        return "🔥"
    if "改善" in str(s):
        return "📈"
    return "❄️"


def format_heatmap(industries: List[Dict], top_n: int) -> str:
    if not industries:
        return "无行业数据"

    lines = [
        "=" * 110,
        f"  行业动量热力图  (前{min(len(industries), top_n)}个行业)",
        "=" * 110,
        f"{'排名':>3} {'行业':<10} {'状态':<5} {'广度%':>6} {'5日%':>6} {'20日%':>7} "
        f"{'RSI':>5} {'换手%':>6} {'MA20距%':>7} {'PE':>7} {'市值/亿':>8} {'股票':>4}",
        "-" * 110,
    ]

    for i, m in enumerate(industries[:top_n], 1):
        icon = _status_icon(m)
        mv_str = f"{m['total_mv']/1e8:.0f}" if m.get("total_mv") else "—"
        pe_str = _fmt(m.get("pe_median"), decimals=1)
        to_str = _fmt(m.get("avg_turnover"), decimals=2)

        lines.append(
            f"{i:>3} {m['industry']:<10} {icon:<5} "
            f"{m['breadth_pct']:>5.1f}% "
            f"{_fmt(m.get('avg_ret_5d'), '%', 2):>6} "
            f"{_fmt(m.get('avg_ret_20d'), '%', 2):>7} "
            f"{_fmt(m.get('avg_rsi'), '', 1):>5} "
            f"{to_str:>6} "
            f"{_fmt(m.get('avg_ma20_dist_pct'), '%', 1):>7} "
            f"{pe_str:>7} "
            f"{mv_str:>8} "
            f"{m['stock_count']:>4}"
        )
        # Momentum score bar
        score = m.get("momentum_score", 0)
        bar_len = int(score * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"     动量: {bar} {score:.3f}")

    lines.append("-" * 110)
    return "\n".join(lines)


def format_inflection(candidates: List[Dict]) -> str:
    """格式化拐点探测输出。"""
    if not candidates:
        return "拐点探测器: 当前无行业触发拐点信号"

    lines = [
        "",
        "=" * 100,
        '  拐点探测器 — 寻找"冻死→解冻"的行业拐点（类似3月光模块）',
        "=" * 100,
        f"  {'排名':>3} {'行业':<10} {'信号':>4} {'肌肉':>4} {'深度':>6} {'量价':>6} {'龙头':>6} {'20日%':>7} {'60日%':>7} {'广度%':>6}",
        "-" * 100,
    ]

    for i, m in enumerate(candidates[:20], 1):
        s = m.get("inflection_signals", {})
        hits = m.get("inflection_hits", 0)
        bars = "█" * hits + "░" * (3 - hits)
        mem = s.get("muscle_memory", ("⚪", ""))[0]
        depth_s = s.get("depth", ("⚪", ""))
        money_s = s.get("moneyflow", ("⚪", ""))
        lead_s = s.get("leader_lead", ("⚪", ""))

        # Format moneyflow string
        mf_reason = money_s[1] if len(money_s) > 1 else ""
        mf_short = mf_reason[:20] if mf_reason else ""

        lines.append(
            f"  {i:>3} {m['industry']:<10} {bars:<4} {mem:<4} "
            f"{depth_s[0]} {m.get('depth_from_peak_pct',0):>4.0f}%  "
            f"{money_s[0]} {mf_short:<20}  "
            f"{lead_s[0]} {m.get('leader_lead_pct',0):>4.0f}%  "
            f"{_fmt(m.get('avg_ret_20d'), '%', 2):>6} "
            f"{_fmt(m.get('avg_ret_60d'), '%', 2):>6} "
            f"{_fmt(m.get('breadth_pct'), '%', 1):>5}"
        )
        # 信号详情
        details = []
        for name, (light, reason) in s.items():
            if light == "🟢":
                details.append(f"{name}: {reason}")
        if details:
            lines.append(f"       🟢 {' | '.join(details)}")

    lines.append("-" * 100)
    lines.append(f"  共 {len(candidates)} 个行业触发至少1个信号，上方仅显示前20")
    lines.append("  三灯全亮 = 拐点概率最高 | 肌肉记忆🟢 = 曾有过辉煌，复苏概率更高")
    return "\n".join(lines)


def format_radar(radar_list: List[Dict]) -> str:
    if not radar_list:
        return "轮动雷达: 无符合条件的冷门行业（所有行业广度均 ≥60%）"

    lines = [
        "",
        "=" * 130,
        "  轮动雷达 — 行业苏醒探测 (广度 < 60%，含量价背离+超跌深度+龙头先行)",
        "=" * 130,
        f"{'排名':>3} {'行业':<10} {'标签':<5} {'苏醒分':>6} {'洗盘':>5} {'反转':>5} "
        f"{'领涨':>5} {'估值':>5} {'偏离':>5} {'量价':>5} {'20日%':>7} {'广度%':>6} {'深度%':>6}",
        "-" * 130,
    ]

    for i, m in enumerate(radar_list, 1):
        d = m.get("wake_up_detail", {})
        lines.append(
            f"{i:>3} {m['industry']:<10} {m.get('wake_label', '—'):<5} "
            f"{m['wake_up_score']:>5.2f}  "
            f"{d.get('washout', 0):>4.2f}  {d.get('reversal', 0):>4.2f}  "
            f"{d.get('leader', 0):>4.2f}  {d.get('valuation', 0):>4.2f}  "
            f"{d.get('divergence', 0):>4.2f}  {d.get('volume_divergence', 0):>4.2f}  "
            f"{_fmt(m.get('avg_ret_20d'), '%', 2):>7}  "
            f"{m['breadth_pct']:>5.1f}%  "
            f"{m.get('depth_from_peak_pct', 0):>5.1f}%"
        )

    lines.append("-" * 130)
    return "\n".join(lines)


def format_industry_detail(name: str, detail: Dict) -> str:
    if detail.get("error"):
        return f"行业 '{name}': {detail['error']}"

    lines = [
        "",
        "=" * 90,
        f"  行业深入分析: {name}",
        "=" * 90,
        f"  股票数: {detail['stock_count']} (原始 {detail.get('stock_count_raw', '?')})",
        f"  广度: {detail['breadth_pct']}% 站上MA20 | {detail.get('breadth_ma60_pct', 0)}% 站上MA60",
        f"  收益: 5日 {_fmt(detail.get('avg_ret_5d'), '%', 2)} | "
        f"20日 {_fmt(detail.get('avg_ret_20d'), '%', 2)} | "
        f"60日 {_fmt(detail.get('avg_ret_60d'), '%', 2)}",
        f"  RSI: 均值 {_fmt(detail.get('avg_rsi'), '', 1)} | "
        f"PE中位: {_fmt(detail.get('pe_median'), '', 1)} | "
        f"换手: {_fmt(detail.get('avg_turnover'), '%', 2)}",
        "",
    ]

    # RSI distribution
    rsi_d = detail.get("rsi_dist", {})
    if rsi_d:
        total = sum(rsi_d.values())
        if total > 0:
            bar_os = "█" * int(rsi_d["oversold"] / total * 20)
            bar_w = "█" * int(rsi_d["weak"] / total * 20)
            bar_n = "█" * int(rsi_d["neutral"] / total * 20)
            bar_s = "█" * int(rsi_d["strong"] / total * 20)
            bar_ob = "█" * int(rsi_d["overbought"] / total * 20)
            lines.append(
                f"  RSI分布: 超卖({rsi_d['oversold']}){bar_os} | "
                f"弱势({rsi_d['weak']}){bar_w} | "
                f"中性({rsi_d['neutral']}){bar_n} | "
                f"偏强({rsi_d['strong']}){bar_s} | "
                f"超买({rsi_d['overbought']}){bar_ob}"
            )

    # Wake-up detail if cold
    wu = detail.get("wake_up_score")
    if wu is not None:
        d = detail.get("wake_up_detail", {})
        lines.append(f"\n  轮动苏醒评分: {wu:.3f} {detail.get('wake_label', '')}")
        lines.append(
            f"    洗盘深度: {d.get('washout', 0):.2f} | "
            f"反转信号: {d.get('reversal', 0):.2f} | "
            f"领涨股: {d.get('leader', 0):.2f} | "
            f"估值: {d.get('valuation', 0):.2f} | "
            f"偏离: {d.get('divergence', 0):.2f}"
        )

    # Top leaders
    leaders = detail.get("_leaders", [])[:5]
    if leaders:
        lines.append(f"\n  Top 5 领涨股:")
        for s in leaders:
            above = "✓" if s["above_ma20"] else "✗"
            lines.append(
                f"    {s['code']} {s['name'][:6]:<6} "
                f"20日 {_fmt(s['ret_20d'], '%', 2):>7}  "
                f"5日 {_fmt(s['ret_5d'], '%', 2):>7}  "
                f"RSI {_fmt(s['rsi'], '', 1):>5}  "
                f"MA20:{above}"
            )

    # Bottom laggards
    laggards = detail.get("_laggards", [])[:5]
    if laggards:
        lines.append(f"\n  Bottom 5 拖累股:")
        for s in laggards:
            above = "✓" if s["above_ma20"] else "✗"
            lines.append(
                f"    {s['code']} {s['name'][:6]:<6} "
                f"20日 {_fmt(s['ret_20d'], '%', 2):>7}  "
                f"5日 {_fmt(s['ret_5d'], '%', 2):>7}  "
                f"RSI {_fmt(s['rsi'], '', 1):>5}  "
                f"MA20:{above}"
            )

    lines.append("=" * 90)
    return "\n".join(lines)


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="行业轮动雷达 — Sector Momentum & Rotation Detection"
    )
    parser.add_argument("--heatmap", action="store_true", help="仅显示动量热力图")
    parser.add_argument("--radar", action="store_true", help="仅显示轮动雷达")
    parser.add_argument("--inflection", action="store_true", help="拐点探测器（找3月光模块那种机会）")
    parser.add_argument("--top", type=int, default=20, help="显示前N个行业 (默认20)")
    parser.add_argument("--industry", type=str, default=None, help="深入分析特定行业")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--backfill", action="store_true", help="回填缺失行业分类")
    parser.add_argument("--names-only", action="store_true",
                       help="仅输出行业名（逗号分隔），配合 buy_plan --industries 使用")
    args = parser.parse_args()

    engine = SectorRadarEngine()

    # Backfill mode
    if args.backfill:
        print("回填缺失行业分类...")
        result = engine.backfill_null_industries()
        if result.get("ok"):
            print(f"完成: 共 {result['total_null']} 只缺失, 回填 {result['backfilled']} 只, "
                  f"剩余 {result.get('remaining', 0)} 只")
        else:
            print(f"回填失败: {result.get('error', '未知错误')}")
        return

    # Determine which views to show
    if args.heatmap and not args.radar:
        show_heatmap, show_radar = True, False
    elif args.radar and not args.heatmap:
        show_heatmap, show_radar = False, True
    else:
        show_heatmap, show_radar = True, True

    print("加载行业数据...")
    report = engine.run(
        show_heatmap=show_heatmap,
        show_radar=show_radar,
        show_inflection=args.inflection or (not args.heatmap and not args.radar),
        top_n=args.top,
        industry=args.industry,
    )

    # Warnings
    for w in report.get("warnings", []):
        print(f"  ⚠ {w}")

    if report.get("error"):
        print(f"错误: {report['error']}")
        return

    # --names-only: 只输出行业名，方便管道给 buy_plan
    if args.names_only:
        names = []
        if report.get("radar"):
            names.extend([m["industry"] for m in report["radar"]])
        if report.get("heatmap") and not names:
            names.extend([m["industry"] for m in report["heatmap"]])
        print(", ".join(names))
        return

    if args.json:
        # Strip internal fields for JSON output
        clean = {}
        for k, v in report.items():
            if k in ("heatmap", "radar"):
                clean[k] = [
                    {kk: vv for kk, vv in item.items() if not str(kk).startswith("_")}
                    for item in v
                ]
            elif k == "industry_detail":
                clean[k] = {
                    kk: vv for kk, vv in v.items() if not str(kk).startswith("_")
                }
            else:
                clean[k] = v
        print(json.dumps(clean, ensure_ascii=False, indent=2, default=str))
        return

    # Formatted output
    output_parts = []

    # Summary
    s = report.get("summary", {})
    if s:
        output_parts.append(
            f"数据概览: {s.get('total_industries_with_data', '?')} 个行业 | "
            f"{s.get('total_stocks_with_signal', '?')} 只有效信号 | "
            f"信号日期: {s.get('signal_date', '?')} | "
            f"{s.get('null_industry_stocks', '?')} 只缺行业分类"
        )

    if report.get("heatmap"):
        output_parts.append(format_heatmap(report["heatmap"], args.top))

    if report.get("radar"):
        output_parts.append(format_radar(report["radar"]))

    if report.get("inflection"):
        output_parts.append(format_inflection(report["inflection"]))

    if report.get("industry_detail"):
        output_parts.append(format_industry_detail(args.industry, report["industry_detail"]))

    print("\n".join(output_parts))


if __name__ == "__main__":
    main()
