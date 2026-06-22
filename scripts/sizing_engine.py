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

from engine_config import load_middle_config


def _normalize_volatility(value: Any, default: float = 0.02) -> float:
    """Return 20d volatility as a decimal value.

    Historical trend docs store values like 2.35 for 2.35%, while sizing
    budgets use 0.0235. Accept both forms to avoid 100x over-shrinking.
    """
    try:
        vol = float(value)
    except (TypeError, ValueError):
        return default
    if vol <= 0:
        return default
    if vol > 1:
        return vol / 100
    return vol


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
    equity = portfolio.get("equity", portfolio.get("total_equity", 1000000))
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
    _cfg = load_middle_config("position_sizing", {
        "baseline_vol": 0.025,
        "drawdown_penalty_1": 0.10,
        "drawdown_penalty_2": 0.15,
        "drawdown_multiplier_1": 0.7,
        "drawdown_multiplier_2": 0.5,
        "high_vol_threshold": 0.25,
        "high_vol_multiplier": 0.8,
        "concentration_threshold": 0.15,
        "concentration_multiplier": 0.6,
        "open_base_weight": 0.20,
        "max_single_weight": 0.20,
        "max_single_reason": "CAP_20PCT",
        "add_multiplier": 0.5,
        "vol_norm_floor": 0.5,
        "max_risk_budget": {"strong": 0.70, "neutral": 0.50, "weak": 0.30},
    })

    if drawdown > _cfg.get("drawdown_penalty_1", 0.10):
        dd_penalty = _cfg.get("drawdown_multiplier_1", 0.7)
    if drawdown > _cfg.get("drawdown_penalty_2", 0.15):
        dd_penalty = _cfg.get("drawdown_multiplier_2", 0.5)
    scaled *= dd_penalty

    # ── 波动惩罚 ──
    ss = signal.get("state_snapshot", {})
    raw_vol_20d = (ss.get("risk") or {}).get("volatility_20d", 0) or 0
    vol_20d = _normalize_volatility(raw_vol_20d, default=0)
    if vol_20d > _cfg.get("high_vol_threshold", 0.25):
        scaled *= _cfg.get("high_vol_multiplier", 0.8)

    # ── 集中度惩罚 ──
    max_weight = max((p.get("weight", 0) for p in positions.values()), default=0)
    if max_weight > _cfg.get("concentration_threshold", 0.15):
        scaled *= _cfg.get("concentration_multiplier", 0.6)

    # ── 动态风险预算（参数从 config 读取）──
    baseline = _normalize_volatility(
        portfolio.get("baseline_vol", _cfg.get("baseline_vol", 0.025)),
        default=0.025,
    )
    # 按 market_regime 选档（牛/震/熊不同风险预算）
    regime = portfolio.get("market_regime", "neutral")
    budget_map = _cfg.get("max_risk_budget", {"strong": 0.70, "neutral": 0.50, "weak": 0.30})
    max_budget = portfolio.get("max_risk_budget", budget_map.get(regime, 0.50))
    used = 0.0
    for sym, pos in positions.items():
        w = abs(pos.get("weight", 0))
        v = _normalize_volatility(pos.get("volatility", 0.02))
        used += w * v / baseline
    remaining = max(0, max_budget - used)

    # 仓位波动估算（目标标的）
    raw_target_vol = (signal.get("state_snapshot", {}).get("risk") or {}).get("volatility_20d", 0.02) or 0.02
    target_vol = _normalize_volatility(raw_target_vol, default=0.02)
    vol_norm = target_vol / baseline  # 高波动股占更多预算

    # ── 仓位计算 + 诊断跟踪 ──
    drop_reasons = []
    raw_weight = 0
    vol_norm_floor = _cfg.get("vol_norm_floor", 0.5)
    max_single_weight = _cfg.get("max_single_weight", 0.20)
    max_single_reason = _cfg.get("max_single_reason", "CAP_20PCT")
    if action == "OPEN":
        raw = scaled * _cfg.get("open_base_weight", 0.20) / max(vol_norm, vol_norm_floor)
        raw_weight = raw
        target_weight = raw
        if target_weight > max_single_weight: drop_reasons.append(max_single_reason); target_weight = min(target_weight, max_single_weight)
        if remaining <= 0: drop_reasons.append("BUDGET_ZERO"); target_weight = 0
        elif target_weight > remaining: drop_reasons.append("BUDGET_LIMIT"); target_weight = remaining
    elif action == "ADD":
        add_amount = current_weight * scaled * _cfg.get("add_multiplier", 0.5) / max(vol_norm, vol_norm_floor)
        raw_weight = current_weight + max(0, add_amount)
        target_weight = raw_weight
        if add_amount <= 0: drop_reasons.append("ADD_ZERO")
        elif remaining <= 0: drop_reasons.append("BUDGET_ZERO"); target_weight = current_weight
        elif add_amount > remaining: drop_reasons.append("BUDGET_LIMIT")
        if target_weight > max_single_weight: drop_reasons.append(max_single_reason); target_weight = min(target_weight, max_single_weight)
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
        "trace": {"raw_weight": round(raw_weight, 4),
                  "target_vol_raw": raw_target_vol,
                  "target_vol": round(target_vol, 4),
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
            "vol_adjusted": vol_20d > _cfg.get("high_vol_threshold", 0.25),
            "cap_hit": target_weight >= max_single_weight,
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
