#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Portfolio strategy skill entry.

This module turns a manually maintained portfolio file into practical
position-level and portfolio-level actions for Dungineer.
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import yaml

from data_capability_service import DataCapabilityService
from market_data_provider import MarketDataProvider
from strategy_signals import StrategySignalCollector
from sector_radar import SectorRadarEngine


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PORTFOLIO = os.path.join(PROJECT_ROOT, "data", "portfolio.yaml")
EXAMPLE_PORTFOLIO = os.path.join(PROJECT_ROOT, "data", "portfolio.example.yaml")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value: float) -> str:
    return f"{value:.2f}%"


def _percentile_label(pct: float) -> str:
    if pct is None:
        return ""
    if pct < 20:
        return "(低估)"
    if pct > 60:
        return "(偏高)"
    return "(合理)"


class PortfolioStrategy:
    """Manual portfolio review and action planner."""

    def __init__(self, portfolio_path: Optional[str] = None):
        self.portfolio_path = portfolio_path or DEFAULT_PORTFOLIO
        if not os.path.exists(self.portfolio_path):
            self.portfolio_path = EXAMPLE_PORTFOLIO
        self.data = self._load_portfolio()
        self.data_provider = MarketDataProvider()
        self.signal_collector = StrategySignalCollector()
        self._data_service = None
        self._data_service_error = ""
        self._industry_momentum: Dict[str, float] = {}
        self._industry_momentum_loaded = False
        # portfolio 行业标签 → sector_radar 行业名（后者仅覆盖 A 股，港股/ETF 用最相关 A 股行业代理）
        self._INDUSTRY_SECTOR_MAP: Dict[str, str] = {
            "电信运营": "通信设备",
            "消费电子": "元器件",
            "互联网": "互联网",
            "工业有色": "__AGG_有色金属__",  # 聚合：铜+铝+铅锌+小金属+黄金+矿物制品
            "铁路": "运输设备",
            "铁路运输": "运输设备",
            "港股科技": "互联网",
            "通信运营商": "通信设备",
        }
        # 聚合行业的成分列表
        self._AGG_INDUSTRIES: Dict[str, List[str]] = {
            "__AGG_有色金属__": ["铜", "铝", "铅锌", "小金属", "黄金", "矿物制品", "钢加工"],
        }
        # 百分位缓存已改为模块级 classmethod 缓存

    def _ensure_industry_momentum(self) -> Dict[str, float]:
        """加载行业动量分（sector_radar 热力图），缓存避免重复计算。

        返回 {portfolio 行业标签: momentum_score}。港股/ETF 行业通过映射表
        关联到最相关的 A 股行业；聚合行业（如有色金属）取成分均值。
        加载失败时返回空 dict，调用方降级为无条件放行。
        """
        if self._industry_momentum_loaded:
            return self._industry_momentum
        self._industry_momentum_loaded = True
        try:
            engine = SectorRadarEngine()
            result = engine.run(show_heatmap=True, show_radar=False, top_n=100)
            heatmap = result.get("heatmap", [])
            raw: Dict[str, float] = {}
            for m in heatmap:
                name = m.get("industry", "")
                score = m.get("momentum_score")
                if name and score is not None:
                    raw[name] = score
            # 先复制直接命中的
            for pf_ind, sr_ind in self._INDUSTRY_SECTOR_MAP.items():
                if sr_ind.startswith("__AGG_"):
                    continue  # 聚合项单独处理
                if sr_ind in raw:
                    self._industry_momentum[pf_ind] = raw[sr_ind]
            # 聚合行业：取成分平均值
            for agg_key, members in self._AGG_INDUSTRIES.items():
                scores = [raw[m] for m in members if m in raw]
                if scores:
                    avg = sum(scores) / len(scores)
                    # 反向查找映射到这个聚合项的所有 portfolio 行业
                    for pf_ind, sr_ind in self._INDUSTRY_SECTOR_MAP.items():
                        if sr_ind == agg_key:
                            self._industry_momentum[pf_ind] = round(avg, 3)
        except Exception:
            pass
        return self._industry_momentum

    def _load_portfolio(self) -> Dict[str, Any]:
        with open(self.portfolio_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("account", {})
        data.setdefault("positions", [])
        data.setdefault("watchlist", [])
        return data

    def _get_industry_peers(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """从 MongoDB 按行业编码聚合：PE/PB 中位数/均值/分位。"""
        result = {"available": False, "pe_median": None, "pb_median": None,
                  "pe_mean": None, "pb_mean": None,
                  "pe_percentile": None, "pb_percentile": None,
                  "peer_count": 0}
        ind_code = row.get("industry_code")
        ind_name = row.get("industry")
        if not ind_code and not ind_name:
            return result

        query = {"pe": {"$gt": 0}}
        if ind_code:
            query["industry_code"] = ind_code
        elif ind_name:
            query["industry"] = ind_name
        else:
            return result

        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore
            import os
            config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
            mongodb_config = _load_mongodb_config(config_path)
            store = MongoFactorDataStore(mongodb_config, readonly=True, ensure_indexes=False)
            peers = list(store.db[store.collections["basic_info"]].find(
                query, {"pe": 1, "pb": 1, "_id": 0}
            ))
            if not peers:
                return result
            pe_vals = sorted(p.get("pe") for p in peers if p.get("pe") and p["pe"] > 0)
            pb_vals = sorted(p.get("pb") for p in peers if p.get("pb") and p["pb"] > 0)
            n = len(pe_vals)
            if n == 0:
                return result
            mid = n // 2
            result["available"] = True
            result["peer_count"] = n
            result["pe_median"] = round(pe_vals[mid] if n % 2 else (pe_vals[mid-1] + pe_vals[mid]) / 2, 2)
            result["pe_mean"] = round(sum(pe_vals) / n, 2)
            # PE分位：该股票PE在同行业中的排名（越低越便宜）
            stock_pe = row.get("pe")
            if stock_pe and stock_pe > 0 and n > 1:
                result["pe_percentile"] = round(sum(1 for p in pe_vals if p <= stock_pe) / n * 100, 1)
            if pb_vals:
                m = len(pb_vals)
                m_mid = m // 2
                result["pb_median"] = round(pb_vals[m_mid] if m % 2 else (pb_vals[m_mid-1] + pb_vals[m_mid]) / 2, 2)
                result["pb_mean"] = round(sum(pb_vals) / m, 2)
                stock_pb = row.get("pb")
                if stock_pb and stock_pb > 0 and m > 1:
                    result["pb_percentile"] = round(sum(1 for p in pb_vals if p <= stock_pb) / m * 100, 1)
        except Exception:
            pass
        return result

    # ---- 百分位排名（模块级缓存，format_review 等独立函数也可调用）----

    _PCT_CACHE: Dict[str, Dict[str, Any]] = {}
    _PCT_CACHE_TIME: Optional[datetime] = None
    _PCT_CACHE_ERROR: str = ""

    @classmethod
    def _load_percentile_ranks(cls, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """加载全市场 + 行业内因子综合得分的百分位排名（缓存4小时）。

        Returns:
            {code: {market_pct, industry_pct, industry_name, industry_peers, total_stocks}}
        """
        now = datetime.now()
        if not force_refresh and cls._PCT_CACHE is not None:
            if cls._PCT_CACHE_TIME and (now - cls._PCT_CACHE_TIME).total_seconds() < 14400:
                return cls._PCT_CACHE

        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore

            config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
            mongodb_config = _load_mongodb_config(config_path)
            store = MongoFactorDataStore(mongodb_config, readonly=True, ensure_indexes=False)

            # 1. 取所有信号的 composite_score
            signals = list(store.db["stock_signals"].find(
                {"factor.composite_score": {"$exists": True}},
                {"code": 1, "factor.composite_score": 1, "_id": 0},
            ))
            if not signals:
                cls._PCT_CACHE = {}
                cls._PCT_CACHE_TIME = now
                return {}

            # 2. 取 basic_info 的行业信息
            all_codes = [s["code"] for s in signals]
            basic_map: Dict[str, Dict[str, str]] = {}
            for b in store.db[store.collections["basic_info"]].find(
                {"code": {"$in": all_codes}},
                {"code": 1, "industry": 1, "industry_code": 1, "_id": 0},
            ):
                basic_map[b["code"]] = {
                    "industry": str(b.get("industry", "")),
                    "industry_code": str(b.get("industry_code", "")),
                }

            # 3. 全市场排序
            all_scores = [(s["code"], s["factor"]["composite_score"]) for s in signals]
            all_scores.sort(key=lambda x: x[1])
            total = len(all_scores)

            # 4. 按行业分组排序
            industry_scores: Dict[str, List[tuple]] = defaultdict(list)
            for code, score in all_scores:
                ind_code = basic_map.get(code, {}).get("industry_code", "")
                if ind_code:
                    industry_scores[ind_code].append((code, score))
            for ind in industry_scores:
                industry_scores[ind].sort(key=lambda x: x[1])

            # 5. 构建查找表
            cache: Dict[str, Dict[str, Any]] = {}
            for i, (code, score) in enumerate(all_scores):
                market_pct = round(i / total * 100, 1)
                entry: Dict[str, Any] = {
                    "composite_score": score,
                    "market_pct": market_pct,
                    "total_stocks": total,
                }

                ind_code = basic_map.get(code, {}).get("industry_code", "")
                if ind_code and ind_code in industry_scores:
                    ind_list = industry_scores[ind_code]
                    ind_rank = sum(1 for _, s in ind_list if s < score)
                    n_peers = len(ind_list)
                    entry["industry_pct"] = round(ind_rank / n_peers * 100, 1) if n_peers else None
                    entry["industry_name"] = basic_map[code].get("industry", "")
                    entry["industry_peers"] = n_peers

                cache[code] = entry

            cls._PCT_CACHE = cache
            cls._PCT_CACHE_TIME = now
            cls._PCT_CACHE_ERROR = ""
            return cache
        except Exception as e:
            cls._PCT_CACHE_ERROR = str(e)
            cls._PCT_CACHE = {}
            return {}

    @classmethod
    def _pct_suffix(cls, code: str) -> str:
        """返回全市场+行业百分位的格式化后缀，如 ' 全市场前15% 行业前8%'"""
        cache = cls._load_percentile_ranks()
        entry = cache.get(code, {})
        if not entry:
            return ""
        parts = [f"全市场前{entry['market_pct']}%"]
        if entry.get("industry_pct") is not None:
            parts.append(f"行业前{entry['industry_pct']}%")
        return " " + " ".join(parts)

    def review(self, ensure_data: bool = False) -> Dict[str, Any]:
        account = self.data.get("account", {})
        target = account.get("target", {}) or {}
        positions = self.data.get("positions", []) or []
        watchlist = self.data.get("watchlist", []) or []
        all_assets = positions + watchlist
        data_status = self._data_status_for_assets(positions, ensure_data=ensure_data)

        base_currency = account.get("base_currency", "CNY")
        currencies = self._collect_currencies(account, all_assets)
        fx_rates = self.data_provider.resolve_fx_rates(
            base_currency,
            account.get("fx_rates_to_base", {}) or {},
            currencies,
        )
        position_rows = self._build_position_rows(positions, fx_rates)
        # 观察池伪持仓（quantity=0，无PnL，但拿到实时价格和PE/PB）
        watchlist_rows = self._build_position_rows(watchlist, fx_rates)
        for w in watchlist_rows:
            w["is_watchlist"] = True
            w["quantity"] = 0.0
            w["market_value"] = 0.0
            w["unrealized_pnl"] = 0.0
            w["unrealized_pnl_pct"] = 0.0
            w["cost_price"] = 0.0
            w["cost_value"] = 0.0
        position_value = sum(row["market_value"] for row in position_rows)
        account_values = self._calculate_account_values(account, fx_rates, position_value)
        cash = account_values["cash"]
        total_assets = account_values["total_assets"]

        industry_rows = self._build_industry_rows(position_rows, total_assets)
        market_rows = self._build_group_rows(position_rows, total_assets, "market", "未分类市场")
        asset_type_rows = self._build_group_rows(position_rows, total_assets, "asset_type", "unknown")
        style_rows = self._build_group_rows(position_rows, total_assets, "style", "未分类风格")
        summary = {
            "portfolio_file": self.portfolio_path,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "broker": account.get("broker", "unknown"),
            "base_currency": base_currency,
            "fx_rates_to_base": fx_rates,
            "fx_rate_errors": {},
            "risk_profile": account.get("risk_profile", "balanced"),
            "cash": round(cash, 2),
            "cash_source": account_values["cash_source"],
            "cash_by_currency": account.get("cash_by_currency", {}),
            "position_value": round(position_value, 2),
            "total_assets": round(total_assets, 2),
            "total_assets_source": account_values["total_assets_source"],
            "cash_pct": round(cash / total_assets * 100, 2) if total_assets else 0,
            "position_count": len(position_rows),
            "unrealized_pnl": round(sum(row["unrealized_pnl"] for row in position_rows), 2),
            "unrealized_pnl_pct": self._portfolio_pnl_pct(position_rows),
        }

        constraints = {
            "max_single_position_pct": _to_float(target.get("max_single_position_pct"), 15),
            "max_industry_position_pct": _to_float(target.get("max_industry_position_pct"), 30),
            "min_cash_pct": _to_float(target.get("min_cash_pct"), 20),
            "profit_target_pct": _to_float(target.get("profit_target_pct"), 12),
            "max_drawdown_pct": _to_float(target.get("max_drawdown_pct"), 8),
            "strategy_goal": target.get("strategy_goal", "稳健盈利"),
        }
        goal_progress = self._evaluate_goal_progress(summary, constraints)
        summary["goal_progress"] = goal_progress

        alerts = self._build_alerts(summary, position_rows, industry_rows, constraints)
        diagnosis = self._build_diagnosis(
            summary,
            position_rows,
            industry_rows,
            market_rows,
            asset_type_rows,
            style_rows,
            constraints,
        )
        # 每只持仓嵌入七维信号数据和行业对比
        review_context = {
            "summary": summary, "constraints": constraints,
            "goal_progress": goal_progress, "industry_exposure": industry_rows,
            "market_exposure": market_rows, "style_exposure": style_rows,
        }
        # 从 MongoDB 补 industry_code（review 时不依赖 portfolio.yaml 手填）
        _mongo_ind_cache: Dict[str, str] = {}
        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore
            config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
            mongodb_config = _load_mongodb_config(config_path)
            mongo_store = MongoFactorDataStore(mongodb_config, readonly=True, ensure_indexes=False)
            for row in position_rows + watchlist_rows:
                code = row.get("code", "")
                if not code:
                    continue
                doc = mongo_store.get_basic(code)
                if doc and doc.get("industry_code"):
                    row["industry_code"] = doc["industry_code"]
                    _mongo_ind_cache[code] = doc["industry_code"]
                # 自身 PE 历史分位（来自信号缓存/Tushare）
                try:
                    sig = mongo_store.db["stock_signals"].find_one({"code": code})
                    if sig and sig.get("pe_percentile") is not None:
                        row["pe_percentile_self"] = sig["pe_percentile"]
                except Exception:
                    pass
                # 港股做空数据 + 占比
                if row.get("market") == "港股":
                    try:
                        now = datetime.now()
                        if now.hour < 12:
                            candidates = [(now - timedelta(days=1), "day")]
                        elif now.hour < 16:
                            candidates = [(now, "morning"), (now - timedelta(days=1), "day")]
                        else:
                            candidates = [(now, "day"), (now, "morning"), (now - timedelta(days=1), "day")]
                        ss = None
                        for target, period in candidates:
                            target_date = target.strftime("%-d %B %Y")
                            ss = mongo_store.db["stock_shortsell"].find_one(
                                {"code": row["code"], "trade_date": target_date, "period": period})
                            if ss:
                                break
                        if ss:
                            amt = ss.get("amount", 0) or 0
                            total_amt = _to_float(doc.get("latest_amount"), 0) if doc else 0
                            ratio = round(amt / total_amt * 100, 1) if total_amt > 0 and amt > 0 else None
                            row["shortsell"] = {"shares": ss.get("shares"),
                                                "amount": amt, "ratio": ratio,
                                                "period": ss.get("period"),
                                                "date": ss.get("trade_date")}
                        # 南向持仓（从 MongoDB 缓存读取，更稳定）
                        sb_rows = list(mongo_store.db["stock_southbound"].find(
                            {"code": row["code"]}, sort=[("trade_date", -1)]).limit(60))
                        if sb_rows:
                            sb_latest = sb_rows[0]
                            ratio_now = sb_latest.get("ratio")
                            ratio_prev = sb_rows[1].get("ratio") if len(sb_rows) > 1 else None
                            # 月变动：找 ~30天前 最接近的一条
                            latest_date = sb_latest.get("trade_date")
                            ratio_month = None
                            if latest_date and len(sb_rows) > 1:
                                try:
                                    t_latest = datetime.strptime(latest_date, "%Y%m%d")
                                    t_target = t_latest - timedelta(days=30)
                                    best = None
                                    for r in sb_rows[1:]:
                                        t_r = datetime.strptime(r.get("trade_date", ""), "%Y%m%d")
                                        if t_r <= t_target:
                                            best = r
                                            break
                                    if best is None and len(sb_rows) > 1:
                                        best = sb_rows[-1]
                                    if best:
                                        ratio_month = round(ratio_now - best.get("ratio", 0), 3)
                                except Exception:
                                    pass
                            row["_hk_southbound"] = {
                                "vol": sb_latest.get("vol"),
                                "ratio": ratio_now,
                                "ratio_change": round(ratio_now - ratio_prev, 3)
                                if ratio_now is not None and ratio_prev is not None else None,
                                "ratio_change_monthly": ratio_month,
                            }
                    except Exception:
                        pass
                # 股息率（A股 daily_basic，仅 stock 类型）
                if row.get("asset_type") == "stock":
                    try:
                        dv = mongo_store.db["stock_dividend"].find_one(
                            {"code": row["code"]}, sort=[("trade_date", -1)])
                        if dv:
                            row["dividend_yield"] = dv.get("dv_ttm") or dv.get("dv_ratio")
                    except Exception:
                        pass
                # 龙虎榜检测（持仓是否上榜）
                try:
                    from datetime import datetime as _dt
                    today_str = _dt.now().strftime("%Y%m%d")
                    tl = mongo_store.db["top_list"].find_one(
                        {"ts_code": {"$regex": f"^{row['code']}"},
                         "trade_date": today_str})
                    if tl:
                        row["top_list"] = {
                            "l_buy": tl.get("l_buy", 0) or 0,
                            "l_sell": tl.get("l_sell", 0) or 0,
                            "net_amount": tl.get("net_amount", 0) or 0,
                            "reason": tl.get("reason", ""),
                            "pct_change": tl.get("pct_change", 0) or 0,
                        }
                except Exception:
                    pass
        except Exception:
            pass

        original_positions = self.data.get("positions", []) or []
        for i, row in enumerate(position_rows):
            orig = original_positions[i] if i < len(original_positions) else row
            goal_impact = self._evaluate_position_goal_impact(row, summary, constraints)
            strategy_signals = self.signal_collector.collect_for_position(orig)
            decision_dimensions = self._build_decision_dimensions(
                row, review_context, goal_impact, strategy_signals)
            rule_precheck = self._plan_for_position(row, summary, constraints)
            industry_peers = self._get_industry_peers(row)
            # Tushare 专属信号（回购/筹码/预期）
            try:
                row["tushare_signals"] = self.data_provider.get_tushare_signals(
                    row.get("code", ""), row.get("market", "A股"))
            except Exception:
                pass
            # 港股：用 MongoDB 缓存的南向数据覆盖（避免 Tushare 包 numpy 兼容问题）
            sb = row.pop("_hk_southbound", None)
            if sb:
                ts = row.get("tushare_signals")
                if ts and isinstance(ts, dict):
                    ts["hk_hold"] = sb
            row["strategy_signals"] = strategy_signals
            add_conditions = self._evaluate_add_conditions(row, summary, constraints)
            trim_conditions = self._evaluate_trim_conditions(row)
            row["goal_impact"] = goal_impact
            row["add_conditions"] = add_conditions
            row["trim_conditions"] = trim_conditions
            row["decision_dimensions"] = decision_dimensions
            row["rule_precheck"] = rule_precheck
            row["industry_peers"] = industry_peers
            # 实时趋势信号（覆盖缓存，确保盘中RSI/KDJ/MA基于最新价格）
            try:
                live_trend = self.data_provider.get_trend_signal(
                    row.get("code", ""), row.get("market", "A股"))
                if live_trend.get("available"):
                    row["trend_signal"] = live_trend
            except Exception:
                pass
            # 动态止损建议
            row["dynamic_stop"] = self._suggest_dynamic_stop(row)

        # 观察池：补趋势信号 + 买入时机评估 + Tushare信号 + 行业同侪
        for row in watchlist_rows:
            code = row.get("code", "")
            market = row.get("market", "A股")
            # 趋势信号
            try:
                trend = self.data_provider.get_trend_signal(code, market)
                row["trend_signal"] = trend
            except Exception:
                row["trend_signal"] = {"available": False}
            # PE 分位（从 stock_signals 缓存读取）
            try:
                sig_doc = self.data_provider.mongo.db["stock_signals"].find_one(
                    {"code": code}, sort=[("computed_at", -1)])
                if sig_doc and sig_doc.get("pe_percentile") is not None:
                    row["pe_percentile_self"] = sig_doc["pe_percentile"]
            except Exception:
                row["pe_percentile_self"] = None
            # 多因子
            try:
                stock_doc = self.data_provider.mongo.get_basic(code)
                if stock_doc is None:
                    stock_doc = {"code": code, "close": row.get("current_price", 0)}
                quotes = self.data_provider.mongo.get_recent_quotes(code, 260)
                fin = self.data_provider.mongo.get_latest_financial(code)
                pyramid = self.signal_collector._get_pyramid_strategy()
                factor_raw = pyramid.calculate_composite_score(stock_doc, quotes, fin)
                row["factor"] = {
                    "composite_score": factor_raw.get("composite_score", 0),
                    "factor_scores": factor_raw.get("factor_scores", {}),
                }
            except Exception:
                row["factor"] = {"composite_score": 0, "factor_scores": {}}
            # Tushare 信号
            try:
                row["tushare_signals"] = self.data_provider.get_tushare_signals(
                    code, market)
            except Exception:
                pass
            sb = row.pop("_hk_southbound", None)
            if sb:
                ts = row.get("tushare_signals")
                if ts and isinstance(ts, dict):
                    ts["hk_hold"] = sb
            row["industry_peers"] = self._get_industry_peers(row)
            # v2: 加载 alpha_context + 调用 entry_engine
            try:
                fac_doc = self.data_provider.mongo.db["stock_factors"].find_one(
                    {"code": row.get("code", ""), "trade_date": {"$exists": True}},
                    sort=[("trade_date", -1)]
                )
                if fac_doc:
                    g = fac_doc.get("group", "")
                    fs = fac_doc.get("factors", {}).get(g, {}).get("factor_scores", {})
                    row["alpha_context"] = {
                        "momentum": fs.get("momentum", 0.5),
                        "cycle": fac_doc.get("cycle_score", 0.5),
                        "turnaround": fac_doc.get("turnaround_score", 0.5),
                    }
            except Exception:
                row["alpha_context"] = {}
            # entry_engine 买入时机评估
            from entry_engine import evaluate
            row["buy_timing"] = evaluate(row)

        # 周期汇总（组合级，按风格聚合）
        cycle_summary = self._build_cycle_summary(position_rows)

        # 市场情绪（北向/南向/两融）
        try:
            market_sentiment = self.data_provider.get_market_sentiment()
        except Exception:
            market_sentiment = {"available": False}

        # 宏观/行业新闻（华尔街见闻）
        try:
            market_news = self.data_provider.get_macro_news(limit=8)
        except Exception:
            market_news = []

        # 大盘宽度 + 组合相关性
        try:
            market_breadth = self._get_market_breadth()
        except Exception:
            market_breadth = {"available": False}
        try:
            corr_matrix = self._get_correlation_matrix(position_rows)
        except Exception:
            corr_matrix = {"available": False}

        return {
            "summary": summary,
            "constraints": constraints,
            "market_sentiment": market_sentiment,
            "market_news": market_news,
            "market_breadth": market_breadth,
            "correlation": corr_matrix,
            "goal_progress": goal_progress,
            "diagnosis": diagnosis,
            "cycle_summary": cycle_summary,
            "positions": position_rows,
            "industry_exposure": industry_rows,
            "market_exposure": market_rows,
            "asset_type_exposure": asset_type_rows,
            "style_exposure": style_rows,
            "alerts": alerts,
            "sector_heatmap": self._get_sector_heatmap_summary(),
            "watchlist": watchlist_rows,
            "data_status": data_status,
        }

    def position_plan(self, code: str, ensure_data: bool = False) -> Dict[str, Any]:
        if ensure_data:
            self._ensure_data_for_code(code)
        review = self.review(ensure_data=False)
        code = code.strip()
        for position in review["positions"]:
            if position["code"] == code:
                position_data_status = self._find_data_status(review.get("data_status", {}), code)
                rule_precheck = self._plan_for_position(position, review["summary"], review["constraints"])
                goal_impact = self._evaluate_position_goal_impact(position, review["summary"], review["constraints"])
                strategy_signals = self.signal_collector.collect_for_position(position)
                decision_dimensions = self._build_decision_dimensions(
                    position,
                    review,
                    goal_impact,
                    strategy_signals,
                )
                agent_context = self._build_agent_context(
                    position=position,
                    review=review,
                    goal_impact=goal_impact,
                    strategy_signals=strategy_signals,
                    decision_dimensions=decision_dimensions,
                    rule_precheck=rule_precheck,
                )
                return {
                    "summary": review["summary"],
                    "constraints": review["constraints"],
                    "goal_progress": review.get("goal_progress", {}),
                    "position": position,
                    "data_status": position_data_status,
                    "goal_impact": goal_impact,
                    "strategy_signals": strategy_signals,
                    "decision_dimensions": decision_dimensions,
                    "rule_precheck": rule_precheck,
                    "agent_context": agent_context,
                    "plan": {
                        "action": "等待 Agent 综合判断",
                        "reason": "代码层仅完成数据采集、规则初筛和信号结构化；最终动作需要由 Agent 结合目标、仓位、趋势、多因子、周期和事件统一推理。",
                    },
                }

        for item in review["watchlist"]:
            if str(item.get("code", "")).strip() == code:
                return {
                    "summary": review["summary"],
                    "watchlist_item": item,
                    "plan": {
                        "action": "观察",
                        "reason": item.get("reason", "在观察池中，但尚未形成持仓"),
                        "buy_zone": item.get("target_buy_zone", "未设置"),
                        "risk": "买入前需要确认当前组合现金比例和行业暴露是否允许新增仓位",
                    },
                }

        return {
            "error": f"未在持仓或观察池中找到股票: {code}",
            "available_codes": [p["code"] for p in review["positions"]],
        }

    def analyze_stock(self, code: str, market: str = "A股") -> Dict[str, Any]:
        """分析任意股票（不需在 portfolio.yaml 中）。"""
        from data_import_pipeline import import_stocks, _load_mongodb_config
        import os

        code = str(code).strip()
        market = str(market)

        # 1. 补齐数据
        config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
        mongodb_config = _load_mongodb_config(config_path)
        from factor_data_import_service import MongoFactorDataStore
        store = MongoFactorDataStore(mongodb_config)
        import_stocks([code], store)

        # 2. 构造虚拟 position
        fake_position = {
            "code": code,
            "name": code,
            "market": market,
            "asset_type": "stock",
            "currency": "HKD" if market == "港股" else "CNY",
            "quantity": 0,
            "cost_price": 0,
            "industry": "",
            "style": "",
            "thesis": "(未在持仓中，外部分析)",
        }

        # 3. 拉行情 + 跑七维信号
        price_result = self.data_provider.get_price(code, market)
        current_price = price_result.get("price", 0)
        strategy_signals = self.signal_collector.collect_for_position(fake_position)
        industry_peers = self._get_industry_peers({"code": code, "industry_code": None, "industry": None})

        # 从 MongoDB 取名称
        try:
            doc = store.get_basic(code)
            if doc:
                fake_position["name"] = doc.get("name", code)
                fake_position["industry"] = doc.get("industry", "")
                fake_position["industry_code"] = doc.get("industry_code", "")
                industry_peers = self._get_industry_peers({
                    "code": code,
                    "industry_code": doc.get("industry_code", ""),
                    "industry": doc.get("industry", ""),
                })
        except Exception:
            pass

        # 趋势信号
        try:
            trend_signal = self.data_provider.get_trend_signal(code, market)
        except Exception:
            trend_signal = {"available": False, "reason": "趋势数据获取失败"}

        # 报价快照
        try:
            quote = self.data_provider.get_quote_snapshot(code, market)
        except Exception:
            quote = {"available": False}

        # 新闻
        try:
            news = self.data_provider.get_news(code, limit=10)
        except Exception:
            news = []

        # 资金流向
        try:
            moneyflow = self.data_provider.get_moneyflow(code)
        except Exception:
            moneyflow = {"available": False}

        # Tushare 专属信号
        try:
            tushare_signals = self.data_provider.get_tushare_signals(code, market)
        except Exception:
            tushare_signals = {"available": False}
        # 港股：用 MongoDB 缓存的南向数据覆盖
        if market == "港股" and tushare_signals.get("available"):
            try:
                sb_rows = list(store.db["stock_southbound"].find(
                    {"code": code}, sort=[("trade_date", -1)]).limit(60))
                if sb_rows:
                    sb_latest = sb_rows[0]
                    ratio_now = sb_latest.get("ratio")
                    ratio_prev = sb_rows[1].get("ratio") if len(sb_rows) > 1 else None
                    latest_date = sb_latest.get("trade_date")
                    ratio_month = None
                    if latest_date and len(sb_rows) > 1:
                        try:
                            t_latest = datetime.strptime(latest_date, "%Y%m%d")
                            t_target = t_latest - timedelta(days=30)
                            best = None
                            for r in sb_rows[1:]:
                                t_r = datetime.strptime(r.get("trade_date", ""), "%Y%m%d")
                                if t_r <= t_target:
                                    best = r
                                    break
                            if best is None and len(sb_rows) > 1:
                                best = sb_rows[-1]
                            if best:
                                ratio_month = round(ratio_now - best.get("ratio", 0), 3)
                        except Exception:
                            pass
                    tushare_signals["hk_hold"] = {
                        "vol": sb_latest.get("vol"),
                        "ratio": ratio_now,
                        "ratio_change": round(ratio_now - ratio_prev, 3)
                        if ratio_now is not None and ratio_prev is not None else None,
                        "ratio_change_monthly": ratio_month,
                    }
            except Exception:
                pass
        # 股息率（A股 daily_basic，仅 stock 类型）
        dividend_yield = None
        if fake_position.get("asset_type") == "stock":
            try:
                dv = store.db["stock_dividend"].find_one(
                    {"code": code}, sort=[("trade_date", -1)])
                if dv:
                    dividend_yield = dv.get("dv_ttm") or dv.get("dv_ratio")
            except Exception:
                pass

        # PE 自身分位
        try:
            sig = store.db["stock_signals"].find_one({"code": code})
            pe_self = sig.get("pe_percentile") if sig else None
        except Exception:
            pe_self = None

        return {
            "code": code,
            "name": fake_position["name"],
            "market": market,
            "current_price": current_price,
            "price_source": price_result.get("source"),
            "price_available": price_result.get("available", False),
            "quote_snapshot": quote,
            "trend_signal": trend_signal,
            "strategy_signals": strategy_signals,
            "industry_peers": industry_peers,
            "news": news,
            "moneyflow": moneyflow,
            "tushare_signals": tushare_signals,
            "pe_percentile_self": pe_self,
            "dividend_yield": dividend_yield,
            "market_breadth": self._get_market_breadth(),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _get_data_service(self) -> Optional[DataCapabilityService]:
        if self._data_service_error:
            return None
        if self._data_service is not None:
            return self._data_service
        try:
            self._data_service = DataCapabilityService()
            return self._data_service
        except Exception as exc:
            self._data_service_error = str(exc)
            return None

    def _data_needs_for_asset(self, asset: Dict[str, Any]) -> List[str]:
        if asset.get("asset_type") == "etf":
            return ["basic", "quotes"]
        return ["basic", "quotes", "financial"]

    def _data_status_for_assets(self, assets: List[Dict[str, Any]], ensure_data: bool = False) -> Dict[str, Any]:
        service = self._get_data_service()
        if service is None:
            return {
                "available": False,
                "reason": self._data_service_error or "数据能力层不可用",
                "items": [],
            }

        items = []
        for asset in assets:
            try:
                needs = self._data_needs_for_asset(asset)
                if ensure_data and asset.get("asset_type") == "stock":
                    result = service.ensure_asset_data(asset, needs=needs, refresh=False)
                    status = result.get("asset", {})
                    status["synced"] = result.get("synced", False)
                    if result.get("sync_result"):
                        status["sync_result"] = result["sync_result"]
                else:
                    status = service.get_data_status(asset, needs=needs)
                    status["synced"] = False
                items.append(status)
            except Exception as exc:
                items.append({
                    "code": str(asset.get("code", "")),
                    "name": asset.get("name"),
                    "market": asset.get("market"),
                    "ready": False,
                    "missing": ["unknown"],
                    "reason": str(exc),
                    "synced": False,
                })

        return {
            "available": True,
            "items": items,
            "ready": sum(1 for item in items if item.get("ready")),
            "not_ready": sum(1 for item in items if not item.get("ready")),
        }

    def _ensure_data_for_code(self, code: str) -> None:
        service = self._get_data_service()
        if service is None:
            return
        normalized = code.strip()
        for asset in self.data.get("positions", []) or []:
            if str(asset.get("code", "")).strip() == normalized:
                if asset.get("asset_type") == "stock":
                    service.ensure_asset_data(asset, needs=self._data_needs_for_asset(asset), refresh=False)
                return

    def _find_data_status(self, data_status: Dict[str, Any], code: str) -> Dict[str, Any]:
        for item in data_status.get("items", []) or []:
            if str(item.get("code", "")).strip() == code:
                return item
        return {}

    def _evaluate_goal_progress(self, summary: Dict[str, Any], constraints: Dict[str, Any]) -> Dict[str, Any]:
        profit_target = constraints["profit_target_pct"]
        max_drawdown = constraints["max_drawdown_pct"]
        pnl_pct = summary.get("unrealized_pnl_pct", 0)

        target_progress_pct = pnl_pct / profit_target * 100 if profit_target else 0
        remaining_to_target_pct = max(profit_target - pnl_pct, 0)
        drawdown_usage_pct = abs(min(pnl_pct, 0)) / max_drawdown * 100 if max_drawdown else 0
        remaining_drawdown_pct = max(max_drawdown + pnl_pct, 0) if pnl_pct < 0 else max_drawdown

        if pnl_pct >= profit_target:
            state = "目标兑现"
            action_bias = "优先保护利润，考虑分批止盈或上移保护线"
        elif pnl_pct >= profit_target * 0.6:
            state = "利润保护"
            action_bias = "减少新增风险，优先守住已有利润"
        elif pnl_pct <= -max_drawdown:
            state = "回撤控制"
            action_bias = "先降风险，不新增仓位"
        elif pnl_pct < 0:
            state = "回撤内修复"
            action_bias = "复核持仓逻辑，避免盲目补仓"
        else:
            state = "围绕目标推进"
            action_bias = "保留上行弹性，同时控制回撤"

        return {
            "state": state,
            "action_bias": action_bias,
            "portfolio_pnl_pct": round(pnl_pct, 2),
            "profit_target_pct": round(profit_target, 2),
            "target_progress_pct": round(target_progress_pct, 2),
            "remaining_to_target_pct": round(remaining_to_target_pct, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "drawdown_usage_pct": round(drawdown_usage_pct, 2),
            "remaining_drawdown_pct": round(remaining_drawdown_pct, 2),
        }

    def _evaluate_position_goal_impact(
        self,
        row: Dict[str, Any],
        summary: Dict[str, Any],
        constraints: Dict[str, Any],
    ) -> Dict[str, Any]:
        total_assets = summary.get("total_assets", 0)
        if not total_assets:
            contribution_pct = 0
        else:
            contribution_pct = row.get("unrealized_pnl", 0) / total_assets * 100

        pnl_pct = row.get("unrealized_pnl_pct", 0)
        profit_target = constraints["profit_target_pct"]
        max_drawdown = constraints["max_drawdown_pct"]

        if pnl_pct >= profit_target:
            role = "已达到单票目标"
            interpretation = "这只持仓已经完成目标收益，应优先考虑利润保护。"
        elif pnl_pct >= profit_target * 0.6:
            role = "接近单票目标"
            interpretation = "这只持仓对目标有明显贡献，继续持有时应考虑上移保护线。"
        elif pnl_pct <= -max_drawdown:
            role = "拖累目标"
            interpretation = "这只持仓已经达到最大回撤容忍，应优先处理风险。"
        elif pnl_pct < 0:
            role = "轻度拖累"
            interpretation = "这只持仓当前拖累目标，但仍在回撤容忍范围内。"
        else:
            role = "正向贡献"
            interpretation = "这只持仓正在帮助组合接近盈利目标。"

        return {
            "role": role,
            "interpretation": interpretation,
            "position_pnl_pct": round(pnl_pct, 2),
            "portfolio_contribution_pct": round(contribution_pct, 2),
            "target_progress_pct": round(pnl_pct / profit_target * 100, 2) if profit_target else 0,
            "drawdown_usage_pct": round(abs(min(pnl_pct, 0)) / max_drawdown * 100, 2) if max_drawdown else 0,
        }

    def _build_decision_dimensions(
        self,
        position: Dict[str, Any],
        review: Dict[str, Any],
        goal_impact: Dict[str, Any],
        strategy_signals: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        summary = review["summary"]
        constraints = review["constraints"]
        signals = strategy_signals.get("signals", {})
        factor = signals.get("factor", {})
        trend = signals.get("trend", {})
        event = signals.get("event", {})

        price = position.get("current_price_decision", position.get("current_price", 0))
        decision_currency = position.get("decision_currency", position.get("currency", "CNY"))
        pnl_pct = position.get("unrealized_pnl_pct", 0)
        weight = position.get("weight_pct", 0)

        industry_weight = self._lookup_exposure_weight(review.get("industry_exposure", []), "industry", position.get("industry"))
        market_weight = self._lookup_exposure_weight(review.get("market_exposure", []), "market", position.get("market"))
        style_weight = self._lookup_exposure_weight(review.get("style_exposure", []), "style", position.get("style"))

        price_factors = []
        risk_factors = []
        missing = []
        stop_loss = position.get("stop_loss_price", 0)
        add_price = position.get("add_price", 0)
        trim_price = position.get("trim_price", 0)
        take_profit = position.get("take_profit_price", 0)
        if stop_loss:
            price_factors.append(f"距离止损价 {stop_loss} {decision_currency}: {_pct((price - stop_loss) / stop_loss * 100) if stop_loss else 'N/A'}")
        if add_price:
            price_factors.append(f"距离加仓观察价 {add_price} {decision_currency}: {_pct((price - add_price) / add_price * 100) if add_price else 'N/A'}")
        if trim_price:
            price_factors.append(f"距离减仓观察价 {trim_price} {decision_currency}: {_pct((trim_price - price) / price * 100) if price else 'N/A'}")
        if take_profit:
            price_factors.append(f"距离止盈价 {take_profit} {decision_currency}: {_pct((take_profit - price) / price * 100) if price else 'N/A'}")

        if not position.get("price_available"):
            missing.append("实时价格不可用，风控判断只能按成本估算")
        if trend.get("available"):
            trend_status = trend.get("status", "趋势未判断")
            trend_support = trend.get("supporting_factors", [])
            trend_risk = trend.get("risk_factors", [])
        else:
            trend_status = "趋势数据不足"
            trend_support = []
            trend_risk = []
            missing.append(f"趋势: {trend.get('reason', '不可用')}")

        factor_support = factor.get("supporting_factors", []) if factor.get("available") else []
        factor_risk = factor.get("risk_factors", []) if factor.get("available") else []
        factor_missing = [] if factor.get("available") else [f"多因子: {factor.get('reason', '不可用')}"]
        event_support = event.get("supporting_factors", []) if event.get("available") else []
        event_risk = event.get("risk_factors", []) if event.get("available") else []
        event_missing = [] if event.get("available") else [f"事件/基本面: {event.get('reason', '不可用')}"]

        # 行业动量维度（sector_radar 热力图，替代旧周期评分）
        ind_momentum = self._ensure_industry_momentum()
        ind_name = position.get("industry", "")
        ind_momentum_score = ind_momentum.get(ind_name) if ind_name else None
        if ind_momentum_score is not None:
            if ind_momentum_score >= 0.7:
                ind_momentum_status = "🔥领先"
            elif ind_momentum_score >= 0.4:
                ind_momentum_status = "📈改善"
            else:
                ind_momentum_status = "❄️滞后"
            ind_supporting = [f"{ind_name} 行业动量分 {ind_momentum_score:.3f}"]
            ind_risks = [] if ind_momentum_score >= 0.4 else [f"{ind_name} 行业动量偏弱 ({ind_momentum_score:.3f})"]
        else:
            ind_momentum_status = "无数据"
            ind_supporting = []
            ind_risks = []

        dimensions = [
            {
                "name": "目标维度",
                "status": goal_impact.get("role", "未判断"),
                "conclusion": goal_impact.get("interpretation", "未判断"),
                "supporting_factors": [
                    f"个股盈亏 {_pct(goal_impact.get('position_pnl_pct', 0))}",
                    f"对组合贡献 {_pct(goal_impact.get('portfolio_contribution_pct', 0))}",
                    f"组合目标状态 {review.get('goal_progress', {}).get('state', '未知')}",
                ],
                "risk_factors": [] if pnl_pct >= 0 else [f"当前仍拖累组合目标 {_pct(abs(pnl_pct))}"],
                "missing_data": [],
            },
            {
                "name": "组合角色维度",
                "status": position.get("style", "未填写"),
                "conclusion": f"该资产承担 {position.get('style', '未分类')} 角色，行业为 {position.get('industry', '未分类')}。",
                "supporting_factors": [position.get("thesis", "未填写买入逻辑")],
                "risk_factors": [] if style_weight < 30 else [f"{position.get('style')} 风格暴露较高: {_pct(style_weight)}"],
                "missing_data": [] if position.get("thesis") else ["缺少买入逻辑 thesis"],
            },
            {
                "name": "仓位约束维度",
                "status": "护栏内" if weight <= constraints["max_single_position_pct"] else "超过单票护栏",
                "conclusion": f"单票仓位 {_pct(weight)}，行业暴露 {_pct(industry_weight)}，市场暴露 {_pct(market_weight)}。",
                "supporting_factors": [] if weight > constraints["max_single_position_pct"] else ["未超过单票仓位护栏"],
                "risk_factors": self._position_risks(position, weight, industry_weight, market_weight, style_weight, constraints),
                "missing_data": [],
            },
            {
                "name": "价格与风控维度",
                "status": self._price_zone(position),
                "conclusion": f"当前决策价 {price} {decision_currency}，按止损/加仓/减仓/止盈条件判断。",
                "supporting_factors": price_factors,
                "risk_factors": risk_factors,
                "missing_data": missing[:],
            },
            {
                "name": "趋势维度",
                "status": trend_status,
                "conclusion": self._trend_conclusion(trend),
                "supporting_factors": trend_support,
                "risk_factors": trend_risk,
                "missing_data": [] if trend.get("available") else [trend.get("reason", "趋势数据不可用")],
            },
            {
                "name": "行业动量维度",
                "status": ind_momentum_status,
                "conclusion": f"所属行业 {position.get('industry', '未知')} 动量分 {ind_momentum_score}" if ind_momentum_score is not None else "行业动量数据待接入",
                "supporting_factors": ind_supporting,
                "risk_factors": ind_risks,
                "missing_data": [] if ind_momentum_score is not None else ["sector_radar 信号缓存不可用"],
            },
            {
                "name": "事件/基本面维度",
                "status": "可用" if event.get("available") or factor.get("available") else "数据不足",
                "conclusion": self._event_and_basic_conclusion(event, factor),
                "supporting_factors": factor_support + event_support,
                "risk_factors": factor_risk + event_risk,
                "missing_data": factor_missing + event_missing,
            },
        ]
        return dimensions

    def _build_agent_context(
        self,
        position: Dict[str, Any],
        review: Dict[str, Any],
        goal_impact: Dict[str, Any],
        strategy_signals: Dict[str, Any],
        decision_dimensions: List[Dict[str, Any]],
        rule_precheck: Dict[str, Any],
    ) -> Dict[str, Any]:
        summary = review.get("summary", {})
        return {
            "purpose": "strategy_data_context",
            "portfolio_goal": review.get("goal_progress", {}),
            "portfolio_summary": {
                "total_assets": summary.get("total_assets"),
                "cash_pct": summary.get("cash_pct"),
                "position_value": summary.get("position_value"),
                "unrealized_pnl_pct": summary.get("unrealized_pnl_pct"),
                "base_currency": summary.get("base_currency"),
                "fx_rates_to_base": summary.get("fx_rates_to_base", {}),
            },
            "constraints": review.get("constraints", {}),
            "position": {
                "code": position.get("code"),
                "name": position.get("name"),
                "market": position.get("market"),
                "asset_type": position.get("asset_type"),
                "style": position.get("style"),
                "industry": position.get("industry"),
                "thesis": position.get("thesis"),
                "current_price": position.get("current_price"),
                "current_price_decision": position.get("current_price_decision"),
                "currency": position.get("currency"),
                "decision_currency": position.get("decision_currency"),
                "cost_price": position.get("cost_price"),
                "cost_currency": position.get("cost_currency"),
                "weight_pct": position.get("weight_pct"),
                "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
                "stop_loss_price": position.get("stop_loss_price"),
                "add_price": position.get("add_price"),
                "trim_price": position.get("trim_price"),
                "take_profit_price": position.get("take_profit_price"),
                "invalid_conditions": position.get("invalid_conditions", []),
            },
            "goal_impact": goal_impact,
            "decision_dimensions": decision_dimensions,
            "rule_precheck": rule_precheck,
            "strategy_signal_context": strategy_signals.get("llm_context", {}),
        }

    def _build_cycle_summary(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按持仓行业汇总行业动量分（sector_radar 热力图），替代旧风格周期评分。

        不再使用三个代表资产手工打分；直接引用 sector_radar 五因子标准化动量分。
        """
        momentum = self._ensure_industry_momentum()
        seen: Dict[str, Dict[str, Any]] = {}
        for row in positions:
            ind = row.get("industry", "未分类")
            style = row.get("style", "未分类")
            if ind not in seen:
                ms = momentum.get(ind)
                if ms is not None:
                    if ms >= 0.7:
                        status = "🔥领先"
                    elif ms >= 0.4:
                        status = "📈改善"
                    else:
                        status = "❄️滞后"
                else:
                    status = "无数据"
                seen[ind] = {
                    "industry": ind,
                    "style": style,
                    "momentum_score": ms,
                    "status": status,
                }
        styles = list(seen.values())
        summary_parts = []
        for s in styles:
            ms = s["momentum_score"]
            ms_str = f"{ms:.3f}" if ms is not None else "N/A"
            summary_parts.append(f"{s['industry']}({s['style']}): {s['status']}({ms_str})")
        return {
            "styles": styles,
            "summary": "；".join(summary_parts),
        }

    def _suggest_dynamic_stop(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """基于市场数据计算动态止损建议，识别所有下方支撑层。

        核心逻辑：找出当前价格下方的所有支撑位，推荐放在最强支撑
        （最近的、有技术意义的价位）下方 1-2% 的位置。

        支撑位来源:
          - MA20 / MA60 / MA120 均线
          - 20日 / 60日 最低价 (swing low)
          - 52周低点
          - 布林下轨
          - 整数关口
        """
        trend = row.get("trend_signal") or row.get("strategy_signals", {}).get("trend") or {}
        current_price = row.get("current_price", 0)
        static_stop = row.get("stop_loss_price") or 0
        style = row.get("style", "")
        code = row.get("code", "")

        if not current_price or current_price <= 0:
            return {"available": False, "reason": "无有效现价"}

        # 收集所有支撑位 (name, price, strength)
        # strength: 1=弱(可能破), 3=强(多次验证)
        supports: List[tuple] = []

        if trend.get("available"):
            ma = trend.get("ma", {}) or {}

            # MA 均线支撑（趋势股的核心支撑）
            for ma_name in ["ma20", "ma60"]:
                ma_val = ma.get(ma_name)
                if ma_val and 0 < ma_val < current_price:
                    strength = 3 if ma_name == "ma60" else 2  # MA60 比 MA20 更强
                    supports.append((f"{ma_name.upper()}", ma_val, strength))

            # 布林下轨
            boll_pct = trend.get("technical_indicators", {}).get("bollinger", {}).get("percent_b")
            if boll_pct is not None and 0 < boll_pct < 0.3:
                # 反推下轨价格: %B = (price - lower) / (upper - lower)
                # 简化：下轨 ≈ 现价 / (1 + 带宽/2)
                ma20_v = ma.get("ma20")
                if ma20_v and ma20_v > 0:
                    boll_low = current_price * 0.93  # 默认估算
                    if boll_low < current_price:
                        supports.append(("布林下轨", round(boll_low, 2), 2))
                elif current_price > 0:
                    boll_low = current_price * 0.93
                    supports.append(("布林下轨", round(boll_low, 2), 1))

        # 52周低点（重要心理支撑）
        try:
            basic = self.data_provider.mongo.get_basic(code)
            if basic:
                low52 = basic.get("fifty_two_week_low")
                if low52 and 0 < low52 < current_price:
                    supports.append(("52周低点", float(low52), 3))
        except Exception:
            pass

        # 20日最低点 + 60日最低点
        for days, label, strength in [(20, "20日最低", 1), (60, "60日最低", 2)]:
            try:
                quotes = self.data_provider.mongo.get_recent_quotes(code, days)
                if quotes and len(quotes) >= days // 2:
                    lows = [q.get("low", q.get("close", 0)) for q in quotes if q.get("low")]
                    if lows:
                        swing_low = min(lows)
                        if 0 < swing_low < current_price:
                            supports.append((label, round(swing_low, 2), strength))
            except Exception:
                pass

        # 整数关口（心理支撑）
        price_int = int(current_price)
        if price_int >= 100:
            step = 10
        elif price_int >= 10:
            step = 1
        else:
            step = 1
        # 找下方的整数关口
        round_lvl = (current_price // step) * step
        if isinstance(round_lvl, float):
            round_lvl = int(round_lvl)
        if round_lvl < current_price and round_lvl > 0:
            supports.append((f"整数关口({round_lvl})", float(round_lvl), 1))

        # ATR 通道止损（波段股参考）
        if trend.get("available"):
            vol = trend.get("volatility_20d")
            if vol and vol > 0:
                ret_20d = (trend.get("returns", {}) or {}).get("return_20d") or 0
                est_high = current_price * (1 + abs(ret_20d) / 100 + vol / 200)
                atr_stop = est_high * (1 - 2 * vol / 100)
                if 0 < atr_stop < current_price:
                    supports.append(("ATR通道(2x)", round(atr_stop, 2), 2))

        if not supports:
            return {
                "available": True,
                "method": "无可用支撑数据",
                "price": static_stop if static_stop > 0 else round(current_price * 0.85, 2),
                "current_stop": static_stop,
                "distance_pct": round((current_price - (static_stop or current_price * 0.85)) / current_price * 100, 1),
                "supports": [],
                "all_methods": {},
            }

        # 按价格降序排列（最靠近现价的支撑排最前）
        supports.sort(key=lambda x: x[1], reverse=True)

        # 选推荐止损价：找最近的那层强支撑，把止损放在它下方 ~2%
        recommended = None
        method = "?"

        # 优先：第一层强支撑（strength >= 2）
        for name, price, strength in supports:
            if strength >= 2:
                recommended = round(price * 0.98, 2)  # 强支撑下方 2%
                method = f"{name}下方2%"
                break

        # 兜底：最近的那层支撑（任何强度）
        if recommended is None and supports:
            name, price, _ = supports[0]
            recommended = round(price * 0.98, 2)
            method = f"{name}下方2%"

        # 如果推荐止损高于静态止损太多，用静态止损兜底
        if recommended is None:
            recommended = static_stop if static_stop > 0 else round(current_price * 0.85, 2)
            method = "无支撑-兜底"

        return {
            "available": True,
            "method": method,
            "price": round(recommended, 2),
            "current_stop": static_stop if static_stop > 0 else None,
            "distance_pct": round((current_price - recommended) / current_price * 100, 2),
            "supports": [{"name": s[0], "price": s[1], "strength": s[2]} for s in supports],
        }

    def _evaluate_add_conditions(self, row: Dict[str, Any], summary: Dict[str, Any],
                                  constraints: Dict[str, Any]) -> Dict[str, Any]:
        """评估加仓条件组合（替代固定加仓价判断）。

        Returns 五个条件的逐一状态，供 Agent 综合判断。
        """
        signals = row.get("strategy_signals", {}).get("signals", {})
        trend = signals.get("trend", {})
        cycle = signals.get("cycle", {})
        weight = row.get("weight_pct", 0)
        cash_pct = summary.get("cash_pct", 0)
        min_cash = constraints.get("min_cash_pct", 10)
        max_single = constraints.get("max_single_position_pct", 25)

        # 条件1: 趋势 — 站上MA20 或 趋势不是"偏弱"
        trend_status = trend.get("status", "")
        ma20 = (trend.get("ma_decision") or trend.get("ma") or {}).get("ma20")
        price = row.get("current_price_decision", row.get("current_price", 0))
        trend_ok = (trend_status not in ("趋势偏弱",)) and (ma20 is None or price >= ma20)

        # 条件2: RSI — >30 且不在深度超卖
        rsi = (trend.get("technical_indicators") or {}).get("rsi14")
        rsi_ok = rsi is not None and rsi >= 35

        # 条件3: 行业动量 — 所属行业不处于滞后区间（momentum >= 0.3）
        industry = row.get("industry", "")
        momentum = self._ensure_industry_momentum()
        ind_score = momentum.get(industry) if industry else None
        # 无行业映射时降级放行（不卡周期）
        cycle_ok = ind_score is None or ind_score >= 0.3

        # 条件4: 仓位 — 未超上限
        weight_ok = weight < max_single * 0.8  # <20% 绿灯

        # 条件5: 现金 — 可动用空间 > 0
        cash_ok = (cash_pct - min_cash) > 0

        met = sum([trend_ok, rsi_ok, cycle_ok, weight_ok, cash_ok])
        return {
            "conditions": [
                {"name": "趋势站上MA20", "met": trend_ok,
                 "detail": f"status={trend_status}, MA20={ma20}, price={price}"},
                {"name": "RSI脱离弱势(≥35)", "met": rsi_ok,
                 "detail": f"RSI14={rsi}"},
                {"name": "行业动量不滞后(≥0.3)", "met": cycle_ok,
                 "detail": f"industry={industry}, momentum={ind_score}"},
                {"name": f"仓位<{max_single*0.8:.0f}%", "met": weight_ok,
                 "detail": f"当前{weight:.1f}%"},
                {"name": "有可用现金", "met": cash_ok,
                 "detail": f"可动用{cash_pct - min_cash:.1f}%"},
            ],
            "met": met,
            "total": 5,
            "verdict": "多数条件满足，可考虑加仓" if met >= 4 else
                       "部分条件满足，需 Agent 综合判断" if met >= 3 else
                       "多数条件不满足，不建议加仓",
        }

    def _evaluate_trim_conditions(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """评估止盈条件组合（动态，非固定价）。

        Returns 四个条件的逐一状态。
        """
        signals = row.get("strategy_signals", {}).get("signals", {})
        trend = signals.get("trend", {})
        profit_target = 20  # 默认浮盈20%触发止盈评估

        pnl_pct = row.get("unrealized_pnl_pct", 0)
        rsi = (trend.get("technical_indicators") or {}).get("rsi14")
        trend_status = trend.get("status", "")

        # 条件1: 浮盈超过15%
        profit_ok = pnl_pct >= 15

        # 条件2: RSI超买(>70) 或接近超买(>65)
        rsi_ok = rsi is not None and rsi >= 65

        # 条件3: 中期趋势转弱（60日收益为负 或 趋势偏弱）
        returns = trend.get("returns", {})
        ret_60d = returns.get("return_60d")
        trend_weakening = (ret_60d is not None and ret_60d < 0) or trend_status == "趋势偏弱"

        # 条件4: 仓位偏高（>15%），止盈后释放空间
        weight = row.get("weight_pct", 0)
        weight_high = weight > 15

        met = sum([profit_ok, rsi_ok, trend_weakening, weight_high])
        return {
            "conditions": [
                {"name": f"浮盈>15%(当前{_pct(pnl_pct)})", "met": profit_ok,
                 "detail": ""},
                {"name": f"RSI≥65(当前{rsi})", "met": rsi_ok,
                 "detail": ""},
                {"name": f"60日收益<0(当前{ret_60d}%)" if ret_60d is not None else "60日收益未知",
                 "met": trend_weakening,
                 "detail": ""},
                {"name": f"仓位>15%(当前{weight:.1f}%)", "met": weight_high,
                 "detail": ""},
            ],
            "met": met,
            "total": 4,
            "verdict": "多个止盈条件触发，建议评估锁定利润" if met >= 3 else
                       "部分条件触发，可考虑分批止盈" if met >= 2 else
                       "止盈条件尚不充分",
        }

    def _get_sector_heatmap_summary(self) -> List[str]:
        """获取行业热力图摘要（Top 5），内嵌到 review 输出中。"""
        try:
            from sector_radar import SectorRadarEngine
            engine = SectorRadarEngine()
            groups, _, _ = engine.load_all_industry_data()
            metrics = [engine._compute_industry_metrics(g) for g in groups]
            valid = [m for m in metrics if m.get("available")]
            heatmap = engine.compute_heatmap(valid)
            lines = []
            for m in heatmap[:5]:
                icon = "🔥" if m.get("momentum_score", 0) >= 0.7 else "📈"
                lines.append(f"{icon}{m['industry']}({m.get('breadth_pct',0):.0f}%)")
            return lines
        except Exception:
            return []

    def _get_market_breadth(self) -> Dict[str, Any]:
        """大盘宽度：全市场站上 MA20/MA60 的股票占比。"""
        try:
            cutoff = (_utc_now() - __import__("datetime").timedelta(days=2)).strftime("%Y-%m-%d")
            sigs = list(self.data_provider.mongo.db["stock_signals"].find(
                {"computed_at": {"$gte": cutoff}},
                {"code": 1, "trend.status": 1, "_id": 0}
            ))
            if len(sigs) < 100:
                return {"available": False, "reason": "信号缓存不足"}
            above_ma20 = 0
            above_ma60 = 0
            for sig in sigs:
                status = (sig.get("trend") or {}).get("status", "")
                if status == "趋势较强":
                    above_ma20 += 1
                    above_ma60 += 1
                elif status in ("震荡分歧", "修复中"):
                    above_ma20 += 1
            total = max(len(sigs), 1)
            pct20 = round(above_ma20 / total * 100, 1)
            pct60 = round(above_ma60 / total * 100, 1)
            width = "强" if pct60 > 50 else "正常" if pct60 > 30 else "偏弱" if pct60 > 15 else "弱"
            return {"available": True, "above_ma20_pct": pct20, "above_ma60_pct": pct60,
                    "width": width, "sample": total}
        except Exception:
            return {"available": False, "reason": "计算失败"}

    def _get_correlation_matrix(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """组合相关性矩阵（基于近60日日收益）。"""
        import numpy as np
        codes = [p["code"] for p in positions]
        names = {p["code"]: p.get("name", p["code"]) for p in positions}
        returns = {}
        for p in positions:
            code = p["code"]
            bars = self.data_provider.get_bars(code, p.get("market", "A股"), days=60).get("bars", [])
            if len(bars) < 30:
                continue
            closes = [b["close"] for b in bars if b.get("close", 0) > 0]
            if len(closes) < 30:
                continue
            rets = [(closes[i] - closes[i+1]) / closes[i+1] * 100 for i in range(len(closes)-1)]
            returns[code] = rets

        if len(returns) < 2:
            return {"available": False}

        # 对齐长度
        min_len = min(len(r) for r in returns.values())
        aligned = {k: v[-min_len:] for k, v in returns.items()}
        matrix = np.corrcoef(list(aligned.values()))
        pairs = []
        for i, ci in enumerate(codes):
            for j, cj in enumerate(codes):
                if i < j and ci in aligned and cj in aligned:
                    corr = round(float(matrix[i][j]), 2)
                    if abs(corr) > 0.5:  # 只显示显著相关
                        pairs.append({"pair": f"{names[ci]}-{names[cj]}", "corr": corr})
        pairs.sort(key=lambda x: -abs(x["corr"]))
        return {"available": True, "pairs": pairs[:10] if pairs else [],
                "avg_corr": round(float(np.mean(np.abs(matrix[np.triu_indices_from(matrix, 1)]))), 2) if len(aligned) > 1 else None}

    def _lookup_exposure_weight(self, rows: List[Dict[str, Any]], key: str, value: Any) -> float:
        for row in rows:
            if row.get(key) == value:
                return _to_float(row.get("weight_pct"))
        return 0.0

    def _trend_status_from_factor(self, factor: Dict[str, Any]) -> str:
        scores = factor.get("factor_scores", {})
        momentum = _to_float(scores.get("momentum"), 0.5)
        composite = _to_float(factor.get("composite_score"), 0.5)
        if momentum >= 0.7 and composite >= 0.6:
            return "趋势较强"
        if momentum <= 0.4:
            return "趋势偏弱"
        return "趋势中性"

    def _trend_conclusion(self, trend: Dict[str, Any]) -> str:
        if not trend.get("available"):
            return trend.get("reason", "趋势数据不可用")
        returns = trend.get("returns", {})
        ma = trend.get("ma", {})
        interpretation = trend.get("interpretation", {})
        technical_summary = interpretation.get("technical_summary")
        if technical_summary:
            return f"{technical_summary}该结论仅为趋势事实摘要，最终动作需结合目标、仓位、多因子、周期和事件信号由大模型判断。"
        return (
            f"趋势状态 {trend.get('status', '未判断')}；"
            f"5日涨跌幅 {returns.get('return_5d')}%，"
            f"20日涨跌幅 {returns.get('return_20d')}%，"
            f"60日涨跌幅 {returns.get('return_60d')}%；"
            f"MA20 {ma.get('ma20')}。"
        )

    def _position_risks(
        self,
        position: Dict[str, Any],
        weight: float,
        industry_weight: float,
        market_weight: float,
        style_weight: float,
        constraints: Dict[str, Any],
    ) -> List[str]:
        risks = []
        if weight > constraints["max_single_position_pct"]:
            risks.append(f"单票仓位超过护栏: {_pct(weight)}")
        elif weight > constraints["max_single_position_pct"] * 0.8:
            risks.append(f"单票仓位接近护栏: {_pct(weight)}")
        if industry_weight > constraints["max_industry_position_pct"]:
            risks.append(f"行业暴露超过护栏: {_pct(industry_weight)}")
        elif industry_weight > constraints["max_industry_position_pct"] * 0.8:
            risks.append(f"行业暴露偏高: {_pct(industry_weight)}")
        if market_weight > 40:
            risks.append(f"{position.get('market')} 市场暴露较高: {_pct(market_weight)}")
        if style_weight > 30:
            risks.append(f"{position.get('style')} 风格暴露较高: {_pct(style_weight)}")
        return risks

    def _price_zone(self, position: Dict[str, Any]) -> str:
        price = position.get("current_price_decision", position.get("current_price", 0))
        if not position.get("price_available"):
            return "价格未确认"
        if position.get("stop_loss_price") and price <= position["stop_loss_price"]:
            return "风险区"
        if position.get("take_profit_price") and price >= position["take_profit_price"]:
            return "兑现区"
        if position.get("trim_price") and price >= position["trim_price"]:
            return "减仓观察区"
        if position.get("add_price") and price <= position["add_price"]:
            return "加仓观察区"
        return "安全观察区"

    def _event_conclusion(self, event: Dict[str, Any]) -> str:
        if not event.get("available"):
            return event.get("reason", "事件/基本面数据不可用")
        sentiment = _to_float(event.get("sentiment_score"))
        negative_keywords = event.get("negative_keywords", []) or []
        positive_keywords = event.get("positive_keywords", []) or []
        if sentiment > 0.2 and negative_keywords:
            return (
                "近期事件/新闻整体偏正面，但同时出现负面关键词 "
                f"({'、'.join(negative_keywords[:5])})，需要确认风险是否与公司本身直接相关。"
            )
        if sentiment > 0.2:
            return "近期事件/新闻偏正面，可作为持有逻辑的辅助支撑。"
        if sentiment > 0 and negative_keywords and positive_keywords:
            return "近期事件/新闻多空混合，不能单独作为加仓依据，需要等待更多确认。"
        if sentiment < -0.2:
            return "近期事件/新闻偏负面，需要复核买入逻辑。"
        if negative_keywords:
            return f"近期事件/新闻情绪中性但存在风险词 ({'、'.join(negative_keywords[:5])})，需要观察是否发酵。"
        return "近期事件/新闻情绪中性，暂不构成强交易信号。"

    def _event_and_basic_conclusion(self, event: Dict[str, Any], factor: Dict[str, Any]) -> str:
        if event.get("available"):
            return self._event_conclusion(event)
        if factor.get("available"):
            score = _to_float(factor.get("composite_score"), 0.5)
            if score >= 0.7:
                return "多因子基本面/估值/动量综合较强，可作为持有逻辑的辅助支撑。"
            if score <= 0.45:
                return "多因子综合偏弱，需要复核估值、成长、质量或动量是否拖累持仓逻辑。"
            return "多因子综合中性，暂不构成强交易信号。"
        return event.get("reason", "事件/基本面数据不可用")

    def save_report(self, report: Dict[str, Any], prefix: str = "portfolio_review") -> str:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(REPORTS_DIR, f"{prefix}_{timestamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return path

    def _calculate_account_values(
        self,
        account: Dict[str, Any],
        fx_rates: Dict[str, Any],
        position_value: float,
    ) -> Dict[str, Any]:
        cash = self._calculate_cash(account, fx_rates)
        configured_total_assets = _to_float(account.get("total_assets"))

        if configured_total_assets > 0:
            # 券商显示的总资产最权威。现金则反推为总资产 - 持仓市值。
            derived_cash = max(configured_total_assets - position_value, 0)
            return {
                "cash": derived_cash,
                "cash_source": "derived_from_total_assets",
                "total_assets": configured_total_assets,
                "total_assets_source": "account.total_assets",
            }

        return {
            "cash": cash,
            "cash_source": "cash_by_currency" if account.get("cash_by_currency") else "cash",
            "total_assets": cash + position_value,
            "total_assets_source": "cash_plus_positions",
        }

    def _collect_currencies(self, account: Dict[str, Any], positions: List[Dict[str, Any]]) -> List[str]:
        currencies = list((account.get("cash_by_currency") or {}).keys())
        currencies.extend(str(position.get("currency", "CNY")) for position in positions)
        return currencies

    def _calculate_cash(self, account: Dict[str, Any], fx_rates: Dict[str, Any]) -> float:
        cash_by_currency = account.get("cash_by_currency") or {}
        if not cash_by_currency:
            return _to_float(account.get("cash"))

        total = 0.0
        for currency, value in cash_by_currency.items():
            total += _to_float(value) * _to_float(fx_rates.get(currency), 1)
        return total

    def _build_position_rows(self, positions: List[Dict[str, Any]], fx_rates: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = []
        for item in positions:
            currency = item.get("currency", "CNY")
            fx_rate = _to_float(fx_rates.get(currency), 1)
            quantity = _to_float(item.get("quantity"))
            cost_price = _to_float(item.get("cost_price"))
            price_result = self.data_provider.get_price(
                code=str(item.get("code", "")).strip(),
                market=str(item.get("market", "A股")),
                asset_type=str(item.get("asset_type", "stock")),
                allow_akshare=bool(item.get("allow_akshare_price_fallback", False)),
                cost_price=cost_price,
            )
            current_price = _to_float(price_result.get("price"))
            cost_value = quantity * cost_price
            market_value = quantity * current_price
            # 折基础币种
            cost_value_base = cost_value * fx_rate
            market_value_base = market_value * fx_rate
            pnl_base = market_value_base - cost_value_base
            pnl_pct = pnl_base / cost_value_base * 100 if cost_value_base else 0

            row = dict(item)
            row.update({
                "code": str(item.get("code", "")).strip(),
                "market": item.get("market", "A股"),
                "asset_type": item.get("asset_type", "stock"),
                "currency": currency,
                "fx_rate_to_base": fx_rate,
                "quantity": quantity,
                "cost_price": cost_price,
                "current_price": current_price,
                "current_price_base": round(current_price * fx_rate, 4),
                "current_price_decision": round(current_price, 4),
                "price_available": bool(price_result.get("available")),
                "price_source": price_result.get("source", "unknown"),
                "price_error": price_result.get("error", ""),
                "cost_value": round(cost_value, 2),
                "market_value_local": round(market_value, 2),
                "cost_value_base": round(cost_value_base, 2),
                "market_value": round(market_value_base, 2),
                "unrealized_pnl": round(pnl_base, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "stop_loss_price": _to_float(item.get("stop_loss_price")),
            })
            # 从 MongoDB 补 PE/PB（供行业分位计算）
            try:
                doc = self.data_provider.mongo.get_basic(row["code"]) if self.data_provider.mongo_available else None
                if doc:
                    row["pe"] = _to_float(doc.get("pe"), None)
                    row["pb"] = _to_float(doc.get("pb"), None)
            except Exception:
                pass
            rows.append(row)
        return rows

    def _build_industry_rows(self, positions: List[Dict[str, Any]], total_assets: float) -> List[Dict[str, Any]]:
        exposure: Dict[str, float] = {}
        for row in positions:
            industry = row.get("industry") or "未分类"
            exposure[industry] = exposure.get(industry, 0.0) + row["market_value"]

        rows = []
        for industry, value in sorted(exposure.items(), key=lambda x: x[1], reverse=True):
            rows.append({
                "industry": industry,
                "market_value": round(value, 2),
                "weight_pct": round(value / total_assets * 100, 2) if total_assets else 0,
            })
        return rows

    def _build_group_rows(
        self,
        positions: List[Dict[str, Any]],
        total_assets: float,
        key: str,
        fallback: str,
    ) -> List[Dict[str, Any]]:
        exposure: Dict[str, float] = {}
        for row in positions:
            group = row.get(key) or fallback
            exposure[group] = exposure.get(group, 0.0) + row["market_value"]

        rows = []
        for group, value in sorted(exposure.items(), key=lambda x: x[1], reverse=True):
            rows.append({
                key: group,
                "market_value": round(value, 2),
                "weight_pct": round(value / total_assets * 100, 2) if total_assets else 0,
            })
        return rows

    def _portfolio_pnl_pct(self, positions: List[Dict[str, Any]]) -> float:
        cost = sum(row["cost_value_base"] for row in positions)
        pnl = sum(row["unrealized_pnl"] for row in positions)
        return round(pnl / cost * 100, 2) if cost else 0

    def _build_alerts(
        self,
        summary: Dict[str, Any],
        positions: List[Dict[str, Any]],
        industries: List[Dict[str, Any]],
        constraints: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        alerts: List[Dict[str, str]] = []
        min_cash_pct = constraints["min_cash_pct"]
        max_single = constraints["max_single_position_pct"]
        max_industry = constraints["max_industry_position_pct"]

        if summary["cash_pct"] < min_cash_pct:
            alerts.append({
                "level": "warning",
                "type": "cash_low",
                "message": f"现金比例 {_pct(summary['cash_pct'])} 低于目标 {_pct(min_cash_pct)}，新增买入需要谨慎。",
            })

        for row in positions:
            weight = row["market_value"] / summary["total_assets"] * 100 if summary["total_assets"] else 0
            row["weight_pct"] = round(weight, 2)
            if weight > max_single:
                alerts.append({
                    "level": "warning",
                    "type": "single_position_high",
                    "message": f"{row['code']} {row.get('name', '')} 单票仓位 {_pct(weight)} 超过目标 {_pct(max_single)}。",
                })
            if not row.get("price_available"):
                alerts.append({
                    "level": "info",
                    "type": "price_unavailable",
                    "message": f"{row['code']} {row.get('name', '')} 自动查价失败，当前按成本价估算仓位。原因: {row.get('price_error') or '未知'}",
                })
                continue
            if row["stop_loss_price"] and row["current_price"] <= row["stop_loss_price"]:
                alerts.append({
                    "level": "warning",
                    "type": "stop_loss_warning",
                    "message": f"{row['code']} {row.get('name', '')} 当前价 {row['current_price']} 已进入止损预警区 {row['stop_loss_price']}，需复核买入逻辑和趋势。",
                })

        for row in industries:
            if row["weight_pct"] > max_industry:
                alerts.append({
                    "level": "warning",
                    "type": "industry_high",
                    "message": f"{row['industry']} 行业仓位 {_pct(row['weight_pct'])} 超过目标 {_pct(max_industry)}。",
                })

        return alerts

    def _build_diagnosis(
        self,
        summary: Dict[str, Any],
        positions: List[Dict[str, Any]],
        industries: List[Dict[str, Any]],
        markets: List[Dict[str, Any]],
        asset_types: List[Dict[str, Any]],
        styles: List[Dict[str, Any]],
        constraints: Dict[str, Any],
    ) -> Dict[str, Any]:
        cash_pct = summary["cash_pct"]
        min_cash_pct = constraints["min_cash_pct"]
        max_single = constraints["max_single_position_pct"]
        max_industry = constraints["max_industry_position_pct"]
        profit_target = constraints["profit_target_pct"]
        max_drawdown = constraints["max_drawdown_pct"]
        available_to_deploy_pct = max(cash_pct - min_cash_pct, 0)
        portfolio_pnl_pct = summary.get("unrealized_pnl_pct", 0)
        target_progress_pct = portfolio_pnl_pct / profit_target * 100 if profit_target else 0
        drawdown_usage_pct = abs(min(portfolio_pnl_pct, 0)) / max_drawdown * 100 if max_drawdown else 0

        if cash_pct < min_cash_pct:
            cash_state = "现金偏低"
            buy_budget = "暂停新增仓位"
        elif available_to_deploy_pct < 5:
            cash_state = "现金刚好达标"
            buy_budget = "只适合非常小仓位试探"
        elif available_to_deploy_pct < 15:
            cash_state = "现金适中"
            buy_budget = "可小仓位寻找机会"
        else:
            cash_state = "现金充足"
            buy_budget = "可分批配置，但仍需保留安全垫"

        if portfolio_pnl_pct >= profit_target:
            goal_state = "已达到盈利目标"
        elif portfolio_pnl_pct >= profit_target * 0.6:
            goal_state = "接近盈利目标"
        elif portfolio_pnl_pct > 0:
            goal_state = "盈利推进中"
        elif portfolio_pnl_pct <= -max_drawdown:
            goal_state = "已触及最大回撤"
        elif portfolio_pnl_pct < 0:
            goal_state = "目标回撤内波动"
        else:
            goal_state = "目标刚开始"

        concentration_notes: List[str] = []
        avoid_directions: List[str] = []
        preferred_directions: List[str] = []

        top_positions = sorted(positions, key=lambda row: row.get("weight_pct", 0), reverse=True)
        for row in top_positions:
            weight = row.get("weight_pct", 0)
            if weight >= max_single:
                concentration_notes.append(f"{row['code']} {row.get('name', '')} 单票仓位已超过上限，不宜继续加仓。")
                avoid_directions.append(f"继续加仓 {row.get('name', row['code'])}")
            elif weight >= max_single * 0.8:
                concentration_notes.append(f"{row['code']} {row.get('name', '')} 仓位接近单票上限，后续以持有或减仓观察为主。")

        if industries:
            top_industry = industries[0]
            if top_industry["weight_pct"] >= max_industry:
                concentration_notes.append(f"{top_industry['industry']} 行业已超过行业上限，新增仓位应避开同方向。")
                avoid_directions.append(f"继续增加{top_industry['industry']}仓位")
            elif top_industry["weight_pct"] >= max_industry * 0.75:
                concentration_notes.append(f"{top_industry['industry']} 行业占比较高，新增同类资产需要谨慎。")

        market_map = {row["market"]: row["weight_pct"] for row in markets if "market" in row}
        hk_weight = market_map.get("港股", 0)
        a_weight = market_map.get("A股", 0)
        if hk_weight >= 35:
            concentration_notes.append("港股仓位偏高，新增港股成长资产需要更高确定性。")
            avoid_directions.append("继续增加港股成长仓位")
        elif hk_weight >= 30:
            concentration_notes.append("港股仓位已经不低，新增仓位最好优先考虑低相关方向。")

        style_map = {row["style"]: row["weight_pct"] for row in styles if "style" in row}
        cyclical_weight = sum(weight for style, weight in style_map.items() if "资源" in style or "周期" in style)
        growth_weight = sum(weight for style, weight in style_map.items() if "科技" in style or "成长" in style)
        defensive_weight = sum(weight for style, weight in style_map.items() if "红利" in style or "防御" in style)

        if cyclical_weight >= 20:
            avoid_directions.append("继续追高资源周期资产")
        if growth_weight + hk_weight >= 45:
            avoid_directions.append("继续提高高弹性成长资产占比")
        if defensive_weight < 25:
            preferred_directions.append("红利防御或低波动资产")
        if a_weight < 50 and hk_weight >= 30:
            preferred_directions.append("A股低相关方向")
        if available_to_deploy_pct > 5:
            preferred_directions.append("等待周期确认后的分批买入机会")

        risk_tone = "均衡偏进攻"
        if cyclical_weight + growth_weight >= defensive_weight + 20:
            risk_tone = "进攻偏强"
        elif defensive_weight >= cyclical_weight + growth_weight:
            risk_tone = "防守偏稳"

        notes = [
            f"当前组合风格：{risk_tone}。",
            f"目标状态：{goal_state}，当前组合浮盈 {_pct(portfolio_pnl_pct)}，盈利目标 {_pct(profit_target)}。",
            f"目标进度：约 {_pct(target_progress_pct)}；最大回撤容忍 {_pct(max_drawdown)}，当前回撤占用约 {_pct(drawdown_usage_pct)}。",
            f"现金状态：{cash_state}，在保留最低现金 {_pct(min_cash_pct)} 后，可动用空间约 {_pct(available_to_deploy_pct)}。",
        ]
        if concentration_notes:
            notes.extend(concentration_notes)
        else:
            notes.append("当前未发现明显单票或行业过度集中。")

        if not preferred_directions:
            preferred_directions.append("暂时观望，等待更清晰的市场周期信号")
        if not avoid_directions:
            avoid_directions.append("无明确禁忌方向，但新增仓位仍需遵守单票和行业上限")

        return {
            "risk_tone": risk_tone,
            "cash_state": cash_state,
            "goal_state": goal_state,
            "target_progress_pct": round(target_progress_pct, 2),
            "drawdown_usage_pct": round(drawdown_usage_pct, 2),
            "available_to_deploy_pct": round(available_to_deploy_pct, 2),
            "buy_budget": buy_budget,
            "notes": notes,
            "preferred_directions": list(dict.fromkeys(preferred_directions)),
            "avoid_directions": list(dict.fromkeys(avoid_directions)),
            "style_summary": {
                "cyclical_pct": round(cyclical_weight, 2),
                "growth_pct": round(growth_weight, 2),
                "defensive_pct": round(defensive_weight, 2),
            },
        }

    def _build_actions(
        self,
        summary: Dict[str, Any],
        positions: List[Dict[str, Any]],
        industries: List[Dict[str, Any]],
        constraints: Dict[str, Any],
        alerts: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        actions: List[Dict[str, str]] = []
        alert_types = {alert["type"] for alert in alerts}

        if "stop_loss_triggered" in alert_types:
            actions.append({
                "action": "优先处理止损",
                "reason": "已有持仓触发止损条件，先处理风险再考虑新增买入。",
            })

        portfolio_pnl_pct = summary.get("unrealized_pnl_pct", 0)
        profit_target = constraints["profit_target_pct"]
        max_drawdown = constraints["max_drawdown_pct"]
        if portfolio_pnl_pct >= profit_target:
            actions.append({
                "action": "组合目标兑现",
                "reason": f"组合浮盈 {_pct(portfolio_pnl_pct)} 已达到盈利目标 {_pct(profit_target)}，优先考虑分批锁定收益，而不是继续扩大风险。",
            })
        elif portfolio_pnl_pct >= profit_target * 0.6:
            actions.append({
                "action": "组合利润保护",
                "reason": f"组合浮盈 {_pct(portfolio_pnl_pct)} 已达到盈利目标的六成以上，新增仓位应更谨慎，并逐步上移保护线。",
            })
        elif portfolio_pnl_pct <= -max_drawdown:
            actions.append({
                "action": "组合回撤控制",
                "reason": f"组合回撤已达到 {_pct(max_drawdown)} 的容忍线，优先降风险，不新增仓位。",
            })
        else:
            actions.append({
                "action": "围绕目标推进",
                "reason": f"组合当前浮盈 {_pct(portfolio_pnl_pct)}，距离盈利目标 {_pct(profit_target)} 仍有空间，操作重点是保留上行弹性并控制回撤。",
            })

        if summary["cash_pct"] < constraints["min_cash_pct"]:
            actions.append({
                "action": "控制新增买入",
                "reason": "现金比例低于目标，除非有高确定性机会，否则应先等待或减仓腾挪。",
            })
        else:
            deployable_pct = max(summary["cash_pct"] - constraints["min_cash_pct"], 0)
            actions.append({
                "action": "允许小仓位寻找机会",
                "reason": f"现金比例满足目标，理论可动用空间约 {_pct(deployable_pct)}，但新增仓位应避开已偏高的行业和风格。",
            })

        if industries:
            top_industry = industries[0]
            if top_industry["weight_pct"] > constraints["max_industry_position_pct"]:
                actions.append({
                    "action": "降低行业集中度",
                    "reason": f"{top_industry['industry']} 暴露过高，新增仓位应避开同类资产。",
                })

        for row in positions:
            plan = self._plan_for_position(row, summary, constraints)
            actions.append({
                "action": plan["action"],
                "target": f"{row['code']} {row.get('name', '')}".strip(),
                "reason": plan["reason"],
            })

        return actions

    def _plan_for_position(
        self,
        row: Dict[str, Any],
        summary: Dict[str, Any],
        constraints: Dict[str, Any],
    ) -> Dict[str, str]:
        price = row.get("current_price_decision", row["current_price"])
        price_currency = row.get("decision_currency", row.get("currency", "CNY"))
        weight = row.get("weight_pct", row["market_value"] / summary["total_assets"] * 100 if summary["total_assets"] else 0)
        stop_loss = row.get("stop_loss_price", 0)
        take_profit = row.get("take_profit_price", 0)
        add_price = row.get("add_price", 0)
        trim_price = row.get("trim_price", 0)
        pnl_pct = row.get("unrealized_pnl_pct", 0)
        max_single = constraints["max_single_position_pct"]
        profit_target = constraints["profit_target_pct"]

        if not row.get("price_available"):
            return {
                "action": "价格数据缺失",
                "reason": "当前自动查价失败，仓位按成本价估算；这是数据缺口，不生成交易建议。",
            }
        if stop_loss and price <= stop_loss:
            return {
                "action": "触发止损规则",
                "reason": f"当前决策价 {price} {price_currency} 已低于或等于止损价 {stop_loss} {price_currency}；需由 Agent 结合逻辑失效条件和组合目标判断是否执行风险处理。",
            }
        if take_profit and price >= take_profit:
            return {
                "action": "触发止盈规则",
                "reason": f"当前决策价 {price} {price_currency} 已达到止盈价 {take_profit} {price_currency}；需由 Agent 判断买入逻辑是否兑现以及是否分批处理。",
            }
        if trim_price and price >= trim_price:
            return {
                "action": "触发减仓观察价",
                "reason": f"当前决策价 {price} {price_currency} 已进入减仓观察区 {trim_price} {price_currency}；这是价格触发事实，不是自动减仓建议。",
            }
        if pnl_pct >= profit_target:
            return {
                "action": "达到单票盈利目标",
                "reason": f"当前浮盈 {_pct(pnl_pct)} 已达到盈利目标 {_pct(profit_target)}；需由 Agent 判断是否兑现、继续持有或调整保护线。",
            }
        if pnl_pct >= profit_target * 0.6:
            return {
                "action": "接近单票盈利目标",
                "reason": f"当前浮盈 {_pct(pnl_pct)} 已达到目标收益 {_pct(profit_target)} 的六成以上；需由 Agent 判断利润保护方式。",
            }
        if pnl_pct <= -constraints["max_drawdown_pct"]:
            return {
                "action": "达到单票回撤容忍",
                "reason": f"当前浮亏 {_pct(abs(pnl_pct))} 已达到最大回撤容忍 {_pct(constraints['max_drawdown_pct'])}；需由 Agent 结合止损、趋势和买入逻辑判断风险处理。",
            }
        if pnl_pct <= -constraints["max_drawdown_pct"] * 0.5:
            return {
                "action": "浮亏复核触发",
                "reason": f"当前浮亏 {_pct(abs(pnl_pct))} 已接近最大回撤容忍度的一半；需由 Agent 复核买入逻辑、趋势和周期是否仍成立。",
            }
        if add_price and price <= add_price and summary["cash_pct"] >= constraints["min_cash_pct"]:
            if weight >= max_single * 0.9:
                return {
                    "action": "跌破加仓观察价且接近仓位上限",
                    "reason": f"当前决策价 {price} {price_currency} 低于加仓观察价 {add_price} {price_currency}，同时仓位 {_pct(weight)} 接近单票上限 {_pct(max_single)}；这是冲突信号，需由 Agent 综合判断。",
                }
            return {
                "action": "跌破加仓观察价",
                "reason": f"当前决策价 {price} {price_currency} 低于加仓观察价 {add_price} {price_currency}，且现金比例满足最低约束；该价格锚点可能已失效，需由 Agent 结合趋势、多因子、周期和事件判断是否等待新锚点。",
            }
        if weight > constraints["max_single_position_pct"]:
            return {
                "action": "超过单票仓位护栏",
                "reason": f"单票仓位 {_pct(weight)} 已高于护栏上限 {_pct(constraints['max_single_position_pct'])}；这是仓位约束事实。",
            }
        if pnl_pct > 0:
            return {
                "action": "未触发价格规则且浮盈",
                "reason": f"当前浮盈 {_pct(pnl_pct)}，尚未进入止盈或减仓区；需继续结合信号判断。",
            }
        if pnl_pct < 0:
            return {
                "action": "未触发止损但浮亏",
                "reason": f"当前浮亏 {_pct(abs(pnl_pct))}，未触发止损；需由 Agent 结合目标、趋势和基本面判断是否继续持有、观察或处理风险。",
            }
        return {
            "action": "未触发价格规则",
            "reason": "未触发止损、止盈、加仓或减仓观察价；代码层无进一步动作判断。",
        }


def format_review(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    goal = report.get("goal_progress", {})
    diagnosis = report.get("diagnosis", {})
    constraints = report.get("constraints", {})

    # ── 组合全景 ──
    lines = [
        "=" * 80,
        "持仓策略复盘 — 仅供 Agent 综合判断，不含操作建议",
        "=" * 80,
        f"账户: {summary['broker']} | 风险: {summary['risk_profile']} | 币种: {summary.get('base_currency', 'CNY')}",
        f"总资产: {summary['total_assets']:.2f} | 持仓市值: {summary['position_value']:.2f} | 现金: {summary['cash']:.2f} ({_pct(summary['cash_pct'])})",
        f"浮动盈亏: {summary['unrealized_pnl']:.2f} ({_pct(summary['unrealized_pnl_pct'])})",
        f"汇率: {summary.get('fx_rates_to_base', {})}",
        "",
        f"目标: {constraints.get('strategy_goal','')} | 盈利目标 {_pct(constraints.get('profit_target_pct',0))} | 最大回撤 {_pct(constraints.get('max_drawdown_pct',0))}",
        f"状态: {goal.get('state','')} | 目标进度 {_pct(goal.get('target_progress_pct',0))} | 回撤占用 {_pct(goal.get('drawdown_usage_pct',0))}",
        f"现金: {diagnosis.get('cash_state','')} | 可动用 {_pct(diagnosis.get('available_to_deploy_pct',0))}",
        f"风格倾向: {diagnosis.get('risk_tone','')}",
    ]

    # 市场资金面
    ms = report.get("market_sentiment", {})
    if ms.get("available"):
        nf = ms.get("north_bound", {})
        mg = ms.get("margin", {})
        flow_parts = []
        if nf.get("available"):
            note = nf.get("_note", "")
            src = nf.get("source", "")
            age = nf.get("age_days")
            age_str = f"({age}天前)" if age is not None and age > 0 else ""
            flow_parts.append(f"北向5日净买额: {nf.get('north_5d_buy','?')}亿{age_str}")
            flow_parts.append(f"南向5日净买额: {nf.get('south_5d_buy','?')}亿")
            if "fallback" in str(src):
                flow_parts.append(f"⚠️北向/南向来自Tushare兜底(买入额非净买额)")
        if mg.get("available"):
            flow_parts.append(f"两融: 融资余额{mg.get('rzye','?')}亿")
        mf = ms.get("market_moneyflow", {})
        if mf.get("available"):
            flow_parts.append(f"全市场主力5日: {mf.get('net_main_5d','?')}亿 ({mf.get('pos_days','')})")
        if flow_parts:
            lines.append(f"资金面: {' | '.join(flow_parts)}")
        # 行业资金流
        imf = ms.get("industry_moneyflow", {})
        if imf.get("available"):
            data_date = imf.get("data_date", "?")
            lines.append(f"行业资金流 ({data_date} 主力净额):")
            top_in = imf.get("top_inflow", [])[:5]
            top_out = imf.get("top_outflow", [])[:5]
            if top_in:
                in_str = " | ".join(f"{x['industry']} {x['net_flow']:+.1f}亿" for x in top_in)
                lines.append(f"  🟢流入: {in_str}")
            if top_out:
                out_str = " | ".join(f"{x['industry']} {x['net_flow']:+.1f}亿" for x in top_out)
                lines.append(f"  🔴流出: {out_str}")

    # 行业热力图摘要
    sh = report.get("sector_heatmap", [])
    if sh:
        lines.append(f"行业动量: {' | '.join(sh)}")

    # 宏观/行业新闻
    mn = report.get("market_news", [])
    if mn:
        lines.append("宏观要闻:")
        for n in mn[:5]:
            title = n.get("title", "")[:90]
            if title:
                lines.append(f"  - {title}")
        lines.append("")

    # 大盘宽度
    mb = report.get("market_breadth", {})
    if mb.get("available"):
        lines.append(f"大盘宽度: MA20以上{mb.get('above_ma20_pct')}% | MA60以上{mb.get('above_ma60_pct')}% | 判断: {mb.get('width')} ({mb.get('sample')}样本)")

    # ── 暴露分析 ──
    lines.extend(["", "-" * 40, "行业暴露:", ""])
    for row in report.get("industry_exposure", []):
        pct = row['weight_pct']
        flag = " ⚠️" if pct > constraints.get("max_industry_position_pct", 30) else ""
        lines.append(f"  {row['industry']}: {_pct(pct)}{flag}")
    lines.extend(["", "市场暴露:"])
    for row in report.get("market_exposure", []):
        lines.append(f"  {row['market']}: {_pct(row['weight_pct'])}")
    lines.extend(["", "风格暴露:"])
    for row in report.get("style_exposure", []):
        lines.append(f"  {row['style']}: {_pct(row['weight_pct'])}")

    # ── 行业动量汇总（sector_radar 热力图，替代旧风格周期评分）──
    cycle = report.get("cycle_summary", {})
    if cycle.get("styles"):
        lines.extend(["", "-" * 40, "行业动量判断（组合级，来自 sector_radar）:", ""])
        for s in cycle["styles"]:
            ms = s.get("momentum_score")
            ms_str = f"{ms:.3f}" if ms is not None else "N/A"
            lines.append(f"  {s['industry']} ({s['style']}): {s['status']} 动量分={ms_str}")

    # ── 诊断 ──
    notes = diagnosis.get("notes", [])
    if notes:
        lines.extend(["", "-" * 40, "组合诊断:", ""])
        for note in notes:
            lines.append(f"  - {note}")
        lines.append(f"  优先方向: {'、'.join(diagnosis.get('preferred_directions',[])) or '待确认'}")
        lines.append(f"  避免方向: {'、'.join(diagnosis.get('avoid_directions',[])) or '无'}")
        lines.append(f"  仓位判断: {diagnosis.get('buy_budget','未判断')}")

    # ── 每只持仓七维卡 ──
    lines.extend(["", "=" * 80, "单只持仓数据卡", "=" * 80])
    for row in report["positions"]:
        code = row.get("code", "")
        name = row.get("name", "")
        market = row.get("market", "A股")
        decision_ccy = row.get("decision_currency", row.get("cost_currency", row.get("currency", "CNY")))

        lines.extend([
            "",
            f"┌─ {code} {name} [{market}] ─────────────────────────────────────────────┐",
        ])
        # 基本状态
        price_avail = "✓" if row.get("price_available") else "✗ 价格未确认"
        lines.append(f"│ 仓位 {_pct(row.get('weight_pct',0))} | "
                     f"现价 {row.get('current_price_decision', row['current_price']):.4f} {decision_ccy} | "
                     f"成本 {row.get('cost_price')} {row.get('cost_currency', decision_ccy)} | "
                     f"盈亏 {_pct(row.get('unrealized_pnl_pct',0))} | {price_avail}")

        # 买入逻辑
        thesis = row.get("thesis", "")
        if thesis:
            lines.append(f"│ 买入逻辑: {thesis}")

        # 目标贡献
        impact = row.get("goal_impact", {})
        if impact:
            lines.append(f"│ 目标贡献: {impact.get('role','?')} — {impact.get('interpretation','')}")

        # 行业对比
        peers = row.get("industry_peers", {})
        if peers.get("available"):
            peer_n = peers.get('peer_count', 0)
            # 自身 PE 历史分位（来自 Tushare）
            pe_self = row.get("pe_percentile_self")
            if pe_self is not None:
                lines.append(f"│ PE自身分位: {pe_self}% {_percentile_label(pe_self)} (近2年)")
            if row.get("market") == "港股":
                lines.append(f"│ 行业参照: 港股暂无")
            elif peer_n < 5:
                lines.append(f"│ 行业参照: ⚠️ 仅{peer_n}只，参照不足")
            else:
                pe_pct = peers.get('pe_percentile')
                pb_pct = peers.get('pb_percentile')
                pe_label = _percentile_label(pe_pct) if pe_pct is not None else ""
                pb_label = _percentile_label(pb_pct) if pb_pct is not None else ""
                lines.append(f"│ 行业参照 ({peer_n}只): "
                             f"PE中位 {peers.get('pe_median')} | PB中位 {peers.get('pb_median')}")
                pct_parts = []
                if pe_pct is not None:
                    pct_parts.append(f"PE分位 {pe_pct}% {pe_label}")
                if pb_pct is not None:
                    pct_parts.append(f"PB分位 {pb_pct}% {pb_label}")
                if pct_parts:
                    lines.append(f"│   {' | '.join(pct_parts)}")

        # 止损预警线
        stop = row.get("stop_loss_price", 0)
        price = row.get("current_price_decision", row["current_price"])
        if stop:
            dist = (price - stop) / stop * 100 if stop else 0
            if dist < 10:
                zone = "🔴 紧迫"
            elif dist < 15:
                zone = "🟡 警戒"
            elif dist < 25:
                zone = "🟢 安全"
            else:
                zone = "✅ 宽裕"
            lines.append(f"│ 止损预警线: {stop} (距 {_pct(dist)}, {zone})")
        # 动态止损建议
        ds = row.get("dynamic_stop", {})
        if ds.get("available") and ds.get("price"):
            ds_method = ds.get("method", "?")
            ds_price = ds["price"]
            ds_dist = ds.get("distance_pct", 0)
            static = ds.get("current_stop")
            lines.append(f"│ 动态止损建议: {ds_price} ({ds_method}) (距 {_pct(ds_dist)})")
            if static and ds_price != static:
                diff = (ds_price - static) / static * 100
                direction = "↑" if diff > 0 else "↓"
                lines.append(f"│  ⤷ 比静态止损{direction}{abs(diff):.0f}%，更{'紧' if diff > 0 else '松'}")
            # 下方支撑层
            supports = ds.get("supports", [])
            if supports:
                sup_str = " │ ".join(
                    f"{'★' if s['strength']>=3 else '☆'} {s['name']}:{s['price']}"
                    for s in supports[:4]
                )
                lines.append(f"│ 下方支撑: {sup_str}")

        # 止盈条件评估
        trim_cond = row.get("trim_conditions", {})
        if trim_cond:
            cond_bits = []
            for c in trim_cond.get("conditions", []):
                mark = "✓" if c["met"] else "✗"
                cond_bits.append(f"{mark}{c['name']}")
            lines.append(f"│ 止盈条件: {' | '.join(cond_bits)}  ({trim_cond['met']}/{trim_cond['total']})")

        # 加仓条件评估
        add_cond = row.get("add_conditions", {})
        if add_cond:
            cond_bits = []
            for c in add_cond.get("conditions", []):
                mark = "✓" if c["met"] else "✗"
                cond_bits.append(f"{mark}{c['name']}")
            lines.append(f"│ 加仓条件: {' | '.join(cond_bits)}  ({add_cond['met']}/{add_cond['total']})")

        # 最佳加仓价：趋势上方→MA20回踩，趋势下方→支撑位
        best_add_price = None
        best_add_note = ""
        add_price_cfg = row.get("add_price", 0)
        trend_data = (row.get("strategy_signals", {}).get("signals", {}).get("trend", {}))
        ma = (trend_data.get("ma_decision") or trend_data.get("ma") or {})
        ma20_val = ma.get("ma20")
        ma60_val = ma.get("ma60")
        rsi_val = (trend_data.get("technical_indicators") or {}).get("rsi14")
        kdj_j = (trend_data.get("technical_indicators") or {}).get("kdj_j")
        price = row.get("current_price_decision", row["current_price"])
        if add_price_cfg and add_price_cfg > 0:
            best_add_price = round(add_price_cfg, 2)
            best_add_note = "预设加仓价"
        elif ma20_val and ma20_val > 0 and price and price >= ma20_val:
            # 趋势上方：等回踩 MA20
            best_add_price = round(ma20_val, 2)
            best_add_note = "MA20回踩位"
        elif price and ma20_val and price < ma20_val:
            # 趋势下方：用支撑位（MA60 或 现价下方5%），不用 MA20
            support = None
            if ma60_val and ma60_val < price:
                support = ma60_val
            if support is None:
                support = round(price * 0.93, 2)  # 现价下方7%作为参考支撑
            best_add_price = round(support, 2) if support > 0 else None
            best_add_note = "MA60支撑位" if (ma60_val and ma60_val < price) else "估测支撑区"
        if best_add_price and best_add_price > 0:
            dist_to_best = round((price - best_add_price) / best_add_price * 100, 1)
            # 时机附注
            timing_notes = []
            if rsi_val and rsi_val > 65:
                timing_notes.append(f"RSI{int(rsi_val)}偏高待回落")
            if rsi_val and rsi_val < 30:
                timing_notes.append(f"RSI{int(rsi_val)}极弱待企稳")
            if kdj_j and kdj_j > 80:
                timing_notes.append("KDJ超买")
            if kdj_j and kdj_j < 30:
                timing_notes.append("KDJ超卖关注")
            note_str = f" ({'; '.join(timing_notes)})" if timing_notes else ""
            if best_add_price < price:
                dist_hint = "，接近支撑" if abs(dist_to_best) < 5 else ""
            else:
                dist_hint = "" if abs(dist_to_best) < 3 else "，再等等"
            lines.append(f"│ 最佳加仓价: {best_add_price} (距{dist_to_best:+.1f}%{dist_hint}) [{best_add_note}]{note_str}")

        # 规则触发（仅事实）
        precheck = row.get("rule_precheck", {})
        if precheck and precheck.get("action") not in ("未触发价格规则", "未触发价格规则且浮盈", "未触发止损但浮亏"):
            lines.append(f"│ ⚡ 触发: {precheck.get('action')} — {precheck.get('reason','')}")

        # 辅助信号摘要
        signals = row.get("strategy_signals", {})
        sig_map = signals.get("signals", {})
        # 因子
        factor = sig_map.get("factor", {})
        if factor.get("available"):
            scores = factor.get("factor_scores", {})
            interp = factor.get("interpretation", {})
            lines.append(f"│ 因子: 综合 {factor.get('composite_score',0):.2f}{PortfolioStrategy._pct_suffix(code)} "
                         f"(价值{scores.get('value',0):.2f}/成长{scores.get('growth',0):.2f}"
                         f"/质量{scores.get('quality',0):.2f}/动量{scores.get('momentum',0):.2f}) "
                         f"最强:{interp.get('strongest_factor','?')} 最弱:{interp.get('weakest_factor','?')}")
        # 趋势（优先用实时趋势信号）
        trend = row.get("trend_signal") or sig_map.get("trend", {})
        if trend.get("available"):
            ret = trend.get("returns", {})
            ma = trend.get("ma_decision", {}) or trend.get("ma", {})
            ti = trend.get("technical_indicators", {})
            kdj = ti.get("kdj", {})
            lines.append(f"│ 趋势: {trend.get('status','?')} | "
                         f"5/20/60d {ret.get('return_5d')}%/{ret.get('return_20d')}%/{ret.get('return_60d')}% | "
                         f"MA20 {ma.get('ma20','?')} | RSI14 {ti.get('rsi14','?')} | "
                         f"KDJ K:{kdj.get('k','?')} D:{kdj.get('d','?')} J:{kdj.get('j','?')} | "
                         f"量价:{ti.get('volume_price_signal','?')}")
            bottom = trend.get("interpretation", {}).get("bottom_fishing_analysis", {})
            if bottom.get("flags"):
                flags = bottom["flags"]
                flag_bits = []
                if flags.get("below_ma20"): flag_bits.append("MA20下方")
                if flags.get("below_ma60"): flag_bits.append("MA60下方")
                if flags.get("near_add_zone"): flag_bits.append("近加仓区")
                if flags.get("add_price_broken"): flag_bits.append("加仓价已破")
                if flags.get("short_stabilizing"): flag_bits.append("短线企稳")
                if flags.get("medium_trend_weak"): flag_bits.append("中期趋势弱")
                if flags.get("high_volatility"): flag_bits.append("高波动")
                if flags.get("close_to_stop_loss"): flag_bits.append("近止损线")
                if flag_bits:
                    lines.append(f"│  探底标记: {', '.join(flag_bits)}")
        # 个股资金流向
        try:
            mf = self.data_provider.get_moneyflow(row.get("code", ""))
            if mf.get("available") and mf.get("data"):
                d = mf["data"]
                buy_lg = _to_float(d.get("buy_lg_amount"), 0)
                sell_lg = _to_float(d.get("sell_lg_amount"), 0)
                net = buy_lg - sell_lg
                if net != 0:
                    direction = "净流入" if net > 0 else "净流出"
                    lines.append(f"│ 大单资金: {direction} {abs(net/10000):.0f}万")
        except Exception:
            pass

        # Tushare 信号（回购/筹码/预期）
        ts = row.get("tushare_signals", {}) or {}
        if ts.get("available"):
            ts_parts = []
            # 港股：南向持仓
            hk = ts.get("hk_hold")
            if hk:
                ratio = hk.get("ratio")
                rc = hk.get("ratio_change")
                if ratio is not None:
                    chg_str = f" (日变动{'+' if rc and rc > 0 else ''}{rc}%)" if rc is not None else ""
                    mc = hk.get("ratio_change_monthly")
                    month_str = f" 月变动{'+' if mc and mc > 0 else ''}{mc}pp" if mc is not None else ""
                    ts_parts.append(f"南向持股{ratio}%{chg_str}{month_str}")
            # 港股做空（金额 + 占比）
            ss = row.get("shortsell")
            if ss:
                period_note = "(早盘)" if ss.get("period") == "morning" else ""
                ratio_str = ""
                if ss.get("ratio") is not None:
                    # 早盘数据标注偏差
                    if ss.get("period") == "morning":
                        ratio_str = f", 早盘占比{ss.get('ratio')}%(⚠半日)"
                    else:
                        ratio_str = f", 占比{ss.get('ratio')}%"
                ts_parts.append(f"卖空{ss.get('amount',0)/1e8:.1f}亿{ratio_str}({ss.get('date','')})")
            # 龙虎榜（持仓是否上榜）
            tl = row.get("top_list")
            if tl:
                net = tl.get("net_amount", 0) or 0
                direction = "净买" if net > 0 else "净卖"
                ts_parts.append(f"📊龙虎榜: {direction}{abs(net/1e4):.0f}万 ({tl.get('reason','')})")
            # A股：回购
            bb = ts.get("buyback")
            if bb:
                ts_parts.append(f"回购{bb.get('count','?')}次({bb.get('latest_date','')})")
            # A股：股东人数
            hc = ts.get("holder_change")
            if hc is not None:
                direction = "集中" if hc < 0 else "分散"
                ts_parts.append(f"股东人数{direction}({abs(hc):.1f}%)")
            fc = ts.get("forecast")
            if fc:
                fc_min = fc.get('p_change_min')
                fc_max = fc.get('p_change_max')
                try:
                    if fc_min is not None and str(fc_min) != 'nan':
                        fc_min = round(float(fc_min), 1)
                    else:
                        fc_min = None
                except Exception:
                    fc_min = None
                try:
                    if fc_max is not None and str(fc_max) != 'nan':
                        fc_max = round(float(fc_max), 1)
                    else:
                        fc_max = None
                except Exception:
                    fc_max = None
                if fc_min is not None or fc_max is not None:
                    ts_parts.append(f"预期利润{fc_min or '?'}~{fc_max or '?'}%")
            # 股息率（A股 daily_basic）
            dy = row.get("dividend_yield")
            if dy is not None:
                if not ts.get("available"):
                    ts_parts = []
                ts_parts.append(f"股息率(TTM){dy}%")
            # 个股融资融券（margin_detail）
            mg = ts.get("margin")
            if mg:
                mg_parts = []
                if mg.get("rzye") is not None:
                    mg_parts.append(f"融资余额{mg['rzye']}万")
                if mg.get("net_buy") is not None:
                    direction = "净买" if mg['net_buy'] > 0 else "净卖"
                    mg_parts.append(f"{direction}{abs(mg['net_buy'])}万")
                if mg_parts:
                    ts_parts.append(f"两融: {' '.join(mg_parts)}")
            # 财务指标（fina_indicator）
            fin = ts.get("financial")
            if fin:
                fin_parts = []
                if fin.get("roe") is not None:
                    fin_parts.append(f"ROE{fin['roe']}%")
                if fin.get("gross_margin") is not None:
                    fin_parts.append(f"毛利率{fin['gross_margin']}%")
                if fin_parts:
                    ts_parts.append(f"财务: {' | '.join(fin_parts)}")
            if ts_parts:
                lines.append(f"│ 资金/筹码: {' | '.join(ts_parts)}")

        lines.append(f"└{'─' * 77}┘")

    # ── 相关性 ──
    corr = report.get("correlation", {})
    if corr.get("available") and corr.get("pairs"):
        lines.extend(["", "持仓相关性（|r|>0.5）:"])
        for p in corr["pairs"]:
            r = p["corr"]
            flag = "🔴高" if abs(r) > 0.7 else "🟡中"
            lines.append(f"  {p['pair']}: r={r} {flag}")
        if corr.get("avg_corr"):
            lines.append(f"  平均相关: {corr['avg_corr']}")

    # ── 观察池 ──
    watchlist = report.get("watchlist", []) or []
    if watchlist:
        lines.extend(["", "=" * 80, "观察池数据卡", "=" * 80])
        for row in watchlist:
            code = row.get("code", "")
            name = row.get("name", "")
            market = row.get("market", "")
            price = row.get("current_price", 0)
            price_src = row.get("price_source", "?")
            reason = row.get("reason", "")
            buy_zone = row.get("target_buy_zone", "")
            dy = row.get("dividend_yield")
            ts = row.get("tushare_signals", {}) or {}
            pe_self = row.get("pe_percentile_self")
            trend = row.get("trend_signal", {}) or {}
            factor = row.get("factor", {}) or {}
            buy_timing = row.get("buy_timing", {}) or {}

            lines.extend([
                f"┌─ {code} {name} [{market}] ── 观察 ──{'─' * 50}┐",
                f"│ 现价: {price} | 来源: {price_src}",
            ])

            # 买入时机（portfolio 从 signal + state_snapshot 生成 UI）
            if buy_timing:
                sig = buy_timing.get("signal", {})
                stars = "★" * buy_timing.get("score", 0) + "☆" * (5 - buy_timing.get("score", 0))
                action_map = {"OPEN": "🟢 可新开", "ADD": "🟡 可加仓", "NONE": "🔴 不操作"}
                mode_ui = action_map.get(sig.get("action_type", "NONE"), "⚪")
                lines.append(f"│ 买入时机: {mode_ui} {stars} "
                           f"(tech={buy_timing.get('technical_score',0):.2f} +α={buy_timing.get('alpha_bonus',0):+.2f})")

            # 趋势（从 state_snapshot 读取）
            if trend.get("available"):
                ma = trend.get("ma", {}) or {}
                rets = trend.get("returns", {}) or {}
                ss = buy_timing.get("state_snapshot", {}) if buy_timing else {}
                rsi_v = (ss.get("reversal") or {}).get("rsi14")
                kdj_j = (ss.get("reversal") or {}).get("kdj_j")
                ret5 = rets.get("return_5d")
                ret20 = rets.get("return_20d")
                ma20_v = ma.get("ma20")
                ma20_d = buy_timing.get("ma20_dist_pct")

                trend_parts = []
                if ma20_v:
                    trend_parts.append(f"MA20 {ma20_v:.2f}")
                if ma20_d is not None:
                    direction = "↑" if ma20_d > 0 else "↓"
                    trend_parts.append(f"距MA20 {ma20_d:+.1f}%{direction}")
                if rsi_v is not None:
                    trend_parts.append(f"RSI {rsi_v:.1f}")
                if kdj_j is not None:
                    trend_parts.append(f"KDJ J={kdj_j:.1f}")
                if ret5 is not None:
                    trend_parts.append(f"5日 {ret5:+.2f}%")
                if ret20 is not None:
                    trend_parts.append(f"20日 {ret20:+.2f}%")
                lines.append(f"│ 趋势: {' | '.join(trend_parts)}")

            # 因子
            scores = factor.get("factor_scores", {})
            if scores:
                comp = factor.get("composite_score", 0)
                best = max(scores, key=scores.get) if scores else "?"
                worst = min(scores, key=scores.get) if scores else "?"
                lines.append(f"│ 因子: 综合{comp:.2f}{PortfolioStrategy._pct_suffix(code)} 最强:{best}({scores.get(best,0):.2f}) 最弱:{worst}({scores.get(worst,0):.2f})")

            # 买入时机详情（从 state_snapshot 生成）
            if buy_timing and buy_timing.get("state_snapshot"):
                ss = buy_timing["state_snapshot"]
                t = ss.get("trend", {})
                m = ss.get("momentum_short", {})
                r = ss.get("reversal", {})
                v = ss.get("volume", {})
                a = ss.get("alpha", {})
                mk = ss.get("market", {})
                lines.append(f"│  ⤷ 趋势: ret20d={t.get('ret20d','?'):+.1f}% MA20={'上' if t.get('above_ma20') else '下'}")
                lines.append(f"│  ⤷ 动量: ret5d={m.get('ret5d','?'):+.1f}% 加速={'是' if m.get('accelerating') else '否'}")
                lines.append(f"│  ⤷ RSI={r.get('rsi14','?'):.0f} KDJ_J={r.get('kdj_j','?')} 量={v.get('signal','正常')}")
                lines.append(f"│  ⤷ alpha: m={a.get('momentum',0):.2f} c={a.get('cycle',0):.2f} t={a.get('turnaround',0):.2f}")
                lines.append(f"│  ⤷ 市场: {mk.get('regime','?')} cycle={mk.get('cycle_level',0):.2f}")

            # 静态信息
            if pe_self is not None:
                lines.append(f"│ PE自身分位: {pe_self:.0f}% ({'低估' if pe_self < 20 else '偏高' if pe_self > 60 else '合理'})")
            if dy is not None:
                lines.append(f"│ 股息率(TTM): {dy}%")
            if reason:
                lines.append(f"│ 理由: {reason}")
            if buy_zone:
                # 计算距目标区间
                try:
                    parts = buy_zone.replace("~", "-").replace("—", "-").split("-")
                    z_lo = float(parts[0].strip())
                    z_hi = float(parts[1].strip()) if len(parts) > 1 else z_lo * 1.1
                    if price < z_lo:
                        zone_note = f" (距下限 {((z_lo-price)/price*100):.1f}%)"
                    elif price > z_hi:
                        zone_note = f" (超出上限 {((price-z_hi)/z_hi*100):.1f}%)"
                    else:
                        zone_note = " (✓ 在区间内!)"
                except Exception:
                    zone_note = ""
                lines.append(f"│ 目标买入区间: {buy_zone}{zone_note}")
            # Tushare 信号
            if ts.get("available"):
                ts_parts = []
                hk = ts.get("hk_hold")
                if hk:
                    r = hk.get("ratio")
                    rc = hk.get("ratio_change")
                    mc = hk.get("ratio_change_monthly")
                    chg = f" 日变动{'+' if rc and rc>0 else ''}{rc}%" if rc is not None else ""
                    month_str = f" 月变动{'+' if mc and mc>0 else ''}{mc}pp" if mc is not None else ""
                    if r is not None:
                        ts_parts.append(f"南向持股{r}%{chg}{month_str}")
                bb = ts.get("buyback")
                if bb:
                    ts_parts.append(f"回购{bb.get('count','?')}次({bb.get('latest_date','')})")
                fc = ts.get("forecast")
                if fc:
                    fc_min = fc.get('p_change_min')
                    fc_max = fc.get('p_change_max')
                    try:
                        fc_min = round(float(fc_min), 1) if fc_min is not None and str(fc_min) != 'nan' else None
                    except Exception:
                        fc_min = None
                    try:
                        fc_max = round(float(fc_max), 1) if fc_max is not None and str(fc_max) != 'nan' else None
                    except Exception:
                        fc_max = None
                    if fc_min is not None or fc_max is not None:
                        ts_parts.append(f"预期利润{fc_min or '?'}~{fc_max or '?'}%")
                if ts_parts:
                    lines.append(f"│ 信号: {' | '.join(ts_parts)}")
            lines.append(f"└{'─' * 77}┘")

    # ── 告警 ──
    lines.extend(["", "=" * 80, "触发告警（纯事实，不含建议）", "=" * 80])
    if report.get("alerts"):
        for alert in report["alerts"]:
            lines.append(f"  [{alert['level']}] {alert['message']}")
    else:
        lines.append("  无")

    # ── 数据就绪 ──
    lines.extend(["", "数据就绪:"])
    ds = report.get("data_status", {})
    if ds.get("available"):
        for item in ds.get("items", []):
            state = "✓" if item.get("ready") else "✗"
            detail = ""
            if not item.get("ready"):
                detail = f" (缺 {','.join(item.get('missing',[]))})"
            lines.append(f"  {state} {item.get('code')} {item.get('name','')}{detail}")
    else:
        lines.append(f"  数据层不可用: {ds.get('reason','?')}")

    return "\n".join(lines)


def format_stock_analysis(report: Dict[str, Any]) -> str:
    """格式化单只股票外部分析结果。"""
    code = report.get("code", "")
    name = report.get("name", code)
    market = report.get("market", "A股")
    lines = [
        "=" * 80,
        f"{code} {name} [{market}] 独立分析",
        f"生成时间: {report.get('generated_at', '')}",
        "=" * 80,
        f"现价: {report.get('current_price', 'N/A')} (来源: {report.get('price_source', 'N/A')})",
        f"价格可用: {report.get('price_available', False)}",
        "",
    ]

    # 报价快照
    quote = report.get("quote_snapshot", {})
    if quote.get("available"):
        m = quote.get("metrics", {})
        lines.extend([
            "报价快照:",
            f"  PE: {m.get('trailing_pe')} | PB: {m.get('price_to_book')} | 股息率(TTM): {report.get('dividend_yield')}%",
            f"  市值: {m.get('market_cap')} | 52周高: {m.get('fifty_two_week_high')} | 52周低: {m.get('fifty_two_week_low')}",
            f"  来源: {quote.get('source')} | 陈旧: {quote.get('stale', False)}",
            "",
        ])

    # 行业参照
    peers = report.get("industry_peers", {})
    if peers.get("available"):
        pn = peers.get("peer_count", 0)
        if report.get("market") == "港股":
            lines.append(f"行业参照: 港股暂无")
        elif pn < 5:
            lines.append(f"行业参照: ⚠️ 仅{pn}只，参照不足")
        else:
            lines.append(f"行业参照 ({pn}只): PE中位 {peers.get('pe_median')} | PB中位 {peers.get('pb_median')}")

    # 趋势
    trend = report.get("trend_signal", {})
    if trend.get("available"):
        ret = trend.get("returns", {})
        ma = trend.get("ma", {})
        ti = trend.get("technical_indicators", {})
        kdj = ti.get("kdj", {})
        macd = ti.get("macd", {})
        boll = ti.get("bollinger", {})
        lines.extend([
            "",
            "趋势信号:",
            f"  状态: {trend.get('status')} | MA5/20/60: {ma.get('ma5')}/{ma.get('ma20')}/{ma.get('ma60')}",
            f"  收益 5/20/60d: {ret.get('return_5d')}%/{ret.get('return_20d')}%/{ret.get('return_60d')}%",
            f"  波动率20d: {trend.get('volatility_20d')}",
            f"  RSI14: {ti.get('rsi14')} | KDJ K:{kdj.get('k')} D:{kdj.get('d')} J:{kdj.get('j')}",
            f"  MACD dif:{macd.get('dif')} dea:{macd.get('dea')} hist:{macd.get('histogram')}",
            f"  BOLL %B: {boll.get('percent_b')} 带宽: {boll.get('bandwidth_pct')}%",
            f"  支撑: {trend.get('supporting_factors', [])}",
            f"  风险: {trend.get('risk_factors', [])}",
        ])

    # 多因子
    signals = report.get("strategy_signals", {})
    sig_map = signals.get("signals", {})
    factor = sig_map.get("factor", {})
    if factor.get("available"):
        scores = factor.get("factor_scores", {})
        interp = factor.get("interpretation", {})
        lines.extend([
            "",
            "多因子:",
            f"  综合: {factor.get('composite_score', 0):.2f} (价值{scores.get('value',0):.2f}/成长{scores.get('growth',0):.2f}/质量{scores.get('quality',0):.2f}/动量{scores.get('momentum',0):.2f})",
            f"  最强: {interp.get('strongest_factor','?')} | 最弱: {interp.get('weakest_factor','?')}",
        ])

    # 新闻
    news = report.get("news", [])
    if news:
        lines.extend(["", f"新闻 ({len(news)}条):", ""])
        for n in news[:5]:
            lines.append(f"  - {n.get('title', '')[:80]}")

    # 资金流向
    mf = report.get("moneyflow", {})
    if mf.get("available"):
        d = mf.get("data", {})
        lines.extend([
            "",
            "资金流向:",
            f"  特大单买/卖: {d.get('buy_elg_amount')}/{d.get('sell_elg_amount')}万",
            f"  大单买/卖:   {d.get('buy_lg_amount')}/{d.get('sell_lg_amount')}万",
        ])

    # PE自身分位
    pe_self = report.get("pe_percentile_self")
    if pe_self is not None:
        lines.append(f"\nPE自身分位: {pe_self}% ({'低估' if pe_self < 20 else '偏高' if pe_self > 60 else '合理'})")

    # Tushare信号
    ts = report.get("tushare_signals", {}) or {}
    if ts.get("available"):
        ts_lines = []
        hk = ts.get("hk_hold")
        if hk:
            r, rc = hk.get("ratio"), hk.get("ratio_change")
            if r is not None:
                chg = f"({'↑' if rc and rc>0 else '↓'}{abs(rc)}%)" if rc is not None else ""
                mc = hk.get("ratio_change_monthly")
                month_str = f" 月变动{'+' if mc and mc>0 else ''}{mc}pp" if mc is not None else ""
                ts_lines.append(f"南向持股{r}%{chg}{month_str}")
        bb = ts.get("buyback")
        if bb:
            ts_lines.append(f"回购{bb.get('count','?')}次(最近{bb.get('latest_date','')})")
        hc = ts.get("holder_change")
        if hc is not None:
            ts_lines.append(f"股东人数{'集中' if hc < 0 else '分散'}({abs(hc):.1f}%)")
        fc = ts.get("forecast")
        if fc:
            fc_min = fc.get('p_change_min')
            fc_max = fc.get('p_change_max')
            try:
                fc_min = round(float(fc_min), 1) if fc_min is not None and str(fc_min) != 'nan' else None
            except Exception:
                fc_min = None
            try:
                fc_max = round(float(fc_max), 1) if fc_max is not None and str(fc_max) != 'nan' else None
            except Exception:
                fc_max = None
            if fc_min is not None or fc_max is not None:
                ts_lines.append(f"一致预期利润{fc_min or '?'}~{fc_max or '?'}%")
        if ts_lines:
            lines.append(f"资金/筹码: {' | '.join(ts_lines)}")

    # 大盘宽度
    mb = report.get("market_breadth", {})
    if mb.get("available"):
        lines.append(f"\n大盘宽度: MA20以上{mb.get('above_ma20_pct')}% | MA60以上{mb.get('above_ma60_pct')}% | {mb.get('width')}")

    lines.extend(["", "=" * 80])
    return "\n".join(lines)


def format_position_plan(report: Dict[str, Any]) -> str:
    if report.get("error"):
        return report["error"]

    summary = report["summary"]
    position = report["position"]
    goal = report.get("goal_progress", {})
    impact = report.get("goal_impact", {})
    strategy_signals = report.get("strategy_signals", {})
    decision_dimensions = report.get("decision_dimensions", [])
    data_status = report.get("data_status", {})
    rule_precheck = report.get("rule_precheck", {})
    agent_context = report.get("agent_context", {})
    plan = report["plan"]

    trade_currency = position.get("currency", "CNY")
    decision_currency = position.get("decision_currency", position.get("cost_currency", trade_currency))
    if trade_currency != decision_currency:
        price_text = (
            f"{position['current_price']} {trade_currency} "
            f"≈ {position.get('current_price_decision')} {decision_currency}"
        )
    else:
        price_text = f"{position['current_price']} {trade_currency}"

    lines = [
        "=" * 80,
        f"{position['code']} {position.get('name', '')} 目标驱动操作计划",
        "=" * 80,
        "组合目标:",
        f"- 当前目标状态: {goal.get('state', 'unknown')}",
        f"- 当前组合浮盈: {_pct(goal.get('portfolio_pnl_pct', 0))}",
        f"- 盈利目标: {_pct(goal.get('profit_target_pct', 0))}",
        f"- 目标进度: {_pct(goal.get('target_progress_pct', 0))}",
        f"- 剩余目标空间: {_pct(goal.get('remaining_to_target_pct', 0))}",
        f"- 最大回撤容忍: {_pct(goal.get('max_drawdown_pct', 0))}",
        f"- 目标动作倾向: {goal.get('action_bias', '未判断')}",
        "",
        "个股状态:",
        f"- 当前价格: {price_text}",
        f"- 成本价格: {position['cost_price']} {position.get('cost_currency', trade_currency)}",
        f"- 当前仓位: {_pct(position.get('weight_pct', 0))}",
        f"- 个股盈亏: {_pct(position.get('unrealized_pnl_pct', 0))}",
        f"- 对组合贡献: {_pct(impact.get('portfolio_contribution_pct', 0))}",
        f"- 目标关系: {impact.get('role', '未判断')}",
        f"- 解读: {impact.get('interpretation', '未判断')}",
        "",
        "数据状态:",
        _format_data_status_line(data_status),
        "",
        "价格条件:",
        f"- 止损价: {position.get('stop_loss_price', 0)} {decision_currency}",
        f"- 加仓观察价: {position.get('add_price', 0)} {decision_currency}",
        f"- 减仓观察价: {position.get('trim_price', 0)} {decision_currency}",
        f"- 止盈价: {position.get('take_profit_price', 0)} {decision_currency}",
        "",
        "买入逻辑:",
        f"- {position.get('thesis', '未填写')}",
        "",
        "七维决策卡:",
    ]

    if decision_dimensions:
        for index, dimension in enumerate(decision_dimensions, 1):
            lines.append(f"{index}. {dimension.get('name', '未知维度')}: {dimension.get('status', '未判断')}")
            lines.append(f"   结论: {dimension.get('conclusion', '未判断')}")
            supporting_items = dimension.get("supporting_factors", [])
            risk_items = dimension.get("risk_factors", [])
            missing_items = dimension.get("missing_data", [])
            if supporting_items:
                lines.append(f"   支撑: {'；'.join(str(item) for item in supporting_items)}")
            if risk_items:
                lines.append(f"   风险: {'；'.join(str(item) for item in risk_items)}")
            if missing_items:
                lines.append(f"   缺失: {'；'.join(str(item) for item in missing_items)}")
    else:
        lines.append("- 暂无七维决策数据")

    lines.append("")
    lines.append("辅助策略信号明细:")

    signals = strategy_signals.get("signals", {})
    if signals:
        factor = signals.get("factor", {})
        if factor.get("available"):
            lines.append(_format_factor_signal_line(factor))
        else:
            lines.append(f"- 多因子: 暂不可用，{factor.get('reason', '无原因')}")

        trend = signals.get("trend", {})
        if trend.get("available"):
            lines.append(_format_trend_signal_line(trend))
        else:
            lines.append(f"- 趋势: 暂不可用，{trend.get('reason', '无原因')}")

        event = signals.get("event", {})
        if event.get("available"):
            themes = "、".join(event.get("themes", [])[:3]) or "无明确主题"
            lines.append(
                f"- 事件/新闻: 可用，新闻数 {event.get('news_count', 0)}，"
                f"情绪 {event.get('sentiment_score', 0):.2f}，主题 {themes}"
            )
        else:
            lines.append(f"- 事件/新闻: 暂不可用，{event.get('reason', '无原因')}")

        cycle = signals.get("cycle", {})
        mapped_cycle = cycle.get("mapped_cycle", "待识别")
        lines.append(f"- 周期: {mapped_cycle}；{cycle.get('reason', '周期强弱待接入')}")
        for item in cycle.get("representatives", [])[:3]:
            lines.append(
                f"  - 代表资产 {item.get('name')}({item.get('code')}): "
                f"{item.get('status')}，20日 {item.get('return_20d')}%，60日 {item.get('return_60d')}%"
            )

        for name, label in [("ml", "ML"), ("microstructure", "资金流/盘口")]:
            signal = signals.get(name, {})
            if signal.get("available"):
                lines.append(f"- {label}: 可用")
            else:
                lines.append(f"- {label}: 暂不可用，{signal.get('reason', '待接入')}")
    else:
        lines.append("- 暂无辅助策略信号")

    supporting = strategy_signals.get("supporting_factors", [])
    risks = strategy_signals.get("risk_factors", [])
    missing = strategy_signals.get("missing_data", [])

    lines.append("")
    lines.append("支撑因素:")
    if supporting:
        for item in supporting:
            lines.append(f"- {item}")
    else:
        lines.append("- 暂无额外支撑信号")

    lines.append("")
    lines.append("风险因素:")
    if risks:
        for item in risks:
            lines.append(f"- {item}")
    else:
        lines.append("- 暂无额外风险信号")

    lines.append("")
    lines.append("缺失数据:")
    if missing:
        for item in missing:
            lines.append(f"- {item}")
    else:
        lines.append("- 暂无")

    lines.extend([
        "",
        "逻辑失效条件:",
    ])

    invalid_conditions = position.get("invalid_conditions") or []
    if invalid_conditions:
        for item in invalid_conditions:
            lines.append(f"- {item}")
    else:
        lines.append("- 未填写")

    lines.extend([
        "",
        "规则初筛:",
        f"- 触发项: {rule_precheck.get('action', '未判断')}",
        f"- 说明: {rule_precheck.get('reason', '未判断')}",
        "",
        "策略数据上下文:",
        f"- purpose: {agent_context.get('purpose', 'strategy_data_context')}",
        "- 已包含组合目标、约束、持仓、七维数据、规则初筛和各策略信号上下文。",
    ])

    return "\n".join(lines)


def _format_data_status_line(data_status: Dict[str, Any]) -> str:
    if not data_status:
        return "- 未获取数据能力状态"
    if data_status.get("reason") and not data_status.get("ready"):
        return f"- 数据能力层状态异常: {data_status.get('reason')}"
    state = "ready" if data_status.get("ready") else "缺失"
    missing = f"，缺失 {','.join(data_status.get('missing', []))}" if data_status.get("missing") else ""
    synced = "，本次已补齐" if data_status.get("synced") else ""
    details = data_status.get("details", {})
    detail_bits = []
    if details.get("quotes"):
        detail_bits.append(f"行情 {details['quotes'].get('count')} 条")
    if details.get("financial"):
        detail_bits.append(f"财务期 {details['financial'].get('report_period')}")
    detail_text = f"，{'；'.join(detail_bits)}" if detail_bits else ""
    return f"- {state}{missing}{synced}{detail_text}"


def _format_factor_signal_line(factor: Dict[str, Any]) -> str:
    scores = factor.get("factor_scores", {})
    weights = factor.get("factor_weights", {})
    interpretation = factor.get("interpretation", {})
    narratives = interpretation.get("factor_narratives", {}) if interpretation else {}
    strongest = interpretation.get("strongest_factor", "暂无") if interpretation else "暂无"
    weakest = interpretation.get("weakest_factor", "暂无") if interpretation else "暂无"

    evidence_parts = []
    for key, label in [("value", "价值"), ("growth", "成长"), ("quality", "质量"), ("momentum", "动量")]:
        evidence = _factor_evidence_from_narrative(narratives.get(key, ""))
        if evidence:
            evidence_parts.append(f"{label}: {evidence}")

    evidence_text = "；".join(evidence_parts) if evidence_parts else "暂无明细数据"
    return (
        "- 多因子数据: 可用，"
        f"综合得分 {factor.get('composite_score', 0):.2f} "
        f"(价值{scores.get('value', 0):.2f} / 成长{scores.get('growth', 0):.2f} / "
        f"质量{scores.get('quality', 0):.2f} / 动量{scores.get('momentum', 0):.2f})，"
        f"权重 价值{weights.get('value', 0):.0%}/成长{weights.get('growth', 0):.0%}/"
        f"质量{weights.get('quality', 0):.0%}/动量{weights.get('momentum', 0):.0%}，"
        f"数据完整度 {factor.get('data_quality_score', 0):.2f}。"
        f"强弱结构数据: 最高 {strongest} / 最低 {weakest}。"
        f"核心明细: {evidence_text}。"
        "用途: 供大模型结合目标、仓位、趋势、周期和事件信号推理，不在代码层直接给交易动作。"
    )


def _factor_evidence_from_narrative(narrative: str) -> str:
    marker = "实际数据: "
    end_marker = "。打分理由:"
    if marker not in narrative:
        return ""
    tail = narrative.split(marker, 1)[1]
    if end_marker in tail:
        return tail.split(end_marker, 1)[0]
    return tail


def _format_trend_signal_line(trend: Dict[str, Any]) -> str:
    returns = trend.get("returns", {})
    price_currency = trend.get("price_currency", "")
    decision_currency = trend.get("decision_currency", price_currency)
    use_decision_price = bool(price_currency and decision_currency and price_currency != decision_currency)
    ma = trend.get("ma_decision", {}) if use_decision_price else trend.get("ma", {})
    interpretation = trend.get("interpretation", {})
    technical_summary = interpretation.get("technical_summary", "")
    ma_structure = interpretation.get("ma_structure", {})
    key_levels = interpretation.get("key_levels", {})
    projection = interpretation.get("scenario_projection", {})
    bottom_fishing = interpretation.get("bottom_fishing_analysis", {})
    technical_indicators = trend.get("technical_indicators", {})

    latest = trend.get("latest_close_decision") if use_decision_price else trend.get("latest_close")
    currency_text = f" {decision_currency}" if decision_currency else ""
    latest_text = f"，现价 {latest}{currency_text}" if latest is not None else ""
    return_text = (
        f"5/20/60日收益 {returns.get('return_5d')}%/"
        f"{returns.get('return_20d')}%/{returns.get('return_60d')}%"
    )
    ma_text = f"MA5/20/60 {ma.get('ma5')}/{ma.get('ma20')}/{ma.get('ma60')}{currency_text}"
    key_level_text = _format_trend_key_levels(key_levels)
    indicator_text = _format_technical_indicators(technical_indicators, decision_currency)
    bottom_fishing_text = _format_bottom_fishing_analysis(bottom_fishing)
    structure_text = ma_structure.get("meaning") or ""
    conversion_note = ""
    if use_decision_price:
        conversion_note = (
            f"价格点位已按汇率 {trend.get('price_to_decision_rate')} "
            f"由 {price_currency} 折算为 {decision_currency}。"
        )

    return (
        f"- 趋势数据: {trend.get('status', '未判断')}{latest_text}，"
        f"{return_text}，{ma_text}。"
        f"{conversion_note}"
        f"结构摘要: {technical_summary}{structure_text} "
        f"{key_level_text}"
        f"{indicator_text}"
        f"{bottom_fishing_text}"
    )


def _format_trend_key_levels(key_levels: Dict[str, Any]) -> str:
    levels = key_levels.get("levels", []) if key_levels else []
    decision_currency = key_levels.get("decision_currency") or ""
    if not levels:
        return "关键位: 暂无足够均线数据。"
    parts = []
    for item in levels:
        price = item.get("price_decision", item.get("price"))
        distance = item.get("distance_pct")
        distance_text = f"，距离 {distance}%" if distance is not None else ""
        currency_text = f" {decision_currency}" if decision_currency else ""
        parts.append(f"{item.get('name')} {price}{currency_text} 为{item.get('role')}{distance_text}")
    return f"关键位: {'；'.join(parts)}。"


def _format_technical_indicators(indicators: Dict[str, Any], decision_currency: str) -> str:
    if not indicators:
        return "技术指标: 暂无。"
    macd = indicators.get("macd", {}) or {}
    boll = indicators.get("bollinger", {}) or {}
    volume = indicators.get("volume", {}) or {}
    ranges = indicators.get("range_position", {}) or {}
    range_bits = []
    for label in ["20d", "60d", "120d"]:
        item = ranges.get(label, {}) or {}
        if item.get("position_pct") is not None:
            range_bits.append(
                f"{label}区间位置 {item.get('position_pct')}%，"
                f"高/低 {item.get('high_decision')}/{item.get('low_decision')} {decision_currency}"
            )
    range_text = "；".join(range_bits) if range_bits else "区间位置不足"
    return (
        "技术指标: "
        f"RSI14 {indicators.get('rsi14')}；"
        f"MACD dif/dea/hist {macd.get('dif')}/{macd.get('dea')}/{macd.get('histogram')}；"
        f"BOLL 上/中/下 {boll.get('upper_decision')}/{boll.get('middle_decision')}/{boll.get('lower_decision')} {decision_currency}，"
        f"%B {boll.get('percent_b')}，带宽 {boll.get('bandwidth_pct')}%；"
        f"成交量 最新/MA5/MA20 {volume.get('latest')}/{volume.get('ma5')}/{volume.get('ma20')}，"
        f"相对MA20 {volume.get('latest_vs_ma20')}；"
        f"{range_text}。"
    )


def _format_bottom_fishing_analysis(analysis: Dict[str, Any]) -> str:
    if not analysis:
        return ""
    support = "；".join(str(item) for item in analysis.get("supporting_factors", [])[:3])
    risks = "；".join(str(item) for item in analysis.get("risk_factors", [])[:3])
    flags = analysis.get("flags", {})
    flag_text = "；".join(
        f"{key}={value}" for key, value in flags.items()
    )
    support_text = f" 支撑: {support}。" if support else ""
    risk_text = f" 风险: {risks}。" if risks else ""
    return (
        "探底分析数据: "
        f"当前价 {analysis.get('current_price')} {analysis.get('decision_currency')}；"
        f"预设加仓价 {analysis.get('add_price')}；"
        f"止损价 {analysis.get('stop_loss_price')}；"
        f"止损缓冲 {analysis.get('stop_buffer_pct')}%；"
        f"相对成本 {analysis.get('drawdown_from_cost_pct')}%；"
        f"状态标记: {flag_text}。"
        f"{support_text}{risk_text}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="持仓策略 Skill")
    parser.add_argument("action", nargs="?", default="review", choices=["review", "analyze"])
    parser.add_argument("--portfolio", "-p", default=None, help="持仓 YAML 文件路径")
    parser.add_argument("--code", "-c", default=None, help="股票代码（analyze 使用）")
    parser.add_argument("--market", "-m", default="A股", help="市场（analyze 使用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--save", action="store_true", help="保存 JSON 报告到 reports 目录")
    args = parser.parse_args()

    strategy = PortfolioStrategy(args.portfolio)

    if args.action == "analyze":
        if not args.code:
            raise SystemExit("analyze 需要 --code 参数")
        report = strategy.analyze_stock(args.code, args.market)
    else:
        report = strategy.review()

    if args.save:
        path = strategy.save_report(report, prefix=f"portfolio_{args.action}")
        report["saved_to"] = path

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.action == "analyze":
        print(format_stock_analysis(report))
    else:
        print(format_review(report))


if __name__ == "__main__":
    main()
