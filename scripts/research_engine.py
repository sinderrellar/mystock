#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
research_engine.py —— 「深度研究」引擎（纯函数层，无 FastAPI/Mongo）

职责：
1. 报告库：解析 reports/ 目录下研究报告的文件名 → {type, target, date}，
   套用 CLAUDE.md 阶段3.0 的有效期规则（VALIDITY_DAYS），算出 fresh/expired。
2. 研究检索：find_fresh_report（命中新鲜报告则跳过重复研究）、
   find_reports_for_target（给投资智辩/综合研判检索「证据底座」）。
3. 归档名映射：archive_filename（新报告统一命名，保证精确命中）。
4. （后续 phase）build_prompt / run_research / run_verdict / CLI 定期入口。

设计要点：
- 报告全局共享（reports/ 目录），本模块只读文件系统，不写 Mongo。
- 日期提取「末尾锚定」优先，兼容 YYYY-MM-DD / YYYYMMDD[_HHMM] / 裸四位年。
- 类型分类「顺序敏感」：verdict → chip/筹码 → 估值/valuation → industry/行业/板块/金属
  → investment/research → other（拉丁词用「非字母数字」边界防 investment vs industry 混淆；
  不能用 \\b，因为 Python 的 \\w 含下划线，`_` 做分隔符时 \\b 会失效）。
- 旧报告命名不统一（investment_research_ / industrial_ / 估值研究_ / research_ /
  chip_analysis，日期 2026-06-11 与 20260608 混用），best-effort 归类，other 不参与有效期。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

# 项目根目录（scripts/ 的上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 让 scripts/ 可被 import（复用 debate_engine / sim_trade）
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if os.path.join(PROJECT_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

# ── 有效期（唯一真相源，来自 CLAUDE.md 阶段3.0）──────────────────────────
VALIDITY_DAYS: Dict[str, int] = {
    "investment_research": 14,   # 个股深度：财报季跨度
    "industry_research": 14,     # 行业研究：格局变化慢
    "stock_value_analyse": 30,   # 估值分析：DCF/护城河框架稳定
    "chip_analysis": 7,          # 筹码分析：每周例行
    "assistant_deep": 14,        # 助手深度分析：对话式多角度，与个股深度同跨度
}

# type → skill agent.md 路径（skills/ 相对 PROJECT_ROOT；chip 用 investment/agent.md）
SKILL_AGENT: Dict[str, str] = {
    "investment_research": os.path.join("skills", "investment_research", "agent.md"),
    "industry_research": os.path.join("skills", "industry_research", "agent.md"),
    "stock_value_analyse": os.path.join("skills", "stock_value_analyse", "agent.md"),
    "chip_analysis": os.path.join("skills", "investment", "agent.md"),
}

TYPE_LABEL: Dict[str, str] = {
    "investment_research": "公司研究",
    "industry_research": "行业研究",
    "stock_value_analyse": "估值分析",
    "chip_analysis": "筹码分析",
    "assistant_deep": "助手深度分析",
    "verdict": "综合研判",
    "other": "其他",
}


def get_reports_dir() -> str:
    """报告目录。env RESEARCH_REPORTS_DIR 覆盖（测试隔离），默认 <项目根>/reports。"""
    return os.path.abspath(
        os.environ.get("RESEARCH_REPORTS_DIR") or os.path.join(PROJECT_ROOT, "reports")
    )


# ═══════════════════════ 文件名解析 ═══════════════════════

def _mk_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_date(stem: str) -> Optional[date]:
    """从去扩展名的文件名提取日期，末尾锚定优先。

    优先级：
    1. 末尾 YYYY-MM-DD（如 2026-06-11）
    2. 末尾 YYYYMMDD[可选 _HHMM/数字]（如 20260608、20260609_1537）
    3. 末尾裸四位年（如 2022；精度只到年，用 1-1 占位）
    4. 兜底搜中间（YYYY-MM-DD 优先，再 YYYYMMDD）
    """
    # 1. 末尾 YYYY-MM-DD
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})$", stem)
    if m:
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    # 2. 末尾 YYYYMMDD（可带 _HHMM 等数字后缀）
    m = re.search(r"(\d{4})(\d{2})(\d{2})(?:_\d+)?$", stem)
    if m:
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    # 3. 末尾裸四位年
    m = re.search(r"(\d{4})$", stem)
    if m:
        y = int(m.group(1))
        if 1990 <= y <= 2100:
            return date(y, 1, 1)
    # 4. 兜底搜中间
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", stem)
    if m:
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    m = re.search(r"(\d{4})(\d{2})(\d{2})", stem)
    if m:
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    return None


