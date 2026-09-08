#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research_tasks.py —— 「深度研究」任务编排（Mongo 持久化 + 单 worker 队列）

职责：
- 任务表 `sim_research_tasks`：per-user，status = queued|running|done|failed|interrupted。
- 有界线程池（ThreadPoolExecutor，并发度 RESEARCH_WORKERS，默认 3），POST 只入队立即返回。
- 每个任务在一个线程里调 wecode（subprocess 独立进程），多个研究可并发执行。
- 重启恢复：queued/running 置 interrupted（update 非 delete，遵守 CLAUDE.md 禁删）。

设计要点：
- 任务 per-user（复用 sim_trade 的 user_id 隔离），报告全局共享（reports/ 目录）。
- 惰性启动：首次 enqueue 时 _ensure_worker() 拉起线程池，先 recover_interrupted_tasks()。
- session 追溯并发安全：debate_engine.run_agent_with_session 用 --session-id 精确绑定，
  各任务 session 文件互不干扰（不再依赖目录 diff 猜新文件）。
- uvicorn --workers N 下进程内线程池不跨进程（v1 不处理）。
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_engine

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")

COL_TASKS = "sim_research_tasks"


def _get_mongo():
    """复用 config_complete.yaml 的 MongoDB 配置，返回 tradingagents 库。"""
    from pymongo import MongoClient

    from factor_data_import_service import _load_mongodb_config

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    client = MongoClient(cfg["uri"], serverSelectionTimeoutMS=5000)
    return client[cfg["database"]]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 进程启动时间（模块加载时定格），用于区分「本次入队」与「上次进程遗留」
_PROCESS_START = _now()


def _ensure_indexes(db) -> None:
    db[COL_TASKS].create_index([("task_id", 1)], unique=True, name="sim_rt_task_id")
    db[COL_TASKS].create_index([("user_id", 1), ("created_at", -1)], name="sim_rt_user_time")
    db[COL_TASKS].create_index([("status", 1)], name="sim_rt_status")


# ═══════════════════════ 任务 CRUD ═══════════════════════

def enqueue(
    user_id: str,
    rtype: str,
    target: str = "",
    code: str = "",
    market: str = "A股",
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """新建任务并入队，返回 task_id。"""
    db = _get_mongo()
    _ensure_indexes(db)
    task_id = uuid.uuid4().hex
    now = _now()
    db[COL_TASKS].insert_one(
        {
            "task_id": task_id,
            "user_id": user_id,
            "type": rtype,
            "target": target,
            "code": code,
            "market": market,
            "payload": payload or {},
            "status": "queued",
            "report": None,
            "result": None,
            "error": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        }
    )
    _ensure_worker()
    _executor.submit(_run, task_id)
    return task_id


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    db = _get_mongo()
    doc = db[COL_TASKS].find_one({"task_id": task_id})
    if doc:
        doc.pop("_id", None)
    return doc


def list_tasks(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    db = _get_mongo()
    docs = list(
        db[COL_TASKS].find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    )
    for d in docs:
        d.pop("_id", None)
    return docs


def recover_interrupted_tasks() -> int:
    """服务重启时，把上次进程遗留的 queued/running 置 interrupted（update 非 delete）。

    只恢复 created_at 早于本次进程启动的任务，避免 worker 惰性启动时误伤刚入队的任务。
    """
    db = _get_mongo()
    res = db[COL_TASKS].update_many(
        {"status": {"$in": ["queued", "running"]}, "created_at": {"$lt": _PROCESS_START}},
        {
            "$set": {
                "status": "interrupted",
                "error": "服务重启，任务中断",
                "finished_at": _now(),
            }
        },
    )
    return res.modified_count


# ═══════════════════════ worker（有界线程池）══════════════════════════

# 并发度：同时最多跑 N 个 wecode。走 copilot.weibo.com 的 LLM 代理，过高易被限流，保守默认 3。
MAX_CONCURRENT = int(os.environ.get("RESEARCH_WORKERS", "3"))

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _execute_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """按 type 分发执行。返回 {report, ...} 结果。"""
    rtype = task.get("type")
    task_id = task.get("task_id", "")

    # 会话文件一出现就落库 session_id，让前端在 running 期间就能拉到「研究过程」
    def _on_session(sid: str) -> None:
        if not sid or not task_id:
            return
        try:
            _get_mongo()[COL_TASKS].update_one(
                {"task_id": task_id}, {"$set": {"session_id": sid}}
            )
        except Exception:
            pass

    if rtype == "verdict":
        from research_engine import run_verdict

        return run_verdict(task.get("payload") or {}, on_session=_on_session)
    if rtype == "debate_turn":
        # Phase 4 支持：由 debate_session 提供执行函数
        from debate_session import run_debate_turn

        return run_debate_turn(task)
    return research_engine.run_research(
        rtype,
        target=task.get("target", ""),
        code=task.get("code", ""),
        market=task.get("market", "A股"),
        on_session=_on_session,
    )


def _run(task_id: str) -> None:
    db = _get_mongo()
    task = get_task(task_id)
    if not task or task.get("status") != "queued":
        return
    db[COL_TASKS].update_one(
        {"task_id": task_id},
        {"$set": {"status": "running", "started_at": _now()}},
    )
    try:
        result = _execute_task(task)
        db[COL_TASKS].update_one(
            {"task_id": task_id},
            {
                "$set": {
                    "status": "done",
                    "report": result.get("report"),
                    "session_id": result.get("session_id") or task.get("session_id"),
                    "result": result,
                    "finished_at": _now(),
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 —— 任何失败都要落到任务状态，不静默
        db[COL_TASKS].update_one(
            {"task_id": task_id},
            {"$set": {"status": "failed", "error": str(exc), "finished_at": _now()}},
        )


def _ensure_worker() -> None:
    global _executor
    with _executor_lock:
        if _executor is not None:
            return
        # 惰性启动时先恢复上次进程遗留的 queued/running 任务（只执行一次）
        try:
            recover_interrupted_tasks()
        except Exception:
            pass
        _executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT, thread_name_prefix="research-worker"
        )
