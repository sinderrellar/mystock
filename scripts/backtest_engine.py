#!/usr/bin/env python3
"""
回测引擎 — 用预计算的历史信号驱动 buy_plan.run()。

先运行: python3 precompute_history.py --start 2026-04-07 --end 2026-06-08
再运行: python3 backtest_engine.py --start 2026-04-07 --end 2026-06-08 --top 10
"""

import argparse
import os
import sys
import time
from collections import defaultdict

import yaml
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config


# ================================================================
# Utilities
# ================================================================

def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ================================================================
# Trade Record
# ================================================================

@dataclass
class Trade:
    code: str
    name: str
    industry: str
    strategy: str
    buy_date: str
    buy_price: float
    sell_date: str = ""
    sell_price: float = 0.0
    exit_reason: str = ""
    return_pct: float = 0.0
    holding_days: int = 0


# ================================================================
# Backtest Engine (real buy_plan)
# ================================================================

class BacktestEngine:
    """用预计算历史信号 + buy_plan.run() 做回测。"""

    def __init__(self, start_date: str, end_date: str,
                 stop_loss_pct: float = 0.15,
                 take_profit_pct: float = 0.30,
                 max_hold_days: int = 60):
        self.start_date = start_date
        self.end_date = end_date
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_hold_days = max_hold_days

        config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
        config = _load_mongodb_config(config_path)
        self.mongo = MongoFactorDataStore(config)

        self._trading_days = self._load_trading_days()

    def _load_trading_days(self) -> List[str]:
        """获取 stock_factors 中已计算的日期列表，按 --start/--end 过滤。"""
        all_dates = sorted(self.mongo.db["stock_factors"].distinct("trade_date"))
        return [d for d in all_dates if self.start_date <= d <= self.end_date]

    # ================================================================
    # Trade simulation
    # ================================================================

    def _simulate_trades(self, signals: List[Dict], signal_date: str,
                         held_codes: set = None) -> List[Trade]:
        """模拟止盈/止损退出。held_codes 用于去重——已持仓股票不再开新仓。"""
        trades = []
        if held_codes is None:
            held_codes = set()
        for s in signals:
            code = s.get("code", "")
            if code in held_codes:
                continue

            # 先查次交易日数据，用 next-day open 作为实际买入价
            quote_query = {"code": code, "period": "daily",
                           "trade_date": {"$gt": signal_date},
                           "data_source": self.mongo.quote_source}
            later = list(self.mongo.db[self.mongo.collections["daily_quotes"]].find(
                quote_query,
                sort=[("trade_date", 1)],
            ).limit(self.max_hold_days))

            if not later:
                continue
            buy_price = _to_float(later[0].get("open"), 0)
            if buy_price <= 0:
                continue
            stop_price = buy_price * (1 - self.stop_loss_pct)
            take_price = buy_price * (1 + self.take_profit_pct)
            strategy = "+".join(s.get("strategy_tags", ["未知"]))

            sell_date = ""
            sell_price = 0.0
            exit_reason = "到期"
            for bar in later:
                high = _to_float(bar.get("high"), bar.get("close", 0))
                low = _to_float(bar.get("low"), bar.get("close", 0))
                close = _to_float(bar.get("close"), 0)
                td = str(bar.get("trade_date", ""))

                if high >= take_price:
                    sell_price = take_price
                    sell_date = td
                    exit_reason = "止盈"
                    break
                elif low <= stop_price:
                    sell_price = stop_price
                    sell_date = td
                    exit_reason = "止损"
                    break
                elif bar == later[-1]:
                    sell_price = close
                    sell_date = td

            if sell_date:
                ret = (sell_price / buy_price - 1) * 100
                holding = (datetime.strptime(sell_date, "%Y-%m-%d") -
                           datetime.strptime(signal_date, "%Y-%m-%d")).days
                trades.append(Trade(
                    code=code,
                    name=s.get("name", code),
                    industry=s.get("industry", ""),
                    strategy=strategy,
                    buy_date=signal_date,
                    buy_price=round(buy_price, 2),
                    sell_date=sell_date,
                    sell_price=round(sell_price, 2),
                    exit_reason=exit_reason,
                    return_pct=round(ret, 2),
                    holding_days=holding,
                ))
                held_codes.add(code)
        return trades

    # ================================================================
    # Main
    # ================================================================

    def run(self, top_n: int = 10, verbose: bool = True) -> Dict[str, Any]:
        """主回测循环：BuyPlanEngine(target_date=date) → 模拟卖出。"""
        from buy_plan import BuyPlanEngine

        all_trades: List[Trade] = []
        days_ok = 0
        held_codes: set = set()

        if not self._trading_days:
            return {"error": f"无预计算数据 ({self.start_date}~{self.end_date})"}

        for i, date in enumerate(self._trading_days):
            if verbose:
                print(f"  [{i+1}/{len(self._trading_days)}] {date}", end=" ", flush=True)

            try:
                engine = BuyPlanEngine()
                report = engine.run(top_n=top_n, target_date=date,
                                    initial_limit=5000, enrich_limit=0)
            except Exception as e:
                if verbose:
                    print(f"— 错误: {e}")
                continue

            candidates = report.get("recommendations", [])
            if not candidates:
                if verbose:
                    print("— 无候选")
                continue

            if verbose:
                strategies = defaultdict(int)
                for c in candidates:
                    for t in c.get("strategy_tags", []):
                        strategies[t] += 1
                strat_str = " ".join(f"{k}:{v}" for k, v in sorted(strategies.items()))
                pipe = report.get("pipeline", {})
                print(f"— {len(candidates)}只 [{strat_str}] ({pipe.get('layer2_strategy',0)}过L2)")

            trades = self._simulate_trades(candidates, date, held_codes)
            all_trades.extend(trades)
            days_ok += 1

        return self._build_report(all_trades, days_ok)

    def _build_report(self, trades: List[Trade], days: int) -> Dict[str, Any]:
        """生成回测报告。"""
        if not trades:
            return {"error": "无交易信号", "days_processed": days}

        wins = [t for t in trades if t.return_pct > 0]
        losses = [t for t in trades if t.return_pct <= 0]
        rets = [t.return_pct for t in trades]
        holds = [t.holding_days for t in trades]

        exit_dist = defaultdict(int)
        for t in trades:
            exit_dist[t.exit_reason] += 1

        ind_dist = defaultdict(lambda: {"cnt": 0, "avg_ret": 0.0})
        for t in trades:
            ind_dist[t.industry]["cnt"] += 1
            ind_dist[t.industry]["avg_ret"] += t.return_pct
        for v in ind_dist.values():
            v["avg_ret"] = round(v["avg_ret"] / max(v["cnt"], 1), 2)

        return {
            "days_processed": days,
            "total_trades": len(trades),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "avg_return": round(sum(rets) / len(rets), 2),
            "median_return": round(sorted(rets)[len(rets) // 2], 2),
            "max_return": round(max(rets), 2),
            "min_return": round(min(rets), 2),
            "avg_win": round(sum([t.return_pct for t in wins]) / max(len(wins), 1), 2),
            "avg_loss": round(sum([t.return_pct for t in losses]) / max(len(losses), 1), 2),
            "profit_factor": round(abs(sum([t.return_pct for t in wins]) /
                                       sum([t.return_pct for t in losses])), 2) if losses else 0,
            "avg_hold_days": round(sum(holds) / max(len(holds), 1), 1),
            "exit_distribution": dict(exit_dist),
            "by_industry": {k: v for k, v in
                            sorted(ind_dist.items(), key=lambda x: x[1]["cnt"], reverse=True)[:10]},
            "trades": [{
                "code": t.code, "name": t.name, "industry": t.industry,
                "strategy": t.strategy,
                "buy_date": t.buy_date, "buy_price": t.buy_price,
                "sell_date": t.sell_date, "sell_price": t.sell_price,
                "return_pct": t.return_pct, "exit_reason": t.exit_reason,
                "holding_days": t.holding_days,
            } for t in sorted(trades, key=lambda x: x.return_pct, reverse=True)],
        }


# ================================================================
# Formatter
# ================================================================

def format_report(report: Dict[str, Any]) -> str:
    if report.get("error"):
        return f"回测: {report['error']}"

    lines = [
        "=" * 70,
        f"回测报告 ({report['days_processed']} 个交易日)",
        "=" * 70,
        "",
        "── 核心指标 ──",
        f"  交易笔数:     {report['total_trades']}",
        f"  胜率:         {report['win_rate']}%",
        f"  平均收益:     {report['avg_return']:.2f}%",
        f"  中位收益:     {report['median_return']:.2f}%",
        f"  最大盈利:     {report['max_return']:.2f}%",
        f"  最大亏损:     {report['min_return']:.2f}%",
        f"  盈亏比:       {report['profit_factor']}",
        f"  平均持仓天数: {report['avg_hold_days']} 天",
        "",
        "── 退出原因 ──",
    ]
    for reason, count in report.get("exit_distribution", {}).items():
        lines.append(f"  {reason}: {count} 笔 ({count/report['total_trades']*100:.1f}%)")

    lines.extend([
        "",
        "── 最近交易 ──",
    ])
    for t in report.get("trades", [])[:10]:
        lines.append(
            f"  {t['code']} {t['name'][:6]:<6} {t['buy_date']} → {t['sell_date']} "
            f"{t['return_pct']:+.2f}% ({t['exit_reason']})"
        )

    lines.append("=" * 70)
    return "\n".join(lines)


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="回测引擎 — 用历史信号驱动 buy_plan")
    parser.add_argument("--start", default="2026-04-07", help="开始日期")
    parser.add_argument("--end", default="2026-06-08", help="结束日期")
    parser.add_argument("--top", type=int, default=10, help="每日候选数")
    parser.add_argument("--stop-pct", type=float, default=0.15)
    parser.add_argument("--take-pct", type=float, default=0.30)
    parser.add_argument("--max-hold", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine = BacktestEngine(
        start_date=args.start, end_date=args.end,
        stop_loss_pct=args.stop_pct, take_profit_pct=args.take_pct,
        max_hold_days=args.max_hold,
    )

    print(f"回测窗口: {args.start} → {args.end}")
    print(f"可用历史日期: {len(engine._trading_days)} 天")
    print()

    import json
    report = engine.run(top_n=args.top)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
