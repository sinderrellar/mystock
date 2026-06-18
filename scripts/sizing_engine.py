#!/usr/bin/env python3
"""
Sizing Engine — 仓位分配层

职责：在"允许交易"的前提下，决定每一笔交易的资金比例。
不选股、不判断买卖、不拦截风险——只决定做多少。

用法:
  from sizing_engine import calculate
  plan = calculate(signal, risk_gate, portfolio, symbol)
"""

from typing import Any, Dict, Optional


def calculate(
    signal: Dict[str, Any],
    risk_gate: Dict[str, Any],
    portfolio: Dict[str, Any],
    symbol: str,
) -> Dict[str, Any]:
    """计算单笔交易的目标仓位。

    Args:
        signal: {action_type, confidence, state_snapshot}
        risk_gate: {OPEN, ADD, position_multiplier, total_risk}
        portfolio: {equity, cash, drawdown, positions: {symbol: {weight, pnl}}}
        symbol: 交易标的

    Returns:
        {action, target_weight, capital, risk_scaled, constraints}
    """
    action = signal.get("action_type", "NONE")
    confidence = signal.get("confidence", 0)
    pos_mult = risk_gate.get("position_multiplier", 0.5)
    equity = portfolio.get("equity", 1000000)
    drawdown = abs(portfolio.get("drawdown", 0))
    positions = portfolio.get("positions", {})
    current = positions.get(symbol, {})
    current_weight = current.get("weight", 0)

    if action == "NONE" or not risk_gate.get(action, False):
        return _empty_result(action, "gate blocked")

    # ── 基础系数 ──
    base_k = {"OPEN": 1.0, "ADD": 0.4}.get(action, 0)
    base = confidence * base_k

    # ── 风险缩放 ──
    scaled = base * pos_mult

    # ── 回撤惩罚 ──
    dd_penalty = 1.0
    if drawdown > 0.10:
        dd_penalty = 0.7
    if drawdown > 0.15:
        dd_penalty = 0.5
    scaled *= dd_penalty

    # ── 波动惩罚 ──
    ss = signal.get("state_snapshot", {})
    vol_20d = (ss.get("risk") or {}).get("volatility_20d", 0) or 0
    if vol_20d > 0.25:
        scaled *= 0.8

    # ── 集中度惩罚 ──
    max_weight = max((p.get("weight", 0) for p in positions.values()), default=0)
    if max_weight > 0.15:
        scaled *= 0.6

    # ── 动态风险预算（参数从 config 读取）──
    import os, yaml as _y
    _cfg = {}
    try:
        _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "config_complete.yaml")
        with open(_cfg_path) as _f: _cfg = _y.safe_load(_f)
        _cfg = _cfg.get("pyramid_middle_layer", {}).get("position_sizing", {})
    except: pass
    baseline = portfolio.get("baseline_vol", _cfg.get("baseline_vol", 0.025))
    # 按 market_regime 选档（牛/震/熊不同风险预算）
    regime = portfolio.get("market_regime", "neutral")
    budget_map = _cfg.get("max_risk_budget", {"strong": 0.70, "neutral": 0.50, "weak": 0.30})
    max_budget = portfolio.get("max_risk_budget", budget_map.get(regime, 0.50))
    used = 0.0
    for sym, pos in positions.items():
        w = abs(pos.get("weight", 0))
        v = pos.get("volatility", 0.02)
        used += w * v / baseline
    remaining = max(0, max_budget - used)

    # 仓位波动估算（目标标的）
    target_vol = (signal.get("state_snapshot", {}).get("risk") or {}).get("volatility_20d", 0.02) or 0.02
    vol_norm = target_vol / baseline  # 高波动股占更多预算

    # ── 仓位计算 + 诊断跟踪 ──
    drop_reasons = []
    if action == "OPEN":
        raw = scaled * 0.20 / max(vol_norm, 0.5)  # 高波少买，低波多买
        target_weight = raw
        if target_weight > 0.20: drop_reasons.append("CAP_20PCT"); target_weight = min(target_weight, 0.20)
        if remaining <= 0: drop_reasons.append("BUDGET_ZERO"); target_weight = 0
        elif target_weight > remaining: drop_reasons.append("BUDGET_LIMIT"); target_weight = remaining
    elif action == "ADD":
        add_amount = current_weight * scaled * 0.5 / max(vol_norm, 0.5)
        target_weight = current_weight + max(0, add_amount)
        if add_amount <= 0: drop_reasons.append("ADD_ZERO")
        elif remaining <= 0: drop_reasons.append("BUDGET_ZERO"); target_weight = current_weight
        elif add_amount > remaining: drop_reasons.append("BUDGET_LIMIT")
        if target_weight > 0.20: drop_reasons.append("CAP_20PCT"); target_weight = min(target_weight, 0.20)
    else:
        target_weight = 0

    return {
        "symbol": symbol,
        "action": action,
        "target_weight": round(target_weight, 4),
        "capital": round(equity * target_weight, 0),
        "confidence": confidence,
        "risk_scaled": round(scaled, 3),
        "drop_reason": drop_reasons[0] if drop_reasons else None,
        "trace": {"raw_weight": round(target_weight, 4),
                  "vol_norm": round(vol_norm, 2),
                  "scaled": round(scaled, 3),
                  "remaining": round(remaining, 4)},
        "budget": {
            "max": max_budget,
            "risk_used": round(used, 3),
            "risk_remaining": round(remaining, 3),
            "vol_normalized": round(vol_norm, 2),
        },
        "constraints": {
            "drawdown_adjusted": dd_penalty < 1.0,
            "vol_adjusted": vol_20d > 0.25,
            "cap_hit": target_weight >= 0.20,
            "budget_hit": remaining <= 0,
        },
    }


def _empty_result(action: str, reason: str) -> Dict[str, Any]:
    return {
        "action": action,
        "target_weight": 0,
        "capital": 0,
        "confidence": 0,
        "risk_scaled": 0,
        "constraints": {"reason": reason},
    }
