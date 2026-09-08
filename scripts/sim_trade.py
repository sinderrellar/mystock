#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sim_trade.py —— 模拟交易撮合引擎 + 账户/持仓/流水 CRUD + portfolio.yaml 投影

职责（属于「投资智辩」统一控制台的模拟交易模块）：
- 多用户模拟账户（MongoDB `sim_accounts`，user_id 隔离，初始 100 万虚拟资金）
- 下单撮合：按实时价成交，佣金万2.5、印花税千1（仅卖出）、T+1（当日买入次日可卖）
- 持仓（`sim_positions`）与成交流水（`sim_trades`）
- 把模拟账户持仓/现金投影写回 data/portfolio.yaml，供 portfolio_strategy.review() 出组合全景

约束（遵守 CLAUDE.md）：
- 禁止 delete/remove：卖出归零的持仓标记 closed=True，不物理删除
- 一切交易数据只增/改，流水只 insert
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# 保证本文件可被独立执行（python3 scripts/sim_trade.py）时也能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml

from market_data_provider import MarketDataProvider

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
DEFAULT_PORTFOLIO = os.path.join(PROJECT_ROOT, "data", "portfolio.yaml")


def user_portfolio_path(user_id: str) -> str:
    """按用户隔离的投影文件路径（web 模拟交易用）。

    与离线手动组合 data/portfolio.yaml 完全分开，避免「某人下单 → 共享 yaml
    → 他人首登被种」的跨用户污染。user_id 做文件名安全化，防路径穿越。
    """
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", str(user_id or "anon")) or "anon"
    return os.path.join(PROJECT_ROOT, "data", f"portfolio_{safe}.yaml")


INITIAL_CASH = 1_000_000.0   # 初始虚拟资金（老板拍板）
COMMISSION_RATE = 0.00025    # 佣金 万2.5（买卖都收）
STAMP_TAX_RATE = 0.001       # 印花税 千1（仅卖出）

COL_ACCOUNTS = "sim_accounts"
COL_POSITIONS = "sim_positions"
COL_TRADES = "sim_trades"
COL_WATCH = "sim_watchlist"

# 用于区分「未传」与「显式传 null（清空）」的哨兵值
_UPDATE_UNSET = object()

_provider: Optional[MarketDataProvider] = None


def _get_provider() -> MarketDataProvider:
    global _provider
    if _provider is None:
        _provider = MarketDataProvider()
    return _provider


def _get_mongo():
    """复用 config_complete.yaml 的 MongoDB 配置，返回 tradingagents 库。"""
    from pymongo import MongoClient

    from factor_data_import_service import _load_mongodb_config

    cfg = _load_mongodb_config(DEFAULT_CONFIG)
    client = MongoClient(cfg["uri"], serverSelectionTimeoutMS=5000)
    return client[cfg["database"]]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ensure_indexes(db) -> None:
    """幂等建索引（user_id 隔离 + 唯一约束）。"""
    db[COL_ACCOUNTS].create_index([("user_id", 1)], unique=True, name="sim_user_id")
    db[COL_POSITIONS].create_index(
        [("user_id", 1), ("code", 1)], name="sim_pos_user_code")
    db[COL_TRADES].create_index([("user_id", 1), ("traded_at", -1)], name="sim_trade_user_time")
    db[COL_WATCH].create_index([("user_id", 1), ("code", 1)], name="sim_watch_user_code")


# ─────────────────────────── 账户 ───────────────────────────

def get_or_create_account(user_id: str) -> Dict[str, Any]:
    db = _get_mongo()
    _ensure_indexes(db)
    acct = db[COL_ACCOUNTS].find_one({"user_id": user_id})
    if acct is None:
        now = _now()
        acct = {
            "user_id": user_id,
            "cash": INITIAL_CASH,
            "initial_cash": INITIAL_CASH,
            "created_at": now,
            "updated_at": now,
        }
        db[COL_ACCOUNTS].insert_one(acct)
    return acct


