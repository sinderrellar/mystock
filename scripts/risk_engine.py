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


def evaluate(
    portfolio: Dict[str, Any],
    positions: List[Dict[str, Any]],
    market: Dict[str, Any],
    entry_signal: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """评估当前系统风险状态。

    Args:
        portfolio: {total_equity, cash, drawdown, target_dd}
        positions: [{code, weight, pnl, industry}]
        market: {volatility_index, breadth, sector_heat}
        entry_signal: {action_type, confidence} from entry_engine

    Returns:
        {risk_score, regime, constraints, breakdowns}
    """
    entry = entry_signal or {"action_type": "NONE", "confidence": 0}

    # ── ① 回撤风险 ──
    dd = abs(portfolio.get("drawdown", 0))
    max_dd = portfolio.get("target_dd", 0.20)
    dd_pressure = min(1.0, dd / max_dd) if max_dd > 0 else 0
    vol_penalty = market.get("volatility_index", 0.5) * 0.5
    drawdown_risk = min(1.0, dd_pressure + vol_penalty)

    # ── ② 集中度风险 ──
    max_stock = max((p.get("weight", 0) for p in positions), default=0)
    sector_exposure: Dict[str, float] = {}
    for p in positions:
        ind = p.get("industry", "其他")
        sector_exposure[ind] = sector_exposure.get(ind, 0) + p.get("weight", 0)
    max_sector = max(sector_exposure.values()) if sector_exposure else 0
    concentration_risk = min(1.0, max_stock * 0.5 + max_sector * 0.5)

    # ── ③ 波动风险 ──
    vol = market.get("volatility_index", 0.5)
    if vol > 0.7:
        vol_risk = 1.0
    elif vol > 0.4:
        vol_risk = 0.6
    else:
        vol_risk = 0.3

    # ── ④ 信号压力 ──
    action = entry.get("action_type", "NONE")
    conf = entry.get("confidence", 0)
    if action == "OPEN":
        signal_risk = conf
    elif action == "ADD":
        signal_risk = conf * 0.6
    else:
        signal_risk = 0.0

    # ── 市场风险 ──
    mkt_regime = market.get("regime", "neutral")
    if mkt_regime == "cooling":
        market_risk = 0.2
    elif mkt_regime == "improving":
        market_risk = -0.1
    else:
        market_risk = 0.0

    # ── 总风险 ──
    total_risk = round(
        0.40 * drawdown_risk + 0.25 * vol_risk
        + 0.15 * concentration_risk + 0.10 * signal_risk
        + 0.10 * max(0, market_risk), 2
    )

    # ── regime ──
    if total_risk < 0.45:
        regime = "low_risk"
    elif total_risk < 0.65:
        regime = "normal"
    else:
        regime = "high_risk"

    # ── Gate（硬闸门，portfolio 必须遵守）──
    gate = {
        "OPEN": total_risk < 0.60,
        "ADD":  total_risk < 0.55,
        "NONE": True,  # 减仓/不操作永远允许
        "position_multiplier": round(max(0.25, 1.0 - total_risk), 2),
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
        "reason_chain": _build_reason(drawdown_risk, vol_risk, concentration_risk, signal_risk, market_risk),
    }


def _build_reason(dd: float, vol: float, conc: float, sig: float, mkt: float) -> list:
    reasons = []
    if dd > 0.5: reasons.append("drawdown dominant")
    if vol > 0.7: reasons.append("vol elevated")
    if conc > 0.5: reasons.append("concentration high")
    if sig > 0.5: reasons.append("signal pressure")
    if mkt > 0: reasons.append("market cooling")
    if not reasons: reasons.append("all clear")
    return reasons