def classify_type(stem_lower: str) -> str:
    """按关键词顺序敏感地分类报告类型。

    拉丁词用「非字母数字」边界 (?<![a-z0-9])…(?![a-z0-9]) 而非 \\b：
    Python 的 \\w 含下划线，文件名用 `_` 做分隔符时 \\b 会失效
    （如 investment_research 中 investment 后跟 _，\\b 判定为非边界）。
    """
    if re.search(r"综合研判|研判", stem_lower):
        return "verdict"
    if re.search(r"chip|筹码", stem_lower):
        return "chip_analysis"
    if re.search(r"assistant_deep|助手深度", stem_lower):
        return "assistant_deep"
    if re.search(r"估值|valuation", stem_lower):
        return "stock_value_analyse"
    if re.search(r"(?<![a-z0-9])(industrial|industry|sector)(?![a-z0-9])|行业|板块|金属", stem_lower):
        return "industry_research"
    if re.search(r"(?<![a-z0-9])(investment|research)(?![a-z0-9])|研究", stem_lower):
        return "investment_research"
    return "other"


def normalize_target(s: str) -> str:
    """归一化标的串，供匹配比较：去括号代码、去分隔符、小写、去停用词。"""
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", "", s)          # 去 (601838) 等代码括号
    s = re.sub(r"[_\-—\s·]+", "", s)         # 去分隔符
    s = s.lower()
    s = re.sub(r"(行业研究|研究报告|行业报告|投资研究|估值研究|研究|报告)", "", s)
    return s


def extract_target(stem: str) -> str:
    """best-effort 提取标的（股票名/行业名/代码）。新归档名保证精确，旧名尽力。"""
    s = stem
    # 去末尾日期及前导分隔符
    s = re.sub(r"[_\-]?(\d{4}-\d{2}-\d{2}|\d{8}(?:_\d+)?|\d{4})$", "", s)
    # 去类型前缀关键词
    s = re.sub(
        r"^(investment_research|investment|research|industrial|industry|"
        r"估值研究|估值|e2e|portfolio_analysis|dashboard|assistant_deep)[_\-]?",
        "",
        s,
    )
    # 去尾部研究类停用词
    s = re.sub(r"[_\-]?(行业研究|研究报告|行业报告|研究|报告)$", "", s)
    # 去残留代码括号
    s = re.sub(r"\([^)]*\)", "", s)
    return s.strip("_\- ")


def parse_report_filename(filename: str) -> Dict[str, Any]:
    """解析报告文件名 → {name, type, target, date}。

    - name：原文件名（含扩展名）
    - type：归类（VALIDITY_DAYS 的 key 之一，或 other）
    - target：best-effort 标的串
    - date：datetime.date 或 None
    """
    stem, _ext = os.path.splitext(filename)
    rtype = classify_type(stem.lower())
    # 筹码分析/综合研判无具体标的，target 置空（避免噪声）
    target = "" if rtype in ("chip_analysis", "verdict") else extract_target(stem)
    return {
        "name": filename,
        "type": rtype,
        "target": target,
        "date": extract_date(stem),
    }


# ═══════════════════════ 报告库读写 ═══════════════════════