def _settle_pending(db, user_id: str) -> None:
    """T+1 解锁：把「昨日及更早买入」的 pending_qty 转入 available_qty。"""
    today = _today()
    rows = db[COL_POSITIONS].find({
        "user_id": user_id,
        "closed": {"$ne": True},
        "pending_qty": {"$gt": 0},
    })
    for pos in rows:
        pd = pos.get("pending_date")
        if pd and pd < today:  # 严格早于今天 → 已隔一个交易日
            db[COL_POSITIONS].update_one(
                {"_id": pos["_id"]},
                {"$set": {
                    "available_qty": pos.get("available_qty", 0) + pos.get("pending_qty", 0),
                    "pending_qty": 0,
                    "pending_date": None,
                    "updated_at": _now(),
                }},
            )


# ─────────────────────────── 下单撮合 ───────────────────────────

def place_order(user_id: str, code: str, side: str, quantity: int) -> Dict[str, Any]:
    """下单。side=buy/sell。按实时价成交，失败抛 ValueError/RuntimeError。"""
    code = str(code or "").strip()
    if code.isdigit():
        code = code.zfill(6)
    if not code:
        raise ValueError("股票代码为空")

    side = (side or "").lower()
    if side not in ("buy", "sell"):
        raise ValueError("side 只能是 buy/sell")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValueError("数量必须是整数")
    if quantity <= 0:
        raise ValueError("数量必须为正整数")

    provider = _get_provider()
    price_info = provider.get_price(code, "A股")
    if not price_info.get("available") or not price_info.get("price"):
        raise RuntimeError(f"无法获取 {code} 实时价格，拒绝成交：{price_info.get('error') or '行情源不可用'}")
    price = float(price_info["price"])

    name, industry, market = code, "", "A股"
    info = provider.get_stock_info(code)
    if info.get("available") and info.get("data"):
        d = info["data"]
        name = d.get("name") or code
        industry = d.get("industry") or ""
        market = d.get("market") or "A股"

    db = _get_mongo()
    acct = get_or_create_account(user_id)
    _settle_pending(db, user_id)
    # settle 后重新取最新 cash
    acct = db[COL_ACCOUNTS].find_one({"user_id": user_id})

    amount = round(price * quantity, 2)
    commission = round(amount * COMMISSION_RATE, 2)

    if side == "buy":
        total_cost = round(amount + commission, 2)
        if acct["cash"] < total_cost:
            raise ValueError(
                f"资金不足：需 {total_cost:.2f}（含佣金 {commission:.2f}），可用现金 {acct['cash']:.2f}")
        new_cash = round(acct["cash"] - total_cost, 2)
        db[COL_ACCOUNTS].update_one(
            {"user_id": user_id},
            {"$set": {"cash": new_cash, "updated_at": _now()}},
        )

        pos = db[COL_POSITIONS].find_one(
            {"user_id": user_id, "code": code, "closed": {"$ne": True}})
        today = _today()
        if pos:
            total_qty = pos["quantity"] + quantity
            cost_basis = pos["cost_price"] * pos["quantity"] + total_cost
            new_cost = round(cost_basis / total_qty, 4)
            new_pending = pos.get("pending_qty", 0) + quantity
            db[COL_POSITIONS].update_one(
                {"_id": pos["_id"]},
                {"$set": {
                    "quantity": total_qty,
                    "cost_price": new_cost,
                    "pending_qty": new_pending,
                    "pending_date": today,
                    "name": name,
                    "industry": industry,
                    "updated_at": _now(),
                }},
            )
        else:
            db[COL_POSITIONS].insert_one({
                "user_id": user_id, "code": code, "name": name, "market": market,
                "quantity": quantity, "available_qty": 0,
                "pending_qty": quantity, "pending_date": today,
                "cost_price": round(total_cost / quantity, 4),
                "stop_loss_price": None, "take_profit_price": None,
                "thesis": "", "industry": industry,
                "closed": False, "created_at": _now(), "updated_at": _now(),
            })
        db[COL_TRADES].insert_one({
            "user_id": user_id, "code": code, "name": name, "side": "buy",
            "price": price, "quantity": quantity, "amount": amount,
            "fee": commission, "tax": 0.0, "traded_at": _now(),
        })
        return {
            "ok": True, "side": "buy", "code": code, "name": name,
            "price": price, "quantity": quantity, "amount": amount,
            "fee": commission, "tax": 0.0, "cash": new_cash,
            "note": "当日买入，T+1 次日可卖",
        }

    # sell
    pos = db[COL_POSITIONS].find_one(
        {"user_id": user_id, "code": code, "closed": {"$ne": True}})
    if not pos:
        raise ValueError(f"未持有 {code}，无法卖出")
    available = pos.get("available_qty", 0)
    if available < quantity:
        raise ValueError(f"可卖数量不足：可卖 {available}，想卖 {quantity}（T+1 当日买入不可卖）")

    tax = round(amount * STAMP_TAX_RATE, 2)
    net = round(amount - commission - tax, 2)
    new_cash = round(acct["cash"] + net, 2)
    db[COL_ACCOUNTS].update_one(
        {"user_id": user_id},
        {"$set": {"cash": new_cash, "updated_at": _now()}},
    )

    new_qty = pos["quantity"] - quantity
    if new_qty <= 0:
        # 归零：标记 closed，不物理删除（守则禁止 delete）
        db[COL_POSITIONS].update_one(
            {"_id": pos["_id"]},
            {"$set": {
                "quantity": 0, "available_qty": 0, "pending_qty": 0,
                "closed": True, "updated_at": _now(),
            }},
        )
    else:
        db[COL_POSITIONS].update_one(
            {"_id": pos["_id"]},
            {"$set": {
                "quantity": new_qty,
                "available_qty": available - quantity,
                "updated_at": _now(),
            }},
        )

    db[COL_TRADES].insert_one({
        "user_id": user_id, "code": code, "name": name, "side": "sell",
        "price": price, "quantity": quantity, "amount": amount,
        "fee": commission, "tax": tax, "traded_at": _now(),
    })
    return {
        "ok": True, "side": "sell", "code": code, "name": name,
        "price": price, "quantity": quantity, "amount": amount,
        "fee": commission, "tax": tax, "cash": new_cash,
    }


