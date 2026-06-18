#!/usr/bin/env python3
"""Portfolio Dashboard — 一行命令生成交互式 HTML 监控面板"""

import argparse
import json
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")


def run_review():
    from portfolio_strategy import PortfolioStrategy
    return PortfolioStrategy().review()


def run_heatmap():
    from sector_radar import SectorRadarEngine
    engine = SectorRadarEngine()
    _, _, summary = engine.load_all_industry_data()
    groups, _, _ = engine.load_all_industry_data()
    if not groups:
        return {"heatmap": [], "radar": [], "error": summary.get("signal_date", "?")}
    metrics = [engine._compute_industry_metrics(g) for g in groups]
    valid = [m for m in metrics if m.get("available")]
    heatmap = engine.compute_heatmap(valid)[:10]
    radar = engine.compute_radar(valid)[:5]
    return {"heatmap": heatmap, "radar": radar}


def _pct(v, d=1):
    if v is None:
        return "—"
    return f"{v:.{d}f}%"


def _color_pnl(v):
    if v >= 0:
        return f'<span class="positive">{_pct(v, 2)}</span>'
    return f'<span class="negative">{_pct(v, 2)}</span>'


def _stop_class(dist, triggered=False):
    if triggered:
        return "stop-triggered"
    if dist and dist < 3:
        return "stop-urgent"
    if dist and dist < 8:
        return "stop-warning"
    return "stop-safe"


