#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debate_engine.py —— 「投资智辩」辩论引擎（唯一调用 wecode 的地方）

职责：
1. get_data_base(code, market) —— 从 scripts 定量层拿真实数据底座（现价/趋势指标/估值/四因子），
   作为三流派辩论的事实基础。这是「财经数据验证」的核心，数据来自工程已有的
   腾讯→新浪→东财→AKShare→Yahoo 多级 fallback + MongoDB，不是 LLM 现编。
2. run_agent(prompt, timeout) —— 用 subprocess 调 `wecode -p` 执行辩论 agent
   （wecode = Claude Code CLI 封装，作为一个 agent 运行）。
3. clean_output / parse_json —— 清理 wecode 输出里的模型 warning 行，定位并解析 JSON。

后续若换 LLM 实现，只改 run_agent 一处，其他逻辑不动。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, Optional

# 项目根目录（scripts/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 让 scripts/ 可被 import
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.join(PROJECT_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

# wecode 可执行文件（Claude Code CLI 封装）
WECODE_BIN = os.environ.get("WECODE_BIN", "/root/.wecode/bin/wecode")


def get_data_base(code: str, market: str = "A股") -> Dict[str, Any]:
    """从 scripts 定量层拿一只股票的数据底座。

    复用 PortfolioStrategy.analyze_stock()（scripts/portfolio_strategy.py:695），
    只提取喂给辩论 agent 的确定性字段：
      - trend_signal：RSI/MACD/KDJ/均线/量价/支撑压力（market_data_provider.get_trend_signal）
      - quote_snapshot：PE/PB/市值/股息率/52周高低点（估值核心）
      - llm_context：四因子(价值/成长/质量/动量)+事件情绪，purpose="llm_input"
    """
    from portfolio_strategy import PortfolioStrategy

    ps = PortfolioStrategy()
    report = ps.analyze_stock(str(code).strip(), market)

    base: Dict[str, Any] = {}
    if isinstance(report.get("price"), dict):
        base["price"] = report["price"]
    if isinstance(report.get("trend_signal"), dict):
        base["trend_signal"] = report["trend_signal"]
    if isinstance(report.get("quote_snapshot"), dict):
        base["quote_snapshot"] = report["quote_snapshot"]
    ss = report.get("strategy_signals")
    if isinstance(ss, dict) and ss.get("llm_context"):
        base["llm_context"] = ss["llm_context"]

    # 名称/行业（供页面标题与流派背景使用）
    base["asset"] = {
        "code": code,
        "name": report.get("name", code),
        "market": market,
        "industry": report.get("industry_peers") and report.get("industry_peers"),
    }
    return base


def clean_output(text: str) -> str:
    """清理 wecode 输出里的非结果行。

    wecode 会在 stdout 开头打模型 warning（例如
    `"deepseek-v4-pro" is not a model this version of Claude Code recognizes...`
    和 `[claude-code:unrecognized_model] {...}`）。这些不是结果，解析前要剔除。
    """
    if not text:
        return ""
    lines = text.splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        # 跳过已知 warning 前缀
        if stripped.startswith('"deepseek-') and "is not a model" in stripped:
            continue
        if stripped.startswith("[claude-code:"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def parse_json(text: str) -> Dict[str, Any]:
    """从 wecode 输出里定位并解析 JSON。

    容错策略（参考 scripts/event_driven_strategy.py:504-516 的解析模式）：
    1. 先剥掉 ```json ... ``` 代码围栏；
    2. 再定位第一个 '{' 到最后一个 '}' 的子串，json.loads；
    3. 失败则抛异常，由上层决定降级（不静默吞掉）。
    """
    cleaned = clean_output(text)

    # 1. 剥 markdown 代码围栏
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    # 2. 定位最外层 JSON 对象
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("wecode 输出中未找到 JSON 对象")

    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        # 尝试去掉首尾可能的逗号/注释等再做一次
        raise ValueError(f"JSON 解析失败: {exc}") from exc


def run_agent(prompt: str, timeout: int = 600) -> str:
    """用 wecode -p 非交互执行一个 agent prompt，返回 stdout 文本。

    wecode 是 Claude Code CLI 的封装，`-p/--print` 是非交互模式。
    工作目录切到项目根，确保 agent.md 里的 skills/ 相对路径可解析。
    """
    try:
        proc = subprocess.run(
            [WECODE_BIN, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=PROJECT_ROOT,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"wecode 执行超时（>{timeout}s）")
    except FileNotFoundError:
        raise RuntimeError(f"wecode 不存在: {WECODE_BIN}，请确认路径")

    if proc.returncode != 0:
        raise RuntimeError(
            f"wecode 退出码 {proc.returncode}: {clean_output(proc.stderr or '')[:500]}"
        )
    return proc.stdout or ""


def _session_dir() -> str:
    """wecode/Claude Code 会话 JSONL 的存储目录。

    Claude Code 把 cwd 映射为 ~/.claude/projects/<cwd 斜杠转短横>/，
    如 /data2/liuyu20/mystock → -data2-liuyu20-mystock。
    """
    slug = PROJECT_ROOT.replace("/", "-")
    return os.path.expanduser(os.path.join("~", ".claude", "projects", slug))


def run_agent_with_session(prompt: str, timeout: int = 600, on_session=None, session_id: Optional[str] = None) -> tuple:
    """run_agent + 确定性 session_id（并发安全）。

    用 `--session-id` 让 wecode 把会话写到确定路径 <session_id>.jsonl，启动后
    轮询该文件一出现就回调 on_session（供 worker 尽早落库 session_id，让前端在
    running 期间就能拉到「研究过程」）。

    不指定 session_id 时自动生成一个 uuid4。并发多个 wecode 时各自 session 文件
    互不干扰，不再依赖「目录 diff 猜新文件」（旧实现并发下会互相抢错会话）。
    """
    if session_id is None or not re.fullmatch(r"[0-9a-fA-F-]{8,36}", session_id):
        session_id = str(uuid.uuid4())
    fpath = os.path.join(_session_dir(), f"{session_id}.jsonl")
    deadline = time.time() + timeout

    try:
        proc = subprocess.Popen(
            [WECODE_BIN, "-p", "--session-id", session_id, prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=PROJECT_ROOT,
        )
    except FileNotFoundError:
        raise RuntimeError(f"wecode 不存在: {WECODE_BIN}，请确认路径")

    def _notify() -> None:
        if on_session:
            try:
                on_session(session_id)
            except Exception:
                pass

    # 1. 轮询等待会话文件出现（尽早拿到 session_id 落库）
    while time.time() < deadline and proc.poll() is None and not os.path.isfile(fpath):
        time.sleep(1)
    if os.path.isfile(fpath):
        _notify()

    # 2. 等待进程结束
    try:
        remaining = max(1, deadline - time.time())
        stdout, stderr = proc.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise TimeoutError(f"wecode 执行超时（>{timeout}s）")

    # 3. 兜底：进程秒结束没轮到轮询，结束后再确认一次
    if os.path.isfile(fpath):
        _notify()

    if proc.returncode != 0:
        raise RuntimeError(
            f"wecode 退出码 {proc.returncode}: {clean_output(stderr or '')[:500]}"
        )
    return stdout or "", session_id


def _summarize_tool(name: str, inp: Dict[str, Any]) -> str:
    """把一个 tool_use 的输入压成一行可读摘要（研究过程展示用）。"""
    n = (name or "").lower()
    if n in ("read", "write", "edit"):
        return f"`{inp.get('file_path') or inp.get('path') or '?'}`"
    if n == "bash":
        return f"```bash\n{str(inp.get('command') or '')[:200]}\n```"
    if n in ("agent", "task"):
        st = inp.get("subagent_type") or inp.get("type") or "subagent"
        desc = str(inp.get("description") or inp.get("prompt") or "")[:200]
        return f"子代理 `{st}`：{desc}"
    if n == "taskcreate":
        return f"`{inp.get('subject') or ''}`"
    if n == "taskupdate":
        return f"任务 `{inp.get('taskId', '')[:8]}…` → {inp.get('status') or inp.get('status', '?')}"
    if n in ("grep", "glob"):
        return f"`{inp.get('pattern') or inp.get('path') or '?'}`"
    if n in ("websearch", "webfetch"):
        return f"`{inp.get('query') or inp.get('url') or '?'}`"
    s = json.dumps(inp, ensure_ascii=False, default=str)
    return s[:300]


def extract_session_process(session_id: str, max_steps: int = 200) -> Dict[str, Any]:
    """把 wecode 会话 JSONL 提炼成可读的「研究过程」markdown。

    只提取 assistant 的 thinking / tool_use / text 块（跳过 user 的 tool_result 噪声）；
    末行 last-prompt 视为会话完成标记。
    返回 {session_id, exists, done, markdown}。
    """
    fpath = os.path.join(_session_dir(), f"{session_id}.jsonl")
    # 防路径穿越：session_id 只允许 uuid/hex 风格（含 -）
    if not re.fullmatch(r"[0-9a-zA-Z-]+", session_id):
        return {"session_id": session_id, "exists": False, "done": False, "markdown": ""}
    if not os.path.isfile(fpath):
        return {"session_id": session_id, "exists": False, "done": False, "markdown": ""}

    steps: list = []
    done = False
    with open(fpath, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "last-prompt":
                done = True
                continue
            if t != "assistant":
                continue
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "thinking":
                    txt = (block.get("thinking") or "").strip()
                    if txt:
                        steps.append(f"**🧠 思考**\n\n{txt[:1500]}")
                elif btype == "tool_use":
                    tname = block.get("name") or "tool"
                    steps.append(
                        f"**🔧 调用工具 `{tname}`**\n\n{_summarize_tool(tname, block.get('input') or {})}"
                    )
                elif btype == "text":
                    txt = (block.get("text") or "").strip()
                    if txt:
                        steps.append(txt)

    markdown = "\n\n---\n\n".join(steps[-max_steps:])
    return {"session_id": session_id, "exists": True, "done": done, "markdown": markdown}


def debate(idea: str, code: str, market: str = "A股") -> Dict[str, Any]:
    """端到端辩论入口：拿数据底座 → 组 prompt → 调 wecode → 解析 JSON。

    返回 debate/agent.md 定义的结构化 JSON（idea/schools/fact_check/synthesis/page）。
    """
    base = get_data_base(code, market)
    name = (base.get("asset") or {}).get("name", code)

    prompt = (
        f"读 {os.path.join(PROJECT_ROOT, 'skills', 'debate', 'agent.md')}，"
        f"严格按它执行下面的任务。你的工作目录是 {PROJECT_ROOT}，"
        f"agent.md 里所有 skills/ 相对路径都基于这个目录。\n\n"
        f"【投资想法】\n{idea}\n\n"
        f"【标的】\n代码：{code}，市场：{market}，名称：{name}\n\n"
        f"【数据底座】（后端 scripts 定量层已算好的真实数据 JSON）\n"
        f"{json.dumps(base, ensure_ascii=False, default=str)}\n\n"
        f"现在按 agent.md 的 Step 0/1/2/3 完整执行，最终只输出一段 ```json 代码块。"
    )

    raw = run_agent(prompt)
    return parse_json(raw)


if __name__ == "__main__":
    # 命令行冒烟：python3 scripts/debate_engine.py "想法" 代码 [市场]
    import argparse

    parser = argparse.ArgumentParser(description="投资智辩辩论引擎冒烟")
    parser.add_argument("idea", help="投资想法")
    parser.add_argument("code", help="股票代码")
    parser.add_argument("--market", "-m", default="A股", help="市场")
    args = parser.parse_args()

    result = debate(args.idea, args.code, args.market)
    print(json.dumps(result, ensure_ascii=False, indent=2))
