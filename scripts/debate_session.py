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

# 证据底座新鲜度：跨自然日或超过该秒数即判定过期，回答前重建（杜绝拿过期数据答当下问题）
EVIDENCE_TTL_SECONDS = int(os.environ.get("EVIDENCE_TTL_SECONDS", "1800"))

# 证据底座序列化上限（normal 全量底座的字符预算；deep 各视角切片取 1/3）
EVIDENCE_MAX_CHARS = int(os.environ.get("EVIDENCE_MAX_CHARS", "12000"))

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

def _build_evidence(code: str, market: str, user_id: str = "") -> Dict[str, Any]:
    """绑定标的时的证据底座：相关新鲜报告（含摘要）+ 定量数据底座 + 持仓上下文。"""
    evidence: Dict[str, Any] = {"reports": [], "data": {}, "position": None}
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

    # 3. 持仓上下文：若该标的是当前用户持仓，注入数量/成本/止损/止盈/浮盈亏
    if user_id:
        evidence["position"] = _position_context(user_id, code, data)

    # 记录底座构建时间，供前端展示「数据截至」+ 供 _evidence_is_stale 判定过期
    evidence["built_at"] = _now()
    return evidence


def _evidence_is_stale(evidence: Dict[str, Any]) -> bool:
    """判定证据底座是否过期：跨自然日 / 超过 TTL / 缺少 built_at（旧会话）均视为过期。

    目的：同一标的的多轮追问、隔天再问，不再沿用首轮构建的现价/指标，
    避免用过期数据答当下问题（CLAUDE.md 明令禁止的静默降级）。
    """
    built = (evidence or {}).get("built_at") or ""
    if not built:
        return True  # 旧会话无 built_at 字段 → 首次回答前重建一次
    try:
        built_dt = datetime.fromisoformat(built)
    except ValueError:
        return True
    if built_dt.date() != datetime.now().date():
        return True  # 盘中隔天：即使 TTL 内也重建，不拿昨天收盘价答今天
    return (datetime.now() - built_dt).total_seconds() > EVIDENCE_TTL_SECONDS


def _current_price_from_data(data: Dict[str, Any]) -> Optional[float]:
    """从数据底座提取现价：优先 quote_snapshot 快照价，次选 trend_signal 最新收盘。"""
    if not isinstance(data, dict):
        return None
    qs = (data.get("quote_snapshot") or {}).get("metrics") or {}
    price = qs.get("regular_market_price")
    if price is not None:
        return float(price)
    ts = data.get("trend_signal") or {}
    lc = ts.get("latest_close")
    if lc is not None:
        return float(lc)
    return None


