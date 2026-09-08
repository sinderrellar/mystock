#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debate_session.py —— 「投资助手」多轮对话会话（Mongo 持久化 + 单轮执行）

职责：
1. 会话 CRUD：`sim_debate_sessions`（per-user，内嵌 messages[]，软删 active=false）。
2. 证据底座：创建会话（绑定标的）时检索 reports/ 相关新鲜报告
   （research_engine.find_reports_for_target）+ get_data_base() 定量数据，
   缓存进 session，后续轮复用，避免每轮重复检索。
3. run_debate_turn(task)：由 research_tasks 单 worker 调用，执行一轮对话 ——
   拼 prompt（话题元信息 + 证据底座 + 最近 N 轮历史 + 本轮问题）→ wecode →
   追加 assistant markdown 回复到 session.messages。

与 debate_engine 的关系：debate_engine.debate() 是旧一次性三流派辩论（保留）；
本模块是「投资助手」多轮对话，复用 debate_engine.get_data_base / run_agent / clean_output。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.join(PROJECT_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import research_engine

DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
COL_SESSIONS = "sim_debate_sessions"

# 单轮对话喂给 wecode 的历史轮数上限（控制 prompt 长度）
HISTORY_TURNS = 6

# name→code 映射进程级缓存（stock_basic_info 低频更新，避免每轮全表扫描）
_NAME_CODE_CACHE: Optional[List[tuple]] = None
_NAME_CODE_CACHE_TS: float = 0.0
_NAME_CODE_CACHE_TTL: float = 3600.0  # 秒


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
    db[COL_SESSIONS].create_index([("session_id", 1)], unique=True, name="sim_ds_id")
    db[COL_SESSIONS].create_index([("user_id", 1), ("updated_at", -1)], name="sim_ds_user_time")


# ═══════════════════════ 证据底座 ═══════════════════════

def _build_evidence(code: str, market: str) -> Dict[str, Any]:
    """绑定标的时的证据底座：相关新鲜报告（含摘要）+ 定量数据底座。"""
    evidence: Dict[str, Any] = {"reports": [], "data": {}}
    if not code:
        return evidence

    # 1. 定量数据底座（get_data_base，复用 debate_engine）
    data: Dict[str, Any] = {}
    name = ""
    try:
        from debate_engine import get_data_base

        data = get_data_base(code, market)
        name = (data.get("asset") or {}).get("name", "")
    except Exception as exc:  # 数据失败不阻断会话，如实标注
        data = {"_error": f"定量数据获取失败: {exc}"}
    evidence["data"] = data

    # 2. 相关新鲜报告（含摘要，最多 4 份）
    try:
        for r in research_engine.find_reports_for_target(code=code, name=name, industry="", limit=4):
            try:
                content = research_engine.read_report(r["name"])["content"]
                evidence["reports"].append(
                    {
                        "type": r["type_label"],
                        "target": r["target"] or r["name"],
                        "date": r["date"],
                        "summary": content[:1500],
                    }
                )
            except Exception:
                continue
    except Exception:
        pass
    return evidence


def _name_code_map(basic) -> List[tuple]:
    """name→code 映射（进程级缓存，TTL 1h）。basic 为 stock_basic_info 集合对象。"""
    global _NAME_CODE_CACHE, _NAME_CODE_CACHE_TS
    now = time.time()
    if _NAME_CODE_CACHE is None or (now - _NAME_CODE_CACHE_TS) > _NAME_CODE_CACHE_TTL:
        _NAME_CODE_CACHE = [
            (d.get("name"), d.get("code"))
            for d in basic.find({}, {"name": 1, "code": 1})
        ]
        _NAME_CODE_CACHE_TS = now
    return _NAME_CODE_CACHE


def _auto_identify(question: str) -> tuple[str, str]:
    """从问题文本自动识别标的代码（「绑定股票代码」输入框已移除的后端兜底）。

    优先级：
    1. 6 位 A 股代码（\\b\\d{6}\\b）→ 到 stock_basic_info 验证存在，避免误判年份/百分比。
    2. 股票中文名 → 加载 stock_basic_info 的 name/code，按名称长度降序做包含匹配（最长优先，避免短名抢占长名）。
    返回 (code, name)；识别不到返回 ("", "")。
    """
    q = (question or "").strip()
    if not q:
        return "", ""
    try:
        db = _get_mongo()
        basic = db["stock_basic_info"]
        for c in re.findall(r"\b\d{6}\b", q):
            doc = basic.find_one({"code": c})
            if doc:
                return c, doc.get("name") or c
        hits = [
            (nm, cd) for nm, cd in _name_code_map(basic)
            if nm and len(nm) >= 2 and nm in q
        ]
        if hits:
            hits.sort(key=lambda x: len(x[0]), reverse=True)
            best = hits[0]
            return best[1] or "", best[0]
    except Exception:
        pass
    return "", ""


def _evidence_text(evidence: Dict[str, Any]) -> str:
    """把证据底座压成可注入 prompt 的文本。"""
    parts: List[str] = []
    reports = evidence.get("reports") or []
    if reports:
        parts.append("【相关研究报告】（reports/ 已有研究结论）")
        for r in reports:
            parts.append(f"- {r['type']}｜{r['target']}（{r['date']}）：\n{r['summary']}")
    else:
        parts.append("【相关研究报告】无（该标的新鲜报告缺失）")
    data = evidence.get("data") or {}
    parts.append(f"【定量数据底座】\n{json.dumps(data, ensure_ascii=False, default=str)[:5000]}")
    return "\n\n".join(parts)


# ═══════════════════════ 会话 CRUD ═══════════════════════

def create_session(
    user_id: str,
    title: str = "",
    code: str = "",
    market: str = "A股",
    first_message: str = "",
    mode: str = "normal",
    web: bool = True,
) -> Dict[str, Any]:
    """新建会话。可选绑定标的（code）并带首问。返回 {session_id, task_id?}。"""
    db = _get_mongo()
    _ensure_indexes(db)
    session_id = uuid.uuid4().hex
    now = _now()

    title = (title or "").strip() or (first_message[:20] + ("…" if len(first_message) > 20 else "")) or "新话题"
    evidence = _build_evidence(code, market) if code else {"reports": [], "data": {}}
    messages: List[Dict[str, Any]] = []
    if first_message:
        messages.append({"role": "user", "content": first_message, "meta": {}, "created_at": now})

    db[COL_SESSIONS].insert_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "title": title,
            "code": code or "",
            "market": market,
            "mode": mode,
            "web": web,
            "evidence": evidence,
            "messages": messages,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
    )

    task_id = None
    if first_message:
        from research_tasks import enqueue

        task_id = enqueue(
            user_id,
            "debate_turn",
            target=session_id,
            code=code or "",
            market=market,
            payload={"session_id": session_id, "question": first_message, "mode": mode, "web": web},
        )
    return {"session_id": session_id, "task_id": task_id}


