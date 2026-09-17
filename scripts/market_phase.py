#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场阶段判定 (Market Phase)

职责：把已有的市场宽度、指数动量、北向资金、两融数据聚合成一个
      结构化的市场阶段判定，供 buy_plan 择时和 portfolio 风控消费。

设计原则：
  - 纯函数，无 IO、无 MongoDB 依赖，便于单元测试
  - 所有输入允许 None（数据缺失时降级为中性，不抛异常）
  - 输出带完整证据链，不做黑盒判断

阶段定义（简化 Wyckoff）：
  markup        上升期 — 正常执行买入计划
  accumulation  筑底期 — 仅允许低吸场景，控制节奏
  distribution  分配期 — 减仓优先，新开仓需更高门槛
  decline       下跌期 — 暂停新买入

用法:
    from market_phase import determine_market_phase
    phase = determine_market_phase(
        breadth_pct=52.0,
        index_momentum_20d=2.5,
        north_flow_5d=120.0,
        margin_trend="up",
    )
    print(phase["phase"], phase["evidence"])
"""

from typing import Any, Dict, List, Optional

# 阶段元数据：分值区间 → 阶段 + 交易约束
PHASE_MARKUP = "markup"
PHASE_ACCUMULATION = "accumulation"
PHASE_DISTRIBUTION = "distribution"
PHASE_DECLINE = "decline"

_PHASE_META: Dict[str, Dict[str, Any]] = {
    PHASE_MARKUP: {
        "label": "上升期",
        "action": "正常执行买入计划",
        "allow_new_buy": True,
        "dip_only": False,
    },
    PHASE_ACCUMULATION: {
        "label": "筑底期",
        "action": "仅低吸场景，控制建仓节奏",
        "allow_new_buy": True,
        "dip_only": True,
    },
    PHASE_DISTRIBUTION: {
        "label": "分配期",
        "action": "减仓优先，新开仓需更高门槛",
        "allow_new_buy": True,
        "dip_only": True,
    },
    PHASE_DECLINE: {
        "label": "下跌期",
        "action": "暂停新买入",
        "allow_new_buy": False,
        "dip_only": True,
    },
}

# 判定阈值（集中管理，便于后续挪到 config）
# 注：此处的强/弱线（55/35）与 buy_plan._resolve_regime 的档位线（60/35，config_complete.yaml 的
# market_regime 段）刻意不同——本模块只做「是否熔断」的择时，regime 才决定阈值套件，两者职责不同，
# 勿强行统一口径。
BREADTH_STRONG = 55.0        # 市场宽度强线（%）
BREADTH_WEAK = 35.0          # 市场宽度弱线（%）
INDEX_MOMENTUM_STRONG = 3.0  # 指数20日动量强线（%）
INDEX_MOMENTUM_WEAK = -3.0   # 指数20日动量弱线（%）


def determine_market_phase(
    breadth_pct: Optional[float] = None,
    index_momentum_20d: Optional[float] = None,
    north_flow_5d: Optional[float] = None,
    margin_trend: Optional[str] = None,
) -> Dict[str, Any]:
    """判定当前市场阶段。

    Args:
        breadth_pct: 市场宽度，站上 MA20 的股票比例（0-100 的百分数）
        index_momentum_20d: 主要指数近20日收益率（%）
        north_flow_5d: 北向资金近5日净买额（亿元，正=净流入）
        margin_trend: 两融余额趋势，"up" / "down" / None

    Returns:
        {
          "phase": str,                # markup / accumulation / distribution / decline
          "label": str,                # 中文标签
          "action": str,               # 交易建议
          "allow_new_buy": bool,       # 是否允许新开仓
          "dip_only": bool,            # 是否仅允许低吸场景
          "score": int,                # 原始打分 -1..4
          "confidence": float,         # 0-1，基于可用信号数量
          "evidence": List[str],       # 证据链
          "signals_available": int,    # 参与判定的信号数
          "signals_total": int,        # 信号总数（4）
        }
    """
    score = 0
    evidence: List[str] = []
    available = 0
    total = 4

    # ① 市场宽度
    if breadth_pct is not None:
        available += 1
        if breadth_pct >= BREADTH_STRONG:
            score += 1
            evidence.append(f"市场宽度 {breadth_pct:.1f}% 偏强（≥{BREADTH_STRONG:.0f}%）")
        elif breadth_pct < BREADTH_WEAK:
            score -= 1
            evidence.append(f"市场宽度 {breadth_pct:.1f}% 偏弱（<{BREADTH_WEAK:.0f}%）")
        else:
            evidence.append(f"市场宽度 {breadth_pct:.1f}% 中性")
    else:
        evidence.append("市场宽度数据缺失")

    # ② 指数动量
    if index_momentum_20d is not None:
        available += 1
        if index_momentum_20d > INDEX_MOMENTUM_STRONG:
            score += 1
            evidence.append(f"指数20日动量 {index_momentum_20d:+.1f}% 向上")
        elif index_momentum_20d < INDEX_MOMENTUM_WEAK:
            score -= 1
            evidence.append(f"指数20日动量 {index_momentum_20d:+.1f}% 向下")
        else:
            evidence.append(f"指数20日动量 {index_momentum_20d:+.1f}% 震荡")
    else:
        evidence.append("指数动量数据缺失")

    # ③ 北向资金
    if north_flow_5d is not None:
        available += 1
        if north_flow_5d > 0:
            score += 1
            evidence.append(f"北向5日净流入 {north_flow_5d:.1f}亿")
        elif north_flow_5d < 0:
            score -= 1
            evidence.append(f"北向5日净流出 {north_flow_5d:.1f}亿")
        else:
            evidence.append("北向5日资金净流量持平")
    else:
        evidence.append("北向资金数据缺失")

    # ④ 两融趋势
    if margin_trend in ("up", "down"):
        available += 1
        if margin_trend == "up":
            score += 1
            evidence.append("两融余额上升，风险偏好回升")
        else:
            score -= 1
            evidence.append("两融余额下降，风险偏好收缩")
    else:
        evidence.append("两融趋势数据缺失")

    # 阶段判定：按得分分档
    if score >= 3:
        phase = PHASE_MARKUP
    elif score == 2:
        phase = PHASE_ACCUMULATION
    elif score >= 0:
        phase = PHASE_DISTRIBUTION
    else:
        phase = PHASE_DECLINE

    # 置信度 = 可用信号占比（数据越全越可信）
    confidence = round(available / total, 2) if total else 0.0

    # 数据严重不足时只展示观察结论，不据此增加交易约束。
    insufficient_data = available <= 1
    if insufficient_data:
        evidence.append(f"⚠ 仅 {available}/{total} 个信号可用，不增加阶段性交易约束")
        if phase in (PHASE_MARKUP, PHASE_DECLINE):
            phase = PHASE_DISTRIBUTION if score < 2 else PHASE_ACCUMULATION

    meta = _PHASE_META[phase]
    return {
        "phase": phase,
        "label": meta["label"],
        "action": "数据不足，沿用基础风控" if insufficient_data else meta["action"],
        "allow_new_buy": True if insufficient_data else meta["allow_new_buy"],
        "dip_only": False if insufficient_data else meta["dip_only"],
        "score": score,
        "confidence": confidence,
        "evidence": evidence,
        "signals_available": available,
        "signals_total": total,
    }


def format_market_phase(phase_result: Dict[str, Any]) -> str:
    """把判定结果渲染成单行文本，供报告头部输出。"""
    if not phase_result:
        return "市场阶段: 未判定"
    return (
        f"市场阶段: {phase_result.get('label', '?')}"
        f"({phase_result.get('phase', '?')}) | "
        f"得分 {phase_result.get('score', 0)} | "
        f"置信度 {phase_result.get('confidence', 0):.0%}"
        f"({phase_result.get('signals_available', 0)}/{phase_result.get('signals_total', 4)}信号) | "
        f"{phase_result.get('action', '')}"
    )


def extract_phase_inputs(market_sentiment: Optional[Dict[str, Any]],
                         breadth_pct: Optional[float] = None) -> Dict[str, Any]:
    """从 MarketDataProvider.get_market_sentiment() 的结果中提取判定输入。

    容错设计：任何字段缺失都返回 None，由 determine_market_phase 降级处理。
    """
    ms = market_sentiment or {}

    # 指数动量：取各指数 return_20d 的均值
    index_momentum = None
    indices = ms.get("indices") or {}
    rets = [v.get("return_20d") for v in indices.values()
            if isinstance(v, dict) and v.get("available") and v.get("return_20d") is not None]
    if rets:
        index_momentum = sum(rets) / len(rets)

    # 北向资金 5日净买额
    north_flow = None
    nb = ms.get("north_bound") or {}
    age_days = nb.get("age_days")
    fresh_enough = age_days is None or age_days <= 7
    if (nb.get("available") and nb.get("usable_for_phase", True)
            and fresh_enough):
        north_flow = nb.get("north_5d_buy")

    # 两融趋势：融资余额环比方向
    margin_trend = None
    mg = ms.get("margin") or {}
    if mg.get("available"):
        change = mg.get("rzye_change")
        if change is not None:
            margin_trend = "up" if change > 0 else "down"

    return {
        "breadth_pct": breadth_pct,
        "index_momentum_20d": index_momentum,
        "north_flow_5d": north_flow,
        "margin_trend": margin_trend,
    }
