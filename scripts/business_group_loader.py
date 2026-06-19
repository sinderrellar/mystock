#!/usr/bin/env python3
"""
生意模式分组加载器 — 所有模块共享的单例。

读取 industry_groups.yaml（行业→组映射）和 4 个组配置文件，
提供分组查询 + 阈值查询 + 权重查询。

用法:
  from business_group_loader import BusinessGroupLoader
  loader = BusinessGroupLoader()
  group = loader.get_group("通信设备")      # → "轻资产成长"
  pool   = loader.get_pool(group)           # → {pe_max:200, min_amount:..., min_market_cap:...}
  weights = loader.get_weights(group)       # → {value:0.10, growth:0.45, quality:0.25, momentum:0.20}
  scoring = loader.get_scoring(group)       # → {pe:{excellent:20, good:35, ...}, pb:{...}, ...}
  all_groups = loader.all_group_names()    # → ["资源周期", "轻资产成长", "稳定现金流", "消费品牌"]
"""

import os
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BusinessGroupLoader:
    """单例加载器，首次初始化后缓存全部配置。"""

    _instance: Optional["BusinessGroupLoader"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._loaded = True

        config_dir = os.path.join(PROJECT_ROOT, "config")

        # ── 1. 加载行业→组映射 ──
        with open(os.path.join(config_dir, "industry_groups.yaml"), encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        self._industry_to_group: Dict[str, str] = {}
        self._group_names: List[str] = []

        for group_name, group_data in raw.items():
            if group_name == "industry_aliases":
                continue
            self._group_names.append(group_name)
            industries = group_data.get("industries", {})
            for _subcat, inds in industries.items():
                for ind in inds:
                    self._industry_to_group[ind] = group_name

        # 别名映射（数据库子分类 → 组名）
        aliases = raw.get("industry_aliases", {})
        for alias_group, entries in aliases.items():
            for entry in entries:
                ind_name = entry.split("→")[0].strip()
                self._industry_to_group[ind_name] = alias_group

        # ── 2. 加载 4 个组配置 ──
        self._group_configs: Dict[str, Dict] = {}
        for name in self._group_names:
            path = os.path.join(config_dir, f"{name}.yaml")
            with open(path, encoding="utf-8") as f:
                self._group_configs[name] = yaml.safe_load(f)

    # ── 公开 API ──

    def get_group(self, industry: str) -> str:
        """行业名 → 组名。未匹配返回空字符串。"""
        return self._industry_to_group.get(industry, "")

    def all_group_names(self) -> List[str]:
        return list(self._group_names)

    def get_pool(self, group: str) -> Dict[str, Any]:
        """获取该组的 pool 过滤阈值。"""
        cfg = self._group_configs.get(group, {})
        return cfg.get("pool", {})

    def get_weights(self, group: str) -> Dict[str, float]:
        """获取该组的因子权重。"""
        cfg = self._group_configs.get(group, {})
        return cfg.get("factor_weights", {})

    def get_scoring(self, group: str) -> Dict[str, Any]:
        """获取该组的因子评分阶梯。"""
        cfg = self._group_configs.get(group, {})
        return cfg.get("scoring", {})

    def get_signals(self, group: str) -> Dict[str, Any]:
        """获取该组的左侧信号灯阈值。"""
        cfg = self._group_configs.get(group, {})
        return cfg.get("signals", {})

    def get_funnel(self, group: str) -> Dict[str, Any]:
        """获取该组的 buy_plan 漏斗阈值。"""
        cfg = self._group_configs.get(group, {})
        return cfg.get("funnel", {})