def list_sessions(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """话题列表（不含消息正文/证据，供左侧栏）。"""
    db = _get_mongo()
    docs = list(
        db[COL_SESSIONS].find({"user_id": user_id, "active": True})
        .sort("updated_at", -1)
        .limit(limit)
    )
    out: List[Dict[str, Any]] = []
    for d in docs:
        d.pop("_id", None)
        d["message_count"] = len(d.get("messages") or [])
        d.pop("messages", None)
        d.pop("evidence", None)
        out.append(d)
    return out


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    db = _get_mongo()
    doc = db[COL_SESSIONS].find_one({"session_id": session_id})
    if doc:
        doc.pop("_id", None)
    return doc


def add_message(session_id: str, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
    """追加一条消息（$push），并刷新 updated_at。"""
    db = _get_mongo()
    now = _now()
    db[COL_SESSIONS].update_one(
        {"session_id": session_id},
        {
            "$push": {"messages": {"role": role, "content": content, "meta": meta or {}, "created_at": now}},
            "$set": {"updated_at": now},
        },
    )


def deactivate_session(session_id: str) -> None:
    """软删（active=false，非物理删除，遵守 CLAUDE.md 禁删）。"""
    db = _get_mongo()
    db[COL_SESSIONS].update_one(
        {"session_id": session_id}, {"$set": {"active": False, "updated_at": _now()}}
    )


# ═══════════════════════ 单轮执行（worker 调用） ═══════════════════════

def run_debate_turn(task: Dict[str, Any]) -> Dict[str, Any]:
    """执行一轮对话：拼 prompt → wecode → 追加 assistant 回复。

    task.payload 需含 {session_id, question, mode?}。question 在端点入队前已作为
    user 消息写入 session；此处据此定位「当前问题」并把之前的消息当历史。
    mode：normal=单 agent 直接答（快）；deep=读 skills/assistant/agent.md 多智能体
    多角度分析（慢），结论顺带归档 reports/。
    """
    from debate_engine import clean_output, run_agent

    payload = task.get("payload") or {}
    session_id = payload.get("session_id") or task.get("target", "")
    question = payload.get("question", "")
    mode = payload.get("mode", "normal") or "normal"
    web = payload.get("web", True)

    session = get_session(session_id)
    if not session:
        raise RuntimeError(f"会话不存在: {session_id}")

    messages = session.get("messages") or []
    # 定位当前问题在 messages 中的位置（内容匹配最后一条 user）
    current_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user" and messages[i].get("content") == question:
            current_idx = i
            break
    if current_idx < 0:
        current_idx = len(messages) - 1
        question = messages[current_idx].get("content", "") if current_idx >= 0 else question

    history = messages[:current_idx]

    # 组 prompt
    code = session.get("code", "")
    market = session.get("market", "A股")
    name = ""
    evidence = session.get("evidence") or {}

    # normal 模式：后端正则快速识别标的（输入框已移除的兜底），动态取数 + 回写缓存。
    #   每轮都重新识别：用户换标的（如从宁德时代换到比亚迪）时切到新标的的底座；
    #   识别不到（泛问/追问）则沿用已绑定标的。deep 模式不预识别，交给 agent.md。
    if mode != "deep":
        auto_code, auto_name = _auto_identify(question)
        if auto_code and auto_code != code:
            code = auto_code
            name = auto_name or ""
            evidence = _build_evidence(code, market)
            try:
                _get_mongo()[COL_SESSIONS].update_one(
                    {"session_id": session_id},
                    {"$set": {"code": code, "market": market, "evidence": evidence}},
                )
            except Exception:
                pass

    if evidence.get("data") and isinstance(evidence["data"], dict):
        name = (evidence["data"].get("asset") or {}).get("name", "") or name or ""

    history_text = _history_text(history)

    if mode == "deep":
        assistant_agent = os.path.join(PROJECT_ROOT, "skills", "assistant", "agent.md")
        helper = (
            f"{os.path.join(PROJECT_ROOT, 'venv', 'bin', 'python')} "
            f"{os.path.join(PROJECT_ROOT, 'scripts', 'assistant_evidence.py')}"
        )
        prompt = (
            f"读 {assistant_agent}，严格按它执行下面的深度分析任务。"
            f"你的工作目录是 {PROJECT_ROOT}，agent.md 里所有 skills/ 相对路径都基于这个目录。\n\n"
            f"【话题】{session.get('title', '')}\n"
            f"【会话ID】{session_id}\n"
            f"【标的】代码：{code or '—'}，市场：{market}，名称：{name or '—'}\n"
            f"【取数助手】个股问题先识别标的代码，再用 Bash 运行以下命令取数据底座：\n"
            f"    {helper} <代码> --market {market} --session {session_id}\n\n"
            f"【联网开关】{'开' if web else '关'}\n\n"
            f"【证据底座】\n{_evidence_text(evidence)}\n\n"
            f"【历史对话】\n{history_text or '（无，这是首轮）'}\n\n"
            f"【本轮问题】\n{question}\n"
        )
        timeout = int(os.environ.get("ASSISTANT_DEEP_TIMEOUT", "1200"))
    else:
        prompt = (
            "你是 mystock 投资控制台的「投资助手」，一位资深金融分析师。"
            "基于下面的证据底座和历史对话，回答用户本轮问题。\n"
            "要求：用事实说话、附推理链条、指出不确定性；不要编造数据，"
            "证据里没有的信息要明确说「未知」；不给出超出能力的确定性买卖指令。\n\n"
            f"【话题】{session.get('title', '')}\n"
            f"【标的】代码：{code or '—'}，市场：{market}，名称：{name or '—'}\n\n"
            f"【证据底座】\n{_evidence_text(evidence)}\n\n"
            f"【历史对话】\n{history_text or '（无，这是首轮）'}\n\n"
            f"【本轮问题】\n{question}\n\n"
            "用 markdown 输出回复（不要 JSON，不要代码围栏）。"
        )
        timeout = int(os.environ.get("DEBATE_TIMEOUT", "600"))

    # DRY RUN：写桩回复，不调真实 wecode（快速验证链路）
    if os.environ.get("RESEARCH_DRY_RUN") == "1":
        reply = (
            f"（DRY RUN 桩回复，mode={mode}）\n\n"
            f"- 话题：{session.get('title', '')}\n- 绑定标的：{code or '—'}\n"
            f"- 证据报告：{len((evidence or {}).get('reports') or [])} 份\n- 历史轮数：{len(history)} 条\n\n"
            f"> 已收到问题：「{question[:80]}」"
        )
    else:
        raw = run_agent(prompt, timeout=timeout)
        reply = clean_output(raw).strip() or "（空回复）"

    # deep 模式顺带归档到 reports/（只增不改，归档失败不阻断对话回复）
    if mode == "deep" and reply:
        try:
            archive_target = (name or code or session.get("title", "") or "对话").strip()
            archive_target = re.sub(r"[\\/:*?\"<>|\s]+", "_", archive_target) or "对话"
            archive_name = research_engine.archive_filename(
                "assistant_deep", target=archive_target, code=code, name=name
            )
            research_engine._write_report(archive_name, reply)
        except Exception:
            pass

    add_message(session_id, "assistant", reply, meta={"mode": mode})
    return {"session_id": session_id, "reply": reply}


def _history_text(messages: List[Dict[str, Any]]) -> str:
    """把最近若干轮历史压成文本（截断到 HISTORY_TURNS 条消息）。"""
    recent = messages[-HISTORY_TURNS:]
    lines = []
    for m in recent:
        role = "用户" if m.get("role") == "user" else "助手"
        content = (m.get("content") or "").strip()
        lines.append(f"{role}：{content[:1500]}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 冒烟：列会话
    import argparse

    parser = argparse.ArgumentParser(description="debate_session 冒烟")
    parser.add_argument("--list", action="store_true", help="列出当前用户会话")
    parser.add_argument("--user", default="zhongyue3", help="用户 id")
    args = parser.parse_args()

    if args.list:
        for s in list_sessions(args.user):
            print(f"[{s['session_id'][:8]}] {s['title']} code={s['code'] or '—'} msg={len(s.get('messages') or [])}")
