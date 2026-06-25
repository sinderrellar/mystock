#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""

Buy Plan — Alpha Rank 候选池生成器

职责：全市场生存过滤 → 补齐预计算因子 → 根据市场宽度动态加权
momentum/cycle/turnaround → 输出 Top N 候选。

不做买卖决策、不做入场许可、不做仓位管理；这些由
entry_engine / risk_engine / sizing_engine / portfolio_controller 负责。



用法:

    python3 buy_plan.py --limit 500 --top 10

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

# Buy Plan Engine — 候选池生成器（因子评分 + 排序）

# ============================================================



class BuyPlanEngine:

    """Alpha Rank 候选池生成引擎。



    因子评分 + 动态权重 + 排序输出。

    输出候选池供 portfolio_controller 消费，不做买卖决策。"""



    def __init__(self, mongo_store: Optional[MongoFactorDataStore] = None):

        config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")

        self.mongo = mongo_store or MongoFactorDataStore(
            _load_mongodb_config(config_path),
            readonly=True,
            ensure_indexes=False,
        )

        self.data = MarketDataProvider(self.mongo)



    # ================================================================

    # 候选池：MongoDB 初筛（市值 + 流动性）

    # ================================================================



    def _broad_screen(self, industries: Optional[List[str]] = None) -> List[Dict[str, Any]]:

        """Layer 0: 生存过滤 — MongoDB 直接查询，和 import_universe_quotes 统一。"""

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

        print(f"生存过滤: {len(raw)} → {len(candidates)} 只")

        return candidates



    # ================================================================

    # 批量补齐数据 + 趋势/因子打分

    # ================================================================



    def _enrich(self, candidates: List[Dict[str, Any]], limit: int = 200,

                target_date: Optional[str] = None) -> List[Dict[str, Any]]:

        """v3：③因子优先 → ②趋势缓存 + fallback。"""

        if len(candidates) > limit:

            candidates = candidates[:limit]



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



    def _get_market_breadth(self, target_date: Optional[str] = None) -> float:

        """全市场站上MA20的股票比例（%）。从 stock_trends 读取。"""

        try:

            trends_coll = self.mongo.db["stock_trends"]

            dates = sorted(trends_coll.distinct("trade_date"), reverse=True)

            if not dates:

                return 50.0

            td = target_date or dates[0]



            # 批量加载收盘价（避免逐只 find_one）

            price_coll = self.mongo.db[self.mongo.collections["daily_quotes"]]

            price_map = {}

            price_query = {"trade_date": td, "period": "daily", "data_source": self.mongo.quote_source}

            for pdoc in price_coll.find(

                price_query,

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



    def _get_portfolio_codes(self, portfolio_path: Optional[str] = None) -> set:

        try:

            from portfolio_state_loader import load_portfolio_yaml

            data = load_portfolio_yaml(portfolio_path)

            return {str(p.get("code", "")).strip()
                    for p in (data.get("positions") or [])
                    if p.get("code")}

        except Exception:

            return set()



    def run(self, top_n: int = 10, initial_limit: int = 500, enrich_limit: int = 200,

            industries: Optional[List[str]] = None,

            target_date: Optional[str] = None,

            apply_portfolio_penalty: bool = True,

            portfolio_codes: Optional[set] = None,

            portfolio_path: Optional[str] = None) -> Dict[str, Any]:

        # 解析 "today" → 实际日期

        if target_date == "today":

            target_date = _utc_now().strftime("%Y-%m-%d")

        date_label = target_date or "最新"

        print("=" * 60)

        print(f"Buy Plan — Alpha Rank 候选池 ({date_label})")

        if industries:

            print(f"  行业范围: {', '.join(industries)}")

        print("=" * 60)



        # 生存过滤（全市场，不抽样）

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

        # 已持仓降权。回测 attribution 可关闭，避免真实 portfolio.yaml 污染历史回测。

        if apply_portfolio_penalty:

            portfolio_codes = portfolio_codes if portfolio_codes is not None else self._get_portfolio_codes(portfolio_path)

        else:

            portfolio_codes = set()

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

            "market_context": {

                "breadth_pct": breadth_pct,

                "regime": regime,

                "alpha_weights": alpha_w,

                "weak_min_cycle": weak_min_cycle,

            },

            "market": {

                "breadth_pct": breadth_pct,

                "regime": regime,

                "alpha_weights": alpha_w,

            },

        }

        return report


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def persist_buy_plan_snapshot(
    mongo: MongoFactorDataStore,
    report: Dict[str, Any],
    params: Dict[str, Any],
) -> Optional[str]:
    """Persist a compact buy_plan snapshot for forward-test review."""
    try:
        coll = mongo.db["buy_plan_snapshots"]
        coll.create_index([("generated_at", -1)], background=True)
        coll.create_index([("run_date", -1)], background=True)
        coll.create_index([("recommendations.code", 1), ("run_date", -1)], background=True)

        generated_at = report.get("generated_at") or _utc_now().isoformat()
        doc = {
            "schema_version": 1,
            "kind": "buy_plan_snapshot",
            "generated_at": generated_at,
            "run_date": (params.get("target_date") or generated_at[:10]),
            "params": _json_safe(params),
            "pipeline": _json_safe(report.get("pipeline", {})),
            "market_context": _json_safe(report.get("market_context", {})),
            "recommendations": _json_safe(report.get("recommendations", [])),
            "created_at": _utc_now(),
        }
        result = coll.insert_one(doc)
        return str(result.inserted_id)
    except Exception as exc:
        print(f"⚠️ buy_plan 留痕失败: {exc}")
        return None


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

    trace = report.get("trace") or {}
    if trace.get("id"):
        lines.append("")
        lines.append(f"留痕: {trace.get('collection', 'buy_plan_snapshots')}/{trace['id']}")

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

    parser.add_argument("--no-trace", action="store_true",

                       help="不写入 buy_plan_snapshots 留痕集合")
    parser.add_argument("--portfolio", "-p", default=None,

                       help="指定 portfolio YAML；影响已持仓降权，默认 data/portfolio.yaml")

    args = parser.parse_args()



    industries = None

    if args.industries:

        industries = [i.strip() for i in args.industries.split(",") if i.strip()]

    engine = BuyPlanEngine()

    report = engine.run(top_n=args.top, initial_limit=args.limit,

                        enrich_limit=args.enrich, industries=industries,

                        target_date=args.date,

                        portfolio_path=args.portfolio)

    if not args.no_trace and not report.get("error"):

        snapshot_id = persist_buy_plan_snapshot(engine.mongo, report, {

            "top_n": args.top,

            "initial_limit": args.limit,

            "enrich_limit": args.enrich,

            "target_date": args.date,

            "industries": industries,

            "portfolio": args.portfolio,

        })

        if snapshot_id:

            report["trace"] = {"collection": "buy_plan_snapshots", "id": snapshot_id}



    if args.json:

        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    else:

        print(format_buy_plan(report))





if __name__ == "__main__":

    main()
