#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
assistant_action.py —— 投资助手「操作」helper（供 wecode 主代理用 Bash 调用）

用户下达操作指令时，主代理用 Bash 调本脚本**代替用户执行平台操作**：

    python3 scripts/assistant_action.py <子命令> --session <sid> [参数...]

子命令：
  place_order      --code <6位> --side buy|sell --quantity <股数>
  update_position  --code <6位> [--stop-loss X] [--take-profit X] [--thesis "…"]
  add_watchlist    --code <6位> [--thesis "…"]
  remove_watchlist --code <6位>
  run_review       （无标的参数，跑该用户组合复盘）
  account          （只读：查账户现金/持仓/观察池）

关键设计（用户隔离的构造性保证）：
1. user_id **永远**从 --session 解析（debate_session.get_session），agent 只持有
   session_id，拿不到、也传不进别人的 user_id —— 与 assistant_evidence.py 同款。
2. 每笔写操作 + run_review 落 sim_agent_operations 审计（只 insert，遵守 CLAUDE.md 禁删）。
3. 无硬护栏：参数只做软校验，失败返回清晰错误让 agent 转述，不硬拦、不静默降级。
4. stdout 输出可读文本（中文结果 + 软提醒），主代理直接读并转述给用户。

说明：本脚本走本地工程数据（MongoDB + 行情源），不受「联网开关」限制。
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.join(PROJECT_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

# 系统 python3 常缺 pymongo：缺依赖就 re-exec 用项目 venv python（一次性，防死循环）
try:
    import pymongo  # noqa: F401
except ImportError:
    _venv = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
    if os.path.exists(_venv) and os.path.abspath(sys.executable) != os.path.abspath(_venv):
        os.execv(_venv, [_venv] + sys.argv)
    sys.stderr.write("缺少 pymongo 且 venv python 不存在\n")
    sys.exit(1)

import argparse
import json
import uuid
from datetime import datetime

import debate_session
import sim_trade

# 操作审计集合（只增不改删）
COL_OPS = "sim_agent_operations"

# 「未传」与「显式清空」的区分（对称 sim_trade._UPDATE_UNSET 语义）
_CLEAR_TOKENS = ("", "null", "none", "clear", "清空")


def _resolve_user(session_id: str) -> str:
    """从 session 解析 user_id —— 用户隔离的构造性保证（agent 只能传 session_id）。"""
    if not session_id:
        sys.stderr.write("错误：缺少 --session（会话 ID），无法确定操作用户\n")
        sys.exit(2)
    sdoc = debate_session.get_session(session_id)
    if not sdoc:
        sys.stderr.write(f"错误：会话不存在 {session_id}\n")
        sys.exit(2)
    user_id = (sdoc.get("user_id") or "").strip()
    if not user_id:
        sys.stderr.write(f"错误：会话 {session_id} 未绑定用户（user_id 为空）\n")
        sys.exit(2)
    return user_id


def _audit(user_id: str, session_id: str, op_type: str, params, result, status: str) -> None:
    """写审计（只 insert，遵守禁删；审计失败不阻断操作，但应尽量成功）。"""
    try:
        db = sim_trade._get_mongo()
        db[COL_OPS].insert_one({
            "op_id": uuid.uuid4().hex,
            "user_id": user_id,
            "session_id": session_id,
            "op_type": op_type,
            "params": params,
            "result": result,
            "status": status,  # ok / error
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"审计写入失败（不影响操作）: {exc}\n")


def _opt_price(v: str):
    """解析止损/止盈：None→未传；空/null/清空→None（清空）；否则 float。"""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _CLEAR_TOKENS:
        return None
    return float(s)


def _out(text: str) -> None:
    print(text)


# ─────────────────────────── 子命令 ───────────────────────────

def cmd_place_order(args) -> int:
    user_id = _resolve_user(args.session)
    params = {"code": args.code, "side": args.side, "quantity": args.quantity}
    try:
        r = sim_trade.place_order(user_id, args.code, args.side, args.quantity)
        _audit(user_id, args.session, "place_order", params, r, "ok")
        verb = "买入" if r.get("side") == "buy" else "卖出"
        lines = [
            f"✅ 已{verb} {r.get('name') or r.get('code')}（{r.get('code')}）",
            f"  成交价 {r.get('price')} × {r.get('quantity')} 股 = {r.get('amount')} 元",
            f"  佣金 {r.get('fee')} 元，印花税 {r.get('tax', 0)} 元，剩余现金 {r.get('cash')} 元",
        ]
        if r.get("note"):
            lines.append(f"  提示：{r.get('note')}")
        _out("\n".join(lines))
        return 0
    except Exception as exc:  # noqa: BLE001
        _audit(user_id, args.session, "place_order", params, {"error": str(exc)}, "error")
        _out(f"❌ 下单失败：{exc}")
        return 1


def cmd_update_position(args) -> int:
    user_id = _resolve_user(args.session)
    kwargs = {}
    if args.stop_loss is not None:
        kwargs["stop_loss_price"] = _opt_price(args.stop_loss)
    if args.take_profit is not None:
        kwargs["take_profit_price"] = _opt_price(args.take_profit)
    if args.thesis is not None:
        t = args.thesis.strip()
        kwargs["thesis"] = None if t.lower() in _CLEAR_TOKENS else t
    params = {"code": args.code, **kwargs}
    try:
        r = sim_trade.update_position(user_id, args.code, **kwargs)
        _audit(user_id, args.session, "update_position", params, r, "ok")
        changed = [k for k in ("stop_loss_price", "take_profit_price", "thesis") if k in kwargs]
        _out(
            f"✅ 已更新 {args.code} 持仓设置（{'、'.join(changed) or '无变更'}）。\n"
            f"  新值：止损 {kwargs.get('stop_loss_price', '未改')}，止盈 {kwargs.get('take_profit_price', '未改')}，逻辑 {kwargs.get('thesis', '未改')}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        _audit(user_id, args.session, "update_position", params, {"error": str(exc)}, "error")
        _out(f"❌ 更新持仓失败：{exc}")
        return 1


def cmd_add_watchlist(args) -> int:
    user_id = _resolve_user(args.session)
    params = {"code": args.code, "thesis": args.thesis or ""}
    try:
        r = sim_trade.add_watchlist(user_id, args.code, args.thesis or "")
        _audit(user_id, args.session, "add_watchlist", params, r, "ok")
        _out(f"✅ {r.get('name') or r.get('code')}（{r.get('code')}）：{r.get('note', '已加入观察池')}")
        return 0
    except Exception as exc:  # noqa: BLE001
        _audit(user_id, args.session, "add_watchlist", params, {"error": str(exc)}, "error")
        _out(f"❌ 加观察池失败：{exc}")
        return 1


def cmd_remove_watchlist(args) -> int:
    user_id = _resolve_user(args.session)
    params = {"code": args.code}
    try:
        r = sim_trade.remove_watchlist(user_id, args.code)
        _audit(user_id, args.session, "remove_watchlist", params, r, "ok")
        _out(f"✅ {r.get('code')}：{r.get('note', '已移出观察池')}")
        return 0
    except Exception as exc:  # noqa: BLE001
        _audit(user_id, args.session, "remove_watchlist", params, {"error": str(exc)}, "error")
        _out(f"❌ 移出观察池失败：{exc}")
        return 1


def cmd_run_review(args) -> int:
    user_id = _resolve_user(args.session)
    params = {}
    try:
        # 先同步持仓/观察池到该用户投影 yaml，确保 review 读到最新组合
        sim_trade.sync_to_yaml(user_id)
        from portfolio_strategy import PortfolioStrategy

        ps = PortfolioStrategy(portfolio_path=sim_trade.user_portfolio_path(user_id))
        report = ps.review()
        summary = report.get("summary") or {}
        alerts = report.get("alerts") or []
        _audit(user_id, args.session, "run_review", params, {"ok": True}, "ok")
        lines = [
            f"✅ 组合复盘完成（截至 {summary.get('generated_at', '')}）",
            f"  现金 {summary.get('cash')} 元 · 持仓市值 {summary.get('position_value')} 元 · 总资产 {summary.get('total_assets')} 元",
            f"  持仓 {summary.get('position_count')} 只 · 未实现盈亏 {summary.get('unrealized_pnl')} 元（{summary.get('unrealized_pnl_pct')}%）",
        ]
        if alerts:
            lines.append("  告警（提醒建议，非硬约束）：")
            for a in alerts[:10]:
                msg = a.get("message") if isinstance(a, dict) else str(a)
                lines.append(f"    - {msg}")
        _out("\n".join(lines))
        return 0
    except Exception as exc:  # noqa: BLE001
        _audit(user_id, args.session, "run_review", params, {"error": str(exc)}, "error")
        _out(f"❌ 组合复盘失败：{exc}")
        return 1


def cmd_account(args) -> int:
    """只读：查账户现金/持仓/观察池（不落审计，纯查询）。"""
    user_id = _resolve_user(args.session)
    try:
        acct = sim_trade.get_account(user_id)
        watch = sim_trade.get_watchlist(user_id)
        lines = [
            f"💰 账户 {user_id}",
            f"  现金 {acct.get('cash')} 元 · 持仓市值 {acct.get('position_value')} 元 · 总资产 {acct.get('total_assets')} 元",
            f"  未实现盈亏 {acct.get('unrealized_pnl')} 元（{acct.get('unrealized_pnl_pct')}%）",
        ]
        positions = acct.get("positions") or []
        if positions:
            lines.append("  持仓：")
            for p in positions:
                lines.append(
                    f"    - {p.get('name') or p.get('code')}（{p.get('code')}）{p.get('quantity')} 股"
                    f"（可卖 {p.get('available_qty')}）成本 {p.get('cost_price')} 现价 {p.get('current_price')}"
                    f" 盈亏 {p.get('unrealized_pnl_pct')}%"
                )
        else:
            lines.append("  持仓：无")
        if watch:
            lines.append("  观察池：" + "、".join(f"{w.get('name') or w.get('code')}({w.get('code')})" for w in watch))
        else:
            lines.append("  观察池：无")
        _out("\n".join(lines))
        return 0
    except Exception as exc:  # noqa: BLE001
        _out(f"❌ 查账户失败：{exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="投资助手操作 helper（替用户执行平台操作）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # 每个子命令都要能带 --session（子命令后接），故用 parent parser 统一注入
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", required=True, help="会话 ID（据此解析操作用户，用户隔离关键）")

    p = sub.add_parser("place_order", help="下单（buy/sell）", parents=[common])
    p.add_argument("--code", required=True, help="股票代码（6 位 A 股）")
    p.add_argument("--side", required=True, help="buy 或 sell")
    p.add_argument("--quantity", type=int, required=True, help="股数（正整数）")
    p.set_defaults(fn=cmd_place_order)

    p = sub.add_parser("update_position", help="更新持仓止损/止盈/逻辑", parents=[common])
    p.add_argument("--code", required=True, help="股票代码")
    p.add_argument("--stop-loss", default=None, help="止损价（null/清空 表示清除）")
    p.add_argument("--take-profit", default=None, help="止盈价（null/清空 表示清除）")
    p.add_argument("--thesis", default=None, help="买入逻辑（null/清空 表示清除）")
    p.set_defaults(fn=cmd_update_position)

    p = sub.add_parser("add_watchlist", help="加入观察池", parents=[common])
    p.add_argument("--code", required=True, help="股票代码")
    p.add_argument("--thesis", default="", help="观察理由")
    p.set_defaults(fn=cmd_add_watchlist)

    p = sub.add_parser("remove_watchlist", help="移出观察池", parents=[common])
    p.add_argument("--code", required=True, help="股票代码")
    p.set_defaults(fn=cmd_remove_watchlist)

    p = sub.add_parser("run_review", help="跑该用户组合复盘", parents=[common])
    p.set_defaults(fn=cmd_run_review)

    p = sub.add_parser("account", help="查账户现金/持仓/观察池（只读）", parents=[common])
    p.set_defaults(fn=cmd_account)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
