#!/usr/bin/env python3
"""
Portfolio State Loader — 新链路组合状态装配层。

职责：
- 读取 data/portfolio.yaml
- 获取汇率和实时/缓存价格
- 计算持仓权重、盈亏、现金比例和 drawdown

不做 review、不做买卖判断、不生成 UI 文案。
"""

import os
import sys
from typing import Any, Dict, List, Tuple

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config
from market_data_provider import MarketDataProvider


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip().lower() in ("", "none", "null"):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_portfolio_yaml(path: str = None) -> Dict[str, Any]:
    """读取手工维护的组合配置。"""
    portfolio_path = path or os.path.join(PROJECT_ROOT, "data", "portfolio.yaml")
    with open(portfolio_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("account", {})
    data.setdefault("positions", [])
    data.setdefault("watchlist", [])
    return data


def _collect_currencies(account: Dict[str, Any], positions: List[Dict[str, Any]]) -> List[str]:
    currencies = list((account.get("cash_by_currency") or {}).keys())
    currencies.extend(str(p.get("currency", "CNY")) for p in positions)
    return currencies


def _calculate_cash(account: Dict[str, Any], fx_rates: Dict[str, float]) -> float:
    cash_by_currency = account.get("cash_by_currency") or {}
    if not cash_by_currency:
        return _to_float(account.get("cash"))

    total = 0.0
    for currency, value in cash_by_currency.items():
        total += _to_float(value) * _to_float(fx_rates.get(currency), 1.0)
    return total


def _build_position_rows(
    data_provider: MarketDataProvider,
    positions: List[Dict[str, Any]],
    fx_rates: Dict[str, float],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for item in positions:
        code = str(item.get("code", "")).strip()
        market = str(item.get("market", "A股"))
        asset_type = str(item.get("asset_type", "stock"))
        currency = str(item.get("currency", "CNY"))
        fx_rate = _to_float(fx_rates.get(currency), 1.0)
        quantity = _to_float(item.get("quantity"))
        cost_price = _to_float(item.get("cost_price"))

        price_result = data_provider.get_price(
            code=code,
            market=market,
            asset_type=asset_type,
            allow_akshare=bool(item.get("allow_akshare_price_fallback", False)),
            cost_price=cost_price,
        )
        current_price = _to_float(price_result.get("price"), cost_price)

        cost_value_local = quantity * cost_price
        market_value_local = quantity * current_price
        cost_value = cost_value_local * fx_rate
        market_value = market_value_local * fx_rate
        unrealized_pnl = market_value - cost_value
        unrealized_pnl_pct = unrealized_pnl / cost_value if cost_value else 0.0

        volatility = 0.02
        try:
            trend_doc = data_provider.mongo.db["stock_trends"].find_one(
                {"code": code}, sort=[("trade_date", -1)]
            )
            if trend_doc and trend_doc.get("volatility_20d") is not None:
                raw_vol = _to_float(trend_doc.get("volatility_20d"), 2.0)
                volatility = raw_vol / 100 if raw_vol > 1 else raw_vol
                volatility = max(0.01, min(volatility, 0.10))
        except Exception:
            pass

        row = dict(item)
        row.update({
            "code": code,
            "market": market,
            "asset_type": asset_type,
            "currency": currency,
            "fx_rate_to_base": fx_rate,
            "quantity": quantity,
            "cost_price": cost_price,
            "current_price": current_price,
            "price_available": bool(price_result.get("available")),
            "price_source": price_result.get("source", "unknown"),
            "price_error": price_result.get("error", ""),
            "cost_value": round(cost_value, 2),
            "market_value": round(market_value, 2),
            "market_value_local": round(market_value_local, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct * 100, 2),
            "volatility": volatility,
        })
        rows.append(row)

    return rows


def build_live_state(path: str = None) -> Dict[str, Any]:
    """构造 portfolio_controller.run() 使用的 live 组合状态。"""
    data = load_portfolio_yaml(path)
    account = data.get("account", {})
    positions = data.get("positions", []) or []

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    store = MongoFactorDataStore(_load_mongodb_config(config_path))
    data_provider = MarketDataProvider(store)

    base_currency = account.get("base_currency", "CNY")
    fx_rates = data_provider.resolve_fx_rates(
        base_currency,
        account.get("fx_rates_to_base") or {},
        _collect_currencies(account, positions),
    )

    rows = _build_position_rows(data_provider, positions, fx_rates)
    position_value = sum(r["market_value"] for r in rows)
    cash = _calculate_cash(account, fx_rates)
    configured_total_assets = _to_float(account.get("total_assets"))
    if configured_total_assets > 0:
        total_assets = configured_total_assets
        cash = max(total_assets - position_value, 0)
        cash_source = "derived_from_total_assets"
    else:
        total_assets = cash + position_value
        cash_source = "cash_by_currency" if account.get("cash_by_currency") else "cash"

    for row in rows:
        row["weight_pct"] = round(row["market_value"] / total_assets * 100, 2) if total_assets else 0

    cost_total = sum(r["cost_value"] for r in rows)
    pnl_total = sum(r["unrealized_pnl"] for r in rows)
    unrealized_pnl_pct = pnl_total / cost_total * 100 if cost_total else 0.0

    target = account.get("target", {}) or {}
    max_drawdown = _to_float(target.get("max_drawdown_pct"), 20) / 100
    drawdown = abs(unrealized_pnl_pct) / 100 if unrealized_pnl_pct < 0 else 0.0

    pos_list = []
    for row in rows:
        pos_list.append({
            "code": row["code"],
            "weight": row.get("weight_pct", 0) / 100,
            "pnl": row.get("unrealized_pnl_pct", 0) / 100,
            "industry": row.get("industry", ""),
            "volatility": row.get("volatility", 0.02),
        })

    pf_meta = {
        "total_equity": total_assets,
        "equity": total_assets,
        "cash_ratio": cash / total_assets if total_assets > 0 else 0,
        "drawdown": drawdown,
        "max_drawdown": max_drawdown,
        "prev_mode": "NORMAL",
        "max_risk_budget": 0.60,
        "positions": {
            p["code"]: {
                "weight": p["weight"],
                "pnl": p["pnl"],
                "industry": p["industry"],
                "volatility": p["volatility"],
            }
            for p in pos_list
        },
    }

    summary = {
        "total_assets": round(total_assets, 2),
        "position_value": round(position_value, 2),
        "cash": round(cash, 2),
        "cash_pct": round(pf_meta["cash_ratio"] * 100, 2),
        "cash_source": cash_source,
        "unrealized_pnl": round(pnl_total, 2),
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        "base_currency": base_currency,
    }

    return {
        "summary": summary,
        "positions": rows,
        "pf_meta": pf_meta,
        "pos_list": pos_list,
        "fx_rates": fx_rates,
        "source": "portfolio_state_loader",
    }
