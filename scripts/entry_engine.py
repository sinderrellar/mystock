#!/usr/bin/env python3
"""
Entry Engine — 交易时机层 (Time-Series Entry Timing)

职责：判断候选股票"现在能不能买"
输入：行情 + 技术信号 + alpha_context
输出：entry_score + ★评级 + BUY/WAIT

不参与选股（那是 buy_plan），不管理持仓（那是 portfolio_strategy）。
"""

from typing import Any, Dict, Optional


def evaluate(stock: Dict[str, Any]) -> Dict[str, Any]:
    """评估单只股票的买入时机。

    Args:
        stock: 必须包含:
            - trend_signal: RSI/KDJ/MACD/MA/status/returns/volume_price_signal
            - current_price: 当前价格
            - alpha_context: {momentum, cycle, turnaround} 来自 buy_plan
            - pe_percentile_self: PE历史分位（可选）
            - target_buy_zone: 目标买入区间（可选）

    Returns:
        {entry_score, technical_score, alpha_bonus, score(1-5★), label, verdict, details}
    """
    trend = stock.get("trend_signal", {}) or {}
    price = stock.get("current_price", 0)
    target_zone = stock.get("target_buy_zone", "")
    pe_pct = stock.get("pe_percentile_self")
    alpha_ctx = stock.get("alpha_context", {})

    if not trend.get("available"):
        return {"entry_score": 0, "technical_score": 0, "alpha_bonus": 0,
                "score": 0, "max_score": 5, "label": "⚪",
                "verdict": "趋势数据不可用", "details": []}

    ma = trend.get("ma", {}) or {}
    rets = trend.get("returns", {}) or {}
    ti = trend.get("technical_indicators", {}) or {}
    rsi = ti.get("rsi14")
    kdj = ti.get("kdj", {}) or {}
    kdj_j = kdj.get("j")
    ret_5d = rets.get("return_5d")
    ret_20d = rets.get("return_20d")
    ma20 = ma.get("ma20")
    ma60 = ma.get("ma60")
    above_ma20 = price > ma20 if ma20 else None
    ma20_dist = (price - ma20) / ma20 * 100 if ma20 and ma20 > 0 else None
    vol_sig = ti.get("volume_price_signal", "")

    details = []

    # ═══════════════════════════════════════════
    # 技术信号层（60%）：回答"现在是不是可交易位置"
    # ═══════════════════════════════════════════

    # ① 趋势方向（30%）
    trend_score = 0.5
    if ret_20d is not None:
        if ret_20d > 0:
            trend_score = 0.9
            details.append(f"20日 {ret_20d:+.1f}% 趋势向上")
        elif ret_20d > -5:
            trend_score = 0.6
            details.append(f"20日 {ret_20d:+.1f}% 跌幅可控")
        else:
            trend_score = 0.2
            details.append(f"20日 {ret_20d:+.1f}% 明显下跌")

    # ② MA位置（20%）
    pos_score = 0.5
    if above_ma20 is True:
        pos_score = 0.8
        details.append(f"站上MA20({ma20:.1f})")
        if ma60 and price > ma60:
            pos_score = 0.9
            details.append(f"站上MA60({ma60:.1f})")
    elif ma20_dist is not None:
        if ma20_dist > -3:
            pos_score = 0.5
            details.append(f"距MA20 {ma20_dist:.1f}%，接近中")
        else:
            pos_score = 0.2
            details.append(f"距MA20 {ma20_dist:.1f}%，远离均线")

    # ③ 短期动量（20%）
    mom_score = 0.5
    if ret_5d is not None:
        if ret_5d > 0:
            mom_score = 0.7
            details.append(f"5日 {ret_5d:+.1f}%")
            if ret_20d is not None and ret_5d > ret_20d:
                mom_score = 0.85
                details.append("5日>20日，加速中")
        elif ret_5d > -3:
            mom_score = 0.5
            details.append(f"5日 {ret_5d:+.1f}% 企稳")
        else:
            mom_score = 0.2
            details.append(f"5日 {ret_5d:+.1f}% 仍在跌")

    # ④ 反转信号（20%）
    rev_score = 0.5
    if rsi is not None:
        if 40 <= rsi <= 60:
            rev_score = 0.8
            details.append(f"RSI {rsi:.0f} 温和")
        elif rsi < 30:
            rev_score = 0.3
            details.append(f"RSI {rsi:.0f} 超卖")
        elif rsi > 75:
            rev_score = 0.2
            details.append(f"RSI {rsi:.0f} 过热")

    if kdj_j is not None and kdj_j > 0 and kdj_j < 10:
        rev_score = min(1.0, rev_score + 0.1)
        details.append(f"KDJ J={kdj_j:.1f} 刚翻正")
    elif kdj_j is not None and kdj_j < 0 and ret_5d is not None and ret_20d is not None and ret_5d > ret_20d:
        rev_score = min(1.0, rev_score + 0.05)
        details.append(f"KDJ J={kdj_j:.1f} 谷底回升")

    # ⑤ 量价配合（10%）
    vol_score = 0.5
    if "放量" in str(vol_sig):
        vol_score = 0.7
        details.append("放量信号")
    if "缩量" in str(vol_sig):
        vol_score = 0.3
        details.append("缩量信号")

    technical_score = 0.30 * trend_score + 0.20 * pos_score \
                    + 0.20 * mom_score + 0.20 * rev_score + 0.10 * vol_score

    # ═══════════════════════════════════════════
    # Alpha 加成层（40%）：buy_plan 因子分
    # 硬约束：技术分不到 0.55，alpha 不生效
    # ═══════════════════════════════════════════
    m = alpha_ctx.get("momentum", 0.5)
    c = alpha_ctx.get("cycle", 0.5)
    t = alpha_ctx.get("turnaround", 0.5)
    alpha_bonus_raw = 0.40 * m + 0.35 * c + 0.25 * t
    alpha_bonus = round((alpha_bonus_raw - 0.5) * 0.6, 2)

    entry_score = technical_score
    if technical_score >= 0.55:
        entry_score = min(1.0, technical_score + alpha_bonus)

    # ═══════════════════════════════════════════
    # 统一状态快照（portfolio 直接消费，不重复评估）
    # ═══════════════════════════════════════════
    vol_20d = trend.get("volatility_20d")
    atr_pct = round(vol_20d / price * 100, 2) if vol_20d and price else None

    cycle = alpha_ctx.get("cycle", 0.5)
    if cycle >= 0.6:   market_regime = "improving"
    elif cycle >= 0.4: market_regime = "neutral"
    else:              market_regime = "cooling"

    state_snapshot = {
        "trend": {
            "ret20d": ret_20d,
            "above_ma20": above_ma20,
            "ma20_dist_pct": round(ma20_dist, 1) if ma20_dist is not None else None,
        },
        "momentum_short": {
            "ret5d": ret_5d,
            "accelerating": (ret_5d is not None and ret_20d is not None and ret_5d > ret_20d),
        },
        "reversal": {
            "rsi14": rsi,
            "kdj_j": round(kdj_j, 1) if kdj_j is not None else None,
        },
        "volume": {"signal": vol_sig},
        "alpha": {
            "momentum": alpha_ctx.get("momentum", 0.5),
            "cycle": cycle,
            "turnaround": alpha_ctx.get("turnaround", 0.5),
        },
        "risk": {
            "volatility_20d": vol_20d,
            "atr_pct": atr_pct,
            "vol_regime": "high" if (vol_20d and vol_20d > 3.0) else "normal",
        },
        "market": {
            "regime": market_regime,
            "cycle_level": cycle,
        },
    }

    # 纯状态机输出：不做 UI 解释，portfolio 负责
    star_score = min(5, max(0, round(entry_score * 5)))
    # entry 只出信号类型，portfolio 结合仓位做唯一决策
    if entry_score >= 0.65:
        action_type = "OPEN"
    elif entry_score >= 0.45:
        action_type = "ADD"
    else:
        action_type = "NONE"

    signal = {
        "action_type": action_type,               # OPEN / ADD / NONE
        "confidence": round(entry_score, 2),
    }

    return {
        "entry_score": round(entry_score, 2),
        "technical_score": round(technical_score, 2),
        "alpha_bonus": alpha_bonus,
        "score": star_score,
        "max_score": 5,
        "signal": signal,
        "reason_chain": [
            f"technical={technical_score:.2f}",
            f"alpha_bonus={alpha_bonus:+.2f}",
            f"entry_score={entry_score:.2f}",
            f"market_regime={market_regime}",
        ],
        "state_snapshot": state_snapshot,
    }
