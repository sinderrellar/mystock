#!/usr/bin/env python3
"""
Portfolio Controller — 组合中枢 / 全局仓位管理器

职责：协调四个引擎，控制组合级风险暴露和全局模式切换。
不是策略、不是风控、不是执行——是"组合的大脑"。

用法:
  from portfolio_controller import evaluate
  decision = evaluate(portfolio_state, entry_signals, risk_output)
"""

from typing import Any, Dict, List


# ═══════════════════════════════════════════════════════════════
# 共享基础设施：run() 和 run_backtest() 共用
# ═══════════════════════════════════════════════════════════════

def _build_entry_input(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """从 buy_plan candidate 构造 entry_engine.evaluate() 需要的 stock dict。

    run() 和 run_backtest() 共用。entry 输入结构只在此处定义。
    """
    alpha = {
        "momentum": (candidate.get("factor", {}).get("factor_scores") or {}).get("momentum", 0.5),
        "cycle": candidate.get("cycle_score", 0.5),
        "turnaround": candidate.get("turnaround_score", 0.5),
    }
    trend = candidate.get("trend", {}) or {}
    ti = trend.get("technical_indicators", {}) or {}
    return {
        "trend_signal": {
            "available": True,
            "ma": trend.get("ma", {}),
            "returns": trend.get("returns", {}),
            "rsi14": ti.get("rsi14"),
            "kdj": ti.get("kdj", {}),
            "status": trend.get("status", ""),
            "volatility_20d": trend.get("volatility_20d"),
            "technical_indicators": ti,
        },
        "current_price": candidate.get("close", 0),
        "alpha_context": alpha,
    }


def _run_decision_chain(
    candidate: Dict[str, Any],
    pf_meta: Dict[str, Any],
    pos_list: List[Dict[str, Any]],
    market: Dict[str, Any],
) -> Dict[str, Any]:
    """单票完整决策链：entry → risk → sizing。

    run() 和 run_backtest() 共用。这是 entry/risk/sizing 的唯一调用入口。
    """
    from entry_engine import evaluate as entry_eval
    from risk_engine import evaluate as risk_eval
    from sizing_engine import calculate as sizing_calc

    stock = _build_entry_input(candidate)
    entry = entry_eval(stock)
    risk = risk_eval(pf_meta, pos_list, market, entry["signal"])
    sizing = sizing_calc(entry["signal"], risk["gate"], pf_meta, candidate["code"])

    alpha = stock["alpha_context"]
    return {
        "code": candidate["code"],
        "name": candidate.get("name", ""),
        "industry": candidate.get("industry", ""),
        "alpha_score": candidate.get("alpha_score", 0),
        "momentum": alpha["momentum"],
        "cycle": alpha["cycle"],
        "turnaround": alpha["turnaround"],
        "entry_signal": entry["signal"],
        "entry_score": entry["entry_score"],
        "technical_score": entry["technical_score"],
        "alpha_bonus": entry["alpha_bonus"],
        "risk_gate": risk["gate"],
        "risk_regime": risk["regime"],
        "sizing": sizing,
        "state_snapshot": entry.get("state_snapshot", {}),
    }


def _run_candidates(
    candidates: List[Dict[str, Any]],
    pf_meta: Dict[str, Any],
    pos_list: List[Dict[str, Any]],
    market: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """逐票跑决策链，返回 enriched decisions 列表。"""
    return [_run_decision_chain(c, pf_meta, pos_list, market) for c in candidates]


# ═══════════════════════════════════════════════════════════════
# 组合层评估
# ═══════════════════════════════════════════════════════════════

def evaluate(
    portfolio: Dict[str, Any],
    signals: List[Dict[str, Any]],
    risk: Dict[str, Any],
    market: Dict[str, Any],
) -> Dict[str, Any]:
    """评估组合状态，输出全局决策和个股动作。

    Args:
        portfolio: {equity, cash_ratio, drawdown, positions: {symbol: {weight, pnl, industry}}}
        signals: [{symbol, action_type, confidence}] from entry_engine
        risk: {total_risk, regime, gate} from risk_engine
        market: {regime, vol_index, breadth}

    Returns:
        {global_mode, actions, constraints, exposures}
    """
    positions = portfolio.get("positions", {})
    equity = portfolio.get("equity", 1_000_000)
    cash_ratio = portfolio.get("cash_ratio", 0.30)
    drawdown = abs(portfolio.get("drawdown", 0))
    max_dd = portfolio.get("max_drawdown", 0.20)
    total_risk = risk.get("total_risk", 0.5)

    # ── ① 组合级暴露计算 ──
    sector_exposure: Dict[str, float] = {}
    total_weight = 0.0
    for sym, pos in positions.items():
        w = pos.get("weight", 0)
        total_weight += w
        ind = pos.get("industry", "其他")
        sector_exposure[ind] = sector_exposure.get(ind, 0) + w

    max_sector = max(sector_exposure.values()) if sector_exposure else 0
    position_count = len(positions)

    # ── ② Regime 状态机 ──
    dd_pressure = drawdown / max_dd if max_dd > 0 else 0
    prev_mode = portfolio.get("prev_mode", "NORMAL")

    if dd_pressure > 0.8 or total_risk > 0.80:
        global_mode = "RISK_OFF"
    elif dd_pressure > 0.6 or total_risk > 0.65:
        global_mode = "DEFENSIVE"
    elif prev_mode == "RISK_OFF" and dd_pressure < 0.4 and total_risk < 0.5:
        global_mode = "RECOVERY"          # 从熔断中恢复
    elif prev_mode == "DEFENSIVE" and dd_pressure < 0.3 and total_risk < 0.4:
        global_mode = "RECOVERY"
    elif dd_pressure < 0.25 and total_risk < 0.35 and cash_ratio > 0.25:
        global_mode = "AGGRESSIVE"
    elif prev_mode == "RECOVERY":
        global_mode = "NORMAL"            # 恢复完成
    else:
        global_mode = "NORMAL"

    # ── ③ 约束 ──
    constraints = {
        "max_single_weight": 0.20,
        "max_sector_weight": 0.30,
        "min_cash_ratio": 0.10 if global_mode != "DEFENSIVE" else 0.25,
        "max_position_count": 12 if global_mode != "DEFENSIVE" else 8,
    }

    # ── ④ 个股动作建议 ──
    actions = []
    for sig in signals:
        sym = sig.get("symbol", "")
        action_type = sig.get("action_type", "NONE")
        conf = sig.get("confidence", 0)
        pos = positions.get(sym, {})
        current_w = pos.get("weight", 0)

        if global_mode == "DEFENSIVE" and action_type == "OPEN":
            continue  # 防守模式不开新仓

        if current_w > constraints["max_single_weight"]:
            actions.append({"symbol": sym, "action": "TRIM",
                           "from": round(current_w, 3),
                           "to": constraints["max_single_weight"],
                           "reason": "超过单票上限"})
        elif global_mode == "DEFENSIVE" and current_w > 0.15:
            actions.append({"symbol": sym, "action": "TRIM",
                           "from": round(current_w, 3), "to": 0.15,
                           "reason": "防守模式降仓"})
        elif action_type in ("OPEN", "ADD"):
            actions.append({"symbol": sym, "action": action_type,
                           "confidence": conf, "current_weight": round(current_w, 3)})

    # ── ⑤ 行业过热检测 ──
    if max_sector > constraints["max_sector_weight"]:
        actions.append({"symbol": "PORTFOLIO", "action": "ROTATE",
                       "sector": max(sector_exposure, key=sector_exposure.get),
                       "exposure": round(max_sector, 2),
                       "reason": "行业过度集中"})

    # ── ⑥ 现金不足告警 ──
    if cash_ratio < constraints["min_cash_ratio"] and global_mode != "AGGRESSIVE":
        actions.append({"symbol": "PORTFOLIO", "action": "DELEVERAGE",
                       "cash": round(cash_ratio, 3),
                       "target": constraints["min_cash_ratio"],
                       "reason": "现金不足"})

    # ── ⑦ 漂移检测 ──
    drift = _detect_drift(positions, sector_exposure, market)

    # ── ⑧ 执行反馈骨架（供 execution loop 回写）──
    feedback = portfolio.get("last_execution_feedback", {})

    return {
        "global_mode": global_mode,
        "actions": actions,
        "constraints": constraints,
        "exposures": {
            "total_weight": round(total_weight, 3),
            "cash_ratio": round(cash_ratio, 3),
            "max_sector": round(max_sector, 2),
            "sector_breakdown": {k: round(v, 3) for k, v in sorted(sector_exposure.items(), key=lambda x: -x[1])[:5]},
            "position_count": position_count,
        },
        "status": {
            "drawdown_pressure": round(dd_pressure, 2),
            "risk_level": total_risk,
            "mode_reason": f"dd={dd_pressure:.0%} risk={total_risk:.2f} cash={cash_ratio:.0%} prev={prev_mode}→{global_mode}",
        },
        "drift": drift,
        "execution_feedback": {
            "last_slippage_bp": feedback.get("avg_slippage_bp"),
            "last_fill_pct": feedback.get("avg_fill_pct"),
        },
        "prev_mode": global_mode,  # 下次调用时传入
    }


def run(date: str = None) -> Dict[str, Any]:
    """主入口：编排全链路 buy_plan → entry → risk → sizing → controller。

    调用昨天写的六个 engine，不替代任何模块的计算。
    """
    from buy_plan import BuyPlanEngine
    from portfolio_strategy import PortfolioStrategy
    from risk_engine import evaluate as risk_eval

    # ── ① 候选池 ──
    bp = BuyPlanEngine()
    bp_result = bp.run(top_n=10, target_date=date, initial_limit=5000, enrich_limit=0)
    candidates = bp_result.get("recommendations", [])

    # ── ② 持仓 review（现有逻辑，完全不动）──
    ps = PortfolioStrategy()
    review_result = ps.review()

    # ── ③ 组合级数据（供 risk + sizing）──
    positions = review_result.get("positions", [])
    from pymongo import MongoClient
    _mongo = MongoClient('mongodb://localhost:27017/')['tradingagents']
    pos_list = []
    for p in positions:
        code = p.get("code","")
        vol = 0.02
        try:
            tdoc = _mongo['stock_trends'].find_one({'code': code}, sort=[('trade_date',-1)])
            if tdoc:
                vol = (tdoc.get('volatility_20d') or 2.0) / 100  # 百分比→小数
                vol = max(0.01, min(vol, 0.10))  # 钳位 1%~10%
        except: pass
        pos_list.append({"code": code, "weight": p.get("weight_pct",0)/100,
                        "pnl": p.get("unrealized_pnl_pct",0)/100,
                        "industry": p.get("industry",""), "volatility": vol})
    total_w = sum(abs(x["weight"]) for x in pos_list)
    dd = review_result.get("summary", {}).get("unrealized_pnl_pct", 0)
    pf_meta = {"total_equity": review_result.get("summary",{}).get("total_assets",1_000_000),
               "cash_ratio": max(0, 1 - total_w), "drawdown": dd/100 if dd else 0,
               "max_drawdown": 0.20, "prev_mode": "NORMAL", "max_risk_budget": 0.60,
               "positions": {x["code"]: {"weight": x["weight"], "pnl": x["pnl"],
                                          "volatility": x["volatility"]}
                             for x in pos_list if x["code"]}}
    market = {"regime": "neutral", "vol_index": 0.55, "breadth": 0.5}

    # ── ④ 逐票 entry + risk + sizing（共享决策链）──
    enriched = _run_candidates(candidates, pf_meta, pos_list, market)
    all_signals = [
        {"symbol": c["code"],
         "action_type": c["entry_signal"]["action_type"],
         "confidence": c["entry_signal"]["confidence"]}
        for c in enriched
    ]

    # ── ⑤ 组合级评估 ──
    risk_total = risk_eval(pf_meta, pos_list, market,
                           all_signals[0] if all_signals else {"action_type":"NONE","confidence":0})
    ctrl = evaluate(pf_meta, all_signals, risk_total, market)

    # ── ⑥ 诊断表 ──
    diag = []
    for c in enriched:
        sig = c["entry_signal"]
        gate = c["risk_gate"]
        sz = c["sizing"]
        diag.append({
            "code": c["code"],
            "action": sig["action_type"],
            "entry": round(c["entry_score"], 3),
            "gate_open": gate.get("OPEN", False),
            "pos_mult": gate.get("position_multiplier", 0),
            "target_w": round(sz.get("target_weight", 0), 4),
            "budget_used": round(sz.get("budget", {}).get("risk_used", 0), 3),
            "budget_rem": round(sz.get("budget", {}).get("risk_remaining", 0), 3),
            "drop_reason": sz.get("drop_reason", ""),
            "trace": sz.get("trace", {}),
            "hit_dd": sz.get("constraints", {}).get("drawdown_adjusted", False),
        })

    return {
        "buy_plan": bp_result,
        "review": review_result,
        "candidates": enriched,
        "diagnostics": diag,
        "global_mode": ctrl["global_mode"],
        "drift": ctrl["drift"],
        "risk_total": risk_total["total_risk"],
        "risk_regime": risk_total["regime"],
        "actions": ctrl["actions"],
        "constraints": ctrl["constraints"],
        "exposures": ctrl["exposures"],
    }


# ═══════════════════════════════════════════════════════════════
# 回测入口
# ═══════════════════════════════════════════════════════════════

def _build_virtual_pf_meta(
    vp: Dict[str, Any],
    config_override: Dict[str, Any] = None,
) -> tuple:
    """从 virtual_portfolio 构造 pf_meta 和 pos_list（回测用）。

    与 run() 中的 pf_meta 对应，区别仅在于数据来源：
    run() 从 PortfolioStrategy.review() 取，这里从 virtual_portfolio 取。
    调用前 virtual_portfolio 必须已完成 mark-to-market（weight/unrealized_pnl 已更新）。
    """
    config = config_override or {}
    positions = vp.get("positions", {})
    equity = vp.get("equity", 1_000_000)
    cash = vp.get("cash", 1_000_000)

    pos_list = []
    for code, pos in positions.items():
        pos_list.append({
            "code": code,
            "weight": pos.get("weight", 0),
            "pnl": pos.get("unrealized_pnl", 0),
            "industry": pos.get("industry", ""),
            "volatility": pos.get("volatility", 0.02),
        })

    # drawdown = 1 - equity / max_equity（mark-to-market 后已更新 max_equity）
    drawdown = max(0, 1 - equity / max(vp.get("max_equity", equity), 1))

    pf_meta = {
        "total_equity": equity,
        "cash_ratio": cash / equity if equity > 0 else 1.0,
        "drawdown": drawdown,
        "max_drawdown": 0.20,
        "prev_mode": "NORMAL",
        "max_risk_budget": config.get("max_risk_budget", 0.60),
        "baseline_vol": config.get("baseline_vol", 0.025),
        "positions": {
            x["code"]: {"weight": x["weight"], "pnl": x["pnl"], "volatility": x["volatility"]}
            for x in pos_list if x["code"]
        },
    }
    return pf_meta, pos_list


def run_backtest(
    date: str,
    virtual_portfolio: Dict[str, Any],
    config_override: Dict[str, Any] = None,
    use_controller: bool = False,
) -> Dict[str, Any]:
    """回测入口：全链路 buy_plan → entry → risk → sizing。

    与 run() 共享 _run_candidates()。区别：
    - pf_meta 从 virtual_portfolio 构造（而非 PortfolioStrategy.review()）
    - universe = candidates + current_positions（持仓股也重新评估）
    - 不调 controller.evaluate()（use_controller=False）
    """
    from buy_plan import BuyPlanEngine
    from risk_engine import evaluate as risk_eval

    config = config_override or {}

    # ── ① 候选池 ──
    bp = BuyPlanEngine()
    bp_result = bp.run(top_n=10, target_date=date, initial_limit=5000, enrich_limit=0)
    candidates = bp_result.get("recommendations", [])

    if not candidates:
        return {"date": date, "actions": [], "sum_target_weight": 0,
                "portfolio_snapshot": {}, "error": "buy_plan 无候选"}

    # ── ② universe = candidates + current_positions ──
    current_positions = virtual_portfolio.get("positions", {})
    universe = list(candidates)
    existing_codes = {c["code"] for c in candidates}

    # 为不在候选池的持仓股构造轻量 candidate，让其也能过决策链
    from pymongo import MongoClient
    _mongo = MongoClient('mongodb://localhost:27017/')['tradingagents']
    for code, pos in current_positions.items():
        if code not in existing_codes:
            factor_doc = _mongo['stock_factors'].find_one(
                {'code': code, 'trade_date': date})
            trend_doc = _mongo['stock_trends'].find_one(
                {'code': code, 'trade_date': date})
            info_doc = _mongo['stock_basic_info'].find_one({'code': code})

            if factor_doc and trend_doc:
                universe.append({
                    "code": code,
                    "name": (info_doc.get("name") if info_doc else code) or code,
                    "industry": pos.get("industry", ""),
                    "close": (trend_doc.get("ma") or {}).get("ma20", pos.get("cost", 0)) or pos.get("cost", 0),
                    "alpha_score": 0.5,
                    "cycle_score": factor_doc.get("cycle_score", 0.5),
                    "turnaround_score": factor_doc.get("turnaround_score", 0.5),
                    "factor": (factor_doc.get("factors") or {}).get(
                        factor_doc.get("group", ""), {"factor_scores": {"momentum": 0.5}}),
                    "trend": {
                        "ma": trend_doc.get("ma", {}),
                        "returns": trend_doc.get("returns", {}),
                        "technical_indicators": {
                            "rsi14": trend_doc.get("rsi14"),
                            "kdj": trend_doc.get("kdj", {}),
                            "volume_price_signal": trend_doc.get("volume_price_signal", ""),
                        },
                        "status": trend_doc.get("status", ""),
                        "volatility_20d": trend_doc.get("volatility_20d"),
                    },
                })

    # ── ③ market（复用 buy_plan 逻辑：直接用 breadth 推 regime）──
    # buy_plan 内部已计算但未在 result 中返回，这里临时推导
    breadth_pct = bp_result.get("breadth_pct", 50)
    if isinstance(breadth_pct, (int, float)) and breadth_pct > 0:
        if breadth_pct >= 60:
            regime = "strong"
        elif breadth_pct >= 35:
            regime = "neutral"
        else:
            regime = "weak"
    else:
        regime = "neutral"
    market = {"regime": regime, "vol_index": 0.55,
              "breadth": breadth_pct / 100 if isinstance(breadth_pct, (int, float)) else 0.5}

    # ── ④ pf_meta ──
    pf_meta, pos_list = _build_virtual_pf_meta(virtual_portfolio, config)

    # ── ⑤ 决策链 ──
    decisions = _run_candidates(universe, pf_meta, pos_list, market)

    # ── ⑥ 生成 actions ──
    pending_buy_codes = {
        o["code"] for o in virtual_portfolio.get("pending_orders", [])
        if o.get("side") == "BUY"
    }

    actions = []
    sum_target_weight = 0.0
    for d in decisions:
        code = d["code"]
        action_type = d["entry_signal"]["action_type"]
        entry_score = d["entry_score"]
        target_weight = d["sizing"].get("target_weight", 0)
        in_position = code in current_positions
        in_pending = code in pending_buy_codes

        if in_pending:
            continue

        if in_position:
            # 持仓股 CLOSE：entry 恶化到 NONE 且 entry_score < 0.35
            if action_type == "NONE" and entry_score < 0.35:
                actions.append({
                    "code": code, "action": "CLOSE",
                    "entry_score": entry_score,
                    "reason": f"信号恶化 entry={entry_score:.2f}",
                })
            # 否则 HOLD（不操作）
        else:
            # 非持仓股 OPEN
            if action_type == "OPEN" and target_weight > 0:
                actions.append({
                    "code": code, "action": "OPEN",
                    "target_weight": round(target_weight, 4),
                    "entry_score": entry_score,
                    "industry": d.get("industry", ""),
                    "reason": d["sizing"].get("trace", {}),
                })
                sum_target_weight += target_weight

    # ── ⑦ 组合快照 ──
    snapshot = {
        "cash": virtual_portfolio.get("cash", 0),
        "equity": virtual_portfolio.get("equity", 0),
        "drawdown": pf_meta.get("drawdown", 0),
        "position_count": len(current_positions),
    }

    return {
        "date": date,
        "actions": actions,
        "sum_target_weight": round(sum_target_weight, 4),
        "portfolio_snapshot": snapshot,
        "decisions": decisions,
        "breadth_pct": breadth_pct,
        "regime": regime,
    }


def _detect_drift(
    positions: Dict[str, Any],
    sectors: Dict[str, float],
    market: Dict[str, Any],
) -> Dict[str, Any]:
    """检测组合风格/行业漂移。"""
    issues = []

    # 行业过度集中
    for ind, w in sectors.items():
        if w > 0.25:
            issues.append(f"行业'{ind}'过度集中({w:.0%})")

    # 单票过大
    for sym, pos in positions.items():
        if pos.get("weight", 0) > 0.18:
            issues.append(f"{sym} 仓位接近上限({pos['weight']:.0%})")

    # 亏损集中
    losers = sum(1 for p in positions.values() if p.get("pnl", 0) < -0.10)
    if losers >= 3:
        issues.append(f"{losers}只浮亏>10%，需排查")

    return {
        "healthy": len(issues) == 0,
        "issues": issues,
    }
