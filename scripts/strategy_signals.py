#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reusable strategy signal aggregation.

This module adapts the existing strategy scripts into optional signal providers.
Signals never decide trades directly. This module is legacy support for the
old review flow; the new trading chain uses buy_plan → entry → risk → sizing
→ portfolio_controller.
"""
import math
import os
from typing import Any, Dict, List, Optional


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")


def _get_thresholds() -> Dict[str, Any]:
    """从 config 读取 strategy_signals 阈值，带默认值兜底。"""
    import yaml
    try:
        with open(DEFAULT_CONFIG) as f:
            cfg = yaml.safe_load(f)
        return (cfg.get("pyramid_middle_layer", {}).get("strategy_signals", {}))
    except Exception:
        return {}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class StrategySignalCollector:
    """Collect optional factor/event/cycle signals for one position."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or DEFAULT_CONFIG
        self._pyramid_strategy = None
        self._data_provider = None

    def collect_for_position(self, position: Dict[str, Any]) -> Dict[str, Any]:
        signals = {
            "factor": self._factor_signal(position),
            "trend": self._trend_signal(position),
            "event": self._event_signal(position),
            "ml": self._unavailable("ML 信号待接入 buy_plan 候选排序，不在持仓 plan 中强制运行"),
            "microstructure": self._unavailable("Level2/资金流信号待接入，当前不作为基础决策依赖"),
        }

        supporting_factors: List[str] = []
        risk_factors: List[str] = []
        missing_data: List[str] = []

        for name, signal in signals.items():
            if not signal.get("available"):
                missing_data.append(f"{name}: {signal.get('reason', '不可用')}")
                continue
            supporting_factors.extend(signal.get("supporting_factors", []))
            risk_factors.extend(signal.get("risk_factors", []))

        return {
            "signals": signals,
            "supporting_factors": supporting_factors,
            "risk_factors": risk_factors,
            "missing_data": missing_data,
            "llm_context": self._build_llm_context(position, signals, supporting_factors, risk_factors, missing_data),
        }

    def _factor_signal(self, position: Dict[str, Any]) -> Dict[str, Any]:
        if position.get("asset_type") == "etf":
            return self._unavailable("ETF 暂不使用个股多因子评分")

        code = str(position.get("code", "")).strip()
        if not code:
            return self._unavailable("缺少股票代码")

        try:
            strategy = self._get_pyramid_strategy()
            provider = strategy.data_provider
            if not provider.available:
                return self._unavailable("MongoDB 数据源不可用")

            basic_collection = provider._get_collection("basic_info")
            if basic_collection is None:
                return self._unavailable("MongoDB basic_info 集合不可用")

            stock = basic_collection.find_one({
                "$or": [
                    {"code": code},
                    {"symbol": code},
                    {"ts_code": code},
                ]
            })
            if not stock:
                return self._unavailable(f"MongoDB 未找到股票基础信息: {code}，请先同步多因子基础数据")

            quotes = provider.get_recent_quotes(code, 260)
            if len(quotes) < 5:
                return self._unavailable(f"MongoDB 行情样本不足: {len(quotes)}，请先同步近 260 日行情")

            financial = provider.get_financial_data(code)
            score_result = strategy.calculate_composite_score(
                stock,
                quotes,
                financial,
                context=position,
            )
            composite_score = score_result["composite_score"]
            factor_scores = score_result["factor_scores"]
            factor_details = score_result.get("factor_details", {})
            value_avail = (factor_details.get("value") or {}).get("available")
            momentum_avail = (factor_details.get("momentum") or {}).get("available")
            missing_parts = score_result.get("missing_data", [])

            supporting, risks = [], []
            if composite_score >= 0.7:
                supporting.append(f"多因子综合得分较强: {composite_score:.2f}")
            elif composite_score <= 0.45:
                risks.append(f"多因子综合得分偏弱: {composite_score:.2f}")

            value_score = factor_scores['value']
            momentum_score = factor_scores['momentum']
            if value_avail and value_score >= 0.7:
                supporting.append(f"价值因子较好: {value_score:.2f}")
            if momentum_avail and momentum_score <= 0.4:
                risks.append(f"动量因子偏弱: {momentum_score:.2f}")
            if missing_parts:
                missing_labels = [self._factor_missing_label(item) for item in missing_parts]
                risks.append(f"多因子缺少{'、'.join(missing_labels)}，部分因子按中性估算")
            data_quality_warn = _get_thresholds().get("data_quality", {}).get("good", 0.75)
            if score_result.get("data_quality_score", 1) < data_quality_warn:
                risks.append(f"多因子数据完整度偏低: {score_result.get('data_quality_score', 0):.2f}")

            return {
                "available": True,
                "source": "pyramid_multifactor_strategy",
                "market": position.get("market"),
                "composite_score": composite_score,
                "factor_scores": factor_scores,
                "factor_weights": score_result["factor_weights"],
                "factor_details": score_result["factor_details"],
                "data_quality_score": score_result["data_quality_score"],
                "interpretation": self._factor_interpretation(position, score_result),
                "llm_context": self._factor_llm_context(position, score_result, supporting, risks, missing_parts),
                "supporting_factors": supporting,
                "risk_factors": risks,
                "missing_data": missing_parts,
                "factor_conflicts": score_result.get("factor_conflicts", []),
            }
        except Exception as exc:
            return self._unavailable(f"多因子信号异常: {exc}")

    def _event_signal(self, position: Dict[str, Any]) -> Dict[str, Any]:
        code = str(position.get("code", "")).strip()
        if not code:
            return self._unavailable("缺少股票代码")
        if position.get("asset_type") == "etf":
            return self._unavailable("ETF 暂不做个股新闻事件分析")

        try:
            provider = self._get_data_provider()
            news_list = provider.get_news(code, limit=10)
            if not news_list:
                return self._unavailable("未获取到个股新闻")

            # 批量情绪分析：N条新闻 → 1次LLM调用（vs 原来N次）
            texts = [f"{item.get('title', '')} {item.get('content', '')}" for item in news_list]
            results = provider.analyze_sentiment_batch(texts)
            scores = [_to_float(r.get("score")) for r in results]
            # 时间衰减加权：越新的新闻权重越高（半衰期约2天）
            weights = [math.exp(-item.get('days_old', 0) / 3.0) for item in news_list]
            total_weight = sum(weights) if weights else 1
            avg_score = sum(s * w for s, w in zip(scores, weights)) / total_weight if scores else 0
            themes = []
            positive_keywords, negative_keywords = [], []
            for text in texts:
                themes.extend(provider.extract_themes(text))
            for r in results:
                positive_keywords.extend(r.get("positive_keywords", []))
                negative_keywords.extend(r.get("negative_keywords", []))
            unique_themes = list(dict.fromkeys(themes))
            supporting, risks = [], []
            if avg_score > 0.2:
                supporting.append(f"新闻情绪偏正面: {avg_score:.2f}")
            elif avg_score < -0.2:
                risks.append(f"新闻情绪偏负面: {avg_score:.2f}")
            if unique_themes:
                supporting.append(f"关联主题: {'、'.join(unique_themes[:3])}")
            if negative_keywords:
                risks.append(f"负面关键词: {'、'.join(list(dict.fromkeys(negative_keywords))[:5])}")

            return {
                "available": True,
                "source": "event_driven_strategy",
                "news_count": len(news_list),
                "sentiment_score": round(avg_score, 3),
                "themes": unique_themes,
                "positive_keywords": list(dict.fromkeys(positive_keywords))[:8],
                "negative_keywords": list(dict.fromkeys(negative_keywords))[:8],
                "latest_titles": [item.get("title", "") for item in news_list[:3]],
                "llm_context": {
                    "purpose": "llm_input",
                    "signal_type": "event",
                    "facts": {
                        "news_count": len(news_list),
                        "sentiment_score": round(avg_score, 3),
                        "themes": unique_themes,
                        "positive_keywords": list(dict.fromkeys(positive_keywords))[:8],
                        "negative_keywords": list(dict.fromkeys(negative_keywords))[:8],
                        "latest_titles": [item.get("title", "") for item in news_list[:3]],
                    },
                    "supporting_factors": supporting,
                    "risk_factors": risks,
                    "missing_data": [],
                },
                "supporting_factors": supporting,
                "risk_factors": risks,
            }
        except Exception as exc:
            return self._unavailable(f"事件信号异常: {exc}")

    def _trend_signal(self, position: Dict[str, Any]) -> Dict[str, Any]:
        try:
            code = str(position.get("code", "")).strip()
            market = str(position.get("market", "A股"))
            price_currency = str(position.get("currency", "CNY"))
            decision_currency = str(position.get("decision_currency",
                                       position.get("cost_currency", price_currency)))
            fx_rate = _to_float(position.get("fx_rate_to_base"), 1.0)
            decision_fx_rate = _to_float(position.get("decision_fx_rate_to_base"), 1.0)
            price_to_decision_rate = fx_rate / decision_fx_rate if decision_fx_rate else 1.0

            provider = self._get_data_provider()
            signal = provider.get_trend_signal(
                code=code,
                market=market,
                decision_currency=decision_currency,
                price_to_decision_rate=price_to_decision_rate,
                add_price=_to_float(position.get("add_price"), None) or None,
                stop_loss_price=_to_float(position.get("stop_loss_price"), None) or None,
                cost_price=_to_float(position.get("cost_price"), None) or None,
            )
            if signal.get("available"):
                return signal
            return self._unavailable(signal.get("reason", "趋势信号不可用"))
        except Exception as exc:
            return self._unavailable(f"趋势信号异常: {exc}")

    def _build_llm_context(
        self,
        position: Dict[str, Any],
        signals: Dict[str, Dict[str, Any]],
        supporting_factors: List[str],
        risk_factors: List[str],
        missing_data: List[str],
    ) -> Dict[str, Any]:
        return {
            "purpose": "llm_input",
            "asset": {
                "code": position.get("code"),
                "name": position.get("name"),
                "market": position.get("market"),
                "asset_type": position.get("asset_type"),
                "style": position.get("style"),
                "industry": position.get("industry"),
            },
            "signal_contexts": {
                name: signal.get("llm_context")
                for name, signal in signals.items()
                if signal.get("available") and signal.get("llm_context")
            },
            "supporting_factors": supporting_factors,
            "risk_factors": risk_factors,
            "missing_data": missing_data,
        }

    def _factor_llm_context(
        self,
        position: Dict[str, Any],
        score_result: Dict[str, Any],
        supporting: List[str],
        risks: List[str],
        missing_parts: List[str],
    ) -> Dict[str, Any]:
        return {
            "purpose": "llm_input",
            "signal_type": "factor",
            "facts": {
                "role": position.get("style"),
                "composite_score": score_result.get("composite_score"),
                "factor_scores": score_result.get("factor_scores", {}),
                "factor_weights": score_result.get("factor_weights", {}),
                "factor_details": score_result.get("factor_details", {}),
                "data_quality_score": score_result.get("data_quality_score"),
            },
            "supporting_factors": supporting,
            "risk_factors": risks,
            "missing_data": missing_parts,
        }

    def _get_pyramid_strategy(self):
        if self._pyramid_strategy is None:
            from pyramid_multifactor_strategy import PyramidMultifactorStrategy

            self._pyramid_strategy = PyramidMultifactorStrategy(self.config_path)
        return self._pyramid_strategy

    def _get_data_provider(self):
        if self._data_provider is None:
            from market_data_provider import MarketDataProvider

            self._data_provider = MarketDataProvider()
        return self._data_provider

    def _unavailable(self, reason: str) -> Dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "supporting_factors": [],
            "risk_factors": [],
        }

    def _factor_missing_label(self, key: str) -> str:
        labels = {
            "financial": "财务数据",
            "quotes_20d": "20日行情",
            "quotes_60d": "60日行情",
        }
        return labels.get(key, key)

    def _factor_interpretation(self, position: Dict[str, Any], score_result: Dict[str, Any]) -> Dict[str, Any]:
        scores = score_result.get("factor_scores", {})
        weights = score_result.get("factor_weights", {})
        composite = _to_float(score_result.get("composite_score"), 0.5)
        data_quality = _to_float(score_result.get("data_quality_score"), 0)
        style = position.get("style", "未分类角色")

        if composite >= 0.75:
            summary = "多因子整体较强，基本面/估值/趋势对当前持仓逻辑形成明显支撑。"
        elif composite >= 0.6:
            summary = "多因子整体偏正面，可以作为继续持有或纳入候选的辅助支撑。"
        elif composite >= 0.45:
            summary = "多因子整体中性，暂时不能单独支持加仓，需要结合价格、周期和仓位约束。"
        else:
            summary = "多因子整体偏弱，需要复核持仓逻辑，尤其不要只因为价格下跌就加仓。"

        role_explanation = self._factor_role_explanation(style, weights)
        factor_evidence = self._factor_evidence(score_result.get("factor_details", {}))
        factor_narratives = self._factor_narratives(scores, factor_evidence)

        strongest = self._pick_factor(scores, reverse=True)
        weakest = self._pick_factor(scores, reverse=False)
        llm_usage_hint = self._factor_llm_usage_hint(composite, data_quality)

        return {
            "summary": summary,
            "role_explanation": role_explanation,
            "factor_narratives": factor_narratives,
            "strongest_factor": strongest,
            "weakest_factor": weakest,
            "llm_usage_hint": llm_usage_hint,
            "data_quality_view": self._data_quality_view(data_quality),
        }

    def _factor_score_view(self, name: str, score: Any, meaning: str) -> str:
        score_value = _to_float(score, 0.5)
        if score_value >= 0.75:
            level = "强"
            implication = "形成支撑"
        elif score_value >= 0.6:
            level = "偏强"
            implication = "略偏正面"
        elif score_value >= 0.45:
            level = "中性"
            implication = "参考价值有限"
        else:
            level = "偏弱"
            implication = "构成风险或拖累"
        return f"{name}因子{level}({score_value:.2f})：代表{meaning}，当前{implication}。"

    def _factor_narratives(self, scores: Dict[str, Any], evidence: Dict[str, str]) -> Dict[str, str]:
        configs = {
            "value": {
                "name": "价值",
                "meaning": "估值、股息或账面价值吸引力",
                "reason": "PE/PB 越合理、股息率越有吸引力，价值分越高；亏损或估值口径异常会压低分数。",
            },
            "growth": {
                "name": "成长",
                "meaning": "营收和利润增长能力",
                "reason": "营收增长和利润增长越强，说明增长动能越好，成长分越高；增长放缓或缺失会降低说服力。",
            },
            "quality": {
                "name": "质量",
                "meaning": "ROE、毛利率和资产负债表质量",
                "reason": "ROE 和毛利率越高、资产负债率越可控，质量分越高；它反映这家公司赚钱质量和财务稳健度。",
            },
            "momentum": {
                "name": "动量",
                "meaning": "近1-12个月价格趋势（含路径质量）",
                "reason": "近1月和近3月收益越强，趋势确认越充分，动量分越高；连续走弱会拖累买入或加仓判断。",
            },
        }
        return {
            key: self._factor_narrative(
                name=config["name"],
                score=scores.get(key),
                meaning=config["meaning"],
                evidence=evidence.get(key, "当前缺少可解释的明细数据"),
                scoring_reason=config["reason"],
            )
            for key, config in configs.items()
        }

    def _factor_narrative(
        self,
        name: str,
        score: Any,
        meaning: str,
        evidence: str,
        scoring_reason: str,
    ) -> str:
        score_value = _to_float(score, 0.5)
        if "按中性分估算" in evidence or evidence == "当前缺少可解释的明细数据":
            return (
                f"{name}因子 {score_value:.2f}，判断为数据不足。"
                f"实际数据: {evidence}。"
                f"打分理由: 因为关键指标缺失，系统没有足够证据判断这个因子强弱，暂时给中性分，避免把未知误判成利好或利空。"
                f"解释: 这个分数不代表{name}真的中性，只代表当前数据不足；做交易判断时应降低这个因子的权重或先补齐数据。"
            )
        if score_value >= 0.75:
            level = "强"
            conclusion = "当前形成明确支撑。"
        elif score_value >= 0.6:
            level = "偏强"
            conclusion = "当前略偏正面，可以作为辅助支撑。"
        elif score_value >= 0.45:
            level = "中性"
            conclusion = "当前参考价值有限，不能单独推动操作。"
        else:
            level = "偏弱"
            conclusion = "当前构成风险或拖累，需要谨慎对待。"
        return (
            f"{name}因子 {score_value:.2f}，判断为{level}。"
            f"实际数据: {evidence}。"
            f"打分理由: {scoring_reason}"
            f"解释: 这个因子代表{meaning}，{conclusion}"
        )

    def _factor_role_explanation(self, style: str, weights: Dict[str, Any]) -> str:
        weight_text = (
            f"价值{_to_float(weights.get('value')):.0%}、成长{_to_float(weights.get('growth')):.0%}、"
            f"质量{_to_float(weights.get('quality')):.0%}、动量{_to_float(weights.get('momentum')):.0%}"
        )
        if "红利" in style or "防御" in style:
            reason = "因为它承担红利防御角色，所以更看重估值安全边际和资产质量。"
        elif "科技" in style or "成长" in style:
            reason = "因为它承担科技成长角色，所以更看重成长、质量和趋势确认。"
        elif "资源" in style or "周期" in style:
            reason = "因为它承担周期弹性角色，所以更看重趋势动量，同时保留估值和质量约束。"
        elif "核心" in style:
            reason = "因为它承担核心资产角色，所以更看重质量稳定性和估值合理性。"
        else:
            reason = "当前角色没有明确特化，使用通用多因子权重。"
        return f"当前权重为 {weight_text}。{reason}"

    def _pick_factor(self, scores: Dict[str, Any], reverse: bool) -> str:
        labels = {
            "value": "价值",
            "growth": "成长",
            "quality": "质量",
            "momentum": "动量",
        }
        if not scores:
            return "暂无"
        key = sorted(scores, key=lambda item: _to_float(scores.get(item)), reverse=reverse)[0]
        return f"{labels.get(key, key)}({_to_float(scores.get(key)):.2f})"

    def _factor_llm_usage_hint(self, composite: float, data_quality: float) -> str:
        if data_quality < 0.5:
            return "LLM使用提示: 数据不足，分析时应降低多因子权重，并优先检查财务和行情缺口。"
        if composite >= 0.7:
            return "LLM使用提示: 多因子分数较高，可作为正向证据，但不能单独推出交易动作。"
        if composite >= 0.55:
            return "LLM使用提示: 多因子略偏正面，需要结合趋势、周期、仓位和目标确认。"
        if composite >= 0.45:
            return "LLM使用提示: 多因子中性，不能提供明确方向，应更多依赖其他维度。"
        return "LLM使用提示: 多因子偏弱，应作为风险证据输入综合推理。"

    def _data_quality_view(self, data_quality: float) -> str:
        if data_quality >= 0.9:
            return "数据较完整，评分可信度较高。"
        if data_quality >= 0.75:
            return "数据基本可用，但仍有少量缺口。"
        if data_quality >= 0.5:
            return "数据不够完整，评分需要保守解读。"
        return "数据缺口较大，评分只能作为弱参考。"

    def _factor_evidence(self, details: Dict[str, Any]) -> Dict[str, str]:
        value = details.get("value", {}) or {}
        growth = details.get("growth", {}) or {}
        quality = details.get("quality", {}) or {}
        momentum = details.get("momentum", {}) or {}

        return {
            "value": self._join_evidence([
                value.get("missing_reason"),
                self._metric_text("PE", value.get("pe")),
                self._pct_text("PE(行业)", value.get("pe_percentile")),
                self._metric_text("PB", value.get("pb")),
                self._pct_text("PB(行业)", value.get("pb_percentile")),
                self._metric_text("股息率", value.get("dividend_yield"), "%"),
                value.get("pe_warning"),
                value.get("pb_warning"),
            ]),
            "growth": self._join_evidence([
                growth.get("missing_reason"),
                self._metric_text("营收增长", growth.get("revenue_growth"), "%"),
                self._metric_text("利润增长", growth.get("profit_growth"), "%"),
                self._metric_text("营收加速", growth.get("revenue_accel"), "pp"),
                self._metric_text("利润加速", growth.get("profit_accel"), "pp"),
                self._metric_text("一致预期增速", growth.get("forecast_growth"), "%"),
                self._decay_text("数据新鲜度", growth.get("data_freshness_decay")),
            ]),
            "quality": self._join_evidence([
                quality.get("missing_reason"),
                self._metric_text("ROE", quality.get("roe"), "%"),
                self._pct_text("ROE(行业)", quality.get("roe_percentile")),
                self._metric_text("毛利率", quality.get("gross_margin"), "%"),
                self._pct_text("毛利率(行业)", quality.get("gm_percentile")),
                self._metric_text("资产负债率", quality.get("debt_ratio"), "%"),
                self._metric_text("OCF/NI", quality.get("ocf_to_net_income")),
                self._decay_text("数据新鲜度", quality.get("data_freshness_decay")),
            ]),
            "momentum": self._join_evidence([
                momentum.get("missing_reason"),
                self._metric_text("近1月收益", momentum.get("return_1m"), "%"),
                self._metric_text("近3月收益", momentum.get("return_3m"), "%"),
                self._metric_text("近6月收益", momentum.get("return_6m"), "%"),
                self._metric_text("近12月收益", momentum.get("return_12m"), "%"),
            ]),
        }

    def _pct_text(self, label: str, value: Any) -> str:
        """百分位文本，仅在有值时展示。"""
        if value is None:
            return ""
        number = _to_float(value, None)
        if number is None:
            return ""
        return f"{label} P{number:.0f}"

    def _decay_text(self, label: str, value: Any) -> str:
        """数据新鲜度衰减文本，仅在非 1.0 时展示。"""
        if value is None:
            return ""
        number = _to_float(value, None)
        if number is None or abs(number - 1.0) < 0.001:
            return ""
        return f"{label} x{number:.2f}"

    def _metric_text(self, label: str, value: Any, suffix: str = "") -> str:
        if value is None or value == "":
            return ""
        number = _to_float(value, None)
        if number is None:
            return f"{label} {value}"
        return f"{label} {number:.2f}{suffix}"

    def _join_evidence(self, items: List[str]) -> str:
        filtered = [str(item) for item in items if item]
        return "；".join(filtered) if filtered else "当前缺少可解释的明细数据"
