#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reusable data capability layer for strategy skills.

The strategy layer should ask this service for data readiness instead of
knowing about AKShare, MongoDB collections, retry rules, or import commands.
"""
import argparse
import os
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from factor_data_import_service import (
    DEFAULT_CONFIG,
    DEFAULT_PORTFOLIO,
    FactorDataImporter,
    MongoFactorDataStore,
    _clean_code,
    _load_mongodb_config,
    _portfolio_positions,
)


DEFAULT_NEEDS = ["basic", "quotes", "financial"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except Exception:
        return None


def _age_hours(value: Any) -> Optional[float]:
    parsed = _parse_dt(value)
    if not parsed:
        return None
    return round((_utc_now() - parsed).total_seconds() / 3600, 2)


class DataCapabilityService:
    """Coordinate reusable data readiness and incremental imports."""

    def __init__(self, config_path: str = DEFAULT_CONFIG, quote_limit: int = 180):
        self.config_path = config_path
        self.quote_limit = quote_limit
        self.store = MongoFactorDataStore(_load_mongodb_config(config_path))
        self.importer = FactorDataImporter(self.store, quote_limit=quote_limit)

    def get_data_status(self, asset: Dict[str, Any], needs: Optional[List[str]] = None) -> Dict[str, Any]:
        needs = needs or DEFAULT_NEEDS
        market = asset.get("market", "A股")
        code = _clean_code(asset.get("code"), market)
        status = {
            "code": code,
            "name": asset.get("name"),
            "market": market,
            "ready": True,
            "needs": needs,
            "missing": [],
            "stale": [],
            "details": {},
        }

        if "basic" in needs:
            basic = self.store.get_basic(code)
            if not basic:
                status["missing"].append("basic")
            else:
                status["details"]["basic"] = {
                    "available": True,
                    "updated_at": str(basic.get("updated_at")),
                    "age_hours": _age_hours(basic.get("updated_at")),
                    "source": basic.get("data_source") or basic.get("sync_source"),
                }

        if "quotes" in needs:
            quote_count = self.store.count_recent_quotes(code, limit=self.quote_limit)
            latest_quote = self.store.get_latest_quote(code)
            if quote_count < min(60, self.quote_limit):
                status["missing"].append("quotes")
            else:
                status["details"]["quotes"] = {
                    "available": True,
                    "count": quote_count,
                    "latest_trade_date": latest_quote.get("trade_date") if latest_quote else None,
                    "source": latest_quote.get("data_source") if latest_quote else None,
                }

        if "financial" in needs:
            financial = self.store.get_latest_financial(code)
            if not financial:
                status["missing"].append("financial")
            else:
                status["details"]["financial"] = {
                    "available": True,
                    "report_period": financial.get("report_period"),
                    "updated_at": str(financial.get("updated_at")),
                    "age_hours": _age_hours(financial.get("updated_at")),
                    "source": financial.get("data_source"),
                }

        status["ready"] = not status["missing"] and not status["stale"]
        return status

    def ensure_asset_data(
        self,
        asset: Dict[str, Any],
        needs: Optional[List[str]] = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        before = self.get_data_status(asset, needs)
        if before["ready"] and not refresh:
            return {"asset": before, "synced": False, "sync_result": None}

        sync_result = self.importer.sync_position({
            **asset,
            "code": before["code"],
        })
        after = self.get_data_status(asset, needs)
        return {
            "asset": after,
            "synced": True,
            "sync_result": sync_result,
            "before": before,
        }

    def ensure_assets_data(
        self,
        assets: List[Dict[str, Any]],
        needs: Optional[List[str]] = None,
        refresh: bool = False,
        sleep_seconds: float = 0.8,
    ) -> Dict[str, Any]:
        results = []
        for asset in assets:
            results.append(self.ensure_asset_data(asset, needs=needs, refresh=refresh))
            if sleep_seconds > 0:
                import time

                time.sleep(sleep_seconds)

        ready_count = sum(1 for item in results if item["asset"].get("ready"))
        return {
            "total": len(results),
            "ready": ready_count,
            "not_ready": len(results) - ready_count,
            "items": results,
        }

    def ensure_universe_basic(self, market: str = "A股") -> Dict[str, Any]:
        return self.importer.sync_universe_basic(market=market)


def _print_status(item: Dict[str, Any]) -> None:
    status = item.get("asset", item)
    ready = "ready" if status.get("ready") else "missing"
    missing = f"，缺失: {','.join(status.get('missing') or [])}" if status.get("missing") else ""
    print(f"- {status.get('code')} {status.get('name') or ''}: {ready}{missing}")
    sync_result = item.get("sync_result")
    if sync_result:
        warnings = sync_result.get("warnings") or []
        warning_text = f"，警告: {'；'.join(warnings)}" if warnings else ""
        print(f"  同步: 行情 {sync_result.get('quotes')} 条，财务 {'有' if sync_result.get('financial') else '无'}{warning_text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="可复用数据能力层：状态检查与增量补齐")
    parser.add_argument("command", choices=["status-portfolio", "ensure-portfolio", "ensure-asset", "ensure-universe-basic"])
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--portfolio", default=DEFAULT_PORTFOLIO)
    parser.add_argument("--include-watchlist", action="store_true")
    parser.add_argument("--code")
    parser.add_argument("--name")
    parser.add_argument("--market", default="A股")
    parser.add_argument("--quote-limit", type=int, default=180)
    parser.add_argument("--needs", default="basic,quotes,financial")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.8)
    args = parser.parse_args()

    needs = [item.strip() for item in args.needs.split(",") if item.strip()]
    service = DataCapabilityService(config_path=args.config, quote_limit=args.quote_limit)

    if args.command == "ensure-universe-basic":
        result = service.ensure_universe_basic(market=args.market)
        print(f"基础池同步完成: {result['market']} 更新 {result['updated']}，失败 {result['failed']}")
        return

    if args.command in ("status-portfolio", "ensure-portfolio"):
        assets = _portfolio_positions(args.portfolio, include_watchlist=args.include_watchlist)
        if args.command == "status-portfolio":
            for asset in assets:
                _print_status(service.get_data_status(asset, needs=needs))
        else:
            result = service.ensure_assets_data(assets, needs=needs, refresh=args.refresh, sleep_seconds=args.sleep)
            print(f"数据补齐完成: 总数 {result['total']}，ready {result['ready']}，not_ready {result['not_ready']}")
            for item in result["items"]:
                _print_status(item)
        return

    if args.command == "ensure-asset":
        if not args.code:
            raise RuntimeError("ensure-asset 需要 --code")
        asset = {
            "code": args.code,
            "name": args.name,
            "market": args.market,
            "asset_type": "stock",
        }
        result = service.ensure_asset_data(asset, needs=needs, refresh=args.refresh)
        _print_status(result)


if __name__ == "__main__":
    main()
