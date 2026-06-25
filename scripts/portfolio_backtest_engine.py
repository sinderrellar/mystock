#!/usr/bin/env python3
"""
Portfolio Backtest Engine — 组合回测引擎（v3 全链路）

走完整决策链: buy_plan → entry → risk → sizing
与实盘 portfolio_controller.run() 共用 _run_decision_chain()。

用法:
  python3 scripts/portfolio_backtest_engine.py \
    --start 2026-03-02 --end 2026-06-17 \
    --top 10 --budget 0.60 --baseline-vol 0.025
"""

import argparse
import contextlib
import io
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config


def _holding_days(entry_date: str, exit_date: str) -> int:
    try:
        return (
            datetime.strptime(exit_date, "%Y-%m-%d")
            - datetime.strptime(entry_date, "%Y-%m-%d")
        ).days
    except Exception:
        return 0


def _pct_arg(value: float) -> float:
    """Convert CLI percent values such as 8 into decimal 0.08."""
    return max(0.0, value / 100)


# ================================================================
# Price Helpers（统一数据源：daily_quotes）
# ================================================================

class PriceProvider:
    """统一价格源。所有 open/close 都走 daily_quotes。"""

    def __init__(self, mongo: MongoFactorDataStore):
        self.coll = mongo.db[mongo.collections["daily_quotes"]]
        self.quote_source = mongo.quote_source
        self._cache = {}

    def get_open(self, code: str, date: str) -> float:
        key = (code, date, "open")
        if key not in self._cache:
            query = {"code": code, "period": "daily", "trade_date": date, "data_source": self.quote_source}
            doc = self.coll.find_one(
                query,
                {"open": 1, "_id": 0})
            self._cache[key] = (doc.get("open", 0) if doc else 0) or 0
        return self._cache[key]

    def get_close(self, code: str, date: str) -> float:
        key = (code, date, "close")
        if key not in self._cache:
            query = {"code": code, "period": "daily", "trade_date": date, "data_source": self.quote_source}
            doc = self.coll.find_one(
                query,
                {"close": 1, "_id": 0})
            self._cache[key] = (doc.get("close", 0) if doc else 0) or 0
        return self._cache[key]


# ================================================================
# Portfolio Backtest Engine
# ================================================================

