#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lightweight multi-factor data import service.

This script reuses the useful parts of TradingAgents-CN-main's data sync design:
standardized documents, MongoDB indexes, and bulk upsert. It intentionally avoids
importing the TradingAgents app/worker stack so this project can keep a small
runtime surface.
"""
import argparse
import os
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any, Dict, Iterable, List, Optional

import yaml


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
DEFAULT_PORTFOLIO = os.path.join(PROJECT_ROOT, "data", "portfolio.yaml")


class ConfigError(RuntimeError):
    """Raised when config_complete.yaml is syntactically or structurally invalid."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "" or value == "--":
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_code(code: Any, market: str = "A股") -> str:
    text = str(code or "").strip()
    if market == "港股":
        return text.lstrip("0").zfill(5)
    if text.isdigit():
        return text.zfill(6)
    return text


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"Invalid YAML in {path}{location}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a YAML mapping at top level")
    return data


def _require_mapping(root: Dict[str, Any], path: str) -> Dict[str, Any]:
    node: Any = root
    for part in path.split("."):
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            raise ConfigError(f"Missing or invalid mapping: {path}")
        node = node[part]
    return node


def _require_number(root: Dict[str, Any], path: str) -> float:
    node: Any = root
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"Missing numeric config: {path}")
        node = node[part]
    if not isinstance(node, (int, float)):
        raise ConfigError(f"Config {path} must be numeric, got {type(node).__name__}")
    return float(node)


def validate_system_config(config: Dict[str, Any], config_path: str = DEFAULT_CONFIG) -> None:
    """Validate config blocks that are required by import/backtest pipelines."""
    mongodb = _require_mapping(config, "data_sources.mongodb")
    if not mongodb.get("enabled"):
        raise ConfigError("data_sources.mongodb.enabled must be true")
    if not mongodb.get("uri"):
        raise ConfigError("data_sources.mongodb.uri is required")
    _require_mapping(config, "data_sources.mongodb.collections")

    _require_mapping(config, "pyramid_middle_layer.market_regime.alpha_weights")
    sizing = _require_mapping(config, "pyramid_middle_layer.position_sizing")
    _require_number(config, "pyramid_middle_layer.position_sizing.baseline_vol")
    budget = sizing.get("max_risk_budget")
    if not isinstance(budget, dict):
        raise ConfigError("pyramid_middle_layer.position_sizing.max_risk_budget must be a mapping")
    for key in ("strong", "neutral", "weak"):
        if not isinstance(budget.get(key), (int, float)):
            raise ConfigError(f"Missing numeric config: pyramid_middle_layer.position_sizing.max_risk_budget.{key}")

    _require_mapping(config, "pyramid_middle_layer.turnaround.weights")
    _require_mapping(config, "pyramid_middle_layer.turnaround.forecast_weight")
    _require_mapping(config, "pyramid_middle_layer.sector_radar")
    _require_mapping(config, "pyramid_bottom_layer.position_sizing")


def _load_mongodb_config(config_path: str) -> Dict[str, Any]:
    config = _load_yaml(config_path)
    validate_system_config(config, config_path)
    mongodb = ((config.get("data_sources") or {}).get("mongodb") or {})
    if not mongodb.get("enabled"):
        raise RuntimeError("config 中 data_sources.mongodb.enabled 未开启")
    if not mongodb.get("uri"):
        raise RuntimeError("config 中 data_sources.mongodb.uri 未配置")
    collections = mongodb.get("collections") or {}
    return {
        "uri": mongodb["uri"],
        "database": mongodb.get("database", "tradingagents"),
        "collections": {
            "basic_info": collections.get("basic_info", "stock_basic_info"),
            "daily_quotes": collections.get("daily_quotes", "stock_daily_quotes"),
            "financial_data": collections.get("financial_data", "stock_financial_data"),
            "hsgt_flow": collections.get("hsgt_flow", "stock_hsgt_flow"),
        },
    }


