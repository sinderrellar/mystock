#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
buyplan_presets.py —— 明日选股「策略预设」按用户持久化（MongoDB）

把用户在前端调好的 5 层筛选参数组合存为命名预设，随 user_id 隔离。
约束（遵守 CLAUDE.md）：禁止 delete/remove 物理删除，删除用软删 active=False。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, Dict, List

# 保证本文件可被独立执行时也能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")

COL_PRESETS = "buyplan_presets"


def _get_mongo():
    """复用 config_complete.yaml 的 MongoDB 配置，返回 tradingagents 库。"""
    from pymongo import MongoClient

    from factor_data_import_service import _load_mongodb_config

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    client = MongoClient(cfg["uri"], serverSelectionTimeoutMS=5000)
    return client[cfg["database"]]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_indexes(db) -> None:
    """幂等建索引（user_id + name 联合，便于按用户列出）。"""
    db[COL_PRESETS].create_index(
        [("user_id", 1), ("name", 1)], name="buyplan_preset_user_name"
    )


def list_presets(user_id: str) -> List[Dict[str, Any]]:
    """返回当前用户的预设（active 项，按名称排序）。"""
    db = _get_mongo()
    _ensure_indexes(db)
    rows = db[COL_PRESETS].find(
        {"user_id": user_id, "active": {"$ne": False}}
    ).sort("name", 1)
    return [
        {
            "name": r["name"],
            "filters": r.get("filters", {}),
            "saved_at": r.get("updated_at", ""),
        }
        for r in rows
    ]


def save_preset(user_id: str, name: str, filters: Dict[str, Any]) -> Dict[str, Any]:
    """保存/覆盖一个命名预设（upsert，同名覆盖）。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("预设名称不能为空")
    if len(name) > 50:
        raise ValueError("预设名称过长（≤50 字）")
    db = _get_mongo()
    _ensure_indexes(db)
    now = _now()
    db[COL_PRESETS].update_one(
        {"user_id": user_id, "name": name},
        {
            "$set": {"filters": filters, "active": True, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {"name": name, "saved": True}


def remove_preset(user_id: str, name: str) -> Dict[str, Any]:
    """软删预设（active=False，遵守禁 delete 守则）。"""
    db = _get_mongo()
    _ensure_indexes(db)
    db[COL_PRESETS].update_many(
        {"user_id": user_id, "name": name, "active": {"$ne": False}},
        {"$set": {"active": False, "updated_at": _now()}},
    )
    return {"name": name, "removed": True}