def list_reports() -> List[Dict[str, Any]]:
    """扫描 reports/ 目录，返回 .md 报告列表（date 倒序），附有效期/新鲜度。"""
    reports_dir = get_reports_dir()
    if not os.path.isdir(reports_dir):
        return []
    today = date.today()
    items: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(reports_dir)):
        if not fn.lower().endswith(".md"):
            continue
        path = os.path.join(reports_dir, fn)
        if not os.path.isfile(path):
            continue
        info = parse_report_filename(fn)
        rtype = info["type"]
        validity_days = VALIDITY_DAYS.get(rtype)
        age_days: Optional[int] = None
        fresh = False
        if info["date"] is not None:
            age_days = (today - info["date"]).days
            if validity_days is not None and age_days >= 0:
                fresh = age_days < validity_days
        items.append(
            {
                "name": info["name"],
                "type": rtype,
                "type_label": TYPE_LABEL.get(rtype, rtype),
                "target": info["target"],
                "date": info["date"].isoformat() if info["date"] else None,
                "age_days": age_days,
                "validity_days": validity_days,
                "fresh": fresh,
                "size": os.path.getsize(path),
            }
        )
    # date 倒序（无日期排最后）
    items.sort(key=lambda x: (x["date"] is None, x["date"] or ""), reverse=False)
    items.sort(key=lambda x: x["date"] or "", reverse=True)
    return items


def read_report(name: str) -> Dict[str, Any]:
    """读单份报告原文。basename + realpath 防路径穿越。"""
    reports_dir = os.path.realpath(get_reports_dir())
    base = os.path.basename(name)
    full = os.path.realpath(os.path.join(reports_dir, base))
    if full != reports_dir and not full.startswith(reports_dir + os.sep):
        raise ValueError(f"非法报告名: {name}")
    if not os.path.isfile(full):
        raise FileNotFoundError(name)
    with open(full, encoding="utf-8") as f:
        content = f.read()
    info = parse_report_filename(base)
    return {
        "name": base,
        "type": info["type"],
        "type_label": TYPE_LABEL.get(info["type"], info["type"]),
        "target": info["target"],
        "date": info["date"].isoformat() if info["date"] else None,
        "content": content,
    }


# ═══════════════════════ 研究检索 ═══════════════════════

def find_fresh_report(rtype: str, target: str = "", code: str = "") -> Optional[Dict[str, Any]]:
    """找一份「新鲜且匹配」的既有报告；命中则跳过重复研究。

    匹配：type 相等、在有效期内、target/code 归一化后互含（chip_analysis 无标的仅按 type）。
    """
    if rtype not in VALIDITY_DAYS:
        return None
    n_target = normalize_target(target)
    best: Optional[Dict[str, Any]] = None
    for r in list_reports():
        if r["type"] != rtype or not r["fresh"]:
            continue
        if rtype == "chip_analysis":
            # 筹码分析无标的，只要类型匹配且新鲜
            pass
        else:
            n_tgt = normalize_target(r["target"])
            matched = False
            if code and code in r["target"]:
                matched = True
            elif n_target and n_tgt and (n_target in n_tgt or n_tgt in n_target):
                matched = True
            if not matched:
                continue
        if best is None or (r["date"] or "") > (best["date"] or ""):
            best = r
    return best


def find_reports_for_target(
    code: str = "", name: str = "", industry: str = "", limit: int = 4
) -> List[Dict[str, Any]]:
    """检索与标的相关的新鲜报告，作为投资智辩/综合研判的「证据底座」。

    相关性打分：代码命中 > 名称命中 > 行业命中；chip_analysis 恒弱相关（大盘结构）；
    新鲜度加成。同类型去重取最高分，最多返回 limit 份（含 name/type/target/date，不含正文）。
    """
    n_name = normalize_target(name)
    n_ind = normalize_target(industry)
    scored: List[tuple] = []
    for r in list_reports():
        if r["type"] == "other" or r["type"] not in VALIDITY_DAYS:
            continue
        r_tgt = normalize_target(r["target"])
        score = 0
        if code and r["type"] in ("investment_research", "stock_value_analyse") and code in r["target"]:
            score += 10
        if n_name and r_tgt and (n_name in r_tgt or r_tgt in n_name):
            score += 8
        if n_ind and r_tgt and (n_ind in r_tgt or r_tgt in n_ind):
            score += 6
        if r["type"] == "chip_analysis":
            score += 2
        # 只有「真实相关」（代码/名称/行业/筹码结构命中）才纳入候选；
        # fresh 仅作排序加分，不能再让「新鲜但无关」的报告混进证据底座。
        if score == 0:
            continue
        if r["fresh"]:
            score += 3
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], (x[1]["date"] or "")))
    seen: set = set()
    picked: List[Dict[str, Any]] = []
    for _score, r in scored:
        if r["type"] in seen:
            continue
        seen.add(r["type"])
        picked.append(r)
        if len(picked) >= limit:
            break
    return picked


