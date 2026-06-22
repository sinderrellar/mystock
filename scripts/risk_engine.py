#!/usr/bin/env python3
"""
Risk Engine — 风险统一层

职责：把"能不能做交易"转化为"在当前状态下允许承担多少风险"
不选股、不判断买卖点、不决定仓位——只输出系统行为边界。

用法:
  from risk_engine import evaluate
  risk_output = evaluate(portfolio_state, positions, market_state, entry_signal)
"""

from typing import Any, Dict, List, Optional

from engine_config import load_middle_config


def evaluate(
    portfolio: Dict[str, Any],
    positions: List[Dict[str, Any]],
    market: Dict[str, Any],
    entry_signal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """评估当前系统风险状态。

    Args:
        portfolio: {total_equity, cash, drawdown, target_dd/max_drawdown}
        positions: [{code, weight, pnl, industry}]
        market: {volatility_index, breadth, sector_heat}
        entry_signal: {action_type, confidence} from entry_engine

    Returns:
        {risk_score, regime, constraints, breakdowns}
    """
    entry = entry_signal or {"action_type": "NONE", "confidence": 0}
    cfg = load_middle_config("risk_engine", {
        "target_drawdown": 0.20,
        "default_volatility_index": 0.5,
        "drawdown_vol_penalty_weight": 0.5,
        "concentration_stock_weight": 0.5,
        "concentration_sector_weight": 0.5,
        "vol_high": 0.7,
        "vol_mid": 0.4,
        "vol_high_score": 1.0,
        "vol_mid_score": 0.6,
        "vol_low_score": 0.3,
        "add_signal_multiplier": 0.6,
        "market_cooling_risk": 0.2,
        "market_improving_risk": -0.1,
        "component_weights": {"drawdown": 0.40, "volatility": 0.25, "concentration": 0.15, "signal": 0.10, "market": 0.10},
        "low_risk_cutoff": 0.45,
        "high_risk_cutoff": 0.65,
        "open_gate": 0.60,
        "add_gate": 0.55,
        "position_multiplier_floor": 0.25,
    })

    # ── ① 回撤风险 ──
    dd = abs(portfolio.get("drawdown", 0))
    max_dd = portfolio.get(
        "target_dd",
        portfolio.get("max_drawdown", cfg.get("target_drawdown", 0.20)),
    )
    dd_pressure = min(1.0, dd / max_dd) if max_dd > 0 else 0
    vol_penalty = market.get("volatility_index", cfg.get("default_volatility_index", 0.5)) * cfg.get("drawdown_vol_penalty_weight", 0.5)
    drawdown_risk = min(1.0, dd_pressure + vol_penalty)

    # ── ② 集中度风险 ──
    max_stock = max((p.get("weight", 0) for p in positions), default=0)
    sector_exposure: Dict[str, float] = {}
    for p in positions:
        ind = p.get("industry", "其他")
        sector_exposure[ind] = sector_exposure.get(ind, 0) + p.get("weight", 0)
    max_sector = max(sector_exposure.values()) if sector_exposure else 0
    concentration_risk = min(
        1.0,
        max_stock * cfg.get("concentration_stock_weight", 0.5)
        + max_sector * cfg.get("concentration_sector_weight", 0.5),
    )

    # ── ③ 波动风险 ──
    vol = market.get("volatility_index", cfg.get("default_volatility_index", 0.5))
    if vol > cfg.get("vol_high", 0.7):
        vol_risk = cfg.get("vol_high_score", 1.0)
    elif vol > cfg.get("vol_mid", 0.4):
        vol_risk = cfg.get("vol_mid_score", 0.6)
    else:
        vol_risk = cfg.get("vol_low_score", 0.3)

    # ── ④ 信号压力 ──
    action = entry.get("action_type", "NONE")
    conf = entry.get("confidence", 0)
    if action == "OPEN":
        signal_risk = conf
    elif action == "ADD":
        signal_risk = conf * cfg.get("add_signal_multiplier", 0.6)
    else:
        signal_risk = 0.0

    # ── 市场风险 ──
    mkt_regime = market.get("regime", "neutral")
    if mkt_regime in ("cooling", "weak"):
        market_risk = cfg.get("market_cooling_risk", 0.2)
    elif mkt_regime in ("improving", "strong"):
        market_risk = cfg.get("market_improving_risk", -0.1)
    else:
        market_risk = 0.0

    # ── 总风险 ──
    weights = cfg.get("component_weights", {})
    total_risk = round(
        weights.get("drawdown", 0.40) * drawdown_risk
        + weights.get("volatility", 0.25) * vol_risk
        + weights.get("concentration", 0.15) * concentration_risk
        + weights.get("signal", 0.10) * signal_risk
        + weights.get("market", 0.10) * max(0, market_risk),
        2,
    )

    # ── regime ──
    if total_risk < cfg.get("low_risk_cutoff", 0.45):
        regime = "low_risk"
    elif total_risk < cfg.get("high_risk_cutoff", 0.65):
        regime = "normal"
    else:
        regime = "high_risk"

    # ── Gate（硬闸门，portfolio 必须遵守）──
    gate = {
        "OPEN": total_risk < cfg.get("open_gate", 0.60),
        "ADD":  total_risk < cfg.get("add_gate", 0.55),
        "NONE": True,  # 减仓/不操作永远允许
        "position_multiplier": round(max(cfg.get("position_multiplier_floor", 0.25), 1.0 - total_risk), 2),
    }

    return {
        "total_risk": total_risk,
        "regime": regime,
        "gate": gate,
        "components": {
            "drawdown": round(drawdown_risk, 2),
            "volatility": round(vol_risk, 2),
            "concentration": round(concentration_risk, 2),
            "signal_pressure": round(signal_risk, 2),
            "market": round(market_risk, 2),
        },
        "reason_chain": _build_reason(drawdown_risk, vol_risk, concentration_risk, signal_risk, market_risk, cfg),
    }


def _build_reason(dd: float, vol: float, conc: float, sig: float, mkt: float, cfg: Dict[str, Any]) -> list:
    reasons = []
    if dd > cfg.get("reason_drawdown", 0.5): reasons.append("drawdown dominant")
    if vol > cfg.get("reason_volatility", 0.7): reasons.append("vol elevated")
    if conc > cfg.get("reason_concentration", 0.5): reasons.append("concentration high")
    if sig > cfg.get("reason_signal", 0.5): reasons.append("signal pressure")
    if mkt > 0: reasons.append("market cooling")
    if not reasons: reasons.append("all clear")
    return reasons