def build_html(review, sector_data):
    summary = review["summary"]
    pos = review["positions"]
    watch = review.get("watchlist", [])
    alerts = review.get("alerts", [])
    cycle = review.get("cycle_summary", {})
    diag = review.get("diagnosis", {})
    heatmap = sector_data.get("heatmap", [])
    radar = sector_data.get("radar", [])

    # ── position cards ──
    pos_cards = []
    for p in pos:
        trend = p.get("trend_signal", {})
        ti = trend.get("technical_indicators", {}) or {}
        kdj = ti.get("kdj", {}) or {}
        rets = trend.get("returns", {}) or {}
        sig = p.get("strategy_signals", {}).get("signals", {})
        factor = sig.get("factor", {})

        stop = p.get("stop_loss_price", 0)
        price = p.get("current_price", 0)
        stop_dist = round((price - stop) / stop * 100, 1) if stop and price else None
        stop_triggered = stop and price <= stop

        add_cond = p.get("add_conditions", {})
        add_met = add_cond.get("met", 0)
        trim_cond = p.get("trim_conditions", {})
        trim_met = trim_cond.get("met", 0)

        pos_cards.append(f"""
        <div class="card {_stop_class(stop_dist, stop_triggered)}">
            <div class="card-header">
                <span class="code">{p['code']}</span>
                <span class="name">{p.get('name', '')}</span>
                <span class="market">{p.get('market', '')}</span>
            </div>
            <div class="card-body">
                <div class="metric-row">
                    <div class="metric"><label>现价</label><span>{price} {p.get('currency', 'CNY')}</span></div>
                    <div class="metric"><label>成本</label><span>{p.get('cost_price', 0)}</span></div>
                    <div class="metric"><label>盈亏</label>{_color_pnl(p.get('unrealized_pnl_pct', 0))}</div>
                    <div class="metric"><label>仓位</label><span>{_pct(p.get('weight_pct', 0))}</span></div>
                </div>
                <div class="metric-row">
                    <div class="metric"><label>RSI14</label><span>{ti.get('rsi14', '—')}</span></div>
                    <div class="metric"><label>KDJ-J</label><span>{kdj.get('j', '—')}</span></div>
                    <div class="metric"><label>5/20/60d</label><span>{rets.get('return_5d', '—')}%/{rets.get('return_20d', '—')}%/{rets.get('return_60d', '—')}%</span></div>
                    <div class="metric"><label>趋势</label><span>{trend.get('status', '—')}</span></div>
                </div>
                <div class="metric-row">
                    <div class="metric"><label>止损价</label><span>{stop or '—'}</span></div>
                    <div class="metric"><label>距止损</label><span class="{'negative' if stop_triggered else ''}">{_pct(stop_dist) if stop_dist else '—'}{' ⚡已触发' if stop_triggered else ''}</span></div>
                    <div class="metric"><label>加仓条件</label><span>{add_met}/5</span></div>
                    <div class="metric"><label>止盈条件</label><span>{trim_met}/4</span></div>
                </div>
                {f'<div class="factor-row"><span>因子: 综合{factor.get("composite_score","?"):.2f}</span><span>价值{factor.get("factor_scores",{}).get("value","?"):.2f}</span><span>成长{factor.get("factor_scores",{}).get("growth","?"):.2f}</span><span>质量{factor.get("factor_scores",{}).get("quality","?"):.2f}</span><span>动量{factor.get("factor_scores",{}).get("momentum","?"):.2f}</span></div>' if factor.get("available") else ""}
            </div>
        </div>""")

    # ── watchlist cards ──
    watch_cards = []
    for w in watch:
        timing = w.get("buy_timing", {})
        bt_score = timing.get("score", "?")
        bt_label = timing.get("label", "—")
        trend = w.get("trend_signal", {})
        ti = trend.get("technical_indicators", {}) or {}
        kdj = ti.get("kdj", {}) or {}
        rets = trend.get("returns", {}) or {}
        target_zone = w.get("target_buy_zone", "")

        watch_cards.append(f"""
        <div class="card watch">
            <div class="card-header">
                <span class="code">{w['code']}</span>
                <span class="name">{w.get('name', '')}</span>
                <span class="timing">{bt_label}</span>
            </div>
            <div class="card-body">
                <div class="metric-row">
                    <div class="metric"><label>现价</label><span>{w.get('current_price', 0)}</span></div>
                    <div class="metric"><label>RSI</label><span>{ti.get('rsi14', '—')}</span></div>
                    <div class="metric"><label>KDJ-J</label><span>{kdj.get('j', '—')}</span></div>
                    <div class="metric"><label>买入时机</label><span>{bt_score}</span></div>
                </div>
                <div class="metric-row">
                    <div class="metric"><label>5/20d</label><span>{rets.get('return_5d', '—')}%/{rets.get('return_20d', '—')}%</span></div>
                    <div class="metric"><label>目标区间</label><span>{target_zone or '—'}</span></div>
                    <div class="metric" style="grid-column: span 2"><label>理由</label><span>{w.get('reason', '')[:60]}</span></div>
                </div>
            </div>
        </div>""")

    # ── sector heatmap rows ──
    sector_rows = []
    for i, h in enumerate(heatmap):
        score = h.get("momentum_score", 0)
        bar = "█" * int(score * 15) + "░" * (15 - int(score * 15))
        status = h.get("status", "")
        sector_rows.append(f"""
        <tr>
            <td>{i+1}</td>
            <td>{h['industry']}</td>
            <td>{status}</td>
            <td>{h.get('breadth_pct', 0):.1f}%</td>
            <td>{h.get('avg_ret_20d', 0):.1f}%</td>
            <td>{h.get('avg_rsi', 0):.1f}</td>
            <td>{h.get('pe_median', '—')}</td>
            <td><span class="bar">{bar}</span> {score:.3f}</td>
        </tr>""")

    radar_rows = []
    for r in radar:
        radar_rows.append(f"""
        <tr>
            <td>{r['industry']}</td>
            <td>{r.get('wake_label', '—')}</td>
            <td>{r.get('wake_up_score', 0):.3f}</td>
            <td>{r.get('avg_ret_20d', 0):.1f}%</td>
            <td>{r.get('breadth_pct', 0):.1f}%</td>
            <td>{r.get('leader_above_ma20_pct', 0):.1f}%</td>
            <td>{r.get('improving_pct', 0):.1f}%</td>
        </tr>""")

    # ── alerts ──
    alert_items = "".join(
        f'<li class="alert-{a["level"]}">{a["level"].upper()}: {a["message"]}</li>'
        for a in alerts
    ) if alerts else "<li>无告警</li>"

    # ── macro news ──
    news_items = "".join(
        f"<li>{n.get('title', n)}</li>"
        for n in review.get("market_news", [])[:5]
    ) or "<li>—</li>"

    cycle_rows = ""
    for style_name, cinfo in cycle.get("cycles", {}).items():
        reps = " | ".join(
            f"{r['name']}: {r.get('status','?')} 20d={r.get('return_20d','?')}%"
            for r in cinfo.get("representatives", [])[:3]
        )
        cycle_rows += f"<tr><td>{style_name}</td><td>{cinfo.get('strength','?')}</td><td>{cinfo.get('score','?')}</td><td>{reps}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Portfolio Dashboard — {summary['generated_at'][:10]}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0d1117; color:#c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; padding:20px; }}
