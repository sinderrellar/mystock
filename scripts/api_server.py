#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_server.py —— 「mystock 统一投资控制台」FastAPI 后端

单端口服务：API + 静态 serve web/dist。

端点一览：
  认证   GET  /api/auth/config       返回 {dev_user, cas_login_url}
         GET  /api/auth/validate     代理 CAS 校验 ticket → {token, user_id, email}
         POST /api/auth/dev-login    本地联调专用（仅 DEV_USER 非空时可用）
  模拟交易 GET  /api/trade/account         当前账户（现金/持仓/市值/盈亏）
         POST /api/trade/order             下单 {code, side, quantity} → 成交结果
         GET  /api/trade/trades            成交流水
         GET  /api/trade/watchlist         观察池 {items[]}
         POST /api/trade/watchlist         加入观察池 {code, thesis?}（幂等/可重新激活）
         POST /api/trade/watchlist/remove  移出观察池 {code}（软删 active=False）
         POST /api/trade/position/update   更新持仓止盈/止损/逻辑 {code, ...}
                                           （缺省不改；显式 null 清空）
  业务    GET  /api/portfolio        组合全景（复用 review()，缓存 3min）
         POST /api/portfolio/refresh 强制重算
         GET  /api/sector            行业雷达（复用 run_heatmap()）
         GET  /api/buyplan           明日选股（复用 buy_plan.run()）
         GET  /api/stock/search?q=   股票搜索（code 前缀 + name 模糊）
  （保留）POST /api/debate、GET /api/data/{code}、POST /api/refine、GET /api/demo、/api/health

启动：
  cd /data2/liuyu20/mystock
  DEV_USER=zhongyue3 venv/bin/python -m uvicorn scripts.api_server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import quote
from urllib.request import urlopen

# 让 scripts/ 目录（debate_engine / sim_trade 所在处）可被 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import debate_engine
import debate_session
import research_engine
import research_tasks
import sim_trade
import buyplan_presets

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── CAS 认证（参考 app.py:138-195）────────────────────────────────────────
CAS_VALIDATE_URL = "http://cas.erp.sina.com.cn/cas/validate"
CAS_LOGIN_URL = "https://cas.erp.sina.com.cn/cas/login"
DEV_USER = os.environ.get("DEV_USER", "").strip()  # 本地联调：非空则跳过真实 CAS

_SESSIONS: Dict[str, Dict[str, Any]] = {}  # token -> {user_id, expires_at}

# ── 组合全景缓存 ──────────────────────────────────────────────────────────
_portfolio_cache: Dict[str, Any] = {"user_id": None, "data": None, "ts": None}


def _json_default(obj: Any) -> Any:
    """把 numpy 标量等非原生类型转成可 JSON 序列化的原生类型。"""
    if hasattr(obj, "item") and hasattr(obj, "dtype"):
        return obj.item()
    return str(obj)


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=_json_default))


def _get_current_user(authorization: Optional[str] = Header(None)) -> str:
    """解析登录态，返回 user_id。DEV_USER 非空时直接返回（本地联调跳过 CAS）。"""
    if DEV_USER:
        return DEV_USER
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:].strip()
    sess = _SESSIONS.get(token)
    if not sess or sess["expires_at"] < datetime.now():
        raise HTTPException(status_code=401, detail="登录已过期")
    return sess["user_id"]


app = FastAPI(title="mystock 统一投资控制台", description="组合全景 / 行业雷达 / 明日选股 / 模拟交易 / 投资智辩")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════ 认证 ═══════════════════════════

@app.get("/api/auth/config")
def auth_config() -> Dict[str, str]:
    """前端登录页据此决定：走真实 CAS 还是显示「开发模式登录」按钮。"""
    return {"dev_user": DEV_USER, "cas_login_url": CAS_LOGIN_URL}