def _position_context(user_id: str, code: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """查询 sim_positions 是否为该用户持仓；是则返回持仓上下文（含浮盈亏），否则 None。

    现价取自数据底座（避免重复拉行情），成本/止损/止盈/逻辑取自 sim_positions。
    查询失败返回 None，不阻断证据底座。
    """
    if not user_id or not code:
        return None
    try:
        db = _get_mongo()
        pos = db["sim_positions"].find_one(
            {"user_id": user_id, "code": str(code).strip(), "closed": {"$ne": True}}
        )
        if not pos:
            return None
        qty = float(pos.get("quantity") or 0)
        cost = float(pos.get("cost_price") or 0)
        cur = _current_price_from_data(data)
        ctx: Dict[str, Any] = {
            "quantity": qty,
            "available_qty": pos.get("available_qty", 0),
            "cost_price": cost,
            "currency": pos.get("currency") or "CNY",
            "stop_loss_price": pos.get("stop_loss_price"),
            "take_profit_price": pos.get("take_profit_price"),
            "thesis": pos.get("thesis", ""),
            "industry": pos.get("industry", ""),
        }
        if cur is not None:
            mv = qty * cur
            pnl = (cur - cost) * qty
            ctx["current_price"] = round(cur, 2)
            ctx["market_value"] = round(mv, 2)
            ctx["unrealized_pnl"] = round(pnl, 2)
            ctx["unrealized_pnl_pct"] = round((cur - cost) / cost * 100, 2) if cost else 0.0
            if cost and pos.get("stop_loss_price"):
                sl = float(pos["stop_loss_price"])
                ctx["distance_to_stop_loss_pct"] = round((cur - sl) / cur * 100, 2)
        return ctx
    except Exception:
        return None


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


def _common_parts(evidence: Dict[str, Any]) -> List[str]:
    """证据底座里「所有视角通用」的文本段落：数据截至 / 数据自检 / 持仓 / 报告。"""
    parts: List[str] = []
    data = evidence.get("data") if isinstance(evidence, dict) else {}

    # 底座构建时间置顶，让 LLM 明确知道数字取自何时
    built = (evidence or {}).get("built_at")
    if built:
        parts.append(f"【数据截至】{built}（数字取自此时刻，回答时不得把它当实时值）")

    # 数据自检（口径矛盾/脏数据）优先置顶，让 LLM 第一眼看到
    dq = data.get("data_quality") if isinstance(data, dict) else None
    if dq:
        lines = [f"- ⚠️ [{c.get('field')}] {c.get('message')}" for c in dq]
        parts.append("【数据自检】以下异常已由底座侧主动标注，引用相关数字需谨慎：\n" + "\n".join(lines))
    else:
        parts.append("【数据自检】未发现异常")

    # 持仓上下文（该标的恰好是用户持仓时）
    pos = evidence.get("position")
    if pos:
        pnl_pct = pos.get("unrealized_pnl_pct")
        pnl_str = f"{pos.get('unrealized_pnl_pct')}%" if pnl_pct is not None else "未知"
        dist_sl = pos.get("distance_to_stop_loss_pct")
        pos_lines = [
            f"该标的是你的持仓：{pos.get('quantity')} 股，成本 {pos.get('cost_price')}，现价 {pos.get('current_price', '未知')}",
            f"浮盈亏 {pnl_str}（{pos.get('unrealized_pnl', '未知')}），市值 {pos.get('market_value', '未知')}",
            f"止损价 {pos.get('stop_loss_price', '未设')}，止盈价 {pos.get('take_profit_price', '未设')}",
        ]
        if dist_sl is not None:
            pos_lines.append(f"距止损 {dist_sl}%")
        if pos.get("thesis"):
            pos_lines.append(f"买入逻辑：{pos.get('thesis')}")
        parts.append("【持仓上下文】\n" + "\n".join(pos_lines))

    reports = evidence.get("reports") or []
    if reports:
        parts.append("【相关研究报告】（reports/ 已有研究结论）")
        for r in reports:
            parts.append(f"- {r['type']}｜{r['target']}（{r['date']}）：\n{r['summary']}")
    else:
        parts.append("【相关研究报告】无（该标的新鲜报告缺失）")
    return parts


def _safe_dumps(obj: Any, max_chars: int) -> str:
    """序列化为 ≤max_chars 的合法 JSON：超限逐项贪心截断，绝不硬切出非法 JSON。"""

    def dumps(x: Any) -> str:
        return json.dumps(x, ensure_ascii=False, default=str)

    def walk(x: Any, budget: int) -> Any:
        if len(dumps(x)) <= budget:
            return x
        if isinstance(x, dict):
            out: Dict[str, Any] = {}
            for k, v in x.items():
                remain = max(1, budget - len(dumps(out)) - len(dumps(k)) - 4)
                candidate = dict(out)
                candidate[k] = walk(v, remain)
                if len(dumps(candidate)) > budget:
                    out["_truncated"] = True
                    break
                out = candidate
            return out
        if isinstance(x, list):
            out: List[Any] = []
            for v in x:
                remain = max(1, budget - len(dumps(out)) - 3)
                candidate = out + [walk(v, remain)]
                if len(dumps(candidate)) > budget:
                    break
                out = candidate
            return out
        if isinstance(x, str):
            return x[: max(1, budget - 2)]
        return x

    return dumps(walk(obj, max_chars))


def _evidence_text(evidence: Dict[str, Any]) -> str:
    """normal 模式：共性文本 + 定量数据全量（安全截断）。"""
    parts = _common_parts(evidence)
    data = (evidence or {}).get("data") or {}
    parts.append(f"【定量数据底座】\n{_safe_dumps(data, EVIDENCE_MAX_CHARS)}")
    return "\n\n".join(parts)


# 各视角 → prompt 段落标题（与 agent.md Step 1 的派发一一对应）
_VIEW_LABELS = (("value", "价值"), ("growth", "成长"), ("trend", "趋势"), ("risk", "风险"))


def _slice_evidence(evidence: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """deep 模式：把定量数据底座按「价值/成长/趋势/风险」四视角切片。

    每个视角只拿自己需要的字段（附一份小的共性底座，保证子代理自洽），
    避免 4 个子代理各吃一份 ~12KB 全量底座。切片在后端确定性完成，
    主代理只需按 agent.md 指令把对应【X视角数据】转发给对应子代理。
    """
    data = (evidence or {}).get("data") or {}

    # 共性：资产/估值/PE分位/周期（小，各视角都要）
    common = {
        "asset": data.get("asset"),
        "quote_snapshot": data.get("quote_snapshot"),
        "pe_percentile_self": data.get("pe_percentile_self"),
        "cycle": data.get("cycle"),
    }

    lc = data.get("llm_context") or {}
    sc = lc.get("signal_contexts") or {}
    factor = sc.get("factor")
    trend_ctx = sc.get("trend")
    event_ctx = sc.get("event")

    tsig = data.get("trend_signal") or {}
    # 去掉 trend_signal 内嵌的 llm_context（与顶层 llm_context 重复冗余）
    tsig_clean = {k: v for k, v in tsig.items() if k != "llm_context"}
    tsh = data.get("tushare_signals") or {}
    news = data.get("news")

    # 风险视角只要趋势信号里的回撤/波动/支撑压力，不取 RSI/MACD/KDJ 等全量指标
    ti = tsig.get("technical_indicators") or {}
    tsig_risk = {
        "latest_close": tsig.get("latest_close"),
        "volatility_20d": tsig.get("volatility_20d"),
        "returns": tsig.get("returns"),
        "risk_factors": tsig.get("risk_factors"),
        "supporting_factors": tsig.get("supporting_factors"),
        "interpretation": tsig.get("interpretation"),
        "key_levels": ti.get("range_position") or ti.get("key_levels"),
    }

    return {
        "value": {**common,
                  "factor": factor,                     # 四因子（价值/质量重点）
                  "financial": tsh.get("financial"),    # 财务
                  "buyback": tsh.get("buyback"),        # 回购
                  "holder_num": tsh.get("holder_num"),  # 股东户数
                  },
        "growth": {**common,
                   "factor": factor,                    # 四因子（成长/动量重点）
                   "event": event_ctx,                  # 事件情绪
                   "forecast": tsh.get("forecast"),     # 业绩预告
                   "news": news,
                   },
        "trend": {**common,
                  "trend_signal": tsig_clean,           # 技术指标全量
                  "trend_ctx": trend_ctx,
                  "moneyflow": data.get("moneyflow"),
                  },
        "risk": {**common,
                 "trend_signal_risk": tsig_risk,        # 回撤/波动/支撑压力
                 "margin": tsh.get("margin"),           # 两融
                 "holder_change": tsh.get("holder_change"),
                 "news": news,
                 },
    }


def _deep_evidence_text(evidence: Dict[str, Any]) -> str:
    """deep 模式：共性文本 + 四视角切片数据，供主代理按视角转发给子代理。"""
    parts = _common_parts(evidence)
    slices = _slice_evidence(evidence)
    parts.append(
        "【定量数据底座】后端已按「价值/成长/趋势/风险」四视角切片。"
        "派子代理时，把对应【X视角数据】传给对应视角的子代理，勿把全量底座原样转发。"
    )
    per_view = max(2000, EVIDENCE_MAX_CHARS // 3)
    for key, label in _VIEW_LABELS:
        parts.append(f"【{label}视角数据】\n{_safe_dumps(slices.get(key, {}), per_view)}")
    return "\n\n".join(parts)


# ═══════════════════════ 会话 CRUD ═══════════════════════

def create_session(
    user_id: str,
    title: str = "",
    code: str = "",
    market: str = "A股",
    first_message: str = "",
    mode: str = "normal",
    web: bool = False,
) -> Dict[str, Any]:
    """新建会话。可选绑定标的（code）并带首问。返回 {session_id, task_id?}。"""
    db = _get_mongo()
    _ensure_indexes(db)
    session_id = uuid.uuid4().hex
    now = _now()

    title = (title or "").strip() or (first_message[:20] + ("…" if len(first_message) > 20 else "")) or "新话题"
    evidence = _build_evidence(code, market, user_id) if code else {"reports": [], "data": {}, "position": None}
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

def run_debate_turn(task: Dict[str, Any], on_session=None) -> Dict[str, Any]:
    """执行一轮对话：拼 prompt → wecode → 追加 assistant 回复。

    task.payload 需含 {session_id, question, mode?}。question 在端点入队前已作为
    user 消息写入 session；此处据此定位「当前问题」并把之前的消息当历史。
    mode：normal=单 agent 直接答（快）；deep=读 skills/assistant/agent.md 多智能体
    多角度分析（慢），结论顺带归档 reports/。

    on_session(wecode_session_id)：会话 JSONL 一出现就回调（供 worker 落库
    session_id，让前端在 running 期间就能拉到「研究过程」）。
    """
    from debate_engine import clean_output, run_agent_with_session

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

    # 后端正则快速识别标的（输入框已移除的兜底），动态取数 + 回写缓存。
    #   每轮都重新识别：用户换标的（如从宁德时代换到比亚迪）时切到新标的的底座；
    #   识别不到（泛问/追问）则沿用已绑定标的。deep 模式同样在此预取，
    #   不再赌主代理是否会跑 assistant_evidence.py（helper 仍保留作兜底）。
    auto_code, auto_name = _auto_identify(question)
    if auto_code and auto_code != code:
        code = auto_code
        name = auto_name or ""
        evidence = {}
    # 数据新鲜度：换标的必然重建；同标的跨日 / 超 TTL 也重建，杜绝拿过期数据答当下问题
    if code and (not evidence.get("data") or _evidence_is_stale(evidence)):
        evidence = {}
    if code and not evidence.get("data"):
        evidence = _build_evidence(code, market, session.get("user_id", ""))
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

    # 操作 helper（对称「取数助手」）：用户下达操作指令时，agent 用 Bash 调它替用户执行。
    # user_id 由脚本从 --session 解析（用户隔离构造性保证），agent 手里只有 session_id。
    action_helper = (
        f"{os.path.join(PROJECT_ROOT, 'venv', 'bin', 'python')} "
        f"{os.path.join(PROJECT_ROOT, 'scripts', 'assistant_action.py')}"
    )
    action_block = (
        f"    {action_helper} place_order --session {session_id} --code <代码> --side buy|sell --quantity <股数>\n"
        f"    {action_helper} update_position --session {session_id} --code <代码> [--stop-loss X] [--take-profit X] [--thesis \"…\"]\n"
        f"    {action_helper} add_watchlist --session {session_id} --code <代码> [--thesis \"…\"]\n"
        f"    {action_helper} remove_watchlist --session {session_id} --code <代码>\n"
        f"    {action_helper} run_review --session {session_id}\n"
        f"    {action_helper} account --session {session_id}\n"
    )

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
            f"【操作助手】用户明确要求操作平台（下单/卖出、设止损止盈、加/移观察池、跑复盘、查账户）时，"
            f"用 Bash 运行对应命令替用户执行（user_id 后端从会话解析，你只传 --session）：\n"
            f"{action_block}"
            f"  执行后把结果（成交价/金额/余额/告警）报回用户；护栏（金额/次数上限）只是提醒建议、不硬拦。\n\n"
            f"【联网开关】{'开' if web else '关'}\n\n"
            f"【证据底座】\n{_deep_evidence_text(evidence)}\n\n"
            f"【历史对话】\n{history_text or '（无，这是首轮）'}\n\n"
            f"【本轮问题】\n{question}\n"
        )
        timeout = int(os.environ.get("ASSISTANT_DEEP_TIMEOUT", "1200"))
    else:
        prompt = (
            "你是 mystock 投资控制台的「投资助手」，一位资深金融分析师兼平台操作代理。"
            "基于下面的证据底座和历史对话，回答用户本轮问题。\n"
            "分析要求：用事实说话、附推理链条、指出不确定性；不要编造数据，"
            "证据里没有的信息要明确说「未知」。\n"
            "操作要求：你能替用户操作整个模拟盘平台（下单/卖出、设止损止盈、加/移观察池、"
            "跑组合复盘、查账户持仓）。用户明确下达操作指令时，用【操作助手】命令替用户执行并把结果报回；"
            "参数缺失/含糊（如没给股数、方向）才追问一次。绝不自主交易（用户没让操作就不动）；"
            "护栏（金额/次数上限）只做提醒建议、不硬拦。\n\n"
            f"【会话ID】{session_id}\n"
            f"【话题】{session.get('title', '')}\n"
            f"【标的】代码：{code or '—'}，市场：{market}，名称：{name or '—'}\n\n"
            f"【操作助手】（user_id 后端从会话解析，你只传 --session）\n"
            f"{action_block}\n"
            f"【证据底座】\n{_evidence_text(evidence)}\n\n"
            f"【历史对话】\n{history_text or '（无，这是首轮）'}\n\n"
            f"【本轮问题】\n{question}\n\n"
            "用 markdown 输出回复（不要 JSON，不要代码围栏）。"
        )
        timeout = int(os.environ.get("DEBATE_TIMEOUT", "600"))

    # DRY RUN：写桩回复，不调真实 wecode（快速验证链路）
    wecode_sid = ""
    if os.environ.get("RESEARCH_DRY_RUN") == "1":
        reply = (
            f"（DRY RUN 桩回复，mode={mode}）\n\n"
            f"- 话题：{session.get('title', '')}\n- 绑定标的：{code or '—'}\n"
            f"- 证据报告：{len((evidence or {}).get('reports') or [])} 份\n- 历史轮数：{len(history)} 条\n\n"
            f"> 已收到问题：「{question[:80]}」"
        )
    else:
        raw, wecode_sid = run_agent_with_session(
            prompt, timeout=timeout, on_session=on_session
        )
        reply = clean_output(raw).strip() or "（空回复）"

    # deep 模式顺带归档到 reports/（只增不改，归档失败不阻断对话回复）
    # 关键：run_agent 返回后，deep 子代理可能已通过 assistant_evidence.py --session
    # 回写了最新 code/name/evidence，故归档前重读 session 取最新值，避免文件名退回
    # 问题文本（旧实现用本地过期变量，报告名变成 assistant_deep_<问题文本>_*.md）。
    if mode == "deep" and reply:
        try:
            fresh = get_session(session_id) or {}
            arch_code = fresh.get("code") or code
            arch_name = ""
            fe = fresh.get("evidence") or {}
            if isinstance(fe.get("data"), dict):
                arch_name = (fe["data"].get("asset") or {}).get("name", "") or ""
            archive_target = (arch_name or arch_code or session.get("title", "") or "对话").strip()
            archive_target = re.sub(r"[\\/:*?\"<>|\s]+", "_", archive_target) or "对话"
            archive_name = research_engine.archive_filename(
                "assistant_deep", target=archive_target, code=arch_code, name=arch_name
            )
            research_engine._write_report(archive_name, reply)
        except Exception:
            pass

    add_message(
        session_id, "assistant", reply,
        meta={"mode": mode, "session_id": wecode_sid or None},
    )
    return {"session_id": wecode_sid or session_id, "reply": reply}


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