# ─────────────────────────── 查询 ───────────────────────────

def get_account(user_id: str) -> Dict[str, Any]:
    """返回账户 + 持仓（补实时价/市值/盈亏；港股按 HKD 计，总资产折算 CNY）。"""
    db = _get_mongo()
    acct = get_or_create_account(user_id)
    _settle_pending(db, user_id)

    rows = list(db[COL_POSITIONS].find({"user_id": user_id, "closed": {"$ne": True}}))
    provider = _get_provider()

    # 汇率：把各持仓币种折到 CNY（港股 HKD 自动查实时价，失败走内置兜底 0.8567）
    currencies = sorted({(p.get("currency") or "CNY") for p in rows} | {"CNY"})
    try:
        fx = provider.resolve_fx_rates("CNY", {"CNY": 1, "HKD": "auto", "USD": "auto"}, currencies)
    except Exception:
        fx = {"CNY": 1.0, "HKD": 0.8567291793, "USD": 7.2}

    pos_list: List[Dict[str, Any]] = []
    total_mv = 0.0    # 折 CNY 总市值
    total_cost = 0.0  # 折 CNY 总成本
    for p in rows:
        code = p["code"]
        currency = p.get("currency") or "CNY"
        fx_rate = float(fx.get(currency) or 1.0)
        cur = float(p.get("cost_price") or 0.0)
        try:
            pi = provider.get_price(code, p.get("market") or "A股")
            if pi.get("available") and pi.get("price"):
                cur = float(pi["price"])
        except Exception:
            pass
        qty = float(p.get("quantity") or 0)
        cost_price = float(p.get("cost_price") or 0.0)
        mv = cur * qty
        cost = cost_price * qty
        total_mv += mv * fx_rate
        total_cost += cost * fx_rate
        pos_list.append({
            "code": code,
            "name": p.get("name") or code,
            "market": p.get("market") or "A股",
            "currency": currency,
            "quantity": qty,
            "available_qty": p.get("available_qty", 0),
            "cost_price": cost_price,
            "current_price": round(cur, 2),
            "market_value": round(mv, 2),
            "unrealized_pnl": round(mv - cost, 2),
            "unrealized_pnl_pct": round((mv - cost) / cost * 100, 2) if cost else 0.0,
            "industry": p.get("industry", ""),
            "stop_loss_price": p.get("stop_loss_price"),
            "take_profit_price": p.get("take_profit_price"),
            "thesis": p.get("thesis", ""),
        })

    total_assets = round(acct["cash"] + total_mv, 2)
    pnl = round(total_mv - total_cost, 2)
    return {
        "user_id": user_id,
        "cash": round(acct["cash"], 2),
        "initial_cash": acct.get("initial_cash", INITIAL_CASH),
        "positions": pos_list,
        "position_value": round(total_mv, 2),
        "total_assets": total_assets,
        "unrealized_pnl": pnl,
        "unrealized_pnl_pct": round(pnl / total_cost * 100, 2) if total_cost else 0.0,
    }