@app.get("/api/auth/validate")
def cas_validate(ticket: str, service: str) -> Dict[str, str]:
    """代理请求 CAS validate，解析 XML 拿 email，签发自有 session token。"""
    if DEV_USER:
        return _issue_session(DEV_USER, f"{DEV_USER}@sina.com.cn")

    url = f"{CAS_VALIDATE_URL}?ticket={quote(ticket)}&service={quote(service, safe='')}"
    try:
        resp = urlopen(url, timeout=10)
        raw = resp.read()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CAS 校验请求失败: {exc}")

    # CAS 返回 GB2312 编码 XML，逐个尝试解码
    for enc in ("gb2312", "gbk", "gb18030", "utf-8", "latin-1"):
        try:
            xml_text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        xml_text = raw.decode("utf-8", errors="replace")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail=f"CAS 返回解析失败: {exc}")

    email_el = root.find(".//email")
    if email_el is not None and email_el.text:
        email = email_el.text.strip()
    else:
        user_el = root.find(".//username")
        if user_el is not None and user_el.text:
            email = user_el.text.strip()
            if "@" not in email:
                email = f"{email}@sina.com.cn"
        else:
            ns = {"cas": "http://www.yale.edu/tp/cas"}
            failure = root.find(".//cas:authenticationFailure", ns)
            if failure is not None:
                raise HTTPException(status_code=401, detail=f"CAS 认证失败: {failure.get('code', 'unknown')}")
            raise HTTPException(status_code=401, detail="CAS 认证失败，无法获取用户信息")

    user_id = email.split("@")[0] if "@" in email else email
    return _issue_session(user_id, email)


def _issue_session(user_id: str, email: str) -> Dict[str, str]:
    token = uuid.uuid4().hex
    _SESSIONS[token] = {"user_id": user_id, "expires_at": datetime.now() + timedelta(hours=12)}
    try:
        sim_trade.init_user(user_id)
    except Exception:
        pass  # 初始化失败不阻断登录
    return {"token": token, "user_id": user_id, "email": email}


@app.post("/api/auth/dev-login")
def dev_login() -> Dict[str, str]:
    """本地联调专用：跳过 CAS，直接以 DEV_USER 身份登录。"""
    if not DEV_USER:
        raise HTTPException(status_code=404, detail="未启用 DEV_USER，请走真实 CAS")
    return _issue_session(DEV_USER, f"{DEV_USER}@sina.com.cn")


# ═══════════════════════════ 模拟交易 ═══════════════════════════

class OrderRequest(BaseModel):
    code: str = Field(..., description="股票代码，如 300750")
    side: str = Field(..., description="buy / sell")
    quantity: int = Field(..., description="数量（股）")


class WatchAddRequest(BaseModel):
    code: str = Field(..., description="股票代码，如 300750")
    thesis: str = Field("", description="关注逻辑（可空）")


class WatchRemoveRequest(BaseModel):
    code: str = Field(..., description="股票代码，如 300750")


class PositionUpdateRequest(BaseModel):
    """持仓风控/逻辑更新。缺省字段（未出现在请求体）不改；显式传 null 表示清空。"""

    code: str = Field(..., description="股票代码，如 300750")
    stop_loss_price: Optional[float] = Field(None, description="止损价（null 清空）")
    take_profit_price: Optional[float] = Field(None, description="止盈价（null 清空）")
    thesis: Optional[str] = Field(None, description="持有逻辑（null 清空）")