def archive_filename(rtype: str, target: str = "", code: str = "", name: str = "") -> str:
    """新报告统一归档名（与历史命名一致，保证精确命中）。

    - investment_research → investment_research_{name}_{date}.md
    - industry_research    → industrial_{industry}_{date}.md
    - stock_value_analyse  → 估值研究_{name}({code})_{date}.md
    - chip_analysis        → research_chip_analysis_{date}.md
    - verdict              → 综合研判_{date}.md
    """
    today = date.today().isoformat()
    if rtype == "investment_research":
        return f"investment_research_{name or target}_{today}.md"
    if rtype == "industry_research":
        return f"industrial_{target}_{today}.md"
    if rtype == "stock_value_analyse":
        return f"估值研究_{name or target}({code})_{today}.md"
    if rtype == "chip_analysis":
        return f"research_chip_analysis_{today}.md"
    if rtype == "verdict":
        return f"综合研判_{today}.md"
    if rtype == "assistant_deep":
        return f"assistant_deep_{target or code or name}_{today}.md"
    return f"{rtype}_{target or code}_{today}.md"


# ═══════════════════════ 研究执行（Phase 2）══════════════════════

def _write_report(archive_name: str, content: str) -> str:
    """把报告写入 reports/（不存在则创建目录），返回完整路径。"""
    reports_dir = get_reports_dir()
    os.makedirs(reports_dir, exist_ok=True)
    full = os.path.join(reports_dir, archive_name)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


def build_prompt(rtype: str, target: str, code: str, market: str, archive_name: str) -> str:
    """组研究 prompt：指向 skills/*/agent.md + 标的数据底座 + 输出裁剪指令。"""
    agent_rel = SKILL_AGENT.get(rtype)
    if not agent_rel:
        raise ValueError(f"未知研究类型: {rtype}")
    agent_path = os.path.join(PROJECT_ROOT, agent_rel)

    data_base: Dict[str, Any] = {}
    if code:
        try:
            from debate_engine import get_data_base

            data_base = get_data_base(code, market)
        except Exception as exc:  # 数据底座失败不阻断研究，如实标注
            data_base = {"_error": f"数据底座获取失败: {exc}"}

    target_desc = target or code or "大盘"
    return (
        f"读 {agent_path}，严格按它执行下面的研究任务。"
        f"你的工作目录是 {PROJECT_ROOT}，agent.md 里所有 skills/ 相对路径都基于这个目录。\n\n"
        f"【研究任务】\n"
        f"类型：{rtype}（{TYPE_LABEL.get(rtype, rtype)}）\n"
        f"标的：{target_desc}\n"
        f"代码：{code or '—'}\n"
        f"市场：{market}\n\n"
        f"【数据底座】（后端 scripts 定量层已算好的真实数据 JSON，作为证据）\n"
        f"{json.dumps(data_base, ensure_ascii=False, default=str)}\n\n"
        f"【输出要求】\n"
        f"研究完成后，把最终报告写入这一份文件：{os.path.join(get_reports_dir(), archive_name)}\n"
        f"只写这一份 markdown 报告，跳过 Dashboard 部署、DOCX、PDF 等任何其他产物。\n"
        f"报告用 markdown 格式，一级标题=报告名+标的，正文含核心结论、数据来源、推理链条。"
    )


