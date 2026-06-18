#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三层金字塔多因子选股策略
顶层：大类资产配置（行业轮动 + 仓位调整）
中层：多因子选股（价值 + 成长 + 质量 + 动量）
底层：交易执行优化（流动性 + 止盈止损）
┌─────────────────────────────────────────────────────────────┐
│  顶层：大类资产配置 (20%)                                      │
│  ├─ 行业轮动: 选出强势行业（如前5个）                            │
│  └─ 仓位调整: 根据市场情绪决定总仓位                             │
│                    ↓ 缩小范围                                │
├─────────────────────────────────────────────────────────────┤
│  中层: 多因子选股 (50%)                                       │
│  ├─ 从强势行业中选股（或全市场）                                 │
│  ├─ 价值因子: PE/PB/股息率                                    │
│  ├─ 成长因子: 营收/利润增长                                    │
│  ├─ 质量因子: ROE/毛利率/负债率                                │
│  └─ 动量因子: 1月/3月收益率                                    │
│                    ↓ 精选个股                                │
├─────────────────────────────────────────────────────────────┤
│  🆕 ML预测层 (30%)                                           │
│  ├─ 特征：多因子得分 + 技术指标 + 基本面                         │
│  ├─ 模型：随机森林/XGBoost 预测未来N日收益                       │
│  └─ 输出：预测收益率 + 置信度                                   │
│                    ↓                                        │
├─────────────────────────────────────────────────────────────┤
│  底层：交易执行优化 (30%)                                      │
│  ├─ 流动性过滤：成交额/换手率                                   │
│  ├─ 买入区间：当前价 ±X%                                       │
│  └─ 止盈止损：8%止损 / 20%止盈                                 │
└─────────────────────────────────────────────────────────────┘
"""
import os
import sys
import bisect
import json
import yaml
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

try:
    from bson import ObjectId
except ImportError:
    ObjectId = None

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config

os.makedirs('logs', exist_ok=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/pyramid_multifactor.log')
    ]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)


class MongoJSONEncoder(json.JSONEncoder):
    """自定义 JSON 编码器，处理 MongoDB ObjectId 和 datetime"""
    def default(self, obj):
        if ObjectId and isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def _build_factor_data_store(config: Dict, config_path: str = "") -> MongoFactorDataStore:
    """从完整配置字典构建统一的 MongoDB 数据存储实例。"""
    if config_path:
        return MongoFactorDataStore(_load_mongodb_config(config_path))

    mongodb_section = (config.get("data_sources") or {}).get("mongodb") or {}
    if not mongodb_section.get("enabled") or not mongodb_section.get("uri"):
        raise RuntimeError("MongoDB 数据源未启用或 URI 未配置")
    mongodb_config = {
        "uri": mongodb_section["uri"],
        "database": mongodb_section.get("database", "tradingagents"),
        "collections": mongodb_section.get("collections", {
            "basic_info": "stock_basic_info",
            "daily_quotes": "stock_daily_quotes",
            "financial_data": "stock_financial_data",
        }),
    }
    return MongoFactorDataStore(mongodb_config)


class PyramidMultifactorStrategy:
    """三层金字塔多因子选股策略"""

    def __init__(self, config_path: str = "config/config_complete.yaml"):
        self.config_path = config_path
        self.config = self._load_config()
        self.data_provider = _build_factor_data_store(self.config, self.config_path)

        # 加载三层配置
        self.top_layer_config = self.config.get('pyramid_top_layer', {})
        self.middle_layer_config = self.config.get('pyramid_middle_layer', {})
        self.bottom_layer_config = self.config.get('pyramid_bottom_layer', {})

        # 行业横截面归一化配置
        self.ind_config = self.middle_layer_config.get('industry_relative_scoring', {})
        self.ind_enabled = self.ind_config.get('enabled', False)
        self.ind_blend = self.ind_config.get('blend_ratio', 0.5)
        self.ind_min_peers = self.ind_config.get('min_peers', 5)
        self._industry_stats: Optional[Dict[str, Dict]] = None  # 懒加载缓存

    def _load_config(self) -> Dict:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            logger.info(f"成功加载配置文件: {self.config_path}")
            return config
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    # ============================================================
    # 顶层：大类资产配置
    # ============================================================

    def analyze_sector_rotation(self) -> Dict[str, Any]:
        """
        行业轮动分析（优化版）
        使用快速批量查询计算各行业的平均涨幅
        """
        if not self.top_layer_config.get('enabled', True):
            return {'enabled': False}

        rotation_config = self.top_layer_config.get('sector_rotation', {})
        lookback_days = rotation_config.get('lookback_days', 20)
        top_n = rotation_config.get('top_n_sectors', 5)

        logger.info(f"开始行业轮动分析（回溯{lookback_days}天）...")
        
        # 使用快速版本
        sector_performance = self.data_provider.get_sector_performance_fast(lookback_days)
        
        if not sector_performance:
            logger.warning("无法获取行业表现数据")
            return {'enabled': True, 'sectors': [], 'error': '无行业数据'}

        # 选择强势行业
        strong_sectors = sector_performance[:top_n]
        weak_sectors = sector_performance[-top_n:] if len(sector_performance) > top_n else []

        logger.info(f"行业轮动分析完成: {len(sector_performance)} 个行业，强势行业 {len(strong_sectors)} 个")

        return {
            'enabled': True,
            'lookback_days': lookback_days,
            'total_sectors': len(sector_performance),
            'strong_sectors': strong_sectors,
            'weak_sectors': weak_sectors,
            'all_sectors': sector_performance[:20]  # 只返回前20个
        }

    def calculate_position_adjustment(self, market_sentiment: str = 'neutral') -> Dict[str, Any]:
        """
        仓位动态调整
        根据市场情绪调整建议仓位
        """
        position_config = self.top_layer_config.get('position_adjustment', {})
        
        if market_sentiment == 'bullish':
            position = position_config.get('bullish_position', 0.9)
            advice = "市场乐观，建议高仓位运作"
        elif market_sentiment == 'bearish':
            position = position_config.get('bearish_position', 0.5)
            advice = "市场悲观，建议降低仓位"
        else:
            position = position_config.get('neutral_position', 0.7)
            advice = "市场中性，建议适度仓位"

        return {
            'market_sentiment': market_sentiment,
            'suggested_position': position,
            'advice': advice
        }

    # ============================================================
    # 中层：多因子选股
    # ============================================================

    def calculate_value_score(self, stock: Dict, quotes: List[Dict],
                              override_scoring: Optional[Dict[str, Any]] = None
                              ) -> Tuple[float, Dict]:
        """计算价值因子得分 PE、PB、股息率。"""
        if override_scoring:
            # 组 config 的 scoring 段直接有 pe/pb/dividend_yield 阶梯
            scoring = override_scoring
        else:
            scoring = self.middle_layer_config.get('value_factors', {}).get('scoring', {})
        
        scores = {}
        details = {}
        
        ind_fields_val = (self.ind_config.get('fields', {}).get('value', [])
                          if self.ind_enabled else [])

        # PE 评分（绝对分 + 行业百分位混合）
        pe = stock.get('pe') or stock.get('pe_ratio')
        if pe is not None:
            try:
                pe = float(pe)
                pe_scoring = scoring.get('pe', {})
                if pe <= 0:
                    scores['pe'] = 0.1
                    details['pe_warning'] = "PE <= 0"
                else:
                    if pe < pe_scoring.get('excellent', 10):
                        abs_score = 1.0
                    elif pe < pe_scoring.get('good', 15):
                        abs_score = 0.8
                    elif pe < pe_scoring.get('average', 20):
                        abs_score = 0.6
                    elif pe < pe_scoring.get('high', 25):
                        abs_score = 0.4
                    else:
                        abs_score = 0.2
                    scores['pe'] = self._blend_score(
                        stock, 'pe', pe, abs_score, 'pe_sorted',
                        higher_is_better=False, details=details
                    ) if 'pe' in ind_fields_val else abs_score
                    details['pe'] = pe
            except:
                pass

        # PB 评分
        pb = stock.get('pb') or stock.get('pb_ratio')
        if pb is not None:
            try:
                pb = float(pb)
                pb_scoring = scoring.get('pb', {})
                if pb <= 0:
                    scores['pb'] = 0.1
                    details['pb_warning'] = "PB <= 0"
                else:
                    if pb < pb_scoring.get('excellent', 1.0):
                        abs_score = 1.0
                    elif pb < pb_scoring.get('good', 1.5):
                        abs_score = 0.8
                    elif pb < pb_scoring.get('average', 2.0):
                        abs_score = 0.6
                    elif pb < pb_scoring.get('high', 3.0):
                        abs_score = 0.4
                    else:
                        abs_score = 0.2
                    scores['pb'] = self._blend_score(
                        stock, 'pb', pb, abs_score, 'pb_sorted',
                        higher_is_better=False, details=details
                    ) if 'pb' in ind_fields_val else abs_score
                    details['pb'] = pb
            except:
                pass
        
        # 股息率评分（绝对分 + 行业百分位混合）
        dividend = stock.get('dividend_yield')
        if dividend is not None:
            try:
                if isinstance(dividend, str):
                    dividend = float(dividend.replace('%', ''))
                else:
                    dividend = float(dividend)
                div_scoring = scoring.get('dividend_yield', {})
                abs_score = self._score_metric(dividend, div_scoring, higher_is_better=True)
                scores['dividend'] = self._blend_score(
                    stock, 'dividend_yield', dividend, abs_score, 'dy_sorted',
                    higher_is_better=True, details=details
                ) if 'dividend_yield' in ind_fields_val else abs_score
                details['dividend_yield'] = dividend
            except:
                pass
        
        # 计算综合得分
        if scores:
            avg_score = sum(scores.values()) / len(scores)
            details['available'] = True
        else:
            avg_score = 0
            details['available'] = False
            details['missing_reason'] = "缺少 PE、PB、股息率等价值指标"

        return round(avg_score, 3), details

    def get_factor_weights(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """根据组合角色/市场风格匹配 config 中的 style_weights。

        匹配优先级：风格关键词 > 行业关键词 > 市场 > default。
        权重定义在 config_complete.yaml 的 style_weights 节，无需改代码即可调参。
        """
        context = context or {}
        style = str(context.get('style', ''))
        industry = str(context.get('industry', ''))
        market = str(context.get('market', ''))

        # 从 config 匹配风格权重
        style_weights = self.middle_layer_config.get('style_weights', {})
        profile_key = None
        if '红利' in style or '防御' in style or any(kw in industry for kw in [
                '银行', '电力', '铁路', '公路', '港口', '水务', '供气', '通信运营', '电信']):
            profile_key = 'dividend_defensive'
        elif '科技' in style or '成长' in style or any(kw in industry for kw in [
                '半导体', '元器件', '通信设备', '软件', '互联网', 'IT设备',
                '消费电子', '电器仪表', '专用机械', '航空航天', '电气设备', '电脑']):
            profile_key = 'tech_growth'
        elif '资源' in style or '周期' in style or any(kw in industry for kw in [
                '有色', '能源', '煤炭', '石油', '黄金', '钢铁', '化工', '化纤',
                '造纸', '水泥', '玻璃', '矿物']):
            profile_key = 'resource_cyclical'
        elif '核心' in style or market == '港股':
            profile_key = 'core_bluechip'

        # 基础权重 = style_weights.default（IC 校准后的值），匹配到 profile 则覆盖
        default_w = style_weights.get('default', {
            'value': 0.05, 'growth': 0.30, 'quality': 0.15, 'momentum': 0.50})
        weights = dict(default_w)
        if profile_key and profile_key in style_weights:
            weights = {k: style_weights[profile_key].get(k, weights[k])
                       for k in weights}

        total = sum(weights.values()) or 1.0
        return {key: round(value / total, 4) for key, value in weights.items()}

    def calculate_composite_score(
        self,
        stock: Dict,
        quotes: List[Dict],
        financial: Optional[Dict],
        context: Optional[Dict[str, Any]] = None,
        override_weights: Optional[Dict[str, float]] = None,
        override_scoring: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """计算多因子综合得分。

        Args:
            override_weights: 若提供，跳过 config 直接使用这些权重
            override_scoring: 若提供，跳过 config 使用这些评分阶梯（组 config 的 scoring 段）
        """
        value_score, value_details = self.calculate_value_score(
            stock, quotes, override_scoring=override_scoring)
        growth_score, growth_details = self.calculate_growth_score(
            stock, financial, override_scoring=override_scoring)
        quality_score, quality_details = self.calculate_quality_score(
            stock, financial, override_scoring=override_scoring)
        momentum_score, momentum_details = self.calculate_momentum_score(
            quotes, override_scoring=override_scoring)
        weights = override_weights or self.get_factor_weights(context)

        factor_scores = {
            'value': value_score,
            'growth': growth_score,
            'quality': quality_score,
            'momentum': momentum_score,
        }

        detail_groups = {
            'value': value_details,
            'growth': growth_details,
            'quality': quality_details,
            'momentum': momentum_details,
        }

        # 仅用有数据的因子计算 composite，缺失因子的权重重新分配
        available_factors = [k for k in factor_scores
                             if detail_groups.get(k, {}).get('available')]
        if available_factors:
            avail_weight_sum = sum(weights[k] for k in available_factors)
            renorm_weights = {k: weights[k] / avail_weight_sum for k in available_factors}
            composite_score = sum(factor_scores[k] * renorm_weights[k] for k in available_factors)
        else:
            composite_score = 0

        data_quality_score = round(len(available_factors) / len(detail_groups), 3)

        missing_data = []
        if not financial:
            missing_data.append('financial')
        if len(quotes) < 20:
            missing_data.append('quotes_20d')
        if len(quotes) < 60:
            missing_data.append('quotes_60d')

        # 因子共线性诊断：value 高 + quality 低 → 潜在价值陷阱
        factor_conflicts = []
        val_ok = value_details.get('available') and value_score > 0.6
        qual_low = quality_details.get('available') and quality_score <= 0.4
        if val_ok and qual_low:
            factor_conflicts.append({
                "type": "value_quality_divergence",
                "severity": "warning",
                "message": "价值-质量分歧：估值便宜但质量偏低，潜在价值陷阱，需验证利润趋势是否恶化",
                "value_score": value_score,
                "quality_score": quality_score,
            })

        return {
            'composite_score': round(composite_score, 3),
            'factor_scores': factor_scores,
            'factor_weights': weights,
            'factor_details': detail_groups,
            'data_quality_score': data_quality_score,
            'missing_data': missing_data,
            'factor_conflicts': factor_conflicts,
        }

    def calculate_growth_score(self, stock: Dict, financial: Optional[Dict],
                               override_scoring: Optional[Dict[str, Any]] = None
                               ) -> Tuple[float, Dict]:
        """计算成长因子得分 营收增长、利润增长。"""
        if override_scoring:
            scoring = override_scoring
        else:
            scoring = self.middle_layer_config.get('growth_factors', {}).get('scoring', {})
        
        scores = {}
        details = {}
        
        if financial:
            # 从财务数据中获取增长率
            # 注意：实际数据可能需要计算同比增长
            revenue_growth = financial.get('revenue_growth')
            profit_growth = financial.get('profit_growth')
            if profit_growth is None:
                profit_growth = financial.get('netprofit_yoy')
            
            if revenue_growth is not None:
                try:
                    rg = float(revenue_growth)
                    rg_scoring = scoring.get('revenue_growth', {})
                    if rg >= rg_scoring.get('excellent', 20):
                        scores['revenue_growth'] = 1.0
                    elif rg >= rg_scoring.get('good', 15):
                        scores['revenue_growth'] = 0.8
                    elif rg >= rg_scoring.get('average', 10):
                        scores['revenue_growth'] = 0.6
                    elif rg >= rg_scoring.get('low', 0):
                        scores['revenue_growth'] = 0.4
                    else:
                        scores['revenue_growth'] = 0.2
                    details['revenue_growth'] = rg
                except:
                    pass
            
            if profit_growth is not None:
                try:
                    pg = float(profit_growth)
                    pg_scoring = scoring.get('profit_growth', {})
                    if pg >= pg_scoring.get('excellent', 25):
                        scores['profit_growth'] = 1.0
                    elif pg >= pg_scoring.get('good', 15):
                        scores['profit_growth'] = 0.8
                    elif pg >= pg_scoring.get('average', 10):
                        scores['profit_growth'] = 0.6
                    elif pg >= pg_scoring.get('low', 0):
                        scores['profit_growth'] = 0.4
                    else:
                        scores['profit_growth'] = 0.2
                    details['profit_growth'] = pg
                except:
                    pass

        # 增速趋势：加速还是减速（vs 上一季度）
        rev_prev = financial.get('revenue_growth_prev') if financial else None
        prof_prev = financial.get('profit_growth_prev') if financial else None
        rev_curr = financial.get('revenue_growth') if financial else None
        prof_curr = financial.get('profit_growth') if financial else None

        if rev_prev is not None and rev_curr is not None:
            accel = rev_curr - rev_prev
            details['revenue_accel'] = round(accel, 2)
            if accel >= 10:
                scores['revenue_accel'] = 1.0
            elif accel >= 5:
                scores['revenue_accel'] = 0.8
            elif accel >= 0:
                scores['revenue_accel'] = 0.6
            elif accel >= -5:
                scores['revenue_accel'] = 0.4
            else:
                scores['revenue_accel'] = 0.2

        if prof_prev is not None and prof_curr is not None:
            p_accel = prof_curr - prof_prev
            details['profit_accel'] = round(p_accel, 2)
            if p_accel >= 10:
                scores['profit_accel'] = 1.0
            elif p_accel >= 5:
                scores['profit_accel'] = 0.8
            elif p_accel >= 0:
                scores['profit_accel'] = 0.6
            elif p_accel >= -5:
                scores['profit_accel'] = 0.4
            else:
                scores['profit_accel'] = 0.2

        # 分析师一致预期增速（优先 financial，fallback stock）
        fc_min = (financial or {}).get('forecast_min')
        if fc_min is None:
            fc_min = stock.get('forecast_min')
        fc_max = (financial or {}).get('forecast_max')
        if fc_max is None:
            fc_max = stock.get('forecast_max')
        if fc_min is not None and fc_max is not None:
            try:
                fc_avg = (float(fc_min) + float(fc_max)) / 2
                fc_scoring = scoring.get('forecast_growth', {})
                if fc_avg >= fc_scoring.get('excellent', 30):
                    scores['forecast_growth'] = 1.0
                elif fc_avg >= fc_scoring.get('good', 20):
                    scores['forecast_growth'] = 0.8
                elif fc_avg >= fc_scoring.get('average', 10):
                    scores['forecast_growth'] = 0.6
                elif fc_avg >= fc_scoring.get('low', 0):
                    scores['forecast_growth'] = 0.4
                else:
                    scores['forecast_growth'] = 0.2
                details['forecast_growth'] = round(fc_avg, 2)
            except:
                pass

        # 分离前瞻性指标（不受财报新鲜度衰减影响）
        forecast_score = scores.pop('forecast_growth', None)
        if scores:
            avg_score = sum(scores.values()) / len(scores)
            details['available'] = True
        else:
            avg_score = 0
            details['available'] = forecast_score is not None

        # 数据新鲜度衰减（仅对财报指标，一致预期不受影响）
        decay = self._freshness_decay(financial)
        decayed_avg = round(avg_score * decay, 3)
        details['data_freshness_decay'] = decay
        details['raw_score'] = round(avg_score, 3)

        # 一致预期与衰减后的财报得分合并
        if forecast_score is not None:
            final_score = round((decayed_avg + forecast_score) / 2, 3)
            details['forecast_growth_standalone'] = forecast_score
        else:
            final_score = decayed_avg

        return final_score, details

    @staticmethod
    def _score_metric(value: float, thresholds: dict, higher_is_better: bool = True) -> float:
        """6级评分：excellent(1.0) → very_good(0.85) → good(0.7) → average(0.55) → below_avg(0.4) → poor(0.2)."""
        levels = [
            ('excellent', 1.0),
            ('very_good', 0.85),
            ('good', 0.7),
            ('average', 0.55),
            ('below_avg', 0.4),
        ]
        for key, score in levels:
            t = thresholds.get(key)
            if t is None:
                continue
            if higher_is_better and value >= t:
                return score
            if not higher_is_better and value <= t:
                return score
        return 0.2

    # ---- 行业横截面归一化 ----

    def _get_industry_stats(self) -> Dict[str, Dict]:
        """懒加载行业统计数据，同一次策略运行中只构建一次。"""
        if self._industry_stats is not None:
            return self._industry_stats
        self._industry_stats = self._build_industry_stats()
        return self._industry_stats

    def _build_industry_stats(self) -> Dict[str, Dict]:
        """从 MongoDB 构建全行业 PE/PB/ROE/毛利率 排序数组。

        Returns:
            {industry_code: {"pe_sorted": [...], "pb_sorted": [...],
             "roe_sorted": [...], "gm_sorted": [...], "count": int}, ...}
        """
        if not self.data_provider:
            return {}

        db = self.data_provider.db[self.data_provider.collections["basic_info"]]
        try:
            cursor = db.find(
                {"industry_code": {"$ne": None, "$ne": ""},
                 "pe": {"$gt": 0, "$lt": 500},
                 "pb": {"$gt": 0, "$lt": 100}},
                {"code": 1, "industry_code": 1, "pe": 1, "pb": 1, "roe": 1,
                 "gross_margin": 1, "dividend_yield": 1, "_id": 0},
            )
        except Exception:
            return {}

        groups: Dict[str, Dict[str, list]] = defaultdict(lambda: {
            "pe": [], "pb": [], "roe": [], "gross_margin": [], "dividend_yield": [],
        })
        for doc in cursor:
            ic = doc.get("industry_code", "").strip()
            if not ic:
                continue
            g = groups[ic]
            if doc.get("pe"):
                g["pe"].append(float(doc["pe"]))
            if doc.get("pb"):
                g["pb"].append(float(doc["pb"]))
            if doc.get("roe"):
                g["roe"].append(float(doc["roe"]))
            if doc.get("gross_margin"):
                g["gross_margin"].append(float(doc["gross_margin"]))
            if doc.get("dividend_yield") is not None and doc["dividend_yield"] >= 0:
                g["dividend_yield"].append(float(doc["dividend_yield"]))

        stats: Dict[str, Dict] = {}
        for ic, g in groups.items():
            count = len(g["pe"])  # PE 参与度最高，用它做 count 基准
            if count < self.ind_min_peers:
                continue
            stats[ic] = {
                "pe_sorted": sorted(g["pe"]),
                "pb_sorted": sorted(g["pb"]),
                "roe_sorted": sorted(g["roe"]),
                "gm_sorted": sorted(g["gross_margin"]),
                "dy_sorted": sorted(g["dividend_yield"]),
                "count": count,
            }

        logger.info(f"行业横截面统计: {len(stats)} 个行业")
        return stats

    @staticmethod
    def _get_industry_percentile(sorted_vals: List[float], value: float) -> float:
        """二分查找 value 在 sorted_vals 中的百分位 [0, 100]."""
        if not sorted_vals:
            return 50.0
        idx = bisect.bisect_left(sorted_vals, value)
        return round(idx / len(sorted_vals) * 100, 1)

    @staticmethod
    def _percentile_to_score(pct: float, higher_is_better: bool) -> float:
        """百分位 → [0.2, 1.0] 线性映射。"""
        if higher_is_better:
            return round(0.2 + 0.8 * (pct / 100.0), 4)
        else:
            return round(1.0 - 0.8 * (pct / 100.0), 4)

    def _blend_score(self, stock: Dict, metric: str, value: float,
                     abs_score: float, sorted_key: str,
                     higher_is_better: bool, details: Dict) -> float:
        """将绝对分与行业百分位分混合（若行业数据可用）。

        行业统计 _build_industry_stats 构建时过滤了 PE∉(0,500) 和 PB∉(0,100)，
        因此极端估值的股票可能不在行业分布样本中。
        """
        ic = (stock.get('industry_code') or '').strip()
        ind_stats = self._get_industry_stats()
        if not ic:
            details.setdefault('blend_notes', []).append(
                f"{metric}: 无行业代码，未做行业归一化")
            return abs_score
        if ic not in ind_stats:
            details.setdefault('blend_notes', []).append(
                f"{metric}: 行业内样本不足(<{self.ind_min_peers}只)，未做行业归一化")
            return abs_score
        sorted_vals = ind_stats[ic].get(sorted_key, [])
        if not sorted_vals:
            details.setdefault('blend_notes', []).append(
                f"{metric}: 行业缺少分布数据，未做行业归一化")
            return abs_score

        pct = self._get_industry_percentile(sorted_vals, value)
        rel = self._percentile_to_score(pct, higher_is_better)
        details[f'{metric}_percentile'] = pct
        details[f'{metric}_abs_score'] = abs_score

        # 标注：值在行业分布范围外（该股票未参与行业分布构建）
        lo, hi = sorted_vals[0], sorted_vals[-1]
        if value < lo or value > hi:
            details.setdefault('blend_notes', []).append(
                f"{metric}({value}): 值在行业分布范围[{lo:.1f},{hi:.1f}]之外，"
                f"该股票可能未参与行业百分位计算（估值异常/亏损/极端）")

        return round(self.ind_blend * rel + (1 - self.ind_blend) * abs_score, 4)

    def _freshness_decay(self, financial: Optional[Dict]) -> float:
        """财务数据新鲜度衰减系数。越旧的数据可信度越低。"""
        if not financial:
            return 1.0
        rp = financial.get('report_period', '')
        if not rp:
            return 1.0
        try:
            from datetime import datetime, timezone
            rp_str = str(rp)[:10].replace('-', '')
            if len(rp_str) == 8:
                rp_date = datetime(int(rp_str[:4]), int(rp_str[4:6]), int(rp_str[6:8]), tzinfo=timezone.utc)
            else:
                return 1.0
            months_old = (datetime.now(timezone.utc) - rp_date).days / 30.44
            if months_old < 4:
                return 1.0
            elif months_old < 6:
                return 0.9
            elif months_old < 9:
                return 0.75
            elif months_old < 12:
                return 0.6
            else:
                return 0.5
        except Exception:
            return 1.0

    def calculate_quality_score(self, stock: Dict, financial: Optional[Dict],
                                override_scoring: Optional[Dict[str, Any]] = None
                                ) -> Tuple[float, Dict]:
        """计算质量因子得分 ROE、毛利率、资产负债率。"""
        if override_scoring:
            scoring = override_scoring
        else:
            scoring = self.middle_layer_config.get('quality_factors', {}).get('scoring', {})
        ind_fields_qual = (self.ind_config.get('fields', {}).get('quality', [])
                           if self.ind_enabled else [])

        scores = {}
        details = {}

        # ROE（越高越好），避免 0 or x 短路
        roe = stock.get('roe')
        if roe is None and financial:
            roe = financial.get('roe')
        if roe is not None:
            try:
                roe = float(roe)
                roe_scoring = scoring.get('roe', {})
                abs_score = self._score_metric(roe, roe_scoring, higher_is_better=True)
                scores['roe'] = self._blend_score(
                    stock, 'roe', roe, abs_score, 'roe_sorted',
                    higher_is_better=True, details=details
                ) if 'roe' in ind_fields_qual else abs_score
                details['roe'] = roe
            except Exception:
                pass

        # 毛利率（越高越好）
        gross_margin = financial.get('gross_margin') if financial else None
        if gross_margin is not None:
            try:
                gm = float(gross_margin)
                gm_scoring = scoring.get('gross_margin', {})
                abs_score = self._score_metric(gm, gm_scoring, higher_is_better=True)
                scores['gross_margin'] = self._blend_score(
                    stock, 'gm', gm, abs_score, 'gm_sorted',
                    higher_is_better=True, details=details
                ) if 'gross_margin' in ind_fields_qual else abs_score
                details['gross_margin'] = gm
            except Exception:
                pass

        # 资产负债率（越低越好，不做行业归一化）
        debt_ratio = financial.get('debt_to_assets') if financial else None
        if debt_ratio is not None:
            try:
                dr = float(debt_ratio)
                dr_scoring = scoring.get('debt_ratio', {})
                scores['debt_ratio'] = self._score_metric(dr, dr_scoring, higher_is_better=False)
                details['debt_ratio'] = dr
            except Exception:
                pass

        # 经营现金流/净利润（应计质量，越高越好，不做行业归一化）
        ocf_ratio = financial.get('ocf_to_net_income') if financial else None
        if ocf_ratio is not None:
            try:
                ocf = float(ocf_ratio)
                ocf_scoring = scoring.get('ocf_to_net_income', {})
                scores['ocf_to_net_income'] = self._score_metric(ocf, ocf_scoring, higher_is_better=True)
                details['ocf_to_net_income'] = ocf
            except Exception:
                pass

        if scores:
            avg_score = sum(scores.values()) / len(scores)
            details['available'] = True
        else:
            avg_score = 0
            details['available'] = False
            details['missing_reason'] = "缺少 ROE、毛利率、资产负债率等质量指标"

        if ind_fields_qual:
            details['blend_ratio'] = self.ind_blend

        # 数据新鲜度衰减
        decay = self._freshness_decay(financial)
        final_score = round(avg_score * decay, 3)
        details['data_freshness_decay'] = decay
        details['raw_score'] = round(avg_score, 3)

        return final_score, details

    @staticmethod
    def _momentum_path_quality(quotes: List[Dict], n_days: int) -> Optional[Dict[str, Any]]:
        """计算趋势效率、最大回撤、日波动率。

        趋势效率 = |累计收益| / 每日绝对收益之和。
        - 1.0 = 每天都朝同一方向移动（完美趋势）
        - 0.3 = 大量来回震荡，最终净收益仅占总移动的30%
        """
        closes = []
        for i in range(min(n_days, len(quotes)) - 1, -1, -1):
            c = quotes[i].get('close')
            if c is not None:
                closes.append(float(c))
        if len(closes) < 5:
            return None

        daily_returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
        total_return = (closes[-1] - closes[0]) / closes[0]

        abs_sum = sum(abs(r) for r in daily_returns)
        efficiency = abs(total_return) / abs_sum if abs_sum > 0 else 0.0

        # 最大回撤
        peak = closes[0]
        max_dd = 0.0
        for c in closes:
            if c > peak:
                peak = c
            dd = (peak - c) / peak
            if dd > max_dd:
                max_dd = dd

        vol = (sum((r - sum(daily_returns) / len(daily_returns)) ** 2
                   for r in daily_returns) / (len(daily_returns) - 1)) ** 0.5 if len(daily_returns) > 1 else 0

        return {
            'efficiency': round(efficiency, 4),
            'max_drawdown': round(max_dd * 100, 2),
            'daily_vol': round(vol * 100, 4),
        }

    @staticmethod
    def _trend_quality_multiplier(efficiency: float, max_dd_pct: float, total_return_pct: float) -> float:
        """趋势质量惩罚系数。

        趋势效率低 → 路径震荡大 → 动量信号可靠性差 → 降分。
        最大回撤超过累计收益的1.5倍 → 途中风险远超收益 → 额外降分。
        """
        if efficiency >= 0.45:
            multiplier = 1.0
        elif efficiency >= 0.25:
            multiplier = 0.85
        elif efficiency >= 0.12:
            multiplier = 0.7
        else:
            multiplier = 0.55

        abs_ret = abs(total_return_pct)
        if abs_ret > 0.01 and max_dd_pct > abs_ret * 1.5:
            multiplier *= 0.85

        return round(multiplier, 4)

    def calculate_momentum_score(self, quotes: List[Dict],
                                 override_scoring: Optional[Dict[str, Any]] = None
                                 ) -> Tuple[float, Dict]:
        """计算动量因子得分 收益 + 路径质量。"""
        momentum_config = self.middle_layer_config.get('momentum_factors', {})
        if override_scoring:
            scoring = override_scoring
        else:
            scoring = momentum_config.get('scoring', {})
        
        scores = {}
        details = {}
        
        if len(quotes) < 5:
            return 0, {'available': False, 'missing_reason': '行情样本不足5条'}
        
        latest_close = quotes[0].get('close')
        if not latest_close:
            return 0, {'available': False, 'missing_reason': '最新收盘价缺失'}
        
        latest_close = float(latest_close)
        
        # 近1月收益率（约20个交易日）
        if len(quotes) >= 20:
            close_1m = quotes[min(19, len(quotes) - 1)].get('close')
            if close_1m:
                return_1m = (latest_close - float(close_1m)) / float(close_1m) * 100
                r1m_scoring = scoring.get('return_1m', {})
                if return_1m >= r1m_scoring.get('excellent', 10):
                    raw_1m = 1.0
                elif return_1m >= r1m_scoring.get('good', 5):
                    raw_1m = 0.8
                elif return_1m >= r1m_scoring.get('average', 0):
                    raw_1m = 0.6
                elif return_1m >= r1m_scoring.get('negative', -5):
                    raw_1m = 0.4
                else:
                    raw_1m = 0.2
                details['return_1m'] = round(return_1m, 2)
                
                pq_1m = self._momentum_path_quality(quotes, 20)
                if pq_1m:
                    q_mult = self._trend_quality_multiplier(
                        pq_1m['efficiency'], pq_1m['max_drawdown'], return_1m)
                    scores['return_1m'] = round(raw_1m * q_mult, 3)
                    details['path_1m'] = pq_1m
                    details['path_1m']['quality_multiplier'] = q_mult
                else:
                    scores['return_1m'] = raw_1m
        
        # 近3月收益率（约60个交易日）
        if len(quotes) >= 60:
            close_3m = quotes[min(59, len(quotes) - 1)].get('close')
            if close_3m:
                return_3m = (latest_close - float(close_3m)) / float(close_3m) * 100
                r3m_scoring = scoring.get('return_3m', {})
                if return_3m >= r3m_scoring.get('excellent', 20):
                    raw_3m = 1.0
                elif return_3m >= r3m_scoring.get('good', 10):
                    raw_3m = 0.8
                elif return_3m >= r3m_scoring.get('average', 0):
                    raw_3m = 0.6
                elif return_3m >= r3m_scoring.get('negative', -10):
                    raw_3m = 0.4
                else:
                    raw_3m = 0.2
                details['return_3m'] = round(return_3m, 2)
                
                pq_3m = self._momentum_path_quality(quotes, 60)
                if pq_3m:
                    q_mult = self._trend_quality_multiplier(
                        pq_3m['efficiency'], pq_3m['max_drawdown'], return_3m)
                    scores['return_3m'] = round(raw_3m * q_mult, 3)
                    details['path_3m'] = pq_3m
                    details['path_3m']['quality_multiplier'] = q_mult
                else:
                    scores['return_3m'] = raw_3m

        # 近6月收益率（约120交易日，扣除近1月，Fama-French风格）
        if len(quotes) >= 130:
            # 从第21天到第120天（跳过最近1月）
            close_6m_start = quotes[min(119, len(quotes) - 1)].get('close')
            close_6m_end = quotes[min(19, len(quotes) - 1)].get('close')
            if close_6m_start and close_6m_end:
                return_6m = (float(close_6m_end) - float(close_6m_start)) / float(close_6m_start) * 100
                r6m_scoring = scoring.get('return_6m', {})
                if return_6m >= r6m_scoring.get('excellent', 30):
                    raw_6m = 1.0
                elif return_6m >= r6m_scoring.get('good', 15):
                    raw_6m = 0.8
                elif return_6m >= r6m_scoring.get('average', 0):
                    raw_6m = 0.6
                elif return_6m >= r6m_scoring.get('negative', -15):
                    raw_6m = 0.4
                else:
                    raw_6m = 0.2
                details['return_6m'] = round(return_6m, 2)

                # 路径质量用同窗口切片（quotes[19:120]，不含最近1月）
                q_slice_6m = quotes[19:120]
                pq_6m = self._momentum_path_quality(q_slice_6m, len(q_slice_6m))
                if pq_6m:
                    q_mult = self._trend_quality_multiplier(
                        pq_6m['efficiency'], pq_6m['max_drawdown'], return_6m)
                    scores['return_6m'] = round(raw_6m * q_mult, 3)
                    details['path_6m'] = pq_6m
                    details['path_6m']['quality_multiplier'] = q_mult
                else:
                    scores['return_6m'] = raw_6m

        # 近12月收益率（约250交易日，扣除近1月）
        if len(quotes) >= 260:
            close_12m_start = quotes[min(249, len(quotes) - 1)].get('close')
            close_12m_end = quotes[min(19, len(quotes) - 1)].get('close')
            if close_12m_start and close_12m_end:
                return_12m = (float(close_12m_end) - float(close_12m_start)) / float(close_12m_start) * 100
                r12m_scoring = scoring.get('return_12m', {})
                if return_12m >= r12m_scoring.get('excellent', 40):
                    raw_12m = 1.0
                elif return_12m >= r12m_scoring.get('good', 20):
                    raw_12m = 0.8
                elif return_12m >= r12m_scoring.get('average', 0):
                    raw_12m = 0.6
                elif return_12m >= r12m_scoring.get('negative', -20):
                    raw_12m = 0.4
                else:
                    raw_12m = 0.2
                details['return_12m'] = round(return_12m, 2)

                # 路径质量用同窗口切片（quotes[19:250]，不含最近1月）
                q_slice_12m = quotes[19:250]
                pq_12m = self._momentum_path_quality(q_slice_12m, len(q_slice_12m))
                if pq_12m:
                    q_mult = self._trend_quality_multiplier(
                        pq_12m['efficiency'], pq_12m['max_drawdown'], return_12m)
                    scores['return_12m'] = round(raw_12m * q_mult, 3)
                    details['path_12m'] = pq_12m
                    details['path_12m']['quality_multiplier'] = q_mult
                else:
                    scores['return_12m'] = raw_12m

        # ── 短期反转因子（5日，A股均值回归特征）──
        reversal_weight = scoring.get('reversal_weight', 0.0)
        reversal_score = None
        if reversal_weight > 0 and len(quotes) >= 5:
            close_5d = quotes[min(4, len(quotes) - 1)].get('close')
            if close_5d:
                return_5d = (latest_close - float(close_5d)) / float(close_5d) * 100
                reversal_score = self._score_reversal(return_5d)
                details['return_5d'] = round(return_5d, 2)
                details['reversal_5d'] = reversal_score

        if scores:
            avg_score = sum(scores.values()) / len(scores)
            # 混合反转因子（若启用）
            if reversal_score is not None:
                avg_score = round(
                    (1 - reversal_weight) * avg_score + reversal_weight * reversal_score, 3)
            details['available'] = True
        else:
            avg_score = 0
            details['available'] = False
            details['missing_reason'] = "缺少足够的 1月/3月行情样本"

        return round(avg_score, 3), details

    @staticmethod
    def _score_reversal(ret_5d: float) -> float:
        """短期反转评分：跌得越深分越高，捕捉均值回归。

        评分逻辑：
          <= -8%  → 1.0（超跌，反弹潜力最大）
          <= -5%  → 0.8
          <= -2%  → 0.6
          <= 0%   → 0.5（持平）
          <= +3%  → 0.4
          <= +6%  → 0.2
           > +6%  → 0.1（短期涨幅过大，回调风险）

        Args:
            ret_5d: 5日累计收益率（%）
        Returns:
            反转得分 [0.1, 1.0]
        """
        if ret_5d < -8:
            return 1.0
        elif ret_5d < -5:
            return 0.8
        elif ret_5d < -2:
            return 0.6
        elif ret_5d <= 0:
            return 0.5
        elif ret_5d < 3:
            return 0.4
        elif ret_5d < 6:
            return 0.2
        else:
            return 0.1

    def run_multifactor_selection(self, strong_sectors: List[str] = None) -> List[Dict]:
        """
        运行多因子选股
        """
        if not self.middle_layer_config.get('enabled', True):
            return []

        top_n = self.middle_layer_config.get('top_n', 30)
        
        # 获取价值因子过滤条件
        value_filters = self.middle_layer_config.get('value_factors', {}).get('filters', {})
        min_pe = value_filters.get('min_pe', 0)
        max_pe = value_filters.get('max_pe', 100)
        max_pb = value_filters.get('max_pb', 10)

        # 获取股票池（传入配置中的过滤条件）
        mongodb_cfg = (self.config.get('data_sources') or {}).get('mongodb', {})
        pool_cfg = mongodb_cfg.get('stock_pool', {})
        stocks = self.data_provider.get_stock_list(
            limit=500,
            market_allowlist=pool_cfg.get('market_allowlist'),
            min_amount=pool_cfg.get('min_amount'),
        )
        if not stocks:
            logger.warning("股票池为空")
            return []

        logger.info(f"多因子选股：基础池 {len(stocks)} 只股票")

        results = []
        filtered_count = 0
        
        for stock in stocks:
            code = stock.get('code')
            if not code:
                continue
            
            # 如果指定了强势行业，只选择这些行业的股票
            if strong_sectors:
                stock_sector = stock.get('industry')
                if stock_sector not in strong_sectors:
                    continue
            
            # PE 过滤：排除亏损股（PE <= 0）和高估值股
            pe = stock.get('pe') or stock.get('pe_ratio')
            if pe is not None:
                try:
                    pe_val = float(pe)
                    if pe_val <= min_pe or pe_val > max_pe:
                        filtered_count += 1
                        continue
                except:
                    pass
            
            # PB 过滤：排除负净资产和高 PB 股
            pb = stock.get('pb') or stock.get('pb_ratio')
            if pb is not None:
                try:
                    pb_val = float(pb)
                    if pb_val <= 0 or pb_val > max_pb:
                        filtered_count += 1
                        continue
                except:
                    pass
            
            # 获取行情数据
            quotes = self.data_provider.get_recent_quotes(code, 300)
            if len(quotes) < 5:
                continue
            
            # 获取财务数据
            financial = self.data_provider.get_financial_data(code)
            
            score_result = self.calculate_composite_score(
                stock,
                quotes,
                financial,
                context=stock,
            )
            
            # 获取最新价格
            latest_quote = quotes[0] if quotes else {}
            price = latest_quote.get('close') or stock.get('latest_close') or 0
            
            results.append({
                'code': code,
                'name': stock.get('name') or stock.get('stock_name') or '未知',
                'industry': stock.get('industry') or '未分类',
                'price': round(float(price), 2) if price else 0,
                'composite_score': score_result['composite_score'],
                'factor_scores': score_result['factor_scores'],
                'factor_weights': score_result['factor_weights'],
                'factor_details': score_result['factor_details'],
                'data_quality_score': score_result['data_quality_score'],
                'missing_data': score_result['missing_data'],
            })

        # 按综合得分排序
        results.sort(key=lambda x: x['composite_score'], reverse=True)
        
        logger.info(f"多因子选股完成：过滤 {filtered_count} 只（PE/PB不合格），筛选出 {len(results)} 只，返回前 {top_n} 只")
        
        return results[:top_n]

    # ============================================================
    # 底层：交易执行优化
    # ============================================================

    def apply_execution_optimization(self, stocks: List[Dict]) -> List[Dict]:
        """
        应用交易执行优化
        添加流动性过滤、买入区间、止盈止损建议
        """
        if not self.bottom_layer_config.get('enabled', True):
            return stocks

        liquidity_config = self.bottom_layer_config.get('liquidity', {})
        min_amount = liquidity_config.get('min_amount', 100000000)
        
        buy_zone_config = self.bottom_layer_config.get('buy_zone', {})
        stop_loss = self.bottom_layer_config.get('stop_loss', 8)
        take_profit = self.bottom_layer_config.get('take_profit', 20)
        
        position_sizing = self.bottom_layer_config.get('position_sizing', {})

        optimized = []
        
        for stock in stocks:
            code = stock.get('code')
            price = stock.get('price', 0)
            
            if not price:
                continue
            
            # 流动性过滤（如果有成交额数据）
            # 这里假设已经在基础池过滤过了
            
            # 计算买入区间
            buy_min = price * (1 + buy_zone_config.get('min_pct', -3) / 100)
            buy_max = price * (1 + buy_zone_config.get('max_pct', 5) / 100)
            
            # 计算止盈止损价
            stop_loss_price = price * (1 - stop_loss / 100)
            take_profit_price = price * (1 + take_profit / 100)
            
            stock['trade_plan'] = {
                'buy_zone': f"{buy_min:.2f} - {buy_max:.2f}",
                'stop_loss': f"{stop_loss_price:.2f} ({stop_loss}%)",
                'take_profit': f"{take_profit_price:.2f} ({take_profit}%)",
                'position_sizing': {
                    'initial': f"{position_sizing.get('initial_position', 0.3) * 100:.0f}%",
                    'add_1': f"{position_sizing.get('add_position_1', 0.3) * 100:.0f}%",
                    'add_2': f"{position_sizing.get('add_position_2', 0.4) * 100:.0f}%"
                }
            }
            
            optimized.append(stock)
        
        return optimized

    # ============================================================
    # 主流程
    # ============================================================

    def run(self) -> Dict:
        """运行三层金字塔策略"""
        logger.info("=" * 60)
        logger.info("开始运行三层金字塔多因子选股策略")
        logger.info("=" * 60)

        report_date = datetime.now().strftime('%Y-%m-%d')
        
        # 顶层：行业轮动分析
        logger.info("\n📊 顶层：大类资产配置")
        sector_rotation = self.analyze_sector_rotation()
        position_adjustment = self.calculate_position_adjustment('neutral')
        
        # 获取强势行业列表
        strong_sectors = []
        if sector_rotation.get('strong_sectors'):
            strong_sectors = [s['sector'] for s in sector_rotation['strong_sectors']]
            logger.info(f"强势行业: {strong_sectors}")
        
        # 中层：多因子选股
        logger.info("\n📈 中层：多因子选股")
        # 可以选择是否只从强势行业中选股
        # selected_stocks = self.run_multifactor_selection(strong_sectors)
        selected_stocks = self.run_multifactor_selection()  # 从全市场选股
        
        # 底层：交易执行优化
        logger.info("\n🔧 底层：交易执行优化")
        final_stocks = self.apply_execution_optimization(selected_stocks)
        
        # 统计行业分布
        sector_distribution = defaultdict(int)
        for stock in final_stocks:
            sector_distribution[stock.get('industry', '未分类')] += 1
        
        # 生成报告
        report = {
            'metadata': {
                'system': "三层金字塔多因子选股系统",
                'version': "3.0.0",
                'report_date': report_date,
                'generated_at': datetime.now().isoformat()
            },
            'top_layer': {
                'description': "大类资产配置（行业轮动 + 仓位调整）",
                'sector_rotation': sector_rotation,
                'position_adjustment': position_adjustment
            },
            'middle_layer': {
                'description': "多因子选股（价值 + 成长 + 质量 + 动量）",
                'factor_weights': {
                    'value': self.middle_layer_config.get('value_factors', {}).get('weight', 0.4),
                    'growth': self.middle_layer_config.get('growth_factors', {}).get('weight', 0.2),
                    'quality': self.middle_layer_config.get('quality_factors', {}).get('weight', 0.2),
                    'momentum': self.middle_layer_config.get('momentum_factors', {}).get('weight', 0.2)
                },
                'selected_stocks': final_stocks
            },
            'bottom_layer': {
                'description': "交易执行优化（流动性 + 止盈止损）",
                'stop_loss': self.bottom_layer_config.get('stop_loss', 8),
                'take_profit': self.bottom_layer_config.get('take_profit', 20)
            },
            'summary': {
                'total_selected': len(final_stocks),
                'sector_distribution': dict(sector_distribution)
            }
        }
        
        return report

    def save_report(self, report: Dict, format: str = 'json') -> str:
        """保存报告"""
        reports_dir = self.config.get('output', {}).get('reports_dir', './reports')
        os.makedirs(reports_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        if format == 'json':
            filename = f"pyramid_multifactor_{timestamp}.json"
            filepath = os.path.join(reports_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2, cls=MongoJSONEncoder)
            logger.info(f"JSON报告已保存: {filepath}")
            return filepath

        if format == 'text':
            filename = f"pyramid_multifactor_{timestamp}.txt"
            filepath = os.path.join(reports_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(self._format_text_report(report))
            logger.info(f"文本报告已保存: {filepath}")
            return filepath

        return ""

    def _format_text_report(self, report: Dict) -> str:
        """格式化文本报告"""
        lines = []
        metadata = report['metadata']
        
        lines.append("=" * 80)
        lines.append(f"🏛️ {metadata['system']} - {metadata['report_date']}")
        lines.append("=" * 80)
        lines.append(f"⏰ 生成时间: {datetime.now().strftime('%H:%M:%S')}")
        lines.append("")
        
        # 顶层：行业轮动
        lines.append("📊 顶层：大类资产配置")
        lines.append("-" * 40)
        
        top_layer = report.get('top_layer', {})
        sector_rotation = top_layer.get('sector_rotation', {})
        
        if sector_rotation.get('strong_sectors'):
            lines.append("强势行业 Top 5:")
            for i, sector in enumerate(sector_rotation['strong_sectors'], 1):
                lines.append(f"  {i}. {sector['sector']}: {sector['avg_return']:.2f}% ({sector['stock_count']}只)")
        
        position = top_layer.get('position_adjustment', {})
        lines.append(f"\n仓位建议: {position.get('suggested_position', 0.7) * 100:.0f}%")
        lines.append(f"建议: {position.get('advice', '')}")
        lines.append("")
        
        # 中层：多因子选股
        lines.append("📈 中层：多因子选股")
        lines.append("-" * 40)
        
        middle_layer = report.get('middle_layer', {})
        weights = middle_layer.get('factor_weights', {})
        lines.append(f"因子权重: 价值{weights.get('value', 0.4)*100:.0f}% + 成长{weights.get('growth', 0.2)*100:.0f}% + 质量{weights.get('quality', 0.2)*100:.0f}% + 动量{weights.get('momentum', 0.2)*100:.0f}%")
        lines.append("")
        
        stocks = middle_layer.get('selected_stocks', [])
        if stocks:
            lines.append(f"精选股票 Top {len(stocks)}:")
            lines.append("")
            for i, stock in enumerate(stocks, 1):
                lines.append(f"  {i}. {stock['code']} {stock['name']} - {stock['price']}元")
                dq = stock.get('data_quality_score', 0)
                dq_flag = " ⚠数据不全" if dq < 0.5 else (" ·" if dq < 1.0 else "")
                lines.append(f"     行业: {stock['industry']} | 综合: {stock['composite_score']:.3f} | 数据质量: {dq:.0%}{dq_flag}")

                factor_scores = stock.get('factor_scores', {})
                lines.append(f"     因子: 价值{factor_scores.get('value', 0):.2f} 成长{factor_scores.get('growth', 0):.2f} 质量{factor_scores.get('quality', 0):.2f} 动量{factor_scores.get('momentum', 0):.2f}")

                factor_details = stock.get('factor_details', {})

                # ── 估值（含行业百分位）──
                value_details = factor_details.get('value', {})
                if value_details:
                    parts = []
                    if 'pe' in value_details:
                        pct = value_details.get('pe_percentile')
                        pct_str = f" 行业前{pct}%" if pct is not None else ""
                        parts.append(f"PE:{value_details['pe']}{pct_str}")
                    if 'pb' in value_details:
                        pct = value_details.get('pb_percentile')
                        pct_str = f" 行业前{pct}%" if pct is not None else ""
                        parts.append(f"PB:{value_details['pb']}{pct_str}")
                    if 'dividend_yield' in value_details:
                        parts.append(f"股息:{value_details['dividend_yield']}%")
                    if parts:
                        lines.append(f"     估值: {' | '.join(parts)}")

                # ── 质量（含行业百分位 + 现金流 + 新鲜度）──
                quality_details = factor_details.get('quality', {})
                if quality_details and quality_details.get('available'):
                    parts = []
                    if 'roe' in quality_details:
                        pct = quality_details.get('roe_percentile')
                        pct_str = f" 行业前{pct}%" if pct is not None else ""
                        parts.append(f"ROE:{quality_details['roe']}{pct_str}")
                    if 'gross_margin' in quality_details:
                        pct = quality_details.get('gm_percentile')
                        pct_str = f" 行业前{pct}%" if pct is not None else ""
                        parts.append(f"毛利率:{quality_details['gross_margin']}{pct_str}")
                    if 'debt_ratio' in quality_details:
                        parts.append(f"负债率:{quality_details['debt_ratio']}")
                    if 'ocf_to_net_income' in quality_details:
                        ocf = quality_details['ocf_to_net_income']
                        ocf_flag = " ✓" if ocf >= 0.8 else (" ⚠" if ocf < 0.5 else "")
                        parts.append(f"OCF/NI:{ocf}{ocf_flag}")
                    decay = quality_details.get('data_freshness_decay', 1.0)
                    if decay < 1.0:
                        parts.append(f"新鲜度:{decay:.0%}")
                    if parts:
                        lines.append(f"     质量: {' | '.join(parts)}")

                # ── 成长（含新鲜度）──
                growth_details = factor_details.get('growth', {})
                if growth_details and growth_details.get('available'):
                    parts = []
                    if 'revenue_growth' in growth_details:
                        parts.append(f"营收增长:{growth_details['revenue_growth']}%")
                    if 'profit_growth' in growth_details:
                        parts.append(f"利润增长:{growth_details['profit_growth']}%")
                    if 'forecast_growth' in growth_details:
                        parts.append(f"一致预期:{growth_details['forecast_growth']}%")
                    if 'forecast_growth_standalone' in growth_details:
                        parts.append("(不受新鲜度衰减)")
                    decay = growth_details.get('data_freshness_decay', 1.0)
                    if decay < 1.0:
                        parts.append(f"财报新鲜度:{decay:.0%}")
                    if parts:
                        lines.append(f"     成长: {' | '.join(parts)}")

                # ── 动量（含路径质量）──
                momentum_details = factor_details.get('momentum', {})
                if momentum_details and momentum_details.get('available'):
                    parts = []
                    if 'return_1m' in momentum_details:
                        parts.append(f"1月:{momentum_details['return_1m']}%")
                    if 'return_3m' in momentum_details:
                        parts.append(f"3月:{momentum_details['return_3m']}%")
                    if 'return_6m' in momentum_details:
                        parts.append(f"6月:{momentum_details['return_6m']}%")
                    if 'return_12m' in momentum_details:
                        parts.append(f"12月:{momentum_details['return_12m']}%")
                    # 路径质量取最长可用窗口
                    pq = (momentum_details.get('path_12m') or
                          momentum_details.get('path_6m') or
                          momentum_details.get('path_3m') or
                          momentum_details.get('path_1m'))
                    if pq:
                        eff = pq.get('efficiency', '?')
                        dd = pq.get('max_drawdown', '?')
                        qm = pq.get('quality_multiplier', '?')
                        parts.append(f"效率:{eff} 最大回撤:{dd}% 乘数:{qm}")
                    lines.append(f"     动量: {' | '.join(parts)}")

                # ── 因子冲突诊断 ──
                conflicts = stock.get('factor_conflicts', [])
                for c in conflicts:
                    sev = "🚨" if c.get('severity') == 'error' else "⚠️"
                    lines.append(f"     {sev} {c.get('message', '')}")

                # ── 行业归一化标注 ──
                blend_notes: List[str] = []
                for dim in ('value', 'quality'):
                    blend_notes.extend(
                        factor_details.get(dim, {}).get('blend_notes', []))
                for note in blend_notes:
                    lines.append(f"     ℹ️ {note}")

                trade_plan = stock.get('trade_plan', {})
                if trade_plan:
                    lines.append(f"     交易: 买入区间 {trade_plan.get('buy_zone', 'N/A')} | "
                                 f"止损 {trade_plan.get('stop_loss', 'N/A')} | "
                                 f"止盈 {trade_plan.get('take_profit', 'N/A')}")

                lines.append("")
        
        # 底层：交易执行
        lines.append("🔧 底层：交易执行优化")
        lines.append("-" * 40)
        bottom_layer = report.get('bottom_layer', {})
        lines.append(f"止损纪律: {bottom_layer.get('stop_loss', 8)}%")
        lines.append(f"止盈目标: {bottom_layer.get('take_profit', 20)}%")
        lines.append("")
        
        # 统计摘要
        lines.append("📊 统计摘要")
        lines.append("-" * 40)
        summary = report.get('summary', {})
        lines.append(f"精选股票数: {summary.get('total_selected', 0)}")
        
        sector_dist = summary.get('sector_distribution', {})
        if sector_dist:
            lines.append("行业分布:")
            for sector, count in sorted(sector_dist.items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"  - {sector}: {count}只")
        
        lines.append("")
        lines.append("=" * 80)
        
        return "\n".join(lines)


def main():
    """主函数"""
    print("=" * 80)
    print("🏛️ 三层金字塔多因子选股系统")
    print("=" * 80)

    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '../config/config_complete.yaml'
    )

    strategy = PyramidMultifactorStrategy(config_path)
    report = strategy.run()

    # 保存报告
    json_file = strategy.save_report(report, 'json')
    text_file = strategy.save_report(report, 'text')

    # 输出到控制台
    print(strategy._format_text_report(report))

    print(f"\n✅ 策略运行完成!")
    print(f"📁 JSON报告: {json_file}")
    print(f"📁 文本报告: {text_file}")

    summary = report.get('summary', {})
    print(f"📊 精选股票: {summary.get('total_selected', 0)}只")


if __name__ == "__main__":
    main()