class PortfolioBacktestEngine:
    """组合回测引擎。每天跑 controller.run_backtest()，模拟 T+1 执行。"""

    def __init__(self, start_date: str, end_date: str,
                 budget: float = 0.60, baseline_vol: float = 0.025,
                 top_n: int = 10, initial_cash: float = 1_000_000,
                 mode: str = "full", stop_loss_pct: float = 0.0,
                 take_profit_pct: float = 0.0, max_hold_days: int = 0,
                 trailing_stop_pct: float = 0.0):
        self.start_date = start_date
        self.end_date = end_date
        self.budget = budget
        self.baseline_vol = baseline_vol
        self.top_n = top_n
        self.initial_cash = initial_cash
        self.mode = mode
        self.stop_loss_pct = max(0.0, stop_loss_pct)
        self.take_profit_pct = max(0.0, take_profit_pct)
        self.max_hold_days = max(0, max_hold_days)
        self.trailing_stop_pct = max(0.0, trailing_stop_pct)

        config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
        config = _load_mongodb_config(config_path)
        self.mongo = MongoFactorDataStore(config)
        self.prices = PriceProvider(self.mongo)

        self._trading_days = self._load_trading_days()

    def _load_trading_days(self) -> List[str]:
        """加载 trading days 列表（从 stock_factors + daily_quotes 交集）。"""
        factor_dates = set(self.mongo.db["stock_factors"].distinct("trade_date"))
        quote_query: Dict[str, Any] = {"data_source": self.mongo.quote_source, "period": "daily"}
        quote_dates = set(self.mongo.db[self.mongo.collections["daily_quotes"]].distinct("trade_date", quote_query))
        all_dates = sorted(factor_dates & quote_dates)
        return [d for d in all_dates if self.start_date <= d <= self.end_date]

    def _next_trading_day(self, date: str) -> Optional[str]:
        """返回 date 之后的下一个交易日，无则返回 None。"""
        for d in self._trading_days:
            if d > date:
                return d
        return None

    # ================================================================
    # Main Loop
    # ================================================================

    def run(self, verbose: bool = True) -> Dict[str, Any]:
        from portfolio_controller import run_backtest

        config_override = {
            "max_risk_budget": self.budget,
            "baseline_vol": self.baseline_vol,
            "top_n": self.top_n,
        }

        # ── 初始化 virtual_portfolio ──
        vp = {
            "cash": self.initial_cash,
            "equity": self.initial_cash,
            "max_equity": self.initial_cash,
            "positions": {},
            "pending_orders": [],
            "history": [],
        }

        all_trades: List[Dict] = []
        open_positions: List[Dict[str, Any]] = []
        days_processed = 0
        total_orders_executed = 0

        if not self._trading_days:
            return {"error": f"无数据 ({self.start_date}~{self.end_date})"}

        for i, date in enumerate(self._trading_days):
            if verbose:
                print(f"  [{i+1}/{len(self._trading_days)}] {date}", end=" ", flush=True)

            # ── ① 执行 pending_orders（T+1 开盘价成交）──
            executed = self._execute_pending(date, vp)
            total_orders_executed += len(executed)
            for t in executed:
                all_trades.append(t)

            # ── ② mark-to-market（当日 close）──
            self._mark_to_market(date, vp)

            # ── ③ run_backtest ──
            try:
                if verbose:
                    result = run_backtest(date, vp, config_override,
                                          attribution_mode=self.mode)
                else:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = run_backtest(date, vp, config_override,
                                              attribution_mode=self.mode)
            except Exception as e:
                if verbose:
                    print(f"— 错误: {e}")
                continue

            actions = result.get("actions", [])
            days_processed += 1

            # ── ④ 生成 pending_orders ──
            next_date = self._next_trading_day(date)
            new_orders = 0
            actions = self._merge_risk_exit_actions(date, actions, vp)
            for a in actions:
                if not next_date:
                    break
                if a["action"] == "OPEN":
                    vp["pending_orders"].append({
                        "execute_date": next_date,
                        "code": a["code"],
                        "side": "BUY",
                        "target_weight": a.get("target_weight", 0),
                        "industry": a.get("industry", ""),
                    })
                    new_orders += 1
                elif a["action"] == "CLOSE":
                    vp["pending_orders"].append({
                        "execute_date": next_date,
                        "code": a["code"],
                        "side": "SELL",
                        "reason": a.get("reason", "CLOSE信号"),
                    })
                    new_orders += 1

            # ── ⑤ 记录净值 ──
            positions_value = vp["equity"] - vp["cash"]
            vp["history"].append({
                "date": date,
                "equity": round(vp["equity"], 2),
                "cash": round(vp["cash"], 2),
                "positions_value": round(positions_value, 2),
                "position_pct": round(positions_value / vp["equity"], 4) if vp["equity"] > 0 else 0,
                "drawdown": round(1 - vp["equity"] / max(vp["max_equity"], 1), 4),
            })

            if verbose:
                pos_count = len(vp["positions"])
                pending_count = len(vp["pending_orders"])
                actions_str = f"{len(actions)} actions"
                if new_orders:
                    opens = sum(1 for a in actions if a["action"] == "OPEN")
                    closes = sum(1 for a in actions if a["action"] == "CLOSE")
                    actions_str = f"OPEN:{opens} CLOSE:{closes}"
                print(f"— {actions_str} | pos:{pos_count} pend:{pending_count} "
                      f"eq={vp['equity']:,.0f} dd={vp['history'][-1]['drawdown']:.1%}")

        # 最后一天：未平仓持仓只做 mark-to-market，不计入已实现胜率。
        final_date = self._trading_days[-1] if self._trading_days else None
        if final_date:
            open_positions = self._open_position_snapshot(final_date, vp)
            self._mark_to_market(final_date, vp)

        return self._build_report(vp, all_trades, days_processed, total_orders_executed, open_positions)

    def run_with_daily_results(
        self,
        daily_results: Dict[str, Dict[str, Any]],
        verbose: bool = True,
    ) -> Dict[str, Any]:
        """Run simulation using precomputed daily controller results.

        `daily_results` is keyed by trade_date and must contain an `actions` list.
        This is used by attribution so buy_plan is computed once per date and
        reused across all modes.
        """
        vp = {
            "cash": self.initial_cash,
            "equity": self.initial_cash,
            "max_equity": self.initial_cash,
            "positions": {},
            "pending_orders": [],
            "history": [],
        }
        all_trades: List[Dict] = []
        open_positions: List[Dict[str, Any]] = []
        days_processed = 0
        total_orders_executed = 0

        if not self._trading_days:
            return {"error": f"无数据 ({self.start_date}~{self.end_date})"}

        for i, date in enumerate(self._trading_days):
            if verbose:
                print(f"  [{i+1}/{len(self._trading_days)}] {date}", end=" ", flush=True)

            executed = self._execute_pending(date, vp)
            total_orders_executed += len(executed)
            all_trades.extend(executed)
            self._mark_to_market(date, vp)

            result = daily_results.get(date, {})
            actions = self._merge_risk_exit_actions(date, result.get("actions", []), vp)
            days_processed += 1

            next_date = self._next_trading_day(date)
            new_orders = 0
            for a in actions:
                if not next_date:
                    break
                if a["action"] == "OPEN":
                    vp["pending_orders"].append({
                        "execute_date": next_date,
                        "code": a["code"],
                        "side": "BUY",
                        "target_weight": a.get("target_weight", 0),
                        "industry": a.get("industry", ""),
                    })
                    new_orders += 1
                elif a["action"] == "CLOSE":
                    vp["pending_orders"].append({
                        "execute_date": next_date,
                        "code": a["code"],
                        "side": "SELL",
                        "reason": a.get("reason", "CLOSE信号"),
                    })
                    new_orders += 1

            positions_value = vp["equity"] - vp["cash"]
            vp["history"].append({
                "date": date,
                "equity": round(vp["equity"], 2),
                "cash": round(vp["cash"], 2),
                "positions_value": round(positions_value, 2),
                "position_pct": round(positions_value / vp["equity"], 4) if vp["equity"] > 0 else 0,
                "drawdown": round(1 - vp["equity"] / max(vp["max_equity"], 1), 4),
            })

            if verbose:
                print(f"— {len(actions)} actions | pos:{len(vp['positions'])} "
                      f"pend:{len(vp['pending_orders'])} eq={vp['equity']:,.0f}")

        final_date = self._trading_days[-1] if self._trading_days else None
        if final_date:
            open_positions = self._open_position_snapshot(final_date, vp)
            self._mark_to_market(final_date, vp)

        return self._build_report(vp, all_trades, days_processed, total_orders_executed, open_positions)

    # ================================================================
    # Execution Simulator
    # ================================================================

    def _execute_pending(self, date: str, vp: Dict) -> List[Dict]:
        """执行当日到期的 pending_orders。返回已执行的 trade 记录列表。"""
        executed = []
        remaining_orders = []

        for order in vp.get("pending_orders", []):
            if order["execute_date"] != date:
                remaining_orders.append(order)
                continue

            code = order["code"]
            side = order["side"]
            open_price = self.prices.get_open(code, date)

            if open_price <= 0:
                # 无价格数据，跳过（保留订单下次尝试）
                remaining_orders.append(order)
                continue

            if side == "BUY":
                target_weight = order.get("target_weight", 0)
                if target_weight <= 0:
                    continue

                # 执行日按当前权益计算股数
                capital = vp["equity"] * target_weight
                shares = int(capital / open_price / 100) * 100  # A股100股整数倍
                if shares < 100:
                    continue  # 不够买1手

                cost = shares * open_price
                if cost > vp["cash"]:
                    # 现金不足，缩量
                    shares = int(vp["cash"] / open_price / 100) * 100
                    if shares < 100:
                        continue
                    cost = shares * open_price

                vp["cash"] -= cost
                vp["positions"][code] = {
                    "shares": shares,
                    "cost": open_price,
                    "entry_date": date,
                    "peak_price": open_price,
                    "weight": 0,           # mark-to-market 时更新
                    "unrealized_pnl": 0,   # mark-to-market 时更新
                    "industry": order.get("industry", ""),
                    "volatility": 0.02,    # 初始默认，后续 mark-to-market 可从 trends 更新
                }
                executed.append({
                    "code": code, "entry_date": date,
                    "buy_price": open_price, "shares": shares,
                    "target_weight": target_weight,
                })

            elif side == "SELL":
                pos = vp["positions"].get(code)
                if not pos:
                    continue
                sell_value = pos["shares"] * open_price
                vp["cash"] += sell_value
                ret = (open_price / pos["cost"] - 1) * 100 if pos["cost"] > 0 else 0
                holding = _holding_days(pos.get("entry_date", date), date)
                executed.append({
                    "code": code,
                    "entry_date": pos.get("entry_date", ""),
                    "exit_date": date,
                    "exit_reason": order.get("reason", "CLOSE信号"),
                    "buy_price": pos["cost"],
                    "sell_price": open_price,
                    "return_pct": round(ret, 2),
                    "holding_days": holding,
                })
                del vp["positions"][code]

        vp["pending_orders"] = remaining_orders
        return executed

    # ================================================================
    # Mark-to-Market
    # ================================================================

    def _mark_to_market(self, date: str, vp: Dict):
        """用当日 close 更新所有持仓的估值。"""
        positions_value = 0.0

        for code, pos in vp["positions"].items():
            close = self.prices.get_close(code, date)
            if close <= 0:
                close = pos["cost"]  # fallback
            pos["last_close"] = close
            pos["peak_price"] = max(pos.get("peak_price", pos["cost"]), close)
            pos["weight"] = pos["shares"] * close / vp["equity"] if vp["equity"] > 0 else 0
            pos["unrealized_pnl"] = close / pos["cost"] - 1 if pos["cost"] > 0 else 0
            positions_value += pos["shares"] * close

        vp["equity"] = vp["cash"] + positions_value
        vp["max_equity"] = max(vp["max_equity"], vp["equity"])

    def _merge_risk_exit_actions(self, date: str, actions: List[Dict], vp: Dict) -> List[Dict]:
        """Add systematic backtest exits after close, executed next trading day.

        The live controller may issue CLOSE when entry quality collapses. Backtest
        still needs mechanical exits, otherwise a rising window becomes a
        buy-and-hold-until-final-day test with inflated win rate.
        """
        existing_close_codes = {
            a.get("code") for a in actions
            if a.get("action") == "CLOSE" and a.get("code")
        }
        pending_sell_codes = {
            o.get("code") for o in vp.get("pending_orders", [])
            if o.get("side") == "SELL" and o.get("code")
        }
        risk_actions: List[Dict[str, Any]] = []

        for code, pos in vp.get("positions", {}).items():
            if code in existing_close_codes or code in pending_sell_codes:
                continue
            close = pos.get("last_close") or self.prices.get_close(code, date)
            cost = pos.get("cost", 0)
            if close <= 0 or cost <= 0:
                continue

            ret = close / cost - 1
            reason = None
            if self.stop_loss_pct and ret <= -self.stop_loss_pct:
                reason = f"止损触发 {ret:.2%} <= -{self.stop_loss_pct:.2%}"
            elif self.take_profit_pct and ret >= self.take_profit_pct:
                reason = f"止盈触发 {ret:.2%} >= {self.take_profit_pct:.2%}"
            elif self.trailing_stop_pct:
                peak = pos.get("peak_price", cost)
                drawdown = close / peak - 1 if peak > 0 else 0
                if peak > cost and drawdown <= -self.trailing_stop_pct:
                    reason = f"移动止损 {drawdown:.2%} <= -{self.trailing_stop_pct:.2%}"
            if not reason and self.max_hold_days:
                holding = _holding_days(pos.get("entry_date", date), date)
                if holding >= self.max_hold_days:
                    reason = f"最大持仓 {holding}天 >= {self.max_hold_days}天"

            if reason:
                risk_actions.append({
                    "code": code,
                    "action": "CLOSE",
                    "entry_score": None,
                    "reason": reason,
                })

        return list(actions) + risk_actions

    def _open_position_snapshot(self, final_date: str, vp: Dict) -> List[Dict[str, Any]]:
        """Snapshot still-open positions at the backtest end date."""
        rows: List[Dict[str, Any]] = []
        for code, pos in vp.get("positions", {}).items():
            close = self.prices.get_close(code, final_date)
            if close <= 0:
                close = pos.get("last_close") or pos.get("cost", 0)
            cost = pos.get("cost", 0)
            shares = pos.get("shares", 0)
            market_value = shares * close
            rows.append({
                "code": code,
                "entry_date": pos.get("entry_date", ""),
                "as_of_date": final_date,
                "buy_price": cost,
                "mark_price": close,
                "return_pct": round((close / cost - 1) * 100, 2) if cost > 0 else 0,
                "holding_days": _holding_days(pos.get("entry_date", ""), final_date),
                "shares": shares,
                "market_value": round(market_value, 2),
                "industry": pos.get("industry", ""),
            })
        return sorted(rows, key=lambda x: x.get("return_pct", 0), reverse=True)

    # ================================================================
    # Report
    # ================================================================

    def _build_report(self, vp: Dict, trades: List[Dict],
                      days: int, orders_executed: int,
                      open_positions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """生成组合级回测报告。"""
        history = vp.get("history", [])
        if not history:
            return {"error": "无历史记录", "days_processed": days}

        init_equity = self.initial_cash
        final_equity = history[-1]["equity"]
        total_return = (final_equity / init_equity - 1) * 100

        # 最大回撤
        max_dd = max((h.get("drawdown", 0) for h in history), default=0) * 100

        # 平均仓位
        avg_pos = sum(h.get("position_pct", 0) for h in history) / len(history) * 100

        # 交易统计：只统计策略自身退出的 realized trades。
        realized_trades = [
            t for t in trades
            if "return_pct" in t and t.get("exit_reason") != "回测结束平仓"
        ]
        wins = [t for t in realized_trades if t["return_pct"] > 0]
        realized_win_rate = len(wins) / len(realized_trades) * 100 if realized_trades else None
        open_positions = open_positions or []
        open_wins = [p for p in open_positions if p.get("return_pct", 0) > 0]
        open_win_rate = len(open_wins) / len(open_positions) * 100 if open_positions else None
        open_value = sum(p.get("market_value", 0) for p in open_positions)
        open_cost = sum(p.get("shares", 0) * p.get("buy_price", 0) for p in open_positions)
        open_pnl = open_value - open_cost

        exit_dist = defaultdict(int)
        for t in realized_trades:
            exit_dist[t.get("exit_reason", "未知")] += 1

        return {
            "days_processed": days,
            "mode": self.mode,
            "orders_executed": orders_executed,
            "portfolio_summary": {
                "initial_equity": init_equity,
                "final_equity": round(final_equity, 2),
                "total_return_pct": round(total_return, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "avg_position_pct": round(avg_pos, 1),
                "realized_trades": len(realized_trades),
                "realized_win_rate_pct": round(realized_win_rate, 1) if realized_win_rate is not None else None,
                "open_position_count": len(open_positions),
                "open_position_win_rate_pct": round(open_win_rate, 1) if open_win_rate is not None else None,
                "open_positions_market_value": round(open_value, 2),
                "open_positions_unrealized_pnl": round(open_pnl, 2),
                "open_positions_unrealized_pnl_pct": round(open_pnl / open_cost * 100, 2)
                if open_cost > 0 else None,
                "final_position_count": len(vp.get("positions", {})),
                "final_cash": round(vp.get("cash", 0), 2),
            },
            "exit_rules": {
                "stop_loss_pct": round(self.stop_loss_pct * 100, 2),
                "take_profit_pct": round(self.take_profit_pct * 100, 2),
                "max_hold_days": self.max_hold_days,
                "trailing_stop_pct": round(self.trailing_stop_pct * 100, 2),
            },
            "exit_distribution": dict(exit_dist),
            "daily_history": history,
            "trades": sorted(realized_trades, key=lambda x: x.get("return_pct", 0), reverse=True),
            "open_positions": open_positions,
        }


# ================================================================
# Formatter
# ================================================================

def format_report(report: Dict[str, Any]) -> str:
    if report.get("error"):
        return f"回测错误: {report['error']}"

    ps = report.get("portfolio_summary", {})
    lines = [
        "=" * 70,
        f"组合回测报告 ({report['days_processed']} 个交易日, mode={report.get('mode', 'full')})",
        "=" * 70,
        "",
        "── 组合概要 ──",
        f"  初始权益:     {ps.get('initial_equity', 0):,.0f}",
        f"  最终权益:     {ps.get('final_equity', 0):,.0f}",
        f"  总收益:       {ps.get('total_return_pct', 0):+.2f}%",
        f"  最大回撤:     {ps.get('max_drawdown_pct', 0):.2f}%",
        f"  平均仓位:     {ps.get('avg_position_pct', 0):.1f}%",
        f"  已实现交易:   {ps.get('realized_trades', 0)}",
        f"  已实现胜率:   {ps.get('realized_win_rate_pct') if ps.get('realized_win_rate_pct') is not None else 'N/A'}%",
        f"  期末未平仓:   {ps.get('open_position_count', 0)} 只 | "
        f"浮盈亏 {ps.get('open_positions_unrealized_pnl', 0):+,.0f} "
        f"({ps.get('open_positions_unrealized_pnl_pct') if ps.get('open_positions_unrealized_pnl_pct') is not None else 'N/A'}%)",
        f"  期末现金:     {ps.get('final_cash', 0):,.0f}",
        f"  执行订单:     {report.get('orders_executed', 0)}",
        "",
        "── 退出规则 ──",
    ]
    rules = report.get("exit_rules", {})
    lines.extend([
        f"  止损:         {rules.get('stop_loss_pct', 0):.2f}%",
        f"  止盈:         {rules.get('take_profit_pct', 0):.2f}%",
        f"  最大持仓:     {rules.get('max_hold_days', 0)} 天",
        f"  移动止损:     {rules.get('trailing_stop_pct', 0):.2f}%",
        "",
        "── 退出分布 ──",
    ])
    for reason, count in report.get("exit_distribution", {}).items():
        lines.append(f"  {reason}: {count} 笔")

    lines.extend([
        "",
        "── 每日权益（首尾各 5 日）──",
    ])
    hist = report.get("daily_history", [])
    for h in hist[:5]:
        lines.append(
            f"  {h['date']}  eq={h['equity']:,.0f}  cash={h['cash']:,.0f}  "
            f"pos={h['position_pct']:.1%}  dd={h['drawdown']:.2%}")
    if len(hist) > 10:
        lines.append("  ...")
    for h in hist[-5:]:
        lines.append(
            f"  {h['date']}  eq={h['equity']:,.0f}  cash={h['cash']:,.0f}  "
            f"pos={h['position_pct']:.1%}  dd={h['drawdown']:.2%}")

    lines.extend([
        "",
        "── 已实现交易（按收益排序，前 10）──",
    ])
    for t in report.get("trades", [])[:10]:
        lines.append(
            f"  {t['code']}  {t.get('entry_date', '')} → {t.get('exit_date', '')}  "
            f"{t['return_pct']:+.2f}%  ({t.get('exit_reason', '')})"
        )
    if not report.get("trades"):
        lines.append("  无已实现退出；当前结果主要来自持仓 mark-to-market。")

    lines.extend(["", "── 期末未平仓（按浮盈排序，前 10）──"])
    for p in report.get("open_positions", [])[:10]:
        lines.append(
            f"  {p['code']}  {p.get('entry_date', '')} → {p.get('as_of_date', '')}  "
            f"{p['return_pct']:+.2f}%  mark={p.get('mark_price', 0)}"
        )
    if not report.get("open_positions"):
        lines.append("  无期末未平仓。")

    lines.append("=" * 70)
    return "\n".join(lines)


def _aggregate_daily_diagnostics(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    totals = defaultdict(int)
    weighted = defaultdict(float)
    weight_counts = defaultdict(int)
    drop_reasons = defaultdict(int)

    for result in results.values():
        diag = result.get("diagnostics", {}) or {}
        for key in (
            "candidate_count", "decision_count", "new_decision_count",
            "entry_open_count", "entry_block_count", "risk_open_count",
            "risk_block_count", "sizing_positive_count", "sizing_zero_count",
            "action_count",
        ):
            totals[key] += int(diag.get(key, 0) or 0)
        for key in ("avg_target_weight", "avg_raw_weight", "avg_vol_norm"):
            value = diag.get(key)
            if isinstance(value, (int, float)):
                weighted[key] += value
                weight_counts[key] += 1
        for reason, count in (diag.get("drop_reasons") or {}).items():
            drop_reasons[reason] += int(count or 0)

    out = dict(totals)
    for key, value in weighted.items():
        out[key] = round(value / max(weight_counts[key], 1), 4)
    out["drop_reasons"] = dict(drop_reasons)
    return out


def _summary_for_attribution(
    name: str,
    report: Dict[str, Any],
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ps = report.get("portfolio_summary", {})
    return {
        "mode": name,
        "error": report.get("error"),
        "days_processed": report.get("days_processed", 0),
        "total_return_pct": ps.get("total_return_pct"),
        "max_drawdown_pct": ps.get("max_drawdown_pct"),
        "avg_position_pct": ps.get("avg_position_pct"),
        "realized_trades": ps.get("realized_trades"),
        "realized_win_rate_pct": ps.get("realized_win_rate_pct"),
        "open_position_count": ps.get("open_position_count"),
        "open_positions_unrealized_pnl_pct": ps.get("open_positions_unrealized_pnl_pct"),
        "orders_executed": report.get("orders_executed", 0),
        "final_equity": ps.get("final_equity"),
        "diagnostics": diagnostics or {},
    }


def run_attribution(
    start_date: str,
    end_date: str,
    budget: float = 0.60,
    baseline_vol: float = 0.025,
    top_n: int = 10,
    initial_cash: float = 1_000_000,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    max_hold_days: int = 0,
    trailing_stop_pct: float = 0.0,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Run staged attribution backtests on the same window and simulator.

    BuyPlan is computed once per date and reused across all modes.
    """
    from buy_plan import BuyPlanEngine
    from portfolio_controller import build_backtest_actions

    modes = ["baseline", "entry", "risk", "full"]
    base_engine = PortfolioBacktestEngine(
        start_date=start_date,
        end_date=end_date,
        budget=budget,
        baseline_vol=baseline_vol,
        top_n=top_n,
        initial_cash=initial_cash,
        mode="full",
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        max_hold_days=max_hold_days,
        trailing_stop_pct=trailing_stop_pct,
    )
    if not base_engine._trading_days:
        return {"error": f"无数据 ({start_date}~{end_date})"}

    config_override = {
        "max_risk_budget": budget,
        "baseline_vol": baseline_vol,
        "top_n": top_n,
    }
    mode_states = {
        mode: {
            "cash": initial_cash,
            "equity": initial_cash,
            "max_equity": initial_cash,
            "positions": {},
            "pending_orders": [],
        }
        for mode in modes
    }
    daily_results: Dict[str, Dict[str, Dict[str, Any]]] = {mode: {} for mode in modes}
    bp_engine = BuyPlanEngine()
    sim_engines = {
        mode: PortfolioBacktestEngine(
            start_date=start_date,
            end_date=end_date,
            budget=budget,
            baseline_vol=baseline_vol,
            top_n=top_n,
            initial_cash=initial_cash,
            mode=mode,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_hold_days=max_hold_days,
            trailing_stop_pct=trailing_stop_pct,
        )
        for mode in modes
    }
    bp_runs = 0

    for i, date in enumerate(base_engine._trading_days):
        if verbose:
            print(f"  [{i+1}/{len(base_engine._trading_days)}] {date} buy_plan", flush=True)
        if verbose:
            bp_result = bp_engine.run(
                top_n=top_n,
                target_date=date,
                initial_limit=5000,
                enrich_limit=0,
                apply_portfolio_penalty=False,
            )
        else:
            with contextlib.redirect_stdout(io.StringIO()):
                bp_result = bp_engine.run(
                    top_n=top_n,
                    target_date=date,
                    initial_limit=5000,
                    enrich_limit=0,
                    apply_portfolio_penalty=False,
                )
        bp_runs += 1

        for mode in modes:
            sim_engine = sim_engines[mode]
            # Bring the mode portfolio forward before generating same-day signals.
            sim_engine._execute_pending(date, mode_states[mode])
            sim_engine._mark_to_market(date, mode_states[mode])
            daily_results[mode][date] = build_backtest_actions(
                date=date,
                virtual_portfolio=mode_states[mode],
                bp_result=bp_result,
                config_override=config_override,
                attribution_mode=mode,
            )

            # Queue next-day orders so the state remains accurate for later dates.
            next_date = sim_engine._next_trading_day(date)
            if next_date:
                for action in daily_results[mode][date].get("actions", []):
                    if action["action"] == "OPEN":
                        mode_states[mode]["pending_orders"].append({
                            "execute_date": next_date,
                            "code": action["code"],
                            "side": "BUY",
                            "target_weight": action.get("target_weight", 0),
                            "industry": action.get("industry", ""),
                        })
                    elif action["action"] == "CLOSE":
                        mode_states[mode]["pending_orders"].append({
                            "execute_date": next_date,
                            "code": action["code"],
                            "side": "SELL",
                            "reason": action.get("reason", "CLOSE信号"),
                        })

    reports: Dict[str, Any] = {}
    summaries: List[Dict[str, Any]] = []
    diagnostics_by_mode = {
        mode: _aggregate_daily_diagnostics(daily_results[mode])
        for mode in modes
    }
    for mode in modes:
        engine = sim_engines[mode]
        report = engine.run_with_daily_results(daily_results[mode], verbose=False)
        reports[mode] = report
        summaries.append(_summary_for_attribution(
            mode,
            report,
            diagnostics_by_mode.get(mode, {}),
        ))

    base_return = summaries[0].get("total_return_pct")
    for row in summaries:
        if base_return is not None and row.get("total_return_pct") is not None:
            row["return_delta_vs_baseline"] = round(row["total_return_pct"] - base_return, 2)
        else:
            row["return_delta_vs_baseline"] = None

    return {
        "type": "backtest_attribution",
        "start_date": start_date,
        "end_date": end_date,
        "params": {
            "budget": budget,
            "baseline_vol": baseline_vol,
            "top_n": top_n,
            "initial_cash": initial_cash,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "max_hold_days": max_hold_days,
            "trailing_stop_pct": trailing_stop_pct,
        },
        "efficiency": {
            "buy_plan_runs": bp_runs,
            "mode_count": len(modes),
            "trading_days": len(base_engine._trading_days),
            "avoided_buy_plan_runs": max(0, len(modes) * len(base_engine._trading_days) - bp_runs),
        },
        "summaries": summaries,
        "diagnostics": diagnostics_by_mode,
        "reports": reports,
    }


def format_attribution_report(result: Dict[str, Any]) -> str:
    lines = [
        "=" * 92,
        f"全链路回测贡献拆解 ({result.get('start_date')} → {result.get('end_date')})",
        "=" * 92,
        "",
        "mode含义:",
        "  baseline = buy_plan TopN 等权买入",
        "  entry    = baseline + entry gate",
        "  risk     = entry + risk gate + position multiplier",
        "  full     = entry + risk + sizing",
        "",
        f"计算复用: buy_plan 实跑 {result.get('efficiency', {}).get('buy_plan_runs', '?')} 次，"
        f"避免重复 {result.get('efficiency', {}).get('avoided_buy_plan_runs', '?')} 次",
        "",
        f"{'mode':<10} {'收益':>8} {'vs_base':>8} {'最大回撤':>8} {'已实胜率':>8} {'已实':>6} {'未平':>6} {'浮盈%':>8} {'均仓位':>8}",
        "-" * 92,
    ]
    for row in result.get("summaries", []):
        if row.get("error"):
            lines.append(f"{row['mode']:<10} ERROR: {row['error']}")
            continue
        win_rate = row.get("realized_win_rate_pct")
        win_rate_text = f"{win_rate:.1f}%" if win_rate is not None else "N/A"
        open_pnl = row.get("open_positions_unrealized_pnl_pct")
        open_pnl_text = f"{open_pnl:.1f}%" if open_pnl is not None else "N/A"
        lines.append(
            f"{row['mode']:<10} "
            f"{row.get('total_return_pct', 0):>7.2f}% "
            f"{row.get('return_delta_vs_baseline', 0):>7.2f}% "
            f"{row.get('max_drawdown_pct', 0):>7.2f}% "
            f"{win_rate_text:>8} "
            f"{row.get('realized_trades', 0):>6} "
            f"{row.get('open_position_count', 0):>6} "
            f"{open_pnl_text:>8} "
            f"{row.get('avg_position_pct', 0):>7.1f}%"
        )
    lines.extend([
        "",
        "Layer diagnostics:",
        f"{'mode':<10} {'entry_ok':>8} {'entry_blk':>9} {'risk_ok':>8} {'risk_blk':>8} "
        f"{'sizing_ok':>9} {'sizing_0':>9} {'avg_w':>8} {'avg_vol':>8}",
        "-" * 92,
    ])
    for row in result.get("summaries", []):
        diag = row.get("diagnostics", {}) or {}
        lines.append(
            f"{row.get('mode', ''):<10} "
            f"{diag.get('entry_open_count', 0):>8} "
            f"{diag.get('entry_block_count', 0):>9} "
            f"{diag.get('risk_open_count', 0):>8} "
            f"{diag.get('risk_block_count', 0):>8} "
            f"{diag.get('sizing_positive_count', 0):>9} "
            f"{diag.get('sizing_zero_count', 0):>9} "
            f"{diag.get('avg_target_weight', 0):>8.2%} "
            f"{diag.get('avg_vol_norm', 0):>8.2f}"
        )
        drop_reasons = diag.get("drop_reasons") or {}
        if drop_reasons:
            reasons = ", ".join(f"{k}:{v}" for k, v in sorted(drop_reasons.items()))
            lines.append(f"{'':<10} drop_reasons: {reasons}")
    lines.append("=" * 92)
    return "\n".join(lines)


def save_attribution_report(result: Dict[str, Any]) -> Dict[str, str]:
    """Save attribution JSON/MD into reports/ without committing generated output."""
    reports_dir = os.path.join(PROJECT_ROOT, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = os.path.join(reports_dir, f"backtest_attribution_{stamp}")
    json_path = base + ".json"
    md_path = base + ".md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(format_attribution_report(result))
        f.write("\n")
    return {"json": json_path, "md": md_path}


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Portfolio Backtest — 全链路组合回测 (buy_plan → entry → risk → sizing)")
    parser.add_argument("--start", default="2026-03-02", help="开始日期")
    parser.add_argument("--end", default="2026-06-17", help="结束日期")
    parser.add_argument("--top", type=int, default=10, help="每日候选数")
    parser.add_argument("--budget", type=float, default=0.60,
                        help="max_risk_budget (默认 0.60)")
    parser.add_argument("--baseline-vol", type=float, default=0.025,
                        help="baseline_vol 波动率归一基准 (默认 0.025)")
    parser.add_argument("--cash", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--mode", choices=["baseline", "entry", "risk", "full"],
                        default="full", help="单次回测模式")
    parser.add_argument("--stop-loss", type=float, default=0.0,
                        help="回测价格止损百分比，0=关闭，默认关闭")
    parser.add_argument("--take-profit", type=float, default=0.0,
                        help="回测价格止盈百分比，0=关闭，默认关闭")
    parser.add_argument("--max-hold-days", type=int, default=0,
                        help="最大持仓自然日，0=关闭，默认关闭")
    parser.add_argument("--trailing-stop", type=float, default=0.0,
                        help="移动止损百分比，0=关闭，默认关闭")
    parser.add_argument("--attribution", action="store_true",
                        help="一次运行 baseline/entry/risk/full 四档贡献拆解")
    parser.add_argument("--save", action="store_true",
                        help="将 attribution 结果保存到 reports/backtest_attribution_YYYY-MM-DD.json|md")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()

    if args.attribution:
        if not args.quiet:
            print(f"回测贡献拆解窗口: {args.start} → {args.end}")
            print(f"参数: budget={args.budget}, baseline_vol={args.baseline_vol}, "
                  f"top_n={args.top}, cash={args.cash:,.0f}")
        result = run_attribution(
            start_date=args.start,
            end_date=args.end,
            budget=args.budget,
            baseline_vol=args.baseline_vol,
            top_n=args.top,
            initial_cash=args.cash,
            stop_loss_pct=_pct_arg(args.stop_loss),
            take_profit_pct=_pct_arg(args.take_profit),
            max_hold_days=args.max_hold_days,
            trailing_stop_pct=_pct_arg(args.trailing_stop),
            verbose=not args.quiet,
        )
        if args.save:
            paths = save_attribution_report(result)
            result["saved_paths"] = paths
            if not args.quiet:
                print(f"\n已保存: {paths['md']}")
                print(f"已保存: {paths['json']}")
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_attribution_report(result))
        return

    engine = PortfolioBacktestEngine(
        start_date=args.start, end_date=args.end,
        budget=args.budget, baseline_vol=args.baseline_vol,
        top_n=args.top, initial_cash=args.cash, mode=args.mode,
        stop_loss_pct=_pct_arg(args.stop_loss),
        take_profit_pct=_pct_arg(args.take_profit),
        max_hold_days=args.max_hold_days,
        trailing_stop_pct=_pct_arg(args.trailing_stop),
    )

    if not args.quiet:
        print(f"回测窗口: {args.start} → {args.end}")
        print(f"参数: budget={args.budget}, baseline_vol={args.baseline_vol}, "
              f"top_n={args.top}, cash={args.cash:,.0f}, mode={args.mode}")
        print(f"退出: stop_loss={args.stop_loss}%, take_profit={args.take_profit}%, "
              f"max_hold_days={args.max_hold_days}, trailing_stop={args.trailing_stop}%")
        print(f"可用交易日: {len(engine._trading_days)} 天")
        print()

    report = engine.run(verbose=not args.quiet)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