def get_trades(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    db = _get_mongo()
    rows = db[COL_TRADES].find({"user_id": user_id}).sort("traded_at", -1).limit(limit)
    return [{
        "code": r.get("code"),
        "name": r.get("name"),
        "side": r.get("side"),
        "price": r.get("price"),
        "quantity": r.get("quantity"),
        "amount": r.get("amount"),
        "fee": r.get("fee"),
        "tax": r.get("tax"),
        "traded_at": r.get("traded_at"),
    } for r in rows]


# ─────────────────────────── 观察池 ───────────────────────────

def _lookup_stock_basic(db, code: str) -> Dict[str, Any]:
    """从全市场基础表取元数据（name/industry/close），无则返回空 dict。"""
    doc = db["stock_basic_info"].find_one({"code": code})
    return doc or {}


def get_watchlist(user_id: str) -> List[Dict[str, Any]]:
    """返回当前用户观察池（active 项，按加入/更新倒序）。"""
    db = _get_mongo()
    _ensure_indexes(db)
    rows = db[COL_WATCH].find({"user_id": user_id, "active": {"$ne": False}}).sort("updated_at", -1)
    return [{
        "code": r.get("code"),
        "name": r.get("name") or r.get("code"),
        "industry": r.get("industry", ""),
        "thesis": r.get("thesis", ""),
        "add_price": r.get("add_price"),
        "created_at": r.get("created_at"),
    } for r in rows]


def add_watchlist(user_id: str, code: str, thesis: str = "") -> Dict[str, Any]:
    """加入观察池（幂等：已在池→更新 thesis/参考价；曾移除→重新激活）。"""
    code = str(code or "").strip()
    if code.isdigit():
        code = code.zfill(6)
    if not code:
        raise ValueError("股票代码为空")
    db = _get_mongo()
    _ensure_indexes(db)
    meta = _lookup_stock_basic(db, code)
    name = meta.get("name") or code
    industry = meta.get("industry") or ""
    add_price = meta.get("close")
    now = _now()

    existing = db[COL_WATCH].find_one({"user_id": user_id, "code": code})
    if existing:
        if existing.get("active") is False:
            # 曾移除 → 重新激活并刷新信息
            db[COL_WATCH].update_one({"_id": existing["_id"]}, {"$set": {
                "active": True, "name": name, "industry": industry,
                "thesis": thesis or "", "add_price": add_price,
                "updated_at": now,
            }})
        else:
            # 已在池 → 合并 thesis（非空覆盖），参考价保留旧的
            db[COL_WATCH].update_one({"_id": existing["_id"]}, {"$set": {
                "thesis": thesis or existing.get("thesis", ""),
                "updated_at": now,
            }})
        return {"ok": True, "added": False, "code": code, "name": name, "note": "已在观察池"}
    db[COL_WATCH].insert_one({
        "user_id": user_id, "code": code, "name": name, "industry": industry,
        "thesis": thesis or "", "add_price": add_price,
        "active": True, "created_at": now, "updated_at": now,
    })
    return {"ok": True, "added": True, "code": code, "name": name, "note": "已加入观察池"}


def remove_watchlist(user_id: str, code: str) -> Dict[str, Any]:
    """移出观察池（软删：active=False，遵守禁 delete 守则）。"""
    code = str(code or "").strip()
    db = _get_mongo()
    r = db[COL_WATCH].update_many(
        {"user_id": user_id, "code": code, "active": {"$ne": False}},
        {"$set": {"active": False, "updated_at": _now()}},
    )
    if r.modified_count == 0:
        raise ValueError("该标的不在观察池中")
    return {"ok": True, "code": code, "note": "已移出观察池"}


# ─────────────────────────── 持仓设置（止盈/止损/逻辑） ───────────────────────────

def update_position(
    user_id: str,
    code: str,
    stop_loss_price: Any = _UPDATE_UNSET,
    take_profit_price: Any = _UPDATE_UNSET,
    thesis: Any = _UPDATE_UNSET,
) -> Dict[str, Any]:
    """更新持仓的风控/逻辑字段（仅覆盖显式传入的字段；传 None 表示清空）。

    与下单同一条真相源（sim_positions），改后 sync 会投影到 portfolio.yaml，
    组合全景的止损/加仓决策卡即读到最新值。
    """
    code = str(code or "").strip()
    if code.isdigit():
        code = code.zfill(6)
    db = _get_mongo()
    pos = db[COL_POSITIONS].find_one({"user_id": user_id, "code": code, "closed": {"$ne": True}})
    if not pos:
        raise ValueError(f"未持有 {code}，无法设置")
    patch: Dict[str, Any] = {}
    if stop_loss_price is not _UPDATE_UNSET:
        patch["stop_loss_price"] = None if stop_loss_price is None else float(stop_loss_price)
    if take_profit_price is not _UPDATE_UNSET:
        patch["take_profit_price"] = None if take_profit_price is None else float(take_profit_price)
    if thesis is not _UPDATE_UNSET:
        patch["thesis"] = None if thesis is None else str(thesis).strip()
    if not patch:
        return {"ok": True, "code": code, "note": "无变更"}
    patch["updated_at"] = _now()
    db[COL_POSITIONS].update_one({"_id": pos["_id"]}, {"$set": patch})
    return {"ok": True, "code": code, "note": "已更新持仓设置"}


def search_stocks(q: str, limit: int = 10) -> List[Dict[str, Any]]:
    """按代码前缀或名称模糊搜索股票（复用 stock_basic_info）。"""
    q = (q or "").strip()
    if not q:
        return []
    import re

    db = _get_mongo()
    basic = db["stock_basic_info"]
    if q.isdigit():
        docs = basic.find({"code": {"$regex": f"^{q}"}}).limit(limit)
    else:
        docs = basic.find({"name": {"$regex": re.escape(q)}}).limit(limit)
    return [{
        "code": d.get("code"),
        "name": d.get("name"),
        "industry": d.get("industry"),
        "pe": d.get("pe"),
        "pb": d.get("pb"),
        "market": d.get("market"),
        "close": d.get("close"),
    } for d in docs]


# ─────────────────────────── portfolio.yaml 投影 ───────────────────────────

def sync_to_yaml(user_id: str) -> Dict[str, Any]:
    """把模拟账户持仓/现金投影写回「按用户隔离」的 portfolio_<user_id>.yaml，供 review() 出组合全景。

    单一真相源在 Mongo；yaml 只是投影。account 段的 target/risk_profile 等约束保留原样。
    """
    db = _get_mongo()
    acct = get_or_create_account(user_id)
    _settle_pending(db, user_id)
    rows = list(db[COL_POSITIONS].find({"user_id": user_id, "closed": {"$ne": True}}))
    path = user_portfolio_path(user_id)

    ydata: Dict[str, Any] = {}
    # 模板：优先读本用户已有投影；首次投影则继承离线手动 portfolio.yaml 的 account 约束
    for _src in (path, DEFAULT_PORTFOLIO):
        if os.path.exists(_src):
            with open(_src, "r", encoding="utf-8") as f:
                ydata = yaml.safe_load(f) or {}
            break

    ydata.setdefault("account", {})
    ydata["account"].setdefault("cash_by_currency", {})
    ydata["account"]["cash"] = round(acct["cash"], 2)
    ydata["account"]["cash_by_currency"]["CNY"] = round(acct["cash"], 2)

    positions: List[Dict[str, Any]] = []
    for p in rows:
        row: Dict[str, Any] = {
            "code": str(p["code"]),
            "name": p.get("name") or str(p["code"]),
            "market": p.get("market") or "A股",
            "asset_type": p.get("asset_type") or "stock",
            "currency": p.get("currency") or "CNY",
            "quantity": float(p.get("quantity") or 0),
            "cost_price": float(p.get("cost_price") or 0.0),
        }
        if p.get("industry"):
            row["industry"] = p["industry"]
        if p.get("stop_loss_price"):
            row["stop_loss_price"] = p["stop_loss_price"]
        if p.get("take_profit_price"):
            row["take_profit_price"] = p["take_profit_price"]
        if p.get("thesis"):
            row["thesis"] = p["thesis"]
        positions.append(row)
    ydata["positions"] = positions

    # 观察池投影：active 项同步到 yaml 的 watchlist，避免 review() 读到陈旧观察池
    watch_rows = list(db[COL_WATCH].find({"user_id": user_id, "active": {"$ne": False}}).sort("updated_at", -1))
    watchlist: List[Dict[str, Any]] = []
    for w in watch_rows:
        wrow: Dict[str, Any] = {
            "code": str(w.get("code", "")),
            "name": w.get("name") or str(w.get("code", "")),
            "industry": w.get("industry", ""),
            "thesis": w.get("thesis", ""),
        }
        if w.get("add_price"):
            wrow["add_price"] = w["add_price"]
        if w.get("created_at"):
            wrow["created_at"] = w["created_at"]
        watchlist.append(wrow)
    ydata["watchlist"] = watchlist

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(ydata, f, allow_unicode=True, sort_keys=False)

    return {"positions": len(positions), "cash": round(acct["cash"], 2)}


def init_user(user_id: str) -> Dict[str, Any]:
    """初始化用户：建 100 万账户（幂等）。

    不再从共享 portfolio.yaml 播种持仓——那是「某人下单 → 共享 yaml → 他人首登被种」
    跨用户污染的根源（2026-09-07 宁德时代污染事件）。新用户一律空仓 100 万起，
    真实持仓通过下单/观察池在各自账户内建立。
    """
    return get_or_create_account(user_id)


# ─────────────────────────── 自测入口 ───────────────────────────

if __name__ == "__main__":
    import json

    uid = os.environ.get("SIM_USER", "test_user")
    print(f"=== init_user({uid}) ===")
    acct = init_user(uid)
    print(json.dumps({"cash": acct["cash"], "initial_cash": acct.get("initial_cash")}, ensure_ascii=False))

    print(f"\n=== place_order buy 300750 x100 ===")
    try:
        r = place_order(uid, "300750", "buy", 100)
        print(json.dumps(r, ensure_ascii=False))
    except Exception as e:
        print(f"ERROR: {e}")

    print(f"\n=== get_account ===")
    print(json.dumps(get_account(uid), ensure_ascii=False, default=str))

    print(f"\n=== sync_to_yaml ===")
    print(json.dumps(sync_to_yaml(uid), ensure_ascii=False))