class MongoFactorDataStore:
    """统一的 MongoDB 数据存储层，同时支持 Phase 1 选股查询和 Phase 2 数据导入。

    连接管理、索引创建和基础 CRUD 由此类统一提供。
    """

    def __init__(self, mongodb_config: Dict[str, Any]):
        from pymongo import MongoClient

        self.client = MongoClient(mongodb_config["uri"], serverSelectionTimeoutMS=5000)
        self.client.admin.command("ping")
        self.db = self.client[mongodb_config["database"]]
        self.collections = mongodb_config["collections"]
        self.available = True
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        basic = self.db[self.collections["basic_info"]]
        quotes = self.db[self.collections["daily_quotes"]]
        financial = self.db[self.collections["financial_data"]]

        basic.create_index([("code", 1)], name="code_index")
        basic.create_index([("market", 1)], name="market_index")
        basic.create_index([("updated_at", -1)], name="updated_at_index")
        quotes.create_index(
            [("code", 1), ("trade_date", 1), ("data_source", 1), ("period", 1)],
            unique=True,
            name="code_date_source_period_unique",
        )
        quotes.create_index([("code", 1), ("trade_date", -1)], name="code_date_index")
        financial.create_index(
            [("code", 1), ("report_period", 1), ("data_source", 1)],
            unique=True,
            name="code_period_source_unique",
        )
        financial.create_index([("code", 1), ("report_period", -1)], name="code_period_index")

    def upsert_basic(self, doc: Dict[str, Any]) -> None:
        now = _utc_now()
        doc = {**doc, "updated_at": now}

        # 保护已有数据不被 None/0/空字符串覆盖（根源修复）
        protect_fields = ("pe", "pb", "total_mv", "latest_amount", "close", "dividend_yield",
                          "industry", "industry_code")
        existing = self.get_basic(doc["code"])
        if existing:
            for field in protect_fields:
                if doc.get(field) in (None, 0, ""):
                    old_val = existing.get(field)
                    if old_val is not None and old_val != "" and old_val != 0:
                        doc[field] = old_val

        self.db[self.collections["basic_info"]].update_one(
            {"code": doc["code"]},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def upsert_quotes(self, docs: Iterable[Dict[str, Any]]) -> int:
        from pymongo import ReplaceOne

        operations = []
        now = _utc_now()
        for doc in docs:
            full_doc = {
                **doc,
                "period": doc.get("period", "daily"),
                "updated_at": now,
            }
            full_doc.setdefault("created_at", now)
            operations.append(
                ReplaceOne(
                    {
                        "code": full_doc["code"],
                        "trade_date": full_doc["trade_date"],
                        "data_source": full_doc["data_source"],
                        "period": full_doc["period"],
                    },
                    full_doc,
                    upsert=True,
                )
            )
        if not operations:
            return 0
        result = self.db[self.collections["daily_quotes"]].bulk_write(operations, ordered=False)
        return result.upserted_count + result.modified_count

    def upsert_financial(self, doc: Dict[str, Any]) -> None:
        now = _utc_now()
        full_doc = {
            **doc,
            "updated_at": now,
        }
        full_doc.setdefault("created_at", now)
        self.db[self.collections["financial_data"]].replace_one(
            {
                "code": full_doc["code"],
                "report_period": full_doc["report_period"],
                "data_source": full_doc["data_source"],
            },
            full_doc,
            upsert=True,
        )

    def get_basic(self, code: str) -> Optional[Dict[str, Any]]:
        return self.db[self.collections["basic_info"]].find_one({"code": code})

    def get_latest_quote(self, code: str) -> Optional[Dict[str, Any]]:
        return self.db[self.collections["daily_quotes"]].find_one(
            {"code": code, "period": "daily"},
            sort=[("trade_date", -1)],
        )

    def count_recent_quotes(self, code: str, limit: int = 180) -> int:
        return self.db[self.collections["daily_quotes"]].count_documents({
            "code": code,
            "period": "daily",
        }, limit=limit)

    def get_latest_financial(self, code: str) -> Optional[Dict[str, Any]]:
        return self.db[self.collections["financial_data"]].find_one(
            {"code": code},
            sort=[("report_period", -1)],
        )

    def _get_collection(self, key: str):
        """按 key 获取 MongoDB collection 对象（兼容 Phase 1 调用方）。"""
        name = self.collections.get(key)
        if not name:
            return None
        return self.db[name]

    # ========== Phase 1 兼容查询方法 ==========

    def get_stock_list(
        self,
        limit: int = 500,
        market_allowlist: Optional[List[str]] = None,
        min_amount: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """获取基础股票池，按成交额降序，支持市场和成交额过滤。"""
        query: Dict[str, Any] = {}
        if market_allowlist:
            query["market"] = {"$in": market_allowlist}
        if min_amount is not None:
            query["latest_amount"] = {"$gte": min_amount}

        try:
            cursor = self.db[self.collections["basic_info"]].find(query).sort(
                "latest_amount", -1)
            if limit:
                cursor = cursor.limit(int(limit))
            return list(cursor)
        except Exception:
            return []

    def get_recent_quotes(self, code: str, limit: int = 60,
                          as_of_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取近期日线行情（倒序，最新在前）。as_of_date 限制日期上限。"""
        try:
            query: Dict[str, Any] = {"code": code, "period": "daily"}
            if as_of_date:
                query["trade_date"] = {"$lte": as_of_date}
            cursor = self.db[self.collections["daily_quotes"]].find(
                query,
                sort=[("trade_date", -1)],
            ).limit(limit)
            return list(cursor)
        except Exception:
            return []

    def get_financial_data(self, code: str) -> Optional[Dict[str, Any]]:
        """获取最新财务数据（get_latest_financial 的别名）。"""
        return self.get_latest_financial(code)

    def get_all_sectors(self) -> List[str]:
        """获取所有行业分类。"""
        try:
            industries = self.db[self.collections["basic_info"]].distinct("industry")
            return [i for i in industries if i and isinstance(i, str)]
        except Exception:
            return []

    def get_sector_stocks(self, sector: str) -> List[Dict[str, Any]]:
        """获取某行业的所有股票。"""
        try:
            return list(self.db[self.collections["basic_info"]].find({"industry": sector}))
        except Exception:
            return []

    def get_sector_performance_fast(self, lookback_days: int = 20) -> List[Dict[str, Any]]:
        """快速计算行业表现（聚合管道，每行业最多采样5只）。"""
        basic_coll = self.db[self.collections["basic_info"]]
        quotes_coll = self.db[self.collections["daily_quotes"]]
        if basic_coll is None or quotes_coll is None:
            return []

        try:
            pipeline = [
                {"$match": {"industry": {"$ne": None}}},
                {"$group": {
                    "_id": "$industry",
                    "codes": {"$push": "$code"},
                    "count": {"$sum": 1},
                }},
                {"$match": {"count": {"$gte": 3}}},
            ]
            sector_data = list(basic_coll.aggregate(pipeline))
            if not sector_data:
                return []

            sector_performance = []
            for sector_info in sector_data:
                sector = sector_info["_id"]
                codes = sector_info["codes"][:5]

                total_return = 0.0
                valid_count = 0
                for code in codes:
                    quotes = list(quotes_coll.find(
                        {"code": code},
                        {"close": 1, "trade_date": 1},
                        sort=[("trade_date", -1)],
                    ).limit(lookback_days + 1))

                    if len(quotes) >= 2:
                        latest = quotes[0].get("close")
                        oldest = quotes[-1].get("close")
                        if latest and oldest and float(oldest) > 0:
                            ret = (float(latest) - float(oldest)) / float(oldest) * 100
                            total_return += ret
                            valid_count += 1

                if valid_count > 0:
                    sector_performance.append({
                        "sector": sector,
                        "avg_return": round(total_return / valid_count, 2),
                        "stock_count": sector_info["count"],
                        "valid_count": valid_count,
                    })

            sector_performance.sort(key=lambda x: x["avg_return"], reverse=True)
            return sector_performance
        except Exception:
            return []


class FactorDataImporter:
    """Fetch and normalize A-share/HK factor input data."""

    def __init__(self, store: MongoFactorDataStore, quote_limit: int = 180):
        self.store = store
        self.quote_limit = quote_limit
        self._ak = None

    @property
    def ak(self):
        if self._ak is None:
            import akshare as ak

            self._ak = ak
        return self._ak

    def sync_positions(self, positions: List[Dict[str, Any]], sleep_seconds: float = 0.4) -> Dict[str, Any]:
        stats = {"success": 0, "failed": 0, "items": []}
        total = len(positions)
        for i, position in enumerate(positions):
            code = _clean_code(position.get("code"), position.get("market", "A股"))
            if not code:
                continue
            try:
                item = self.sync_position({**position, "code": code})
                stats["items"].append(item)
                stats["success"] += 1
            except Exception as exc:
                stats["items"].append({"code": code, "ok": False, "error": str(exc)})
                stats["failed"] += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            if total >= 50 and (i + 1) % 100 == 0:
                print(f"  进度: {i+1}/{total} (成功 {stats['success']}, 失败 {stats['failed']})")
        return stats

    def sync_universe_basic(self, market: str = "A股") -> Dict[str, Any]:
        if market == "港股":
            return self._sync_hk_universe_basic()
        return self._sync_a_universe_basic()

    def sync_position(self, position: Dict[str, Any]) -> Dict[str, Any]:
        market = position.get("market", "A股")
        asset_type = position.get("asset_type", "stock")
        warnings = []
        if market == "港股":
            basic = self._safe_fetch("港股基础", self._fetch_hk_basic, position, self._base_basic_doc(position, position["code"], "港股", "HKD"), warnings)
            quotes = self._safe_fetch("港股行情", self._fetch_hk_quotes, position, [], warnings)
            financial = self._safe_fetch("港股财务", self._fetch_hk_financial, position, None, warnings)
        elif asset_type == "etf":
            basic = self._safe_fetch("ETF基础", self._fetch_a_basic, position, self._base_basic_doc(position, position["code"], "A股", "CNY"), warnings)
            quotes = self._safe_fetch("ETF行情", self._fetch_etf_quotes, position, [], warnings)
            financial = None
        else:
            basic = self._safe_fetch("A股基础", self._fetch_a_basic, position, self._base_basic_doc(position, position["code"], "A股", "CNY"), warnings)
            quotes = self._safe_fetch("A股行情", self._fetch_a_quotes, position, [], warnings)
            financial = self._safe_fetch("A股财务", self._fetch_a_financial, position, None, warnings)

        self._enrich_basic_valuation(basic, position, financial, warnings)
        self.store.upsert_basic(basic)
        quote_count = self.store.upsert_quotes(quotes)
        if financial:
            self.store.upsert_financial(financial)

        return {
            "code": position["code"],
            "name": position.get("name"),
            "market": market,
            "ok": True,
            "quotes": quote_count,
            "financial": bool(financial),
            "warnings": warnings,
        }

    def _safe_fetch(self, label: str, func, position: Dict[str, Any], fallback: Any, warnings: List[str]) -> Any:
        try:
            return self._with_retries(partial(func, position), label)
        except Exception as exc:
            warnings.append(f"{label}失败: {exc}")
            return fallback

    def _with_retries(self, func, label: str, retries: int = 3, delay_seconds: float = 1.2) -> Any:
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return func()
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(delay_seconds * attempt)
        raise RuntimeError(f"{label}连续{retries}次失败: {last_exc}")

    def _enrich_basic_valuation(
        self,
        basic: Dict[str, Any],
        position: Dict[str, Any],
        financial: Optional[Dict[str, Any]],
        warnings: List[str],
    ) -> None:
        """补齐 PE/PB/股息率等估值指标。

        AKShare 的 A 股/港股 spot 接口不一定稳定返回 PE/PB/股息率。
        单资产同步时先用已获取的财务指标推导 PE/PB，再把 Yahoo quote 作为
        最后一层兜底；全市场基础池不会逐只调 Yahoo，避免请求量过大。
        """
        self._derive_valuation_from_financial(basic, financial)

        need_value_metrics = (
            basic.get("pe") in (None, "", 0)
            or basic.get("pb") in (None, "", 0)
        )
        if not need_value_metrics:
            return

        try:
            from market_data_provider import MarketDataProvider

            snapshot = MarketDataProvider().get_quote_snapshot(
                code=basic.get("code") or position.get("code"),
                market=position.get("market", basic.get("display_market") or basic.get("market")),
            )
        except Exception as exc:
            warnings.append(f"估值兜底失败: {exc}")
            return

        if not snapshot.get("available"):
            warnings.append(f"估值兜底不可用: {snapshot.get('reason', '未知原因')}")
            return

        metrics = snapshot.get("metrics") or {}
        trailing_pe = metrics.get("trailing_pe")
        forward_pe = metrics.get("forward_pe")
        price_to_book = metrics.get("price_to_book")
        dividend_yield = metrics.get("dividend_yield")

        if basic.get("pe") in (None, "", 0):
            basic["pe"] = trailing_pe or forward_pe
        if basic.get("pe_ratio") in (None, "", 0):
            basic["pe_ratio"] = trailing_pe or forward_pe
        if basic.get("pb") in (None, "", 0):
            basic["pb"] = price_to_book
        if basic.get("pb_ratio") in (None, "", 0):
            basic["pb_ratio"] = price_to_book
        if basic.get("dividend_yield") in (None, ""):
            basic["dividend_yield"] = dividend_yield
        if metrics.get("market_cap") and not basic.get("total_mv"):
            basic["total_mv"] = metrics.get("market_cap")

        basic["valuation_source"] = snapshot.get("source")
        basic["valuation_symbol"] = snapshot.get("symbol")

    def _derive_valuation_from_financial(
        self,
        basic: Dict[str, Any],
        financial: Optional[Dict[str, Any]],
    ) -> None:
        if not financial:
            return

        price = _safe_float(basic.get("close"))
        if not price or price <= 0:
            return

        eps_ttm = _safe_float(financial.get("eps_ttm") or financial.get("eps_basic"))
        bps = _safe_float(financial.get("bps"))

        if basic.get("pe") in (None, "", 0) and eps_ttm and eps_ttm > 0:
            basic["pe"] = round(price / eps_ttm, 4)
            basic["pe_ratio"] = basic["pe"]
        if basic.get("pb") in (None, "", 0) and bps and bps > 0:
            basic["pb"] = round(price / bps, 4)
            basic["pb_ratio"] = basic["pb"]

        if basic.get("pe") or basic.get("pb"):
            basic["valuation_source"] = "derived_from_financial"

    def _fetch_a_basic(self, position: Dict[str, Any]) -> Dict[str, Any]:
        code = _clean_code(position.get("code"), "A股")
        basic = self._base_basic_doc(position, code, "A股", "CNY")
        basic["market"] = self._a_board(code)

        # 腾讯单股接口（稳通，有 PE/PB/市值）
        fetched = self._fetch_basic_via_tencent_a(code)
        if fetched:
            basic.update(fetched)
        else:
            basic["basic_warning"] = "A股基础数据获取失败: 腾讯接口不通"
        return basic

    @staticmethod
    def _fetch_basic_via_tencent_a(code: str) -> Optional[Dict[str, Any]]:
        """腾讯A股行情：PE=f39, PB=f46, 总市值(亿)=f45, 52周高/低=f47/f48。"""
        import urllib.request
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = f"https://qt.gtimg.cn/q={prefix}{code}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                text = resp.read().decode("gbk", errors="ignore")
            payload = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = payload.split("~")
            price = _safe_float(fields[3] if len(fields) > 3 else None)
            if not price or price <= 0:
                return None
            return {
                "name": fields[1] if len(fields) > 1 else "",
                "close": price,
                "pct_chg": _safe_float(fields[32] if len(fields) > 32 else None, None),
                "pe": _safe_float(fields[39] if len(fields) > 39 else None, None),
                "pb": _safe_float(fields[46] if len(fields) > 46 else None, None),
                "total_mv": round(_safe_float(fields[45] if len(fields) > 45 else None, 0) * 1e8, 2) if _safe_float(fields[45] if len(fields) > 45 else None, None) else None,
                "latest_amount": _safe_float(fields[37] if len(fields) > 37 else None, None),
                "fifty_two_week_high": _safe_float(fields[47] if len(fields) > 47 else None, None),
                "fifty_two_week_low": _safe_float(fields[48] if len(fields) > 48 else None, None),
                "data_source": "tencent_a",
            }
        except Exception:
            return None

    @staticmethod
    def _eastmoney_secid(code: str) -> str:
        if code.startswith(("5", "6", "9")):
            return f"1.{code}"
        return f"0.{code}"

    @staticmethod
    def _fetch_basic_via_eastmoney(code: str) -> Optional[Dict[str, Any]]:
        """通过东方财富 push2 单股接口获取基础信息（轻量，已验证通）。"""
        import json
        import urllib.parse
        import urllib.request

        secid = FactorDataImporter._eastmoney_secid(code)
        params = urllib.parse.urlencode({
            "secid": secid,
            "fields": "f43,f57,f58,f9,f37,f20,f21,f6,f116",
        })
        url = f"https://push2.eastmoney.com/api/qt/stock/get?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = (payload.get("data") or {})
            price = _safe_float(data.get("f43"))
            if not price or price <= 0:
                return None
            return {
                "name": data.get("f57", ""),
                "close": round(price / 100, 4) if price > 100 else price,
                "pct_chg": _safe_float(data.get("f58"), None),
                "pe": _safe_float(data.get("f9"), None),
                "pb": _safe_float(data.get("f37"), None),
                "total_mv": _safe_float(data.get("f20"), None),
                "circ_mv": _safe_float(data.get("f21"), None),
                "latest_amount": _safe_float(data.get("f6"), None),
                "turnover_rate": _safe_float(data.get("f116"), None),
                "data_source": "eastmoney_push2",
            }
        except Exception:
            return None

    @staticmethod
    def _fetch_basic_via_tencent(code: str) -> Optional[Dict[str, Any]]:
        """通过腾讯行情接口获取基础信息（兜底）。"""
        import urllib.request

        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
        url = f"https://qt.gtimg.cn/q={prefix}{code}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                text = resp.read().decode("gbk", errors="ignore")
            payload = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = payload.split("~")
            price = _safe_float(fields[3] if len(fields) > 3 else None)
            if not price or price <= 0:
                return None
            return {
                "name": fields[1] if len(fields) > 1 else "",
                "close": price,
                "latest_amount": _safe_float(fields[6] if len(fields) > 6 else None, None),
                "data_source": "tencent",
            }
        except Exception:
            return None

    def _sync_a_universe_basic(self) -> Dict[str, Any]:
        df = None
        last_exc = None
        for attempt in range(1, 4):
            try:
                df = self.ak.stock_zh_a_spot_em()
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    import time
                    time.sleep(3 * attempt)

        if df is None or df.empty:
            return {"market": "A股", "updated": 0, "failed": 0,
                    "error": f"重试3次后仍失败: {last_exc}"}

        updated = 0
        failed = 0
        for _, row in df.iterrows():
            code = _clean_code(row.get("代码"), "A股")
            if not code:
                failed += 1
                continue
            doc = self._base_basic_doc(
                {
                    "code": code,
                    "name": row.get("名称"),
                    "asset_type": "stock",
                },
                code,
                "A股",
                "CNY",
            )
            doc.update({
                "market": self._a_board(code),
                "display_market": "A股",
                "name": row.get("名称") or code,
                "close": _safe_float(row.get("最新价")),
                "latest_amount": _safe_float(row.get("成交额")),
                "turnover_rate": _safe_float(row.get("换手率")),
                "pe": _safe_float(row.get("市盈率-动态") or row.get("市盈率")),
                "pb": _safe_float(row.get("市净率")),
                "total_mv": _safe_float(row.get("总市值")),
                "circ_mv": _safe_float(row.get("流通市值")),
                "pct_chg": _safe_float(row.get("涨跌幅")),
                "data_source": "akshare_stock_zh_a_spot_em",
            })
            self.store.upsert_basic(doc)
            updated += 1
        return {"market": "A股", "updated": updated, "failed": failed}

    def _fetch_a_quotes(self, position: Dict[str, Any]) -> List[Dict[str, Any]]:
        code = _clean_code(position.get("code"), "A股")
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"

        # 优先腾讯K线（稳通，有超时保护），避免 Sina 限流挂死
        try:
            docs = self._fetch_quotes_via_tencent(code, prefix)
            if docs:
                return docs
        except Exception:
            pass

        # 兜底: AKShare/Sina（带超时保护）
        symbol = f"{prefix}{code}"
        end_date = _utc_now().strftime("%Y%m%d")
        start_date = (_utc_now() - __import__("datetime").timedelta(days=self.quote_limit + 30)).strftime("%Y%m%d")
        try:
            df = self.ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date, adjust="qfq")
        except Exception:
            return []
        if df is None or df.empty:
            return []
        df = df.tail(self.quote_limit)
        docs = []
        for _, row in df.iterrows():
            trade_date = str(row.get("date") or "")[:10]
            close = _safe_float(row.get("close"))
            if not trade_date or not close:
                continue
            docs.append({
                "code": code,
                "symbol": code,
                "market": "A股",
                "currency": "CNY",
                "trade_date": trade_date,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": close,
                "volume": _safe_float(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "turnover_rate": _safe_float(row.get("turnover"), None),
                "data_source": "akshare_sina_daily",
                "period": "daily",
            })
        return docs

    def _fetch_etf_quotes(self, position: Dict[str, Any]) -> List[Dict[str, Any]]:
        code = _clean_code(position.get("code"), "A股")
        prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"

        # 腾讯K线（ETF和股票通用，最稳定）
        try:
            docs = self._fetch_quotes_via_tencent(code, prefix)
            if docs:
                return docs
        except Exception:
            pass

        # 兜底: Eastmoney → Sina
        end_date = _utc_now().strftime("%Y%m%d")
        start_date = (_utc_now() - __import__("datetime").timedelta(days=self.quote_limit + 30)).strftime("%Y%m%d")
        try:
            df = self.ak.fund_etf_hist_em(symbol=code, period="daily",
                                          start_date=start_date, end_date=end_date, adjust="qfq")
            if df is not None and not df.empty:
                return self._parse_etf_quotes(code, df, "akshare_fund_etf_hist_em")
        except Exception:
            pass
        try:
            df = self.ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
                                          start_date=start_date, end_date=end_date, adjust="qfq")
            if df is not None and not df.empty:
                return self._parse_etf_quotes_sina(code, df)
        except Exception:
            pass
        return []

    @staticmethod
    def _fetch_quotes_via_tencent(code: str, prefix: str) -> List[Dict[str, Any]]:
        """腾讯K线接口（ETF和股票通用，已验证稳通）。"""
        import json as _json
        import urllib.request as _req
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,200,qfq"
        request = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _req.urlopen(request, timeout=8) as resp:
            data = _json.loads(resp.read())
        klines = (data.get("data") or {}).get(f"{prefix}{code}", {}).get("day") or []
        if not klines:
            klines = (data.get("data") or {}).get(f"{prefix}{code}", {}).get("qfqday") or []
        docs = []
        for k in klines[-200:]:
            if len(k) < 5:
                continue
            trade_date, open_p, close_p, high_p, low_p, volume = k[0], k[1], k[2], k[3], k[4], k[5]
            close = _safe_float(close_p)
            if not trade_date or not close:
                continue
            docs.append({
                "code": code, "symbol": code, "market": "A股", "currency": "CNY",
                "trade_date": str(trade_date)[:10],
                "open": _safe_float(open_p),
                "high": _safe_float(high_p),
                "low": _safe_float(low_p),
                "close": close,
                "volume": _safe_float(volume),
                "data_source": "tencent_kline", "period": "daily",
            })
        return docs

    @staticmethod
    def _parse_etf_quotes(code: str, df: Any, source: str) -> List[Dict[str, Any]]:
        docs = []
        for _, row in df.iterrows():
            trade_date = str(row.get("日期") or "")[:10]
            close = _safe_float(row.get("收盘"))
            if not trade_date or not close:
                continue
            docs.append({
                "code": code, "symbol": code, "market": "A股", "currency": "CNY",
                "trade_date": trade_date,
                "open": _safe_float(row.get("开盘")),
                "high": _safe_float(row.get("最高")),
                "low": _safe_float(row.get("最低")),
                "close": close,
                "volume": _safe_float(row.get("成交量")),
                "amount": _safe_float(row.get("成交额")),
                "turnover_rate": _safe_float(row.get("换手率"), None),
                "data_source": source, "period": "daily",
            })
        return docs

    @staticmethod
    def _parse_etf_quotes_sina(code: str, df: Any) -> List[Dict[str, Any]]:
        docs = []
        for _, row in df.iterrows():
            trade_date = str(row.get("date") or "")[:10]
            close = _safe_float(row.get("close"))
            if not trade_date or not close:
                continue
            docs.append({
                "code": code, "symbol": code, "market": "A股", "currency": "CNY",
                "trade_date": trade_date,
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "close": close,
                "volume": _safe_float(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "turnover_rate": _safe_float(row.get("turnover"), None),
                "data_source": "akshare_sina_etf_daily", "period": "daily",
            })
        return docs

    def _fetch_a_financial(self, position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = _clean_code(position.get("code"), "A股")
        try:
            main = self.ak.stock_financial_abstract(symbol=code)
        except Exception:
            main = None
        doc = self._extract_financial_from_akshare_abstract(code, "A股", "CNY", main)
        if not doc:
            return None
        doc["data_source"] = "akshare_stock_financial_abstract"

        # 补充现金流数据
        cf = self._fetch_a_cashflow(code)
        if cf:
            doc.update(cf)
            # 用 abstract 中的净利润计算 OCF/NetIncome
            ni = doc.get("net_income")
            ocf = cf.get("operating_cash_flow")
            if ni and ocf and ni > 0:
                doc["ocf_to_net_income"] = round(ocf / ni, 4)

        # 合并一致预期数据（来自 Tushare forecast API，存于 stock_signals）
        if self.store:
            try:
                fc = self.store.db["stock_signals"].find_one(
                    {"code": code},
                    {"forecast_min": 1, "forecast_max": 1})
                if fc:
                    if fc.get("forecast_min") is not None:
                        doc["forecast_min"] = fc["forecast_min"]
                    if fc.get("forecast_max") is not None:
                        doc["forecast_max"] = fc["forecast_max"]
            except Exception:
                pass

        return doc

    @staticmethod
    def _parse_cf_value(raw: Any) -> Optional[float]:
        """解析带单位的现金流数值，如 '714.47亿' → 71447000000。"""
        if raw is None or raw is False or raw == "False":
            return None
        text = str(raw).strip()
        if not text:
            return None
        try:
            val = float(text)
            return val
        except ValueError:
            pass
        # 提取数字和单位
        import re
        m = re.match(r"([\d.]+)\s*(亿|万)?", text)
        if not m:
            return None
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "亿":
            val *= 100_000_000
        elif unit == "万":
            val *= 10_000
        return val

    def _fetch_a_cashflow(self, code: str) -> Optional[Dict[str, Any]]:
        """从同花顺获取最新一期现金流量表，提取经营现金流净额。"""
        try:
            df = self.ak.stock_financial_cash_ths(symbol=code, indicator="按报告期")
        except Exception:
            return None
        if df is None or df.empty:
            return None

        latest = df.iloc[0]
        ocf = self._parse_cf_value(latest.get("*经营活动产生的现金流量净额"))
        capex = self._parse_cf_value(
            latest.get("购建固定资产、无形资产和其他长期资产支付的现金"))

        result: Dict[str, Any] = {}
        if ocf is not None:
            result["operating_cash_flow"] = ocf
            if capex is not None:
                result["free_cash_flow"] = round(ocf - capex, 4)
        return result if result else None

    def _fetch_hk_basic(self, position: Dict[str, Any]) -> Dict[str, Any]:
        code = _clean_code(position.get("code"), "港股")
        basic = self._base_basic_doc(position, code, "港股", "HKD")

        # 腾讯单股接口（稳通，有 PE/PB/市值/股息率）
        fetched = self._fetch_basic_via_tencent_hk(code)
        if fetched:
            basic.update(fetched)
        else:
            basic["basic_warning"] = "港股基础数据获取失败: 所有轻量源均不通"
        return basic

    @staticmethod
    def _fetch_basic_via_tencent_hk(code: str) -> Optional[Dict[str, Any]]:
        """通过腾讯行情接口获取港股基础信息（含 PE/PB/市值）。"""
        import urllib.request

        url = f"https://qt.gtimg.cn/q=hk{code}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                text = resp.read().decode("gbk", errors="ignore")
            payload = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = payload.split("~")
            price = _safe_float(fields[3] if len(fields) > 3 else None)
            if not price or price <= 0:
                return None
            # 字段索引: 1=名称, 3=最新价, 31=涨跌额, 32=涨跌幅,
            #           33=最高, 34=最低, 37=成交额, 39=PE, 43=PB,
            #           44=总市值(亿), 47=股息率(%), 48=52周高, 49=52周低
            return {
                "name": fields[1] if len(fields) > 1 else "",
                "close": price,
                "pct_chg": _safe_float(fields[32] if len(fields) > 32 else None, None),
                "pe": _safe_float(fields[39] if len(fields) > 39 else None, None),
                "pb": _safe_float(fields[43] if len(fields) > 43 else None, None),
                "dividend_yield": _safe_float(fields[47] if len(fields) > 47 else None, None),
                "total_mv": round(_safe_float(fields[44] if len(fields) > 44 else None, 0) * 1e8, 2) if _safe_float(fields[44] if len(fields) > 44 else None, None) else None,
                "latest_amount": _safe_float(fields[37] if len(fields) > 37 else None, None),
                "fifty_two_week_high": _safe_float(fields[48] if len(fields) > 48 else None, None),
                "fifty_two_week_low": _safe_float(fields[49] if len(fields) > 49 else None, None),
                "data_source": "tencent_hk",
            }
        except Exception:
            return None

    @staticmethod
    def _fetch_basic_via_eastmoney_hk(code: str) -> Optional[Dict[str, Any]]:
        """通过东方财富 push2 港股单股接口获取基础信息。"""
        import json
        import urllib.parse
        import urllib.request

        secid = f"116.{code.lstrip('0')}"
        params = urllib.parse.urlencode({
            "secid": secid,
            "fields": "f43,f57,f58,f9,f37,f20,f6",
        })
        url = f"https://push2.eastmoney.com/api/qt/stock/get?{params}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = (payload.get("data") or {})
            price = _safe_float(data.get("f43"))
            if not price or price <= 0:
                return None
            return {
                "name": data.get("f57", ""),
                "close": round(price / 100, 4) if price > 100 else price,
                "pct_chg": _safe_float(data.get("f58"), None),
                "pe": _safe_float(data.get("f9"), None),
                "pb": _safe_float(data.get("f37"), None),
                "total_mv": _safe_float(data.get("f20"), None),
                "latest_amount": _safe_float(data.get("f6"), None),
                "data_source": "eastmoney_push2_hk",
            }
        except Exception:
            return None

    def _sync_hk_universe_basic(self) -> Dict[str, Any]:
        df = None
        last_exc = None
        for attempt in range(1, 4):
            try:
                df = self.ak.stock_hk_spot()
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    import time
                    time.sleep(3 * attempt)

        if df is None or df.empty:
            return {"market": "港股", "updated": 0, "failed": 0,
                    "error": f"重试3次后仍失败: {last_exc}"}

        updated = 0
        failed = 0
        for _, row in df.iterrows():
            code = _clean_code(row.get("代码"), "港股")
            if not code:
                failed += 1
                continue
            doc = self._base_basic_doc(
                {
                    "code": code,
                    "name": row.get("中文名称") or row.get("名称"),
                    "asset_type": "stock",
                },
                code,
                "港股",
                "HKD",
            )
            doc.update({
                "name": row.get("中文名称") or row.get("名称") or code,
                "close": _safe_float(row.get("最新价")),
                "latest_amount": _safe_float(row.get("成交额")),
                "pe": _safe_float(row.get("市盈率")),
                "total_mv": _safe_float(row.get("总市值")),
                "pct_chg": _safe_float(row.get("涨跌幅")),
                "data_source": "akshare_stock_hk_spot",
            })
            self.store.upsert_basic(doc)
            updated += 1
        return {"market": "港股", "updated": updated, "failed": failed}

    def _fetch_hk_quotes(self, position: Dict[str, Any]) -> List[Dict[str, Any]]:
        code = _clean_code(position.get("code"), "港股")
        df = self.ak.stock_hk_daily(symbol=code, adjust="qfq")
        if df is None or df.empty:
            return []
        df = df.tail(self.quote_limit)
        docs = []
        for _, row in df.iterrows():
            trade_date = row.get("date") or row.get("日期")
            docs.append({
                "code": code,
                "symbol": code,
                "market": "港股",
                "currency": "HKD",
                "trade_date": str(trade_date)[:10],
                "open": _safe_float(row.get("open") or row.get("开盘")),
                "high": _safe_float(row.get("high") or row.get("最高")),
                "low": _safe_float(row.get("low") or row.get("最低")),
                "close": _safe_float(row.get("close") or row.get("收盘")),
                "volume": _safe_float(row.get("volume") or row.get("成交量")),
                "amount": _safe_float(row.get("amount") or row.get("成交额")),
                "data_source": "akshare_stock_hk_daily",
                "period": "daily",
            })
        return [doc for doc in docs if doc["trade_date"] and doc["close"]]

    def _fetch_hk_financial(self, position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = _clean_code(position.get("code"), "港股")

        # 从东方财富港股财务报表取最新季报数据（含Q1/Q2/Q3/年报）
        try:
            income_df = self.ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="报告期")
            balance_df = self.ak.stock_financial_hk_report_em(stock=code, symbol="资产负债表", indicator="报告期")
            cashflow_df = self.ak.stock_financial_hk_report_em(stock=code, symbol="现金流量表", indicator="报告期")
        except Exception:
            return None
        if income_df is None or income_df.empty:
            return None

        dates = sorted(income_df["REPORT_DATE"].unique(), reverse=True)
        latest_date = dates[0]
        latest_income = income_df[income_df["REPORT_DATE"] == latest_date]

        def _get(df, keyword: str):
            rows = df[df["STD_ITEM_NAME"].str.contains(keyword, na=False)]
            if rows.empty:
                return None
            # 排除"少数"前缀的条目（如"少数股东权益" vs "股东权益"）
            exact_rows = rows[~rows["STD_ITEM_NAME"].str.contains("少数", na=False)]
            if not exact_rows.empty:
                return _safe_float(exact_rows.iloc[0]["AMOUNT"])
            return _safe_float(rows.iloc[0]["AMOUNT"])

        revenue = _get(latest_income, "营业额")
        gross_profit = _get(latest_income, "毛利")
        net_income = _get(latest_income, "股东应占溢利") or _get(latest_income, "除税后溢利")

        total_assets = None
        total_liabilities = None
        equity = None
        if balance_df is not None and not balance_df.empty:
            latest_balance = balance_df[balance_df["REPORT_DATE"] == latest_date]
            total_assets = _get(latest_balance, "总资产")
            total_liabilities = _get(latest_balance, "总负债")
            equity = _get(latest_balance, "股东权益")

        gross_margin = round(gross_profit / revenue * 100, 4) if revenue and gross_profit and revenue > 0 else None
        debt_to_assets = round(total_liabilities / total_assets * 100, 4) if total_liabilities and total_assets and total_assets > 0 else None

        # 季报ROE年化：Q1×4, 半年×2, 三季×4/3, 年报×1
        roe = None
        if net_income and equity and equity > 0:
            quarter_roe = net_income / equity * 100
            rp_month = latest_date[5:7]
            factor = {"03": 4, "06": 2, "09": 4/3, "12": 1}.get(rp_month, 1)
            roe = round(quarter_roe * factor, 4)

        # 与去年同期对比计算增长率
        revenue_growth = None
        profit_growth = None
        # 尝试匹配去年同期报告期（例如 2026-03-31 → 2025-03-31）
        prev_year_date = None
        for d in dates:
            if d[:7] == str(int(latest_date[:4]) - 1) + latest_date[4:7]:
                prev_year_date = d
                break
        if prev_year_date:
            prev_income = income_df[income_df["REPORT_DATE"] == prev_year_date]
            prev_revenue = _get(prev_income, "营业额")
            prev_net = _get(prev_income, "股东应占溢利") or _get(prev_income, "除税后溢利")
            if revenue and prev_revenue and prev_revenue > 0:
                revenue_growth = round((revenue - prev_revenue) / prev_revenue * 100, 4)
            if net_income and prev_net and prev_net > 0:
                profit_growth = round((net_income - prev_net) / prev_net * 100, 4)

        # 提取经营现金流
        operating_cf = None
        ocf_to_ni = None
        if cashflow_df is not None and not cashflow_df.empty:
            latest_cf = cashflow_df[cashflow_df["REPORT_DATE"] == latest_date]
            operating_cf = _get(latest_cf, "经营活动") or _get(latest_cf, "经营业务") or _get(latest_cf, "经营活动所得")
            if operating_cf and net_income and net_income > 0:
                ocf_to_ni = round(operating_cf / net_income, 4)

        result = {
            "code": code,
            "symbol": code,
            "full_symbol": f"{code}.HK",
            "market": "港股",
            "currency": "HKD",
            "report_period": self._normalize_report_period(latest_date[:10]),
            "report_type": "latest",
            "data_source": "akshare_stock_financial_hk_report_em",
            "eps_basic": None,
            "eps_ttm": None,
            "bps": round(equity / 100000000, 4) if equity else None,
            "roe": roe,
            "gross_margin": gross_margin,
            "revenue": revenue,
            "revenue_growth": revenue_growth,
            "profit_growth": profit_growth,
            "net_income": net_income,
            "debt_to_assets": debt_to_assets,
            "operating_cash_flow": operating_cf,
            "ocf_to_net_income": ocf_to_ni,
        }

        # 合并一致预期数据（从 stock_signals）
        if self.store:
            try:
                fc = self.store.db["stock_signals"].find_one(
                    {"code": code},
                    {"forecast_min": 1, "forecast_max": 1})
                if fc:
                    result["forecast_min"] = fc.get("forecast_min")
                    result["forecast_max"] = fc.get("forecast_max")
            except Exception:
                pass

        return result

    def _base_basic_doc(self, position: Dict[str, Any], code: str, market: str, currency: str) -> Dict[str, Any]:
        return {
            "code": code,
            "symbol": code,
            "name": position.get("name", code),
            "market": market,
            "display_market": market,
            "market_info": {"market": "HK" if market == "港股" else "CN"},
            "currency": currency,
            "asset_type": position.get("asset_type", "stock"),
            "industry": position.get("industry"),
            "style": position.get("style"),
            "sync_source": "factor_data_import_service",
            "data_version": "1.0",
        }

    def _a_board(self, code: str) -> str:
        if code.startswith("688"):
            return "科创板"
        if code.startswith("300"):
            return "创业板"
        if code.startswith(("600", "601", "603", "605", "000", "001", "002")):
            return "主板"
        if code.startswith(("8", "4")):
            return "北交所"
        return "A股"

    def _quote_doc_from_cn_row(self, code: str, row: Any, market: str, currency: str, source: str) -> Dict[str, Any]:
        trade_date = row.get("日期") or row.get("date") or row.get("trade_date")
        pre_close = row.get("昨收") or row.get("pre_close")
        close = _safe_float(row.get("收盘") or row.get("close"))
        pre_close_value = _safe_float(pre_close)
        pct_chg = _safe_float(row.get("涨跌幅") or row.get("pct_chg"))
        if pct_chg is None and close is not None and pre_close_value:
            pct_chg = (close / pre_close_value - 1) * 100
        return {
            "code": code,
            "symbol": code,
            "market": market,
            "currency": currency,
            "trade_date": str(trade_date)[:10],
            "open": _safe_float(row.get("开盘") or row.get("open")),
            "high": _safe_float(row.get("最高") or row.get("high")),
            "low": _safe_float(row.get("最低") or row.get("low")),
            "close": close,
            "pre_close": pre_close_value,
            "pct_chg": pct_chg,
            "volume": _safe_float(row.get("成交量") or row.get("volume")),
            "amount": _safe_float(row.get("成交额") or row.get("amount")),
            "turnover_rate": _safe_float(row.get("换手率") or row.get("turnover_rate")),
            "data_source": source,
            "period": "daily",
        }

    def _extract_financial_from_akshare_abstract(self, code: str, market: str, currency: str, df: Any) -> Optional[Dict[str, Any]]:
        if df is None or getattr(df, "empty", True):
            return None

        # 跳过标签列（'选项'/'指标'），从近到远找第一个有数据的季度列
        label_cols = {"选项", "指标"}
        date_cols = [col for col in df.columns if col not in label_cols]
        if not date_cols:
            return None

        indicator_col = "指标" if "指标" in df.columns else None
        if not indicator_col:
            return None

        # 尝试每个数据列，直到找到有实际值的（处理最新季度财报未出的情况）
        values: Dict[str, Any] = {}
        chosen_col = None
        for col in date_cols[:4]:  # 最多试4个季度
            col_values = {}
            for _, row in df.iterrows():
                key = str(row.get(indicator_col) or "").strip()
                if not key or key == "nan":
                    continue
                col_values[key] = row.get(col)
            # 检查是否有非空值（至少 ROE 或 营业总收入 有值）
            has_data = any(
                _safe_float(col_values.get(k), None) is not None
                for k in ["净资产收益率(ROE)", "营业总收入", "毛利率", "资产负债率"]
            )
            if has_data:
                values = col_values
                chosen_col = col
                break

        if not chosen_col:
            return None

        def pick(*names: str) -> Optional[float]:
            for name in names:
                if name in values:
                    return _safe_float(values[name])
            return None

        def pick_prev_col(*names: str) -> Optional[float]:
            """从上一季度列提取值（用于计算增速趋势）。"""
            idx = date_cols.index(chosen_col) if chosen_col in date_cols else -1
            if idx < 0 or idx + 1 >= len(date_cols):
                return None
            prev_col = date_cols[idx + 1]
            prev_values = {}
            for _, row in df.iterrows():
                key = str(row.get(indicator_col) or "").strip()
                if key:
                    prev_values[key] = row.get(prev_col)
            for name in names:
                if name in prev_values:
                    return _safe_float(prev_values[name])
            return None

        result = {
            "code": code,
            "symbol": code,
            "full_symbol": f"{code}.SH" if code.startswith("6") else f"{code}.SZ",
            "market": market,
            "currency": currency,
            "report_period": self._normalize_report_period(chosen_col),
            "report_type": "latest",
            "roe": pick("净资产收益率(ROE)", "净资产收益率", "加权净资产收益率", "净资产收益率"),
            "gross_margin": pick("毛利率", "销售毛利率"),
            "net_margin": pick("销售净利率", "净利率"),
            "revenue": pick("营业总收入", "营业收入"),
            "revenue_growth": pick("营业总收入增长率", "营业收入同比增长率", "营业总收入同比增长率", "营收同比增长率"),
            "profit_growth": pick("归属母公司净利润增长率", "净利润同比增长率", "归属母公司股东的净利润同比增长率", "归母净利润同比增长率"),
            "net_income": pick("归母净利润", "净利润", "归属母公司股东的净利润"),
            "debt_to_assets": pick("资产负债率"),
            "eps": pick("基本每股收益"),
            "bps": pick("每股净资产"),
            "roa": pick("总资产报酬率(ROA)", "总资产报酬率"),
            # QoQ 趋势检测：上一季度增速
            "revenue_growth_prev": pick_prev_col("营业总收入增长率", "营业收入同比增长率", "营收同比增长率"),
            "profit_growth_prev": pick_prev_col("归属母公司净利润增长率", "净利润同比增长率", "归属母公司股东的净利润同比增长率", "归母净利润同比增长率"),
        }
        return result

    def _normalize_report_period(self, value: Any) -> str:
        text = str(value or "").replace("-", "").replace("/", "").strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
        return _utc_now().strftime("%Y%m%d")


def _portfolio_positions(path: str, include_watchlist: bool = False) -> List[Dict[str, Any]]:
    portfolio = _load_yaml(path)
    positions = list(portfolio.get("positions") or [])
    if include_watchlist:
        positions.extend(portfolio.get("watchlist") or [])
    return [item for item in positions if item.get("asset_type") == "stock"]


def main() -> None:
    parser = argparse.ArgumentParser(description="同步多因子评分所需的 MongoDB 数据")
    parser.add_argument("command", choices=["sync-portfolio", "sync-universe-basic", "config-check"], help="同步多因子输入数据")
    parser.add_argument("--portfolio", default=DEFAULT_PORTFOLIO, help="持仓 YAML 文件路径")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="系统配置 YAML 文件路径")
    parser.add_argument("--include-watchlist", action="store_true", help="同时同步观察池中的股票")
    parser.add_argument("--market", default="A股", help="市场：A股 或 港股")
    parser.add_argument("--quote-limit", type=int, default=180, help="每只股票保留的日线数量")
    parser.add_argument("--sleep", type=float, default=0.4, help="每只股票同步后的等待秒数，避免请求过快")
    args = parser.parse_args()

    if args.command == "config-check":
        try:
            validate_system_config(_load_yaml(args.config), args.config)
        except ConfigError as exc:
            print(f"CONFIG_ERROR: {exc}")
            raise SystemExit(2)
        print(f"CONFIG_OK: {args.config}")
        return

    mongodb_config = _load_mongodb_config(args.config)
    store = MongoFactorDataStore(mongodb_config)
    importer = FactorDataImporter(store=store, quote_limit=args.quote_limit)

    if args.command == "sync-portfolio":
        positions = _portfolio_positions(args.portfolio, include_watchlist=args.include_watchlist)
        stats = importer.sync_positions(positions, sleep_seconds=args.sleep)
        print(f"同步完成: 成功 {stats['success']}，失败 {stats['failed']}")
        for item in stats["items"]:
            if item.get("ok"):
                warning_text = f"，警告 {'；'.join(item['warnings'])}" if item.get("warnings") else ""
                print(f"- {item['code']} {item.get('name')}: 行情 {item['quotes']} 条，财务 {'有' if item['financial'] else '无'}{warning_text}")
            else:
                print(f"- {item['code']}: 失败，{item.get('error')}")
    elif args.command == "sync-universe-basic":
        result = importer.sync_universe_basic(market=args.market)
        print(f"基础池同步完成: {result['market']} 更新 {result['updated']}，失败 {result['failed']}")


if __name__ == "__main__":
    main()