def run_research(rtype: str, target: str = "", code: str = "", market: str = "A股", on_session=None) -> Dict[str, Any]:
    """执行一次研究：组 prompt → 调 wecode → 落盘 reports/。

    返回 {report: 归档名, archive_name, session_id?, dry_run?}。
    - RESEARCH_DRY_RUN=1 时写桩报告、不调真实 wecode（快速验证链路）。
    - wecode 未按 prompt 写盘时，用 stdout 兜底写入（<100 字符判失败）。
    - on_session(session_id)：会话文件一出现就回调（供 worker 尽早落库 session_id，
      让前端在 running 期间就能拉到「研究过程」）。
    """
    if rtype not in SKILL_AGENT:
        raise ValueError(f"未知研究类型: {rtype}")
    archive_name = archive_filename(rtype, target=target, code=code, name=target)

    if os.environ.get("RESEARCH_DRY_RUN") == "1":
        stub = (
            f"# {TYPE_LABEL.get(rtype, rtype)}：{target or code or '大盘'}\n\n"
            f"（DRY RUN 桩报告，未调用真实 wecode）\n\n"
            f"- 类型：{rtype}\n- 标的：{target}\n- 代码：{code}\n- 归档名：{archive_name}\n"
        )
        _write_report(archive_name, stub)
        return {"report": archive_name, "archive_name": archive_name, "dry_run": True}

    prompt = build_prompt(rtype, target, code, market, archive_name)
    from debate_engine import clean_output, run_agent_with_session

    timeout = int(os.environ.get("RESEARCH_TIMEOUT", "1200"))
    raw, session_id = run_agent_with_session(prompt, timeout=timeout, on_session=on_session)
    cleaned = clean_output(raw)

    full = os.path.join(get_reports_dir(), archive_name)
    if not os.path.isfile(full):
        if len(cleaned) < 100:
            raise RuntimeError(f"wecode 输出过短（{len(cleaned)} 字符），研究疑似失败")
        _write_report(archive_name, cleaned)

    return {"report": archive_name, "archive_name": archive_name, "session_id": session_id}


# ═══════════════════════ 综合研判（Phase 3）══════════════════════

def _verdict_fallback(review_data: Dict[str, Any], fresh_reports: List[Dict[str, Any]], exc: Exception) -> str:
    """wecode 研判失败时的规则兜底：只列事实，不做推断，明确标注。"""
    lines = [
        "# 综合研判（规则生成）",
        "",
        f"> ⚠️ wecode 研判失败（{exc}），以下为规则兜底，仅列事实层，不做推断。",
        "",
        "## 事实层",
        "",
        "### 组合全景数据（顶层字段）",
    ]
    for k, v in review_data.items():
        s = str(v)
        lines.append(f"- **{k}**: {s[:200]}{'…' if len(s) > 200 else ''}")
    lines.append("")
    lines.append("### 新鲜研究报告")
    if fresh_reports:
        for r in fresh_reports:
            lines.append(f"- {r['type_label']}｜{r['target'] or r['name']}（{r['date']}）")
    else:
        lines.append("- 无新鲜报告")
    lines.append("")
    lines.append("## 建议研究方向")
    lines.append("- 人工复核上述数据后，重新触发综合研判，或按 CLAUDE.md 阶段2 逐条检查触发条件。")
    return "\n".join(lines)


