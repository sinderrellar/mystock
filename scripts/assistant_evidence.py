#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
assistant_evidence.py —— 投资助手「深度分析」取数 helper（供 wecode 主代理用 Bash 调用）

主代理在 skills/assistant/agent.md 的 Step 0 识别出标的代码后，用 Bash 调本脚本取该股证据底座：
    python3 scripts/assistant_evidence.py <code> [--market A股] [--session <sid>]

行为：
1. 复用 debate_session._build_evidence(code, market) 取定量数据底座 + 相关新鲜报告摘要。
2. 可选 --session：取数后回写 session 的 code/market/evidence（$set，只改不删），
   供前端展示标的 + 后续轮复用，避免每轮重复取数。
3. stdout 输出文本版证据底座（_evidence_text），主代理直接读，不必解析 JSON。

说明：取数据底座走本地工程数据（MongoDB + 行情源多级 fallback），不是 LLM 联网搜索，
因此不受「联网开关」限制——联网关时也应先取底座再作答。
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

import debate_session


def main() -> int:
    parser = argparse.ArgumentParser(description="取一只股票的证据底座")
    parser.add_argument("code", help="股票代码（如 300750）")
    parser.add_argument("--market", default="A股", help="市场（默认 A股）")
    parser.add_argument("--session", default="", help="可选：会话 ID，取数后回写 session")
    args = parser.parse_args()

    code = (args.code or "").strip()
    if not code:
        sys.stderr.write("错误：未提供股票代码\n")
        return 1

    evidence = debate_session._build_evidence(code, args.market)

    # 可选回写 session（只 $set，遵守禁删约束；失败不阻断取数）
    if args.session:
        try:
            debate_session._get_mongo()[debate_session.COL_SESSIONS].update_one(
                {"session_id": args.session},
                {"$set": {"code": code, "market": args.market, "evidence": evidence}},
            )
        except Exception as exc:  # 回写失败不阻断主代理取数
            sys.stderr.write(f"回写 session 失败（不影响取数）: {exc}\n")

    # 输出文本版证据底座
    name = ""
    if evidence.get("data") and isinstance(evidence["data"], dict):
        name = (evidence["data"].get("asset") or {}).get("name", "") or code
    print(f"【标的】{name}（{code}，{args.market}）")
    print()
    print(debate_session._evidence_text(evidence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
