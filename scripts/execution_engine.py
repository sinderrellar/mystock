#!/usr/bin/env python3
"""
Execution Engine v2 — 订单执行层

职责：把 target_weight 变成不会亏在交易本身的订单流
- 拆单（Order Slicing）
- 选执行方式（VWAP/TWAP/LIMIT）
- 控制市场冲击（Impact + Slippage + Brake）

用法:
  from execution_engine import plan
  result = plan(sizing_output, portfolio_state, market_context)
"""

from typing import Any, Dict, List


def plan(
    sizing: Dict[str, Any],
    portfolio: Dict[str, Any],
    market: Dict[str, Any],
) -> Dict[str, Any]:
    """将目标仓位转换为可执行订单。"""
    action = sizing.get("action", "NONE")
    target_w = sizing.get("target_weight", 0)
    symbol = sizing.get("symbol", "")
    capital = sizing.get("capital", 0)

    if action == "NONE" or target_w <= 0 or capital <= 0:
        return {"orders": [], "strategy": "NONE", "reason": "no trade"}

    current = portfolio.get("positions", {}).get(symbol, {})
    current_w = current.get("weight", 0)
    delta_w = target_w - current_w
    delta_pct = abs(delta_w)

    if delta_pct < 0.001:
        return {"orders": [], "strategy": "HOLD", "reason": "at target"}

    side = "BUY" if delta_w > 0 else "SELL"

    # ── ① 交易规模分类 ──
    if delta_pct < 0.01:
        scale = "small"
    elif delta_pct < 0.05:
        scale = "normal"
    else:
        scale = "large"

    # ── ② 市场环境 ──
    regime = market.get("regime", "neutral")
    vol = market.get("volatility_index", market.get("vol_index", 0.5))
    liquidity = market.get("liquidity", "normal")
    spread_bp = market.get("spread_bp", 5)

    # ── ③ 执行模式选择 ──
    if spread_bp > 20:
        mode, urgency = "PASSIVE_LIMIT", 0.2  # 价差大不进
    elif regime == "cooling" or vol > 0.7:
        mode, urgency = "TWAP_SLOW", 0.3        # 慢慢买
    elif liquidity == "low":
        mode, urgency = "ICEBERG", 0.4          # 隐藏意图
    elif scale == "large":
        mode, urgency = "VWAP", 0.6             # 跟量
    else:
        mode, urgency = "LIMIT_JOIN", 0.8       # 正常接单

    # ── ④ 参与率（控制吃掉市场多少流动性）──
    participation = min(0.10, 0.30 / max(vol, 0.3), 0.15 if liquidity == "low" else 0.10)

    # ── ⑤ 拆单 ──
    slices = _compute_slices(delta_pct, vol, liquidity, scale)
    slice_pct = delta_pct / slices

    orders = []
    for i in range(slices):
        orders.append({
            "slice": i + 1,
            "side": side,
            "qty_pct": round(slice_pct, 4),
            "type": "LIMIT" if "LIMIT" in mode else "MARKET",
        })

    # ── ⑥ 冲击成本 ──
    # Slippage ≈ vol × participation × spread
    slippage = round(vol * participation * spread_bp, 1)
    # Impact ≈ (order_size / ADV)^2，简化
    impact = round((delta_pct / 0.05) ** 2 * 2, 1) if delta_pct > 0.01 else 0
    total_cost_bp = round(slippage + impact, 1)

    # ── ⑦ 执行刹车（保护机制）──
    brake = spread_bp > 30 or (vol > 0.9 and scale == "large")

    return {
        "orders": orders,
        "mode": mode,
        "urgency": urgency,
        "slices": slices,
        "participation_pct": round(participation * 100, 1),
        "slippage_bp": slippage,
        "impact_bp": impact,
        "total_cost_bp": total_cost_bp,
        "brake": brake,
        "scale": scale,
        "delta_weight": round(delta_w, 4),
    }


def _compute_slices(qty_pct: float, vol: float, liquidity: str, scale: str = "normal") -> int:
    """根据波动率和流动性决定拆单数量。"""
    base = 3
    if vol > 0.7:
        base += 3     # 高波多拆
    elif vol > 0.4:
        base += 1
    if liquidity == "low":
        base += 4     # 低流动多拆
    if qty_pct > 0.05 or scale == "large":
        base += 2     # 大单多拆
    return min(base, 12)