def run_verdict(payload: Optional[Dict[str, Any]] = None, on_session=None) -> Dict[str, Any]:
    """综合研判：组合全景（review()）+ 新鲜报告 → 三层研判（事实/推断/建议）。

    - payload 可带 user_id，用于先把模拟账户投影到 yaml 再 review（保证读到最新持仓）。
    - wecode 失败走 _verdict_fallback 规则兜底（明确标注「规则生成」，不静默失败）。
    - on_session(session_id)：同 run_research。
    """
    payload = payload or {}
    user_id = payload.get("user_id", "")
    archive_name = archive_filename("verdict")

    # 1. 组合全景数据
    review_data: Dict[str, Any] = {}
    try:
        import sim_trade

        if user_id:
            try:
                sim_trade.sync_to_yaml(user_id)
            except Exception:
                pass
        from portfolio_strategy import PortfolioStrategy

        review_data = PortfolioStrategy(
            portfolio_path=sim_trade.user_portfolio_path(user_id) if user_id else None
        ).review()
    except Exception as exc:
        review_data = {"_error": f"组合全景获取失败: {exc}"}

    # 2. 新鲜报告摘要
    fresh_reports = [r for r in list_reports() if r["fresh"] and r["type"] in VALIDITY_DAYS]
    report_digests: List[str] = []
    for r in fresh_reports[:8]:
        try:
            content = read_report(r["name"])["content"]
            report_digests.append(
                f"### {r['type_label']}｜{r['target'] or r['name']}（{r['date']}）\n{content[:2500]}"
            )
        except Exception:
            continue

    # 3. 组 prompt
    prompt = (
        f"你是 mystock 投资控制台的资深金融分析师。基于下面的组合全景数据与新鲜研究报告，"
        f"产出一份「综合研判」markdown 报告，分三层：\n"
        f"1. 事实层（数据说了什么）\n2. 推断层（这意味着什么）\n3. 建议研究方向/操作框架（下一步怎么做）\n\n"
        f"【组合全景数据】（JSON）\n{json.dumps(review_data, ensure_ascii=False, default=str)[:8000]}\n\n"
        f"【新鲜研究报告摘要】\n{chr(10).join(report_digests) if report_digests else '（无新鲜报告）'}\n\n"
        f"用 markdown 输出，一级标题=综合研判，末尾附「建议研究方向」段落。"
    )

    # dry run
    if os.environ.get("RESEARCH_DRY_RUN") == "1":
        stub = (
            f"# 综合研判\n\n（DRY RUN 桩报告）\n\n"
            f"- 组合全景顶层字段：{len(review_data)} 个\n- 新鲜报告：{len(fresh_reports)} 份\n"
        )
        _write_report(archive_name, stub)
        return {"report": archive_name, "archive_name": archive_name, "dry_run": True}

    # 4. 调 wecode（失败走规则兜底）
    archive_path = os.path.join(get_reports_dir(), archive_name)
    session_id = None
    try:
        from debate_engine import clean_output, run_agent_with_session

        timeout = int(os.environ.get("RESEARCH_TIMEOUT", "1200"))
        raw, session_id = run_agent_with_session(prompt, timeout=timeout, on_session=on_session)
        cleaned = clean_output(raw)
        if not os.path.isfile(archive_path):
            if len(cleaned) < 100:
                raise RuntimeError("wecode 输出过短")
            _write_report(archive_name, cleaned)
    except Exception as exc:  # noqa: BLE001 —— 兜底，不静默失败
        _write_report(archive_name, _verdict_fallback(review_data, fresh_reports, exc))

    return {"report": archive_name, "archive_name": archive_name, "session_id": session_id}


if __name__ == "__main__":
    # CLI：报告库冒烟（--list）+ 定期/手动触发研究（--type ...）
    import argparse

    parser = argparse.ArgumentParser(
        description="研究引擎 CLI：报告库冒烟，或触发一次研究/综合研判（供 crontab 定期调用）"
    )
    parser.add_argument("--list", action="store_true", help="打印报告列表（含有效期/新鲜度）")
    parser.add_argument(
        "--type",
        choices=["investment_research", "industry_research", "stock_value_analyse", "chip_analysis", "verdict"],
        help="触发研究类型（verdict=综合研判）。与 --target/--code 配合，供 crontab 定期调用",
    )
    parser.add_argument("--target", default="", help="标的（股票名/行业名）")
    parser.add_argument("--code", default="", help="标的代码")
    parser.add_argument("--market", default="A股", help="市场，默认 A股")
    args = parser.parse_args()

    if args.list:
        for r in list_reports():
            flag = "✅fresh" if r["fresh"] else ("⏳expired" if r["validity_days"] else "—")
            print(
                f"[{r['type']:<18}] {r['date'] or '????-??-??'} {flag:<10} "
                f"target={r['target']!r:<16} {r['name']}"
            )
    elif args.type:
        print(f"[research_engine] 触发 {args.type} target={args.target or '—'} code={args.code or '—'} ...")
        try:
            if args.type == "verdict":
                result = run_verdict({})
            else:
                result = run_research(args.type, target=args.target, code=args.code, market=args.market)
            print(f"[research_engine] 完成 → {result.get('report')}")
        except Exception as exc:  # noqa: BLE001 —— 定期任务失败要显式非零退出，供 cron 感知
            print(f"[research_engine] 失败：{exc}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