@app.get("/api/trade/account")
def trade_account(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    return _jsonable(sim_trade.get_account(user_id))


@app.post("/api/trade/order")
def trade_order(req: OrderRequest, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        result = sim_trade.place_order(user_id, req.code, req.side, req.quantity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"下单失败: {exc}")
    # 下单后投影到 portfolio.yaml，供组合全景 review() 读取
    try:
        sim_trade.sync_to_yaml(user_id)
        _portfolio_cache["ts"] = None  # 失效缓存
    except Exception:
        pass
    return _jsonable(result)


@app.get("/api/trade/trades")
def trade_trades(limit: int = 50, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    return {"trades": _jsonable(sim_trade.get_trades(user_id, limit))}


@app.get("/api/trade/watchlist")
def watchlist(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    return {"items": _jsonable(sim_trade.get_watchlist(user_id))}


@app.post("/api/trade/watchlist")
def watchlist_add(req: WatchAddRequest, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        result = sim_trade.add_watchlist(user_id, req.code, thesis=req.thesis)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"加入观察池失败: {exc}")
    # 观察池变更投影到 portfolio.yaml，避免组合全景读到旧观察池
    try:
        sim_trade.sync_to_yaml(user_id)
        _portfolio_cache["ts"] = None
    except Exception:
        pass
    return _jsonable(result)


@app.post("/api/trade/watchlist/remove")
def watchlist_remove(req: WatchRemoveRequest, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        result = sim_trade.remove_watchlist(user_id, req.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"移出观察池失败: {exc}")
    try:
        sim_trade.sync_to_yaml(user_id)
        _portfolio_cache["ts"] = None
    except Exception:
        pass
    return _jsonable(result)


@app.post("/api/trade/position/update")
def position_update(req: PositionUpdateRequest, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    # 只把「请求体中显式出现」的字段传给引擎 → 缺省不改、null 清空
    patch_kwargs: Dict[str, Any] = {}
    for key in ("stop_loss_price", "take_profit_price", "thesis"):
        if key in req.model_fields_set:
            patch_kwargs[key] = getattr(req, key)
    try:
        result = sim_trade.update_position(user_id, req.code, **patch_kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"持仓设置更新失败: {exc}")
    try:
        sim_trade.sync_to_yaml(user_id)
        _portfolio_cache["ts"] = None
    except Exception:
        pass
    return _jsonable(result)


# ═══════════════════════════ 组合全景 ═══════════════════════════

def _run_review(portfolio_path: Optional[str] = None) -> Dict[str, Any]:
    from portfolio_strategy import PortfolioStrategy
    return PortfolioStrategy(portfolio_path=portfolio_path).review()


def _get_portfolio(user_id: str, force: bool = False) -> Dict[str, Any]:
    now = datetime.now()
    cached = _portfolio_cache["data"]
    fresh = (
        cached is not None
        and _portfolio_cache["user_id"] == user_id
        and _portfolio_cache["ts"] is not None
        and (now - _portfolio_cache["ts"]).seconds < 180
    )
    if fresh and not force:
        return cached

    # 先把模拟账户投影到「按用户隔离」的 yaml，保证 review 读到本用户最新持仓/现金
    sim_trade.sync_to_yaml(user_id)
    data = _run_review(sim_trade.user_portfolio_path(user_id))
    _portfolio_cache.update({"user_id": user_id, "data": data, "ts": now})
    return data


@app.get("/api/portfolio")
def portfolio(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        return _jsonable(_get_portfolio(user_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"组合全景生成失败: {exc}")


@app.post("/api/portfolio/refresh")
def portfolio_refresh(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        return _jsonable(_get_portfolio(user_id, force=True))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"组合全景生成失败: {exc}")


# ═══════════════════════════ 行业雷达 ═══════════════════════════

@app.get("/api/sector")
def sector(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        from dashboard import run_heatmap
        return _jsonable(run_heatmap())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"行业雷达生成失败: {exc}")


# ═══════════════════════════ 明日选股 ═══════════════════════════

@app.get("/api/buyplan")
def buyplan(top: int = 15, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        from buy_plan import BuyPlanEngine
        result = BuyPlanEngine().run(top_n=top)
        return _jsonable(result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"明日选股生成失败: {exc}")


class BuyPlanPresetSaveRequest(BaseModel):
    name: str = Field(..., description="预设名称")
    filters: Dict[str, Any] = Field(..., description="5 层筛选参数组合")


class BuyPlanPresetRemoveRequest(BaseModel):
    name: str = Field(..., description="预设名称")


@app.get("/api/buyplan/presets")
def buyplan_presets_list(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        return {"items": _jsonable(buyplan_presets.list_presets(user_id))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取策略预设失败: {exc}")


@app.post("/api/buyplan/presets")
def buyplan_presets_save(req: BuyPlanPresetSaveRequest, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        return _jsonable(buyplan_presets.save_preset(user_id, req.name, req.filters))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"保存策略预设失败: {exc}")


@app.post("/api/buyplan/presets/remove")
def buyplan_presets_remove(req: BuyPlanPresetRemoveRequest, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        return _jsonable(buyplan_presets.remove_preset(user_id, req.name))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"删除策略预设失败: {exc}")


# ═══════════════════════════ 股票搜索 ═══════════════════════════

@app.get("/api/stock/search")
def stock_search(q: str = "", user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    return {"items": _jsonable(sim_trade.search_stocks(q))}


# ═══════════════════════════ 投资智辩（保留） ═══════════════════════════

class DebateRequest(BaseModel):
    idea: str = Field(..., description="用户输入的投资想法，如「我觉得宁德时代能翻倍」")
    code: str = Field(..., description="股票代码，如 300750")
    market: str = Field("A股", description="市场：A股 / 港股")


class RefineRequest(BaseModel):
    page_json: Dict[str, Any] = Field(..., description="当前投资页面 JSON")
    instruction: str = Field(..., description="自然语言微调指令")
    code: Optional[str] = Field(None, description="关联股票代码（可选）")


@app.post("/api/debate")
def debate(req: DebateRequest) -> Dict[str, Any]:
    try:
        return debate_engine.debate(req.idea, req.code, req.market)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"辩论执行失败: {exc}")


@app.get("/api/data/{code}")
def data(code: str, market: str = "A股") -> Dict[str, Any]:
    try:
        return debate_engine.get_data_base(code, market)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"数据获取失败: {exc}")


@app.post("/api/refine")
def refine(req: RefineRequest) -> Dict[str, Any]:
    prompt = (
        "你是投资页面编辑助手。下面是一份投资页面 JSON 和用户的一条微调指令。\n"
        "请只修改 JSON 中与指令相关的字段，保持整体结构不变，返回完整的 ```json 代码块。\n\n"
        f"【微调指令】\n{req.instruction}\n\n"
        "【当前页面 JSON】\n"
        f"{json.dumps(req.page_json, ensure_ascii=False, default=str)}\n"
    )
    if req.code:
        prompt += f"\n【关联股票代码】{req.code}\n"
    try:
        raw = debate_engine.run_agent(prompt)
        return debate_engine.parse_json(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"微调失败: {exc}")


@app.get("/api/demo")
def demo() -> Dict[str, Any]:
    demo_path = os.path.join(PROJECT_ROOT, "data", "demo_debate_300750.json")
    try:
        with open(demo_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="演示数据不存在") from exc


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "wecode": debate_engine.WECODE_BIN, "dev_user": DEV_USER}


# ═══════════════════════════ 深度研究 ═══════════════════════════

@app.get("/api/research/reports")
def research_reports(
    type: Optional[str] = None,
    target: Optional[str] = None,
    user_id: str = Depends(_get_current_user),
) -> Dict[str, Any]:
    """报告库列表：扫 reports/ 文件 + 合并进行中的研究任务。

    - reports：已完成报告，每个附 session_id（该报告对应 wecode 会话，用于「研究过程记录」链接；
      历史报告无对应任务 → session_id 为 None）。
    - in_progress：queued/running 的研究任务（报告库标记「研究中」）。
    """
    reports = research_engine.list_reports()
    if type:
        reports = [r for r in reports if r["type"] == type]
    if target:
        n = research_engine.normalize_target(target)
        reports = [r for r in reports if n and n in research_engine.normalize_target(r["target"])]

    # 任务 → 报告 → session_id 映射（done 且生成了报告的）
    session_by_report: Dict[str, str] = {}
    in_progress: List[Dict[str, Any]] = []
    try:
        tasks = research_tasks.list_tasks(user_id, 50)
    except Exception:
        tasks = []
    for t in tasks:
        status = t.get("status")
        if status in ("queued", "running"):
            in_progress.append(
                {
                    "task_id": t.get("task_id"),
                    "type": t.get("type"),
                    "type_label": research_engine.TYPE_LABEL.get(t.get("type"), t.get("type")),
                    "target": t.get("target", ""),
                    "code": t.get("code", ""),
                    "status": status,
                    "session_id": t.get("session_id"),
                    "created_at": t.get("created_at"),
                }
            )
        elif status == "done" and t.get("report") and t.get("session_id"):
            session_by_report.setdefault(t["report"], t["session_id"])

    for r in reports:
        r["session_id"] = session_by_report.get(r["name"])

    return {
        "reports": reports,
        "in_progress": in_progress,
        "types": list(research_engine.TYPE_LABEL.keys()),
    }


@app.get("/api/research/process/{session_id}")
def research_process(session_id: str, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    """读某次 wecode 会话的「研究过程」（thinking/tool_use/text 提炼 markdown）。"""
    if not re.fullmatch(r"[0-9a-zA-Z-]+", session_id):
        raise HTTPException(status_code=400, detail="非法 session_id")
    from debate_engine import extract_session_process

    return extract_session_process(session_id)


@app.get("/api/research/reports/{name}")
def research_report(name: str, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    """读单份报告原文（markdown）。"""
    try:
        return research_engine.read_report(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ResearchTaskRequest(BaseModel):
    type: str
    target: str = ""
    code: Optional[str] = None
    market: str = "A股"
    force: bool = False


@app.post("/api/research/tasks")
def research_task_create(
    req: ResearchTaskRequest, user_id: str = Depends(_get_current_user)
) -> Dict[str, Any]:
    """触发一次研究。命中新鲜报告则跳过（skipped），否则入队后台执行。"""
    rtype = req.type
    if rtype not in research_engine.VALIDITY_DAYS and rtype != "verdict":
        raise HTTPException(status_code=400, detail=f"未知研究类型: {rtype}")

    # 去重：仅四类研究有有效期规则；verdict（综合研判）每次手动触发，不自动去重
    if not req.force and rtype in research_engine.VALIDITY_DAYS:
        fresh = research_engine.find_fresh_report(rtype, req.target, req.code or "")
        if fresh:
            return {
                "ok": True,
                "skipped": True,
                "report": fresh["name"],
                "fresh_until": fresh["date"],
            }

    # verdict 需要 user_id 以先把模拟账户投影到 yaml 再 review
    payload = {"user_id": user_id} if rtype == "verdict" else None
    try:
        task_id = research_tasks.enqueue(
            user_id, rtype, req.target, req.code or "", req.market, payload
        )
    except Exception as exc:  # Mongo 不可用等
        raise HTTPException(status_code=503, detail=f"研究任务队列不可用: {exc}") from exc
    return {"ok": True, "task_id": task_id, "status": "queued"}


@app.get("/api/research/tasks")
def research_tasks_list(
    limit: int = 20, user_id: str = Depends(_get_current_user)
) -> Dict[str, Any]:
    try:
        tasks = research_tasks.list_tasks(user_id, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"研究任务队列不可用: {exc}") from exc
    return {"tasks": tasks}


@app.get("/api/research/tasks/{task_id}")
def research_task_get(task_id: str, user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        task = research_tasks.get_task(task_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"研究任务队列不可用: {exc}") from exc
    if not task or task.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


# ═══════════════════════════ 投资助手（多轮对话会话） ═══════════════════════════

class DebateSessionCreate(BaseModel):
    title: str = ""
    code: Optional[str] = None
    market: str = "A股"
    first_message: str = ""
    mode: str = "normal"
    web: bool = True


class DebateMessageCreate(BaseModel):
    content: str = Field(..., description="本轮用户问题")
    mode: str = "normal"
    web: bool = True


@app.post("/api/debate/sessions")
def debate_session_create(
    req: DebateSessionCreate, user_id: str = Depends(_get_current_user)
) -> Dict[str, Any]:
    """新建话题（可选绑定标的 + 首问）。带首问则入队一轮辩论任务，返回 task_id 供轮询。"""
    if not req.first_message and not req.title:
        raise HTTPException(status_code=400, detail="请提供话题标题或首问内容")
    try:
        return debate_session.create_session(
            user_id, req.title, req.code or "", req.market, req.first_message, req.mode, req.web
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"会话存储不可用: {exc}") from exc


@app.get("/api/debate/sessions")
def debate_sessions_list(user_id: str = Depends(_get_current_user)) -> Dict[str, Any]:
    try:
        return {"sessions": debate_session.list_sessions(user_id)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"会话存储不可用: {exc}") from exc


@app.get("/api/debate/sessions/{session_id}")
def debate_session_get(
    session_id: str, user_id: str = Depends(_get_current_user)
) -> Dict[str, Any]:
    try:
        s = debate_session.get_session(session_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"会话存储不可用: {exc}") from exc
    if not s or s.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return s


@app.post("/api/debate/sessions/{session_id}/messages")
def debate_message_create(
    session_id: str,
    req: DebateMessageCreate,
    user_id: str = Depends(_get_current_user),
) -> Dict[str, Any]:
    """发一轮：追加 user 消息 → 入队辩论任务（复用研究 worker 队列）。"""
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="问题内容不能为空")
    try:
        s = debate_session.get_session(session_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"会话存储不可用: {exc}") from exc
    if not s or s.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")

    try:
        debate_session.add_message(session_id, "user", req.content.strip())
        task_id = research_tasks.enqueue(
            user_id,
            "debate_turn",
            target=session_id,
            code=s.get("code", ""),
            market=s.get("market", "A股"),
            payload={"session_id": session_id, "question": req.content.strip(), "mode": req.mode, "web": req.web},
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"任务队列不可用: {exc}") from exc
    return {"ok": True, "task_id": task_id}


# 静态 serve 前端构建产物（web/dist）。SPA 回退：非 /api 路径统一返回 index.html，
# 使 react-router 子路由（如 /portfolio）刷新不 404。首次需 `cd web && npm install && npm run build`。
_DIST = os.path.join(PROJECT_ROOT, "web", "dist")
if os.path.isdir(_DIST):
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    assets_dir = os.path.join(_DIST, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="API 端点不存在")
        # 命中真实文件（favicon、manifest 等）直接返回，否则回退 index.html
        file_path = os.path.join(_DIST, full_path)
        if full_path and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(_DIST, "index.html"))