.container {{ max-width:1400px; margin:0 auto; }}
h1 {{ font-size:1.4em; color:#f0f6fc; border-bottom:1px solid #30363d; padding-bottom:10px; margin-bottom:16px; }}
h2 {{ font-size:1.1em; color:#f0f6fc; margin:20px 0 12px; }}
.grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
.grid-3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
.grid-6 {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); gap:12px; }}

/* summary bar */
.summary {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; margin-bottom:16px; }}
.summary .metrics {{ display:flex; gap:24px; flex-wrap:wrap; }}
.summary .metric {{ display:flex; flex-direction:column; }}
.summary .metric label {{ font-size:0.7em; color:#8b949e; text-transform:uppercase; }}
.summary .metric span {{ font-size:1.2em; font-weight:600; }}
.positive {{ color:#3fb950; }}
.negative {{ color:#f85149; }}
.warning-color {{ color:#d29922; }}

/* cards */
.card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; overflow:hidden; }}
.card.stop-triggered {{ border-color:#f85149; border-width:2px; }}
.card.stop-urgent {{ border-color:#f85149; }}
.card.stop-warning {{ border-color:#d29922; }}
.card.watch {{ border-color:#1f6feb30; }}
.card-header {{ padding:8px 14px; background:#1c2129; display:flex; align-items:center; gap:10px; border-bottom:1px solid #30363d; }}
.card-header .code {{ font-weight:700; color:#58a6ff; }}
.card-header .name {{ color:#f0f6fc; }}
.card-header .market {{ color:#8b949e; font-size:0.8em; }}
.card-header .timing {{ margin-left:auto; padding:2px 8px; border-radius:4px; font-size:0.8em; background:#23863620; color:#3fb950; }}
.card-body {{ padding:10px 14px; }}
.metric-row {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:6px; }}
.metric {{ display:flex; flex-direction:column; }}
.metric label {{ font-size:0.65em; color:#8b949e; text-transform:uppercase; }}
.metric span {{ font-size:0.9em; }}
.factor-row {{ display:flex; gap:12px; padding-top:6px; border-top:1px solid #30363d; font-size:0.8em; color:#8b949e; }}

/* tables */
table {{ width:100%; border-collapse:collapse; font-size:0.85em; margin-bottom:12px; }}
th {{ text-align:left; padding:6px 10px; color:#8b949e; border-bottom:1px solid #30363d; font-weight:500; }}
td {{ padding:5px 10px; border-bottom:1px solid #21262d; }}
tr:hover {{ background:#1c2129; }}
.bar {{ font-family:monospace; color:#58a6ff; font-size:0.75em; }}

/* alerts */
.alerts {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px; }}
.alerts li {{ padding:4px 0; font-size:0.85em; list-style:none; }}
.alert-warning {{ color:#d29922; }}
.alert-info {{ color:#58a6ff; }}

/* research */
.research {{ margin-top:16px; font-size:0.8em; color:#8b949e; }}
.research a {{ color:#58a6ff; }}
</style>
</head>
<body>
<div class="container">
<h1>📊 Portfolio Dashboard — {summary['generated_at'][:19]}</h1>

<div class="summary">
    <div class="metrics">
        <div class="metric"><label>总资产</label><span>{summary['total_assets']:,.0f} {summary['base_currency']}</span></div>
        <div class="metric"><label>持仓市值</label><span>{summary['position_value']:,.0f}</span></div>
        <div class="metric"><label>现金</label><span>{summary['cash']:,.0f} ({summary['cash_pct']:.1f}%)</span></div>
        <div class="metric"><label>浮动盈亏</label>{_color_pnl(summary.get('unrealized_pnl_pct', 0))}</div>
        <div class="metric"><label>回撤占用</label><span class="warning-color">{review.get('goal_progress',{}).get('drawdown_usage_pct',0):.1f}%</span></div>
        <div class="metric"><label>可动用</label><span>{diag.get('available_to_deploy_pct','?'):.1f}%</span></div>
        <div class="metric"><label>南向5日</label><span class="positive">{review.get('market_sentiment',{}).get('north_bound',{}).get('south_5d_buy',0):.1f}亿</span></div>
        <div class="metric"><label>主力5日</label><span class="negative">{review.get('market_sentiment',{}).get('market_moneyflow',{}).get('net_main_5d',0):.1f}亿</span></div>
    </div>
</div>

<h2>📈 持仓 ({len(pos)} 只)</h2>
<div class="grid-6">
    {"".join(pos_cards)}
</div>

<h2>👀 观察池 ({len(watch)} 只)</h2>
<div class="grid-6">
    {"".join(watch_cards) if watch_cards else '<div class="card"><div class="card-body">无观察池</div></div>'}
</div>

<div class="grid-2">
    <div>
        <h2>🔥 行业动量 Top 10</h2>
        <table>
            <tr><th>#</th><th>行业</th><th>状态</th><th>广度</th><th>20日</th><th>RSI</th><th>PE</th><th>动量</th></tr>
            {"".join(sector_rows) if sector_rows else '<tr><td colspan="8">无数据</td></tr>'}
        </table>

        <h2>⚡ 苏醒雷达</h2>
        <table>
            <tr><th>行业</th><th>标签</th><th>苏醒分</th><th>20日</th><th>广度</th><th>领涨MA20</th><th>改善%</th></tr>
            {"".join(radar_rows) if radar_rows else '<tr><td colspan="7">无冷门行业</td></tr>'}
        </table>
    </div>
    <div>
        <h2>🔄 周期判断</h2>
        <table>
            <tr><th>风格</th><th>强度</th><th>得分</th><th>代表资产</th></tr>
            {cycle_rows or '<tr><td colspan="4">无数据</td></tr>'}
        </table>

        <h2>⚠️ 告警</h2>
        <div class="alerts"><ul>{alert_items}</ul></div>

        <h2>📰 宏观要闻</h2>
        <div class="alerts"><ul>{news_items}</ul></div>
    </div>
</div>

<div class="research" style="margin-top:24px">
    <p>Dashboard generated at {datetime.now().isoformat(timespec='seconds')}</p>
</div>
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Portfolio Dashboard Generator")
    parser.add_argument("--output", "-o", default=None, help="Output HTML path")
    parser.add_argument("--skip-review", action="store_true", help="Skip portfolio review (use cached)")
    parser.add_argument("--review-json", type=str, default=None, help="Use cached review JSON")
    args = parser.parse_args()

    print("🔄 运行 portfolio review...")
    if args.review_json:
        with open(args.review_json) as f:
            review = json.load(f)
    else:
        review = run_review()

    print("🔥 运行 sector radar...")
    sector_data = run_heatmap()

    print("📄 生成 HTML...")
    html = build_html(review, sector_data)

    out_path = args.output or os.path.join(REPORTS_DIR, f"dashboard_{datetime.now().strftime('%Y%m%d_%H%M')}.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ Dashboard: {out_path}")
    print(f"   浏览器打开: file://{out_path}")


if __name__ == "__main__":
    main()
