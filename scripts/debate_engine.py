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
from typing import Any, Dict, List, Optional

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
    提取喂给辩论 agent 的确定性字段：
      - trend_signal：RSI/MACD/KDJ/均线/量价/支撑压力（market_data_provider.get_trend_signal）
      - quote_snapshot：PE/PB/市值/股息率/52周高低点（估值核心）
      - llm_context：四因子(价值/成长/质量/动量)+事件情绪，purpose="llm_input"
      - moneyflow：主力/大单资金流（analyze_stock 已算，此前漏透传）
      - news：新闻原文（标题+摘要，截断控制长度）
      - tushare_signals：回购/股东户数/两融/业绩预告/财务
      - cycle：行业周期(cycle_score)+趋势/宽度/资金流+业务组因子分（stock_factors 预计算）
      - data_quality：口径一致性自检（52周/MACD/业绩预告/换手率等主动标注）
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

    # 资金流（主力/大单/超大单，tushare，analyze_stock 已算）
    if isinstance(report.get("moneyflow"), dict):
        base["moneyflow"] = report["moneyflow"]

    # 新闻（截断：最多 5 条，正文压缩到 160 字，避免底座膨胀）
    news = report.get("news") or []
    if isinstance(news, list) and news:
        base["news"] = [
            {
                "title": (n.get("title") or "")[:120],
                "summary": (n.get("content") or "")[:160],
                "publish_time": n.get("publish_time"),
                "source": n.get("source"),
            }
            for n in news[:5]
        ]

    # Tushare 专属信号（回购/股东户数/两融/业绩预告/财务，港股含南向）
    if isinstance(report.get("tushare_signals"), dict):
        base["tushare_signals"] = report["tushare_signals"]

    # 自身历史 PE 分位（stock_signals.pe_percentile）
    if report.get("pe_percentile_self") is not None:
        base["pe_percentile_self"] = report["pe_percentile_self"]

    # 行业周期 + 趋势/宽度/资金流 + 业务组因子分（stock_factors 预计算，review 同源）
    base["cycle"] = _load_cycle_context(ps, code)

    # 名称/行业（供页面标题与流派背景使用）
    base["asset"] = {
        "code": code,
        "name": report.get("name", code),
        "market": market,
        "industry": report.get("industry_peers") and report.get("industry_peers"),
    }

    # 口径一致性自检：主动标注脏数据/矛盾，不靠 LLM 碰运气发现
    base["data_quality"] = _data_self_check(base)
    return base


def _load_cycle_context(ps, code: str) -> Dict[str, Any]:
    """从 stock_factors 预计算表取行业周期/趋势/宽度/资金流 + 业务组因子分。

    与 portfolio_strategy.review() 的 alpha_context（:615-626）同源，保证口径一致。
    读取失败返回空 dict，不阻断底座。
    """
    out: Dict[str, Any] = {}
    try:
        fac_doc = ps.data_provider.mongo.db["stock_factors"].find_one(
            {"code": str(code).strip(), "trade_date": {"$exists": True}},
            sort=[("trade_date", -1)],
        )
        if not fac_doc:
            return out
        group = fac_doc.get("group", "")
        gfac = (fac_doc.get("factors") or {}).get(group, {}) or {}
        out = {
            "group": group,
            "trade_date": fac_doc.get("trade_date"),
            "cycle_score": fac_doc.get("cycle_score"),
            "turnaround_score": fac_doc.get("turnaround_score"),
            "industry_trend": fac_doc.get("industry_trend"),
            "industry_breadth": fac_doc.get("industry_breadth"),
            "industry_flow": fac_doc.get("industry_flow"),
            "group_composite_score": gfac.get("composite_score"),
            "group_factor_scores": gfac.get("factor_scores"),
        }
    except Exception:
        pass
    return out


