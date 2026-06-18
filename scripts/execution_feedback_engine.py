#!/usr/bin/env python3
"""
Execution Feedback Engine — 执行反馈闭环

职责：记录执行结果 → 计算误差 → 输出模型修正信号
让系统从"固定规则"升级为"自适应学习"

用法:
  from execution_feedback_engine import capture, analyze
  record = capture(sizing, exec_plan, fill_result)
  signal = analyze(records_history)
"""

from typing import Any, Dict, List


def capture(
    sizing: Dict[str, Any],
    exec_plan: Dict[str, Any],
    fill: Dict[str, Any],
) -> Dict[str, Any]:
    """记录单笔执行结果。

    Args:
        sizing: {symbol, action, target_weight, capital}
        exec_plan: {mode, slices, expected_slippage_bp, total_cost_bp}
        fill: {filled_weight, avg_price, market_vwap, fill_ratio, market_vol, spread_bp}

    Returns:
        ExecutionRecord
    """
    target_w = sizing.get("target_weight", 0)
    filled_w = fill.get("filled_weight", 0)
    fill_ratio = filled_w / target_w if target_w > 0 else 0

    expected_slip = exec_plan.get("total_cost_bp", exec_plan.get("expected_slippage_bp", 2))
    actual_slip = fill.get("actual_slippage_bp", 0)

    return {
        "symbol": sizing.get("symbol", ""),
        "action": sizing.get("action", ""),
        "target_weight": round(target_w, 4),
        "filled_weight": round(filled_w, 4),
        "fill_ratio": round(fill_ratio, 3),
        "expected_slippage_bp": expected_slip,
        "actual_slippage_bp": actual_slip,
        "slippage_error_bp": round(actual_slip - expected_slip, 1),
        "mode": exec_plan.get("mode", "?"),
        "market_vol": fill.get("market_vol", 0.5),
        "spread_bp": fill.get("spread_bp", 5),
        "timestamp": fill.get("timestamp", ""),
    }


def analyze(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """分析执行历史，输出模型修正信号。

    Args:
        history: list of ExecutionRecord from capture()

    Returns:
        FeedbackSignal with adjustments and quality scores
    """
    if not history:
        return {"execution_quality": "OK", "adjustments": {}, "notes": ["无历史数据"]}

    # 核心指标
    fill_ratios = [r["fill_ratio"] for r in history]
    slip_errors = [r["slippage_error_bp"] for r in history]

    avg_fill = sum(fill_ratios) / len(fill_ratios)
    avg_slip_error = sum(slip_errors) / len(slip_errors)

    # 质量评分
    # quality = 0.4×fill + 0.3×(1-|slip_error_norm|) + 0.3×(1-|impact_norm|)
    slip_norm = min(abs(avg_slip_error) / 10.0, 1.0)  # 10bp为基准
    quality = round(0.4 * avg_fill + 0.3 * (1 - slip_norm) + 0.3 * 0.8, 2)

    if quality > 0.8:
        quality_label = "GOOD"
    elif quality > 0.5:
        quality_label = "OK"
    else:
        quality_label = "BAD"

    # 修正信号
    adjustments = {}
    notes = []

    # ① 滑点修正 → sizing
    if avg_slip_error > 5:
        adjustments["sizing_multiplier"] = round(0.90, 2)
        notes.append(f"滑点偏高({avg_slip_error:.1f}bp)，降低仓位系数")
    elif avg_slip_error < -2:
        adjustments["sizing_multiplier"] = round(1.05, 2)
        notes.append("滑点低于预期，可适当放大仓位")

    # ② 成交率修正 → execution urgency
    if avg_fill < 0.6:
        adjustments["execution_urgency"] = 0.3
        notes.append(f"成交率低({avg_fill:.0%})，提高执行激进度")
    elif avg_fill < 0.8:
        adjustments["execution_urgency"] = 0.1
        notes.append(f"成交率偏低({avg_fill:.0%})")

    # ③ 按模式分组分析
    mode_stats = {}
    for r in history:
        m = r["mode"]
        if m not in mode_stats:
            mode_stats[m] = {"count": 0, "total_slip": 0, "total_fill": 0}
        mode_stats[m]["count"] += 1
        mode_stats[m]["total_slip"] += r["actual_slippage_bp"]
        mode_stats[m]["total_fill"] += r["fill_ratio"]

    best_mode = None
    best_cost = 999
    for m, s in mode_stats.items():
        avg_cost = s["total_slip"] / s["count"]
        if avg_cost < best_cost:
            best_cost = avg_cost
            best_mode = m
    if best_mode and best_mode != history[-1]["mode"]:
        notes.append(f"历史最优执行模式: {best_mode}(均滑点{best_cost:.1f}bp)")

    if not notes:
        notes.append("执行质量正常，无需调整")

    return {
        "execution_quality": quality_label,
        "quality_score": quality,
        "avg_fill_ratio": round(avg_fill, 2),
        "avg_slippage_error_bp": round(avg_slip_error, 1),
        "adjustments": adjustments,
        "notes": notes,
        "sample_size": len(history),
    }