def _data_self_check(base: Dict[str, Any]) -> List[Dict[str, Any]]:
    """数据底座口径一致性自检，返回异常清单（空列表=未发现异常）。

    目的：把「52周高低冲突」「MACD 全0」「forecast 越界」等已知脏数据在底座侧主动
    标注出来，而非每次靠 LLM 自己撞见（兆易创新那次两个 agent 各自发现一遍）。
    """
    checks: List[Dict[str, Any]] = []

    qs = (base.get("quote_snapshot") or {}).get("metrics") or {}
    ts = base.get("trend_signal") or {}
    ti = ts.get("technical_indicators") or {}
    rp = ti.get("range_position") or {}

    # 1. 52周高低 vs 120日区间交叉：52周（约250交易日）应 ⊇ 120日
    high_52w = qs.get("fifty_two_week_high")
    low_52w = qs.get("fifty_two_week_low")
    high_120 = (rp.get("120d") or {}).get("high")
    low_120 = (rp.get("120d") or {}).get("low")
    if high_52w and high_120 and high_52w < high_120:
        checks.append({"field": "52周高低", "severity": "warning",
                       "message": f"52周高 {high_52w} < 120日高 {high_120}，口径矛盾（52周应包含120日），引用回撤幅度需谨慎"})
    if low_52w and low_120 and low_52w > low_120:
        checks.append({"field": "52周高低", "severity": "warning",
                       "message": f"52周低 {low_52w} > 120日低 {low_120}，口径矛盾"})

    # 2. MACD 三项全 0 → 未有效计算（历史 bug：_ema_series 递推错误曾致全市场全0）
    macd = ti.get("macd") or {}
    if macd and macd.get("dif") == 0 and macd.get("dea") == 0 and macd.get("histogram") == 0:
        checks.append({"field": "MACD", "severity": "warning",
                       "message": "MACD 三项全 0，疑似未有效计算，引用需谨慎"})

    # 3. 业绩预告越界（如 forecast=1099% 脏数据）
    fc = (base.get("tushare_signals") or {}).get("forecast") or {}
    p_min = fc.get("p_change_min")
    p_max = fc.get("p_change_max")
    if p_min is not None and abs(float(p_min)) >= 1000:
        checks.append({"field": "业绩预告", "severity": "warning",
                       "message": f"业绩预告增速 {p_min}%~{p_max}% 异常（疑似脏数据），不采信"})

    # 4. 换手率缺失
    if "turnover_rate" in qs and not qs.get("turnover_rate"):
        checks.append({"field": "换手率", "severity": "info", "message": "换手率缺失"})

    # 5. 因子口径不一致：实时(llm_context) vs 预计算(stock_factors 业务组) 差异显著
    live_comp = (((base.get("llm_context") or {}).get("signal_contexts") or {})
                 .get("factor", {}).get("facts", {}).get("composite_score"))
    pre_comp = (base.get("cycle") or {}).get("group_composite_score")
    if live_comp is not None and pre_comp is not None and abs(float(live_comp) - float(pre_comp)) >= 0.15:
        checks.append({"field": "因子口径", "severity": "warning",
                       "message": f"实时因子综合 {live_comp} vs 预计算(业务组)综合 {pre_comp} 差异显著，口径不一致，需注明采用哪套"})

    # 6. 预计算因子财务缺失：stock_factors 最新行 fin_report_period 为空 → value/growth/quality 全 0
    gfs = (base.get("cycle") or {}).get("group_factor_scores") or {}
    if gfs and gfs.get("value") == 0 and gfs.get("growth") == 0 and gfs.get("quality") == 0:
        checks.append({"field": "预计算因子", "severity": "warning",
                       "message": "stock_factors 最新行业务组因子分 value/growth/quality 全 0（财务数据缺失），请以实时 llm_context 因子为准"})

    # 7. RSI 与 KDJ 方向背离：同为 0-100 动量指标，正常应落在相近区间；
    #    拉开 ≥40 点（一边偏强一边偏弱）大概率是方向性 bug 或重大背离，需标注存疑。
    #    （历史 bug：_rsi 未反转倒序 closes，Wilder 平滑沿最新→最旧递推，RSI 反向污染，
    #    如 300750 曾算出 RSI=68.41 vs KDJ-K≈18 超卖，正确应为超卖 RSI≈26）
    rsi14 = ti.get("rsi14")
    k_val = (ti.get("kdj") or {}).get("k")
    if isinstance(rsi14, (int, float)) and isinstance(k_val, (int, float)) and rsi14 == rsi14 and k_val == k_val:
        if abs(float(rsi14) - float(k_val)) >= 40:
            checks.append({"field": "RSI/KDJ背离", "severity": "warning",
                           "message": f"RSI14={float(rsi14):.2f} 与 KDJ-K={float(k_val):.2f} 相差 {abs(float(rsi14) - float(k_val)):.1f} 点（≥40），方向疑似背离，引用需谨慎"})

    return checks


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
