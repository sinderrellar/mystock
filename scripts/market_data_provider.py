#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一市场数据获取层 (Market Data Provider)

整合所有行情数据获取能力，对外提供单一入口。上层模块（portfolio_strategy、
strategy_signals、pyramid、event_driven）只依赖此类，不感知底层数据源。

数据维度：
  get_price()           实时价格       Tencent → Sina → Eastmoney → AKShare → Yahoo
  get_quote_snapshot()  报价快照       Yahoo (PE/PB/股息/市值) + MongoDB fallback
  get_bars()            历史K线        MongoDB canonical source
  get_trend_signal()    趋势+技术指标   基于 bars 计算
  get_financial()       财务数据       MongoDB
  get_stock_info()      股票基础信息    MongoDB
  get_news()            个股新闻       AKShare
  get_market_news()     市场热点新闻    AKShare
  get_moneyflow()       资金流向       Tushare → MongoDB cache
  get_orderbook()       盘口数据       Tushare → AKShare → Sina
  get_fx_rate()         汇率          Yahoo → 内置兜底
  get_sector_performance() 行业表现    MongoDB 聚合
  get_market_sentiment()   市场情绪    指数涨跌判断
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# 工具函数
# ============================================================

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    """解析多种日期格式：ISO8601 / YYYYMMDD / YYYY-MM-DD。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    # ISO8601
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    # YYYYMMDD (e.g. "20260508")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 8:
        try:
            return datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]), tzinfo=timezone.utc)
        except Exception:
            pass

    return None


def _data_age_days(data_date: Optional[str]) -> Optional[float]:
    """计算数据距今天数，无法解析则返回 None。"""
    dt = _parse_date(data_date)
    if dt is None:
        return None
    return round((_utc_now() - dt).total_seconds() / 86400, 2)


def _freshness(data_date: Optional[str], max_age_days: float = 1.0) -> Dict[str, Any]:
    """生成 data_date / stale / age_days 三元组。"""
    age = _data_age_days(data_date)
    stale = age is not None and age > max_age_days
    return {
        "data_date": data_date,
        "age_days": age,
        "stale": stale,
        "stale_reason": f"数据已有 {age:.1f} 天" if stale else None,
    }


# ============================================================
# 统一数据获取层
# ============================================================

class MarketDataProvider:
    """统一市场数据获取入口。

    用法:
        provider = MarketDataProvider(mongo_store)
        price = provider.get_price("600941", "A股")
        trend = provider.get_trend_signal("00700", "港股")
    """

    def __init__(self, mongo_store=None):
        # MongoDB 存储层（可延迟注入）
        self._mongo_store = mongo_store
        self._mongo_store_error: str = ""

        # 实时价格查价缓存
        self._ak = None
        self._a_stock_spot = None
        self._a_etf_spot = None
        self._hk_spot = None
        self._spot_fetched_at: Dict[str, float] = {}  # 缓存获取时间戳
        self._spot_cache_ttl = 300  # 5分钟，覆盖单次review全流程
        self._ak_available: Optional[bool] = None
        self._last_error: str = ""
        self._errors: List[str] = []

        # 汇率缓存
        self._fx_cache: Dict[str, float] = {}
        self._fx_errors: Dict[str, str] = {}
        # 汇率兜底文件：上次成功获取的汇率落盘，避免硬编码值过期
        self._fx_fallback_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "fx_fallback.json")

        # Yahoo 行情缓存
        self._yahoo_quote_cache: Dict[str, Dict[str, Any]] = {}
        self._yahoo_bars_cache: Dict[str, List[Dict[str, Any]]] = {}

        # 新闻/事件提供器（延迟初始化）
        self._news_provider = None
        self._sentiment_analyzer = None

    # ================================================================
    # MongoDB 存储层
    # ================================================================

    def _ensure_mongo(self) -> None:
        if self._mongo_store is not None or self._mongo_store_error:
            return
        try:
            from factor_data_import_service import _load_mongodb_config

            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "config_complete.yaml",
            )
            mongodb_config = _load_mongodb_config(config_path)
            from factor_data_import_service import MongoFactorDataStore
            self._mongo_store = MongoFactorDataStore(
                mongodb_config,
                readonly=True,
                ensure_indexes=False,
            )
        except Exception as exc:
            self._mongo_store_error = str(exc)

    @property
    def mongo(self):
        self._ensure_mongo()
        return self._mongo_store

    @property
    def mongo_available(self) -> bool:
        self._ensure_mongo()
        return self._mongo_store is not None

    # ================================================================
    # 1. 实时价格 — Tencent → Sina → Eastmoney → AKShare → Yahoo
    # ================================================================

    def get_price(
        self,
        code: str,
        market: str = "A股",
        asset_type: str = "stock",
        allow_akshare: bool = False,
        cost_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """获取实时价格，多源 fallback。

        Returns:
            {"price": float, "available": bool, "source": str, "data_date": str,
             "stale": bool, "error": str}
        """
        code = str(code).strip()
        market = str(market)
        asset_type = str(asset_type)
        now_iso = _utc_now().isoformat()

        # 轻量源 fallback: Tencent → Sina → Eastmoney
        tencent_price = self._lookup_tencent_price(code, market)
        if tencent_price is not None:
            return {"price": tencent_price, "available": True, "source": "tencent",
                    "data_date": now_iso, "stale": False}

        sina_price = self._lookup_sina_price(code, market)
        if sina_price is not None:
            return {"price": sina_price, "available": True, "source": "sina",
                    "data_date": now_iso, "stale": False}

        eastmoney_price = self._lookup_eastmoney_price(code, market)
        if eastmoney_price is not None:
            return {"price": eastmoney_price, "available": True, "source": "eastmoney",
                    "data_date": now_iso, "stale": False}

        # AKShare 大列表行情（需要显式开启）
        if allow_akshare and self._ensure_akshare():
            try:
                price = None
                if market == "港股":
                    price = self._lookup_hk_price(code)
                elif asset_type == "etf":
                    price = self._lookup_a_etf_price(code)
                else:
                    price = self._lookup_a_stock_price(code)
                if price is not None:
                    return {"price": price, "available": True, "source": "akshare",
                            "data_date": now_iso, "stale": False}
            except Exception as exc:
                self._last_error = f"AKShare 查价异常: {exc}"

        # Yahoo 兜底
        yahoo_price = self._lookup_yahoo_price(code, market)
        if yahoo_price is not None:
            return {"price": yahoo_price, "available": True, "source": "yahoo",
                    "data_date": now_iso, "stale": False}

        return {
            "price": cost_price or 0.0,
            "available": False,
            "source": "cost_price_fallback",
            "data_date": None,
            "stale": True,
            "stale_reason": "所有行情源不可用，使用成本价兜底",
            "error": "；".join(self._errors[-4:]) or self._last_error or "未获取到实时价格",
        }

    def _lookup_tencent_price(self, code: str, market: str) -> Optional[float]:
        symbol = self._tencent_symbol(code, market)
        if not symbol:
            return None
        url = f"https://qt.gtimg.cn/q={urllib.parse.quote(symbol)}"
        try:
            text = self._read_http(url, encoding="gbk")
            payload = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = payload.split("~")
            price = _to_float(fields[3] if len(fields) > 3 else None)
            if price > 0:
                return price
            self._record_error(f"腾讯行情未返回有效价格: {code}")
        except Exception as exc:
            self._record_error(f"腾讯行情异常: {exc}")
        return None

    def _lookup_sina_price(self, code: str, market: str) -> Optional[float]:
        symbol = self._sina_symbol(code, market)
        if not symbol:
            return None
        url = f"https://hq.sinajs.cn/list={urllib.parse.quote(symbol)}"
        try:
            text = self._read_http(url, encoding="gbk", headers={"Referer": "https://finance.sina.com.cn/"})
            payload = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = payload.split(",")
            candidates = [6, 3] if market == "港股" else [3]
            for index in candidates:
                price = _to_float(fields[index] if len(fields) > index else None)
                if price > 0:
                    return price
            self._record_error(f"新浪行情未返回有效价格: {code}")
        except Exception as exc:
            self._record_error(f"新浪行情异常: {exc}")
        return None

    def _lookup_eastmoney_price(self, code: str, market: str) -> Optional[float]:
        secid = self._eastmoney_secid(code, market)
        if not secid:
            return None
        params = urllib.parse.urlencode({"secid": secid, "fields": "f43,f57,f58"})
        url = f"https://push2.eastmoney.com/api/qt/stock/get?{params}"
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        })
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self._record_error(f"东方财富查价异常: {exc}")
            return None
        data = payload.get("data") or {}
        raw_price = data.get("f43")
        price = _to_float(raw_price)
        if price <= 0:
            self._record_error(f"东方财富未返回有效价格: {code}")
            return None
        return round(price / 100, 4)

    def _lookup_yahoo_price(self, code: str, market: str) -> Optional[float]:
        symbols = self._yahoo_symbols(code, market)
        if not symbols:
            return None
        for symbol in symbols:
            snapshot = self._get_yahoo_quote(symbol)
            if snapshot.get("available"):
                price = _to_float((snapshot.get("metrics") or {}).get("regular_market_price"))
                if price > 0:
                    return price
        return None

    # ---- AKShare 价格 ----

    def _ensure_akshare(self) -> bool:
        if self._ak_available is not None:
            return self._ak_available
        try:
            import akshare as ak
            self._ak = ak
            self._ak_available = True
        except Exception as exc:
            self._ak_available = False
            self._last_error = f"AKShare 不可用: {exc}"
        return self._ak_available

    def _spot_expired(self, key: str) -> bool:
        """缓存 key 是否已过期。"""
        fetched = self._spot_fetched_at.get(key, 0)
        return (time.monotonic() - fetched) > self._spot_cache_ttl

    def _lookup_a_stock_price(self, code: str) -> Optional[float]:
        return self._lookup_price_in_rows(self._ensure_a_stock_spot(), code)

    def _lookup_a_etf_price(self, code: str) -> Optional[float]:
        if self._a_etf_spot is None or self._spot_expired("a_etf"):
            self._a_etf_spot = self._ak.fund_etf_spot_em()
            self._spot_fetched_at["a_etf"] = time.monotonic()
        return self._lookup_price_in_rows(self._a_etf_spot, code)

    def _lookup_hk_price(self, code: str) -> Optional[float]:
        return self._lookup_price_in_rows(self._ensure_hk_spot(), code)

    def _ensure_a_stock_spot(self):
        """返回 A 股 spot DataFrame，带 TTL 缓存。"""
        if self._a_stock_spot is None or self._spot_expired("a_stock"):
            self._a_stock_spot = self._ak.stock_zh_a_spot_em()
            self._spot_fetched_at["a_stock"] = time.monotonic()
        return self._a_stock_spot

    def _ensure_hk_spot(self):
        """返回港股 spot DataFrame，带 TTL 缓存。"""
        if self._hk_spot is None or self._spot_expired("hk"):
            self._hk_spot = self._ak.stock_hk_spot_em()
            self._spot_fetched_at["hk"] = time.monotonic()
        return self._hk_spot

    def _lookup_price_in_rows(self, rows: Any, code: str) -> Optional[float]:
        code_candidates = {code, code.lstrip("0")}
        code_columns = ["代码", "symbol", "证券代码"]
        price_columns = ["最新价", "最新", "现价", "close", "last"]
        for code_col in code_columns:
            if code_col not in rows:
                continue
            matched = rows[rows[code_col].astype(str).isin(code_candidates)]
            if matched.empty:
                continue
            row = matched.iloc[0]
            for price_col in price_columns:
                if price_col in row:
                    price = _to_float(row.get(price_col))
                    if price > 0:
                        return price
        self._last_error = f"AKShare 结果中未匹配到价格: {code}"
        return None

    # ---- 价格查询 HTTP 工具 ----

    def _read_http(self, url: str, encoding: str = "utf-8",
                   headers: Optional[Dict[str, str]] = None) -> str:
        request_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(request, timeout=6) as response:
            return response.read().decode(encoding, errors="ignore")

    def _record_error(self, message: str) -> None:
        self._last_error = message
        self._errors.append(message)

    # ---- 代码映射 ----

    def _tencent_symbol(self, code: str, market: str) -> Optional[str]:
        if market == "港股":
            return f"hk{code}"
        if code.startswith(("5", "6", "9")):
            return f"sh{code}"
        if code.startswith(("0", "1", "2", "3")):
            return f"sz{code}"
        self._record_error(f"腾讯行情无法判断市场代码: {code}")
        return None

    def _sina_symbol(self, code: str, market: str) -> Optional[str]:
        if market == "港股":
            return f"hk{code}"
        if code.startswith(("5", "6", "9")):
            return f"sh{code}"
        if code.startswith(("0", "1", "2", "3")):
            return f"sz{code}"
        self._record_error(f"新浪行情无法判断市场代码: {code}")
        return None

    def _eastmoney_secid(self, code: str, market: str) -> Optional[str]:
        if market == "港股":
            return f"116.{code}"
        if code.startswith(("5", "6", "9")):
            return f"1.{code}"
        if code.startswith(("0", "1", "2", "3")):
            return f"0.{code}"
        self._last_error = f"无法判断市场代码: {code}"
        return None

    def _yahoo_symbols(self, code: str, market: str) -> List[str]:
        code = str(code).strip()
        if not code:
            return []
        if market == "港股":
            stripped = code.lstrip("0")
            return list(dict.fromkeys([
                f"{stripped.zfill(4)}.HK",
                f"{stripped}.HK",
                f"{code}.HK",
            ]))
        if code.startswith(("5", "6", "9")):
            return [f"{code}.SS"]
        if code.startswith(("0", "1", "2", "3")):
            return [f"{code}.SZ"]
        return []

    # ================================================================
    # 2. 报价快照 — Yahoo (PE/PB/股息/市值) + MongoDB fallback
    # ================================================================

    def get_quote_snapshot(self, code: str, market: str) -> Dict[str, Any]:
        """获取报价快照，包含 PE/PB/股息率/市值/52周区间。

        PE/PB/市值实时性要求不高，优先级: MongoDB（crontab 每日导入）→ AKShare → Yahoo。
        """
        now_iso = _utc_now().isoformat()

        # 1. MongoDB 缓存（每日 crontab 更新，足够新鲜）
        mongo_result = self._quote_from_mongo(code)
        if mongo_result.get("available"):
            basic = self.mongo.get_basic(code)
            mongo_result["data_date"] = str(basic.get("updated_at") or "")
            mongo_result.update(_freshness(mongo_result["data_date"], max_age_days=2))
            # 股息率 MongoDB 通常没有，实时补齐
            price = (mongo_result.get("metrics") or {}).get("regular_market_price")
            if price and (mongo_result.get("metrics") or {}).get("dividend_yield") is None:
                dy = self._get_dividend_yield(code, price)
                if dy is not None:
                    mongo_result["metrics"]["dividend_yield"] = dy
        if mongo_result.get("available") and not mongo_result.get("stale"):
            return mongo_result

        # 2. AKShare spot — 实时补齐
        akshare_result = self._quote_from_akshare(code, market)
        if akshare_result.get("available"):
            akshare_result["data_date"] = now_iso
            akshare_result["stale"] = False
            price = (akshare_result.get("metrics") or {}).get("regular_market_price")
            if price:
                dy = self._get_dividend_yield(code, price)
                if dy is not None:
                    akshare_result["metrics"]["dividend_yield"] = dy
            return akshare_result

        # 3. 陈旧 MongoDB 数据兜底
        if mongo_result.get("available"):
            return mongo_result

        # 4. Yahoo 最后尝试
        symbols = self._yahoo_symbols(code, market)
        if symbols:
            for symbol in symbols:
                result = self._get_yahoo_quote(symbol)
                if result.get("available"):
                    result["data_date"] = now_iso
                    result["stale"] = False
                    return result

        return {"available": False, "source": "none", "data_date": None,
                "stale": True, "reason": "所有数据源均无数据"}

    def _get_yahoo_quote(self, symbol: str) -> Dict[str, Any]:
        if symbol in self._yahoo_quote_cache:
            return self._yahoo_quote_cache[symbol]

        params = urllib.parse.urlencode({"symbols": symbol})
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return {"available": False, "source": "yahoo", "reason": "请求失败"}

        results = ((payload.get("quoteResponse") or {}).get("result") or [])
        if not results:
            return {"available": False, "source": "yahoo", "reason": "无数据"}

        item = results[0]
        metrics = {
            "symbol": item.get("symbol") or symbol,
            "regular_market_price": _to_float(item.get("regularMarketPrice")),
            "trailing_pe": _to_float(item.get("trailingPE"), None),
            "forward_pe": _to_float(item.get("forwardPE"), None),
            "price_to_book": _to_float(item.get("priceToBook"), None),
            "dividend_yield": self._normalize_dividend_yield(item.get("dividendYield")),
            "market_cap": _to_float(item.get("marketCap"), None),
            "fifty_two_week_high": _to_float(item.get("fiftyTwoWeekHigh"), None),
            "fifty_two_week_low": _to_float(item.get("fiftyTwoWeekLow"), None),
            "fifty_two_week_change_pct": _to_float(item.get("fiftyTwoWeekChangePercent"), None),
        }
        result = {"available": True, "source": "yahoo", "symbol": metrics["symbol"], "metrics": metrics}
        self._yahoo_quote_cache[symbol] = result
        return result

    def _get_dividend_yield(self, code: str, price: float) -> Optional[float]:
        """从 AKShare 分红历史计算股息率（最新一次分红/当前价）。

        AKShare stock_history_dividend_detail 的 `派息` 列单位为 每10股派息(元)。
        """
        if not price or price <= 0:
            return None
        if not self._ensure_akshare():
            return None
        try:
            df = self._ak.stock_history_dividend_detail(symbol=code, indicator="分红")
            if df is None or df.empty:
                return None
            # 取最近一次已实施的分红
            implemented = df[df["进度"] == "实施"]
            if implemented.empty:
                implemented = df  # 兜底：不过滤进度
            latest = implemented.iloc[0]
            dividend_per_10 = _to_float(latest.get("派息"), None)
            if dividend_per_10 is None or dividend_per_10 <= 0:
                return None
            # 每10股派息 → 每股股息 → 股息率
            dps = dividend_per_10 / 10
            return round(dps / price * 100, 4)
        except Exception:
            return None

    def _quote_from_akshare(self, code: str, market: str) -> Dict[str, Any]:
        """通过 AKShare spot (缓存) 获取 PE/PB/市值。

        复用已有 spot 缓存，避免和 _lookup_*_price 各自拉全量。
        """
        if not self._ensure_akshare():
            return {"available": False, "source": "akshare", "reason": "AKShare 不可用"}

        try:
            if market == "港股":
                df = self._ensure_hk_spot()
                code_col = "代码"
                clean = code.lstrip("0")
                row = df[df[code_col].astype(str).str.lstrip("0") == clean]
            else:
                df = self._ensure_a_stock_spot()
                code_col = "代码"
                row = df[df[code_col].astype(str).str.zfill(6) == code.zfill(6)]

            if row.empty:
                return {"available": False, "source": "akshare", "reason": f"AKShare spot 未找到 {code}"}

            r = row.iloc[0]
            price = _to_float(r.get("最新价"))
            metrics = {
                "regular_market_price": price,
                "trailing_pe": _to_float(r.get("市盈率-动态") or r.get("市盈率"), None),
                "price_to_book": _to_float(r.get("市净率"), None),
                "market_cap": _to_float(r.get("总市值"), None),
                "dividend_yield": None,
                "fifty_two_week_high": None,
                "fifty_two_week_low": None,
                "fifty_two_week_change_pct": _to_float(r.get("涨跌幅"), None),
                "turnover_rate": _to_float(r.get("换手率"), None),
                "latest_amount": _to_float(r.get("成交额"), None),
            }
            return {"available": True, "source": "akshare", "metrics": metrics}
        except Exception as exc:
            return {"available": False, "source": "akshare", "reason": str(exc)}

    def _quote_from_mongo(self, code: str) -> Dict[str, Any]:
        if not self.mongo_available:
            return {"available": False, "source": "none", "reason": "MongoDB 不可用"}
        basic = self.mongo.get_basic(code)
        if not basic:
            return {"available": False, "source": "none", "reason": "MongoDB 无此股票"}
        return {
            "available": True,
            "source": "mongodb",
            "metrics": {
                "regular_market_price": _to_float(basic.get("close")),
                "trailing_pe": _to_float(basic.get("pe"), None),
                "price_to_book": _to_float(basic.get("pb"), None),
                "dividend_yield": _to_float(basic.get("dividend_yield"), None),
                "market_cap": _to_float(basic.get("total_mv"), None),
                "fifty_two_week_high": _to_float(basic.get("fifty_two_week_high"), None),
                "fifty_two_week_low": _to_float(basic.get("fifty_two_week_low"), None),
                "turnover_rate": _to_float(basic.get("turnover_rate"), None),
                "latest_amount": _to_float(basic.get("latest_amount"), None),
            },
        }

    def _normalize_dividend_yield(self, value: Any) -> Optional[float]:
        number = _to_float(value, None)
        if number is None:
            return None
        if 0 < number < 1:
            return number * 100
        return number

    # ================================================================
    # 3. 历史K线 — MongoDB canonical source
    # ================================================================

    def get_bars(self, code: str, market: str, days: int = 120,
                 as_of_date: Optional[str] = None) -> Dict[str, Any]:
        """获取历史日线 K 线（倒序，最新在前）。

        只读取 MongoDB 中的 canonical 日线源。缺数据时显式返回 stale，
        不自动拉取或回写其他行情源。

        Args:
            as_of_date: 历史回测截止日期，K线数据不晚于此日（防 look-ahead bias）。

        Returns:
            {"bars": [...], "data_date": str, "stale": bool, "source": str}
        """
        result = {"bars": [], "data_date": None, "stale": False, "source": "none"}

        if self.mongo_available:
            quotes = self.mongo.get_recent_quotes(code, limit=days, as_of_date=as_of_date)
            if quotes:
                result["bars"] = quotes
                result["data_date"] = quotes[0].get("trade_date", "")
                result["source"] = "mongodb"
                age = _data_age_days(result["data_date"])
                result["stale"] = (age is not None and age > 1)
                if result["stale"]:
                    result["stale_reason"] = f"canonical 日线截止 {result['data_date']}（{age:.0f}天前）"
                return result

        if not result["bars"]:
            result["stale"] = True
            result["stale_reason"] = "无 canonical K线数据，请先运行 import-quotes"
        return result

    # ================================================================
    # 4. 趋势+技术指标 — 基于 bars 计算
    # ================================================================

    def get_trend_signal(
        self,
        code: str,
        market: str,
        decision_currency: str = "CNY",
        price_to_decision_rate: float = 1.0,
        add_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        cost_price: Optional[float] = None,
        as_of_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取完整的趋势信号和技术指标。

        Args:
            as_of_date: 历史回测截止日期，K线数据不晚于此日（防 look-ahead bias）。

        Returns:
            包含 status, ma, returns, volatility, technical_indicators,
            interpretation, supporting_factors, risk_factors 等字段
        """
        bars_result = self.get_bars(code, market, days=300, as_of_date=as_of_date)  # 覆盖12月动量+MA60
        bars = bars_result.get("bars", [])
        if len(bars) < 25:
            return self._unavailable(f"历史行情样本不足: {len(bars)}")

        # 注入实时价格：如果最新日线不是今天的，用当前价补一条"今日K线"
        today_str = datetime.now().strftime("%Y-%m-%d")
        latest_bar_date = bars[0].get("trade_date", "") if bars else ""
        if latest_bar_date != today_str:
            try:
                price_result = self.get_price(code, market)
                if price_result.get("available") and price_result.get("price", 0) > 0:
                    live_price = price_result["price"]
                    today_bar = {
                        "trade_date": today_str,
                        "open": bars[0].get("close", live_price),
                        "close": live_price,
                        "high": max(live_price, bars[0].get("high", live_price) or 0),
                        "low": min(live_price, bars[0].get("low", live_price) or live_price),
                        "volume": bars[0].get("volume", 0),
                    }
                    bars.insert(0, today_bar)
            except Exception:
                pass  # 取不到实时价就沿用昨日数据

        closes = [b["close"] for b in bars if b.get("close", 0) > 0]
        if len(closes) < 25:
            return self._unavailable(f"有效收盘价样本不足: {len(closes)}")

        highs = [b.get("high", 0) or 0 for b in bars]
        lows = [b.get("low", 0) or 0 for b in bars]
        volumes = [b.get("volume", 0) or 0 for b in bars]

        latest = closes[0]
        ma5 = self._ma(closes, 5)
        ma20 = self._ma(closes, 20)
        ma60 = self._ma(closes, 60)
        return_5d = self._return_pct(closes, 5)
        return_20d = self._return_pct(closes, 20)
        return_60d = self._return_pct(closes, 60)
        volatility_20d = self._volatility(closes, 20)

        technical_indicators = self._calc_technical_indicators(
            closes, highs, lows, volumes, price_to_decision_rate,
        )

        volume_contracting = self._calc_volume_contraction(volumes)
        rsi = (technical_indicators or {}).get("rsi14")

        status, supporting, risks = self._classify_trend(
            latest, ma5, ma20, ma60,
            return_5d, return_20d, return_60d, volatility_20d,
        )

        interpretation = self._build_trend_interpretation(
            status=status,
            latest=latest,
            ma5=ma5, ma20=ma20, ma60=ma60,
            return_5d=return_5d, return_20d=return_20d, return_60d=return_60d,
            volatility_20d=volatility_20d,
            price_to_decision_rate=price_to_decision_rate,
            decision_currency=decision_currency,
            add_price=add_price,
            stop_loss_price=stop_loss_price,
            cost_price=cost_price,
            volume_contracting=volume_contracting,
            rsi=rsi,
        )

        result = {
            "available": True,
            "source": "computed",
            "status": status,
            "latest_close": round(latest, 4),
            "latest_close_decision": round(latest * price_to_decision_rate, 4),
            "price_to_decision_rate": round(price_to_decision_rate, 6),
            "decision_currency": decision_currency,
            "ma": {"ma5": round(ma5, 4) if ma5 else None,
                   "ma20": round(ma20, 4) if ma20 else None,
                   "ma60": round(ma60, 4) if ma60 else None},
            "ma_decision": {"ma5": round(ma5 * price_to_decision_rate, 4) if ma5 else None,
                            "ma20": round(ma20 * price_to_decision_rate, 4) if ma20 else None,
                            "ma60": round(ma60 * price_to_decision_rate, 4) if ma60 else None},
            "returns": {"return_5d": round(return_5d, 2) if return_5d is not None else None,
                        "return_20d": round(return_20d, 2) if return_20d is not None else None,
                        "return_60d": round(return_60d, 2) if return_60d is not None else None},
            "volatility_20d": round(volatility_20d, 2) if volatility_20d is not None else None,
            "technical_indicators": technical_indicators,
            "supporting_factors": supporting,
            "risk_factors": risks,
            "interpretation": interpretation,
        }
        result["llm_context"] = self._trend_llm_context(result)
        return result

    # ---- 数学工具 ----

    @staticmethod
    def _ma(values: List[float], period: int) -> Optional[float]:
        if len(values) < period:
            return None
        return sum(values[:period]) / period

    @staticmethod
    def _return_pct(values: List[float], days: int) -> Optional[float]:
        if len(values) <= days:
            return None
        prev = values[days]
        latest = values[0]
        if prev <= 0:
            return None
        return (latest - prev) / prev * 100

    @staticmethod
    def _volatility(values: List[float], days: int) -> Optional[float]:
        if len(values) <= days:
            return None
        returns = []
        sample = values[:days + 1]
        for i in range(1, len(sample)):
            if sample[i] > 0:
                returns.append((sample[i - 1] - sample[i]) / sample[i] * 100)
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return variance ** 0.5

    def _ema_series(self, values: List[float], period: int) -> List[float]:
        if not values:
            return []
        multiplier = 2 / (period + 1)
        ema = [values[0]]
        for v in values[1:]:
            ema.append((v - ema[-1]) * multiplier + ema[-1])
        return ema

    # ---- 技术指标 ----

    def _calc_technical_indicators(
        self, closes: List[float], highs: List[float], lows: List[float],
        volumes: List[float], rate: float,
    ) -> Dict[str, Any]:
        latest = closes[0]
        rsi14 = self._rsi(closes, 14)
        macd = self._macd(closes)
        boll = self._bollinger(closes, 20, rate)
        kdj = self._kdj(highs, lows, closes)

        ranges = {}
        for period, label in [(20, "20d"), (60, "60d"), (120, "120d")]:
            if len(closes) >= period and len(highs) >= period and len(lows) >= period:
                high = max(highs[:period])
                low = min(lows[:period])
                pos = (latest - low) / (high - low) * 100 if high != low else None
                ranges[label] = {
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "high_decision": round(high * rate, 4),
                    "low_decision": round(low * rate, 4),
                    "position_pct": round(pos, 2) if pos is not None else None,
                }

        vol_ma5 = self._ma(volumes, 5)
        vol_ma20 = self._ma(volumes, 20)
        latest_vol = volumes[0] if volumes else None
        vol_vs_ma20 = latest_vol / vol_ma20 if latest_vol and vol_ma20 else None

        return {
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "macd": macd,
            "bollinger": boll,
            "kdj": kdj,
            "volume_price_signal": self._volume_price_signal(closes, volumes),
            "range_position": ranges,
            "volume": {
                "latest": round(latest_vol, 2) if latest_vol is not None else None,
                "ma5": round(vol_ma5, 2) if vol_ma5 is not None else None,
                "ma20": round(vol_ma20, 2) if vol_ma20 is not None else None,
                "latest_vs_ma20": round(vol_vs_ma20, 2) if vol_vs_ma20 is not None else None,
            },
        }

    @staticmethod
    def _volume_price_signal(closes: List[float], volumes: List[float]) -> Optional[str]:
        """5日量价关系：放量上涨/缩量下跌/放量滞涨/缩量止跌。"""
        if len(closes) < 6 or len(volumes) < 6:
            return None
        p_chg = (closes[0] - closes[5]) / closes[5] * 100 if closes[5] > 0 else 0
        v5 = sum(volumes[:5]) / 5
        v20 = sum(volumes[5:25]) / 25 if len(volumes) >= 25 else (sum(volumes[5:]) / max(len(volumes[5:]), 1) if len(volumes) > 5 else v5)
        v_ratio = v5 / v20 if v20 > 0 else 1
        if p_chg > 3 and v_ratio > 1.2:
            return "放量上涨"
        if p_chg < -3 and v_ratio < 0.8:
            return "缩量下跌"
        if abs(p_chg) < 1 and v_ratio > 1.3:
            return "放量滞涨"
        if p_chg > -1 and v_ratio < 0.7:
            return "缩量止跌"
        return "量价正常"

    @staticmethod
    def _kdj(highs: List[float], lows: List[float], closes: List[float],
             n: int = 9) -> Dict[str, Optional[float]]:
        """计算 KDJ 指标（9日）。"""
        if len(highs) < n or len(lows) < n or len(closes) < n:
            return {"k": None, "d": None, "j": None}

        # 初始值用第一个 RSV
        k_vals, d_vals = [], []
        k_prev, d_prev = 50.0, 50.0
        for i in range(n - 1, -1, -1):
            high_n = max(highs[i:i + n])
            low_n = min(lows[i:i + n])
            rsv = (closes[i] - low_n) / (high_n - low_n) * 100 if high_n != low_n else 50.0
            k_prev = 2 / 3 * k_prev + 1 / 3 * rsv
            d_prev = 2 / 3 * d_prev + 1 / 3 * k_prev
            k_vals.append(k_prev)
            d_vals.append(d_prev)

        if not k_vals:
            return {"k": None, "d": None, "j": None}

        k = k_vals[-1]
        d = d_vals[-1]
        j = 3 * k - 2 * d
        return {"k": round(k, 2), "d": round(d, 2), "j": round(j, 2)}

    def _rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        """Wilder's smoothed RSI（标准 RSI），与主流平台交叉验证一致。"""
        if len(closes) <= period:
            return None

        # 初始 avg_gain / avg_loss：前 period 根 K 线的简单平均
        gains, losses = [], []
        for i in range(1, period + 1):
            change = closes[i - 1] - closes[i]
            gains.append(max(change, 0))
            losses.append(abs(min(change, 0)))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # 后续 K 线：Wilder 平滑 (n-1)/n × prev + 1/n × current
        for i in range(period + 1, len(closes)):
            change = closes[i - 1] - closes[i]
            avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
            avg_loss = (avg_loss * (period - 1) + abs(min(change, 0))) / period

        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))

    def _macd(self, closes: List[float]) -> Dict[str, Optional[float]]:
        if len(closes) < 35:
            return {"dif": None, "dea": None, "histogram": None}
        ema12 = self._ema_series(closes, 12)
        ema26 = self._ema_series(closes, 26)
        dif = [s - l for s, l in zip(ema12[-len(ema26):], ema26)]
        dea = self._ema_series(dif, 9)
        return {
            "dif": round(dif[0], 4),
            "dea": round(dea[0], 4),
            "histogram": round((dif[0] - dea[0]) * 2, 4),
        }

    def _bollinger(self, closes: List[float], period: int, rate: float) -> Dict[str, Optional[float]]:
        if len(closes) < period:
            return {"upper": None, "middle": None, "lower": None, "percent_b": None, "bandwidth_pct": None}
        sample = closes[:period]
        middle = sum(sample) / period
        variance = sum((x - middle) ** 2 for x in sample) / period
        std = variance ** 0.5
        upper = middle + 2 * std
        lower = middle - 2 * std
        latest = closes[0]
        percent_b = (latest - lower) / (upper - lower) if upper != lower else None
        bandwidth = (upper - lower) / middle * 100 if middle else None
        return {
            "upper": round(upper, 4), "middle": round(middle, 4), "lower": round(lower, 4),
            "upper_decision": round(upper * rate, 4),
            "middle_decision": round(middle * rate, 4),
            "lower_decision": round(lower * rate, 4),
            "percent_b": round(percent_b, 4) if percent_b is not None else None,
            "bandwidth_pct": round(bandwidth, 2) if bandwidth is not None else None,
        }

    # ---- 趋势分类与解释 ----

    def _classify_trend(
        self, latest: float, ma5: Optional[float], ma20: Optional[float],
        ma60: Optional[float], return_5d: Optional[float],
        return_20d: Optional[float], return_60d: Optional[float],
        volatility_20d: Optional[float],
    ) -> Tuple[str, List[str], List[str]]:
        supporting, risks = [], []
        if ma20 and latest >= ma20:
            supporting.append("当前价站上 MA20")
        elif ma20:
            risks.append("当前价低于 MA20")
        if ma60 and latest >= ma60:
            supporting.append("当前价站上 MA60")
        elif ma60:
            risks.append("当前价低于 MA60")
        if ma5 and ma20 and ma5 >= ma20:
            supporting.append("MA5 高于 MA20，短线趋势偏强")
        elif ma5 and ma20:
            risks.append("MA5 低于 MA20，短线趋势偏弱")
        if return_20d is not None:
            if return_20d >= 8:
                supporting.append(f"20日涨幅较强: {return_20d:.2f}%")
            elif return_20d <= -8:
                risks.append(f"20日跌幅较大: {return_20d:.2f}%")
        if volatility_20d is not None and volatility_20d >= 4:
            risks.append(f"20日波动率偏高: {volatility_20d:.2f}%")

        if len(supporting) >= 3 and not risks:
            status = "趋势较强"
        elif len(risks) >= 3:
            status = "趋势偏弱"
        elif supporting and risks:
            status = "震荡分歧"
        elif supporting:
            status = "修复中"
        else:
            status = "趋势中性"
        return status, supporting, risks

    def _build_trend_interpretation(
        self, *, status: str, latest: float,
        ma5: Optional[float], ma20: Optional[float], ma60: Optional[float],
        return_5d: Optional[float], return_20d: Optional[float],
        return_60d: Optional[float], volatility_20d: Optional[float],
        price_to_decision_rate: float, decision_currency: str,
        add_price: Optional[float], stop_loss_price: Optional[float],
        cost_price: Optional[float],
        volume_contracting: Optional[bool] = None,
        rsi: Optional[float] = None,
    ) -> Dict[str, Any]:
        ma_structure = self._describe_ma_structure(latest, ma5, ma20, ma60, price_to_decision_rate)
        key_levels = self._describe_key_levels(latest, ma20, ma60, price_to_decision_rate, decision_currency)
        summary = self._trend_summary(status, ma_structure, return_20d, return_60d, volatility_20d)

        current_price = latest * price_to_decision_rate
        stop_buffer = (current_price - stop_loss_price) / stop_loss_price * 100 if stop_loss_price else None
        drawdown_cost = (current_price - cost_price) / cost_price * 100 if cost_price else None

        return {
            "technical_summary": summary,
            "ma_structure": ma_structure,
            "key_levels": key_levels,
            "scenario_projection": self._build_scenario(latest, ma20, ma60, return_20d, price_to_decision_rate, decision_currency),
            "bottom_fishing_analysis": self._build_bottom_fishing(
                latest, ma20, ma60, return_5d, return_20d, return_60d, volatility_20d,
                price_to_decision_rate, decision_currency,
                add_price, stop_loss_price, cost_price, stop_buffer, drawdown_cost,
                volume_contracting, rsi,
            ),
        }

    def _describe_ma_structure(
        self, latest: float, ma5: Optional[float], ma20: Optional[float],
        ma60: Optional[float], rate: float,
    ) -> Dict[str, Any]:
        positions = []
        for label, val in [("MA5", ma5), ("MA20", ma20), ("MA60", ma60)]:
            if val is None:
                continue
            direction = "上方" if latest >= val else "下方"
            distance = (latest - val) / val * 100 if val else 0
            positions.append({
                "ma": label, "level": round(val, 4),
                "level_decision": round(val * rate, 4),
                "direction": direction, "distance_pct": round(distance, 2),
            })
        if ma5 and ma20 and ma60:
            if ma5 > ma20 > ma60:
                structure, meaning = "多头排列", "短中期均线向上排列，趋势确认度较高。"
            elif ma5 < ma20 < ma60:
                structure, meaning = "空头排列", "短中期均线向下排列，趋势压力较大。"
            elif latest >= ma20 and ma20 >= ma60:
                structure, meaning = "修复偏强", "价格站回中期均线附近，趋势处于修复阶段。"
            elif latest < ma20 and ma20 < ma60:
                structure, meaning = "弱修复", "价格仍受中短期均线压制。"
            else:
                structure, meaning = "均线分歧", "短中期均线方向不一致。"
        else:
            structure, meaning = "样本不足", ""
        return {"structure": structure, "meaning": meaning, "price_position": positions}

    def _describe_key_levels(
        self, latest: float, ma20: Optional[float], ma60: Optional[float],
        rate: float, decision_currency: str,
    ) -> Dict[str, Any]:
        levels = []
        for label, val, meaning in [("MA20", ma20, "短中期趋势位"), ("MA60", ma60, "中期趋势防线")]:
            if val is None:
                continue
            role = "支撑" if latest >= val else "压力"
            distance = (latest - val) / val * 100 if val else 0
            levels.append({
                "name": label, "price": round(val, 4),
                "price_decision": round(val * rate, 4),
                "role": role, "distance_pct": round(distance, 2), "meaning": meaning,
            })
        supports = [l for l in levels if l["role"] == "支撑"]
        pressures = [l for l in levels if l["role"] == "压力"]
        return {
            "levels": levels,
            "nearest_support": min(supports, key=lambda x: abs(x["distance_pct"]), default=None),
            "nearest_pressure": min(pressures, key=lambda x: abs(x["distance_pct"]), default=None),
            "decision_currency": decision_currency,
        }

    def _build_scenario(
        self, latest: float, ma20: Optional[float], ma60: Optional[float],
        return_20d: Optional[float], rate: float, currency: str,
    ) -> Dict[str, str]:
        ma20_text = f"MA20 {ma20 * rate:.4f} {currency}" if ma20 else "MA20"
        ma60_text = f"MA60 {ma60 * rate:.4f} {currency}" if ma60 else "MA60"
        below_ma20 = ma20 is not None and latest < ma20
        below_ma60 = ma60 is not None and latest < ma60

        optimistic = (f"继续站稳 {ma20_text} 且20日收益保持为正，趋势修复有望延续。"
                      if (return_20d is not None and return_20d > 0 and not below_ma20)
                      else f"重新站上并站稳 {ma20_text}，20日收益转正，才算修复得到确认。")
        neutral = (f"在 {ma20_text} 下方缩量震荡或跌幅收敛，可能进入筑底观察段。"
                   if below_ma20
                   else f"围绕 {ma20_text} 反复震荡，适合观察不追。")
        risk = (f"已位于 {ma60_text} 下方，中期支撑存疑，需降趋势信号权重。"
                if below_ma60
                else f"跌破 {ma60_text} 且20日收益转负，中期支撑失效。")
        return {"optimistic": optimistic, "neutral": neutral, "risk": risk}

    @staticmethod
    def _calc_volume_contraction(volumes: List[float]) -> Optional[bool]:
        """判断成交量是否萎缩：近5日均量 < 近20日均量 × 0.7。"""
        if not volumes or len(volumes) < 20:
            return None
        vol_5 = sum(volumes[:5]) / 5
        vol_20 = sum(volumes[:20]) / 20
        return vol_20 > 0 and vol_5 < vol_20 * 0.7

    def _build_bottom_fishing(
        self, latest: float, ma20: Optional[float], ma60: Optional[float],
        return_5d: Optional[float], return_20d: Optional[float],
        return_60d: Optional[float], volatility_20d: Optional[float],
        rate: float, currency: str,
        add_price: Optional[float], stop_loss: Optional[float],
        cost_price: Optional[float],
        stop_buffer: Optional[float], drawdown_cost: Optional[float],
        volume_contracting: Optional[bool] = None,
        rsi: Optional[float] = None,
    ) -> Dict[str, Any]:
        current_price = latest * rate
        ma20_price = ma20 * rate if ma20 else None
        ma60_price = ma60 * rate if ma60 else None
        below_ma20 = ma20 is not None and latest < ma20
        below_ma60 = ma60 is not None and latest < ma60
        add_broken = add_price is not None and current_price <= add_price
        near_add = (add_price is not None and not add_broken and current_price <= add_price * 1.03)
        short_stable = return_5d is not None and return_5d >= 0
        medium_weak = (return_20d is not None and return_20d < 0 and
                       return_60d is not None and return_60d < 0)
        high_vol = volatility_20d is not None and volatility_20d >= 4

        support, risks = [], []
        if below_ma20:
            support.append("价格位于 MA20 下方，具备左侧探底观察场景")
        else:
            risks.append("价格尚未进入 MA20 下方的左侧探底场景")
        if add_broken:
            risks.append("当前价已跌破预设加仓价，原加仓价不再作为买入依据")
        elif near_add:
            support.append("当前价已接近或进入预设加仓观察区")
        elif add_price is not None:
            risks.append("当前价距预设加仓观察区仍偏远")
        if short_stable:
            support.append("5日收益为正，短线跌势有收敛迹象")
        else:
            risks.append("短线尚未出现止跌迹象")
        if stop_buffer is not None and stop_buffer >= 8:
            support.append(f"距止损线仍有 {stop_buffer:.2f}% 缓冲")
        elif stop_buffer is not None:
            risks.append(f"距止损线仅 {stop_buffer:.2f}% 缓冲")
        if below_ma60:
            risks.append("仍在 MA60 下方，中期趋势没有修复")
        if medium_weak:
            risks.append("20日/60日收益仍为负，底部尚未右侧确认")
        if high_vol:
            risks.append(f"20日波动率 {volatility_20d:.2f}% 偏高，探底失败概率上升")

        # RSI 从低位拐头（底部确认信号）
        rsi_bottom_turn = rsi is not None and 20 <= rsi <= 35
        if rsi_bottom_turn:
            support.append(f"RSI {rsi:.1f} 处于底部区域，关注拐头确认")
        if volume_contracting:
            support.append("成交量明显萎缩（5日均量 < 20日均量×0.7），底部常见特征")

        return {
            "purpose": "llm_input",
            "current_price": round(current_price, 4),
            "decision_currency": currency,
            "add_price": round(add_price, 4) if add_price else None,
            "add_price_broken": add_broken,
            "stop_loss_price": round(stop_loss, 4) if stop_loss else None,
            "ma20": round(ma20_price, 4) if ma20_price else None,
            "ma60": round(ma60_price, 4) if ma60_price else None,
            "stop_buffer_pct": round(stop_buffer, 2) if stop_buffer is not None else None,
            "drawdown_from_cost_pct": round(drawdown_cost, 2) if drawdown_cost is not None else None,
            "flags": {
                "below_ma20": below_ma20, "below_ma60": below_ma60,
                "near_add_zone": near_add, "add_price_broken": add_broken,
                "short_stabilizing": short_stable, "medium_trend_weak": medium_weak,
                "high_volatility": high_vol,
                "close_to_stop_loss": stop_buffer is not None and stop_buffer < 5,
                "volume_contracting": volume_contracting or False,
                "rsi_bottom_turn": rsi_bottom_turn,
            },
            "supporting_factors": support,
            "risk_factors": risks,
        }

    def _trend_summary(
        self, status: str, ma_structure: Dict[str, Any],
        return_20d: Optional[float], return_60d: Optional[float],
        volatility_20d: Optional[float],
    ) -> str:
        parts = [f"当前判断为{status}，均线结构为{ma_structure.get('structure', '未判断')}"]
        if return_20d is not None and return_60d is not None:
            if return_20d > 0 and return_60d > 0:
                parts.append("20日/60日收益均为正，短中期趋势有共同支撑")
            elif return_20d > 0 >= return_60d:
                parts.append("20日收益转正但60日仍弱，属于阶段修复而非完整主升")
            elif return_20d <= 0 and return_60d <= 0:
                parts.append("20日/60日收益均不强，趋势仍需修复")
        if volatility_20d is not None and volatility_20d >= 4:
            parts.append(f"20日波动率 {volatility_20d:.2f}% 偏高，信号稳定性下降")
        return "；".join(parts) + "。"

    def _trend_llm_context(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        interp = signal.get("interpretation", {}) or {}
        bottom = interp.get("bottom_fishing_analysis", {}) or {}
        return {
            "purpose": "llm_input",
            "signal_type": "trend",
            "facts": {
                "status": signal.get("status"),
                "price_to_decision_rate": signal.get("price_to_decision_rate"),
                "decision_currency": signal.get("decision_currency"),
                "latest_close": signal.get("latest_close"),
                "latest_close_decision": signal.get("latest_close_decision"),
                "ma": signal.get("ma", {}),
                "ma_decision": signal.get("ma_decision", {}),
                "returns": signal.get("returns", {}),
                "volatility_20d": signal.get("volatility_20d"),
                "technical_indicators": signal.get("technical_indicators", {}),
                "ma_structure": interp.get("ma_structure", {}),
                "key_levels": interp.get("key_levels", {}),
                "bottom_fishing_analysis": bottom,
            },
            "supporting_factors": signal.get("supporting_factors", []),
            "risk_factors": signal.get("risk_factors", []),
            "missing_data": signal.get("missing_data", []),
        }

    # ================================================================
    # 5. 财务数据 — MongoDB
    # ================================================================

    def get_financial(self, code: str) -> Dict[str, Any]:
        """获取最新财务数据。

        Returns:
            {"available": bool, "data": dict|None, "data_date": str, "stale": bool}
        """
        if not self.mongo_available:
            return {"available": False, "data": None, "data_date": None, "stale": True,
                    "stale_reason": "MongoDB 不可用"}
        doc = self.mongo.get_latest_financial(code)
        if not doc:
            return {"available": False, "data": None, "data_date": None, "stale": True,
                    "stale_reason": "MongoDB 无财务数据"}
        report_period = doc.get("report_period", "")
        updated_at = str(doc.get("updated_at") or "")
        freshness = _freshness(report_period or updated_at, max_age_days=120)
        return {"available": True, "data": doc, "data_date": report_period or updated_at,
                **freshness}

    # ================================================================
    # 6. 股票基础信息 — MongoDB
    # ================================================================

    def get_stock_info(self, code: str) -> Dict[str, Any]:
        """获取股票基础信息（名称、行业、PE/PB、市值等）。

        Returns:
            {"available": bool, "data": dict|None, "data_date": str, "stale": bool}
        """
        if not self.mongo_available:
            return {"available": False, "data": None, "data_date": None, "stale": True,
                    "stale_reason": "MongoDB 不可用"}
        doc = self.mongo.get_basic(code)
        if not doc:
            return {"available": False, "data": None, "data_date": None, "stale": True,
                    "stale_reason": "MongoDB 无此股票基础信息"}
        updated_at = str(doc.get("updated_at") or "")
        fr = _freshness(updated_at, max_age_days=7)
        return {"available": True, "data": doc, "data_date": updated_at, **fr}

    # ================================================================
    # 7. 新闻/事件 — AKShare
    # ================================================================

    def get_news(self, code: str, limit: int = 20) -> List[Dict[str, Any]]:
        """获取个股新闻。"""
        self._ensure_news()
        if self._news_provider is None:
            return []
        return self._news_provider.get_stock_news(code, limit=limit)

    def get_market_news(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取市场热点新闻。"""
        self._ensure_news()
        if self._news_provider is None:
            return []
        return self._news_provider.get_market_hot_news(limit=limit)

    def get_macro_news(self, limit: int = 8) -> List[Dict[str, Any]]:
        """获取宏观财经要闻 — 从 MongoDB 读取（由 import-macro-news 定时导入）。"""
        result = []
        if self.mongo_available:
            try:
                today = _utc_now().strftime("%Y%m%d")
                docs = self.mongo.db["market_news"].find(
                    {"fetched_date": today}, sort=[("pub_time", -1)], limit=limit)
                for d in docs:
                    result.append({
                        "title": d.get("title", ""),
                        "pub_time": d.get("pub_time", ""),
                        "src": d.get("source", "华尔街见闻"),
                    })
            except Exception:
                pass
        return result

    def analyze_sentiment(self, text: str) -> Dict[str, Any]:
        """分析文本情绪。"""
        self._ensure_news()
        if self._sentiment_analyzer is None:
            return {"score": 0.0, "sentiment": "neutral", "positive_keywords": [], "negative_keywords": []}
        return self._sentiment_analyzer.analyze_sentiment(text)

    def analyze_sentiment_batch(self, texts: List[str]) -> List[Dict[str, Any]]:
        """批量情绪分析（合并LLM调用，带缓存去重）。"""
        self._ensure_news()
        if self._sentiment_analyzer is None:
            return [{"score": 0.0, "sentiment": "neutral", "positive_keywords": [], "negative_keywords": []}
                    for _ in texts]
        return self._sentiment_analyzer.analyze_sentiment_batch(texts)

    def extract_themes(self, text: str) -> List[str]:
        """提取文本关联的政策主题。"""
        self._ensure_news()
        if self._sentiment_analyzer is None:
            return []
        return self._sentiment_analyzer.extract_themes(text)

    def _ensure_news(self) -> None:
        if self._news_provider is not None:
            return
        try:
            from event_driven_strategy import NewsDataProvider, NewsSentimentAnalyzer
            self._news_provider = NewsDataProvider()
            self._sentiment_analyzer = NewsSentimentAnalyzer()
        except Exception as exc:
            logger.warning(f"新闻模块不可用: {exc}")

    # ================================================================
    # 8. 资金流向 — Tushare → MongoDB cache
    # ================================================================

    def get_moneyflow(self, code: str, refresh: bool = False) -> Dict[str, Any]:
        """获取个股资金流向（主力/散户净流入等）。

        优先从 MongoDB 缓存读；refresh=True 时尝试 Tushare 实时获取。
        """
        # 先查 MongoDB 缓存
        if self.mongo_available and not refresh:
            try:
                doc = self.mongo.db[self.mongo.collections.get("moneyflow", "stock_moneyflow")].find_one(
                    {"symbol": code}, sort=[("trade_date", -1)],
                )
                if doc:
                    return {"available": True, "source": "mongodb", "data": self._normalize_moneyflow(doc)}
            except Exception:
                pass

        # Tushare 实时
        tushare_data = self._fetch_moneyflow_tushare(code)
        if tushare_data:
            return {"available": True, "source": "tushare", "data": tushare_data}
        return {"available": False, "reason": "资金流向数据不可用"}

    def _fetch_moneyflow_tushare(self, code: str) -> Optional[Dict[str, Any]]:
        try:
            import tushare as ts
            token = os.environ.get("TUSHARE_TOKEN", "")
            if token:
                ts.set_token(token)
            pro = ts.pro_api()
            ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            today = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
            df = pro.moneyflow(ts_code=ts_code, start_date=start, end_date=today)
            if df is not None and not df.empty:
                row = df.iloc[0].to_dict()
                return self._normalize_moneyflow(row)
        except Exception:
            pass
        return None

    @staticmethod
    def _normalize_moneyflow(doc: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "trade_date": doc.get("trade_date"),
            "buy_elg_amount": _to_float(doc.get("buy_elg_amount")),
            "sell_elg_amount": _to_float(doc.get("sell_elg_amount")),
            "buy_lg_amount": _to_float(doc.get("buy_lg_amount")),
            "sell_lg_amount": _to_float(doc.get("sell_lg_amount")),
            "buy_md_amount": _to_float(doc.get("buy_md_amount")),
            "sell_md_amount": _to_float(doc.get("sell_md_amount")),
            "buy_sm_amount": _to_float(doc.get("buy_sm_amount")),
            "sell_sm_amount": _to_float(doc.get("sell_sm_amount")),
        }

    # ================================================================
    # 9. 盘口数据 — Tushare → AKShare → Sina
    # ================================================================

    def get_orderbook(self, code: str, market: str = "A股") -> Dict[str, Any]:
        """获取五档买卖盘口数据。

        优先级: Tushare → AKShare → Sina（港股仅买一卖一）。
        """
        # Tushare（A股五档）
        ob = self._fetch_orderbook_tushare(code)
        if ob:
            return {"available": True, "source": "tushare", "data": ob}

        # AKShare（A股五档）
        ob = self._fetch_orderbook_akshare(code)
        if ob:
            return {"available": True, "source": "akshare", "data": ob}

        # Sina 兜底（A股/港股均支持，仅买一卖一 + 现价）
        ob = self._fetch_orderbook_sina(code, market)
        if ob:
            return {"available": True, "source": "sina", "data": ob}

        return {"available": False, "reason": "盘口数据不可用"}

    def _fetch_orderbook_tushare(self, code: str) -> Optional[Dict[str, Any]]:
        try:
            import tushare as ts
            token = os.environ.get("TUSHARE_TOKEN", "")
            if token:
                ts.set_token(token)
            df = ts.get_realtime_quotes(code)
            if df is None or df.empty:
                return None
            row = df.iloc[0]
            return {
                "bid1_price": _to_float(row.get("b1_p")),
                "bid1_volume": _to_float(row.get("b1_v")),
                "bid2_price": _to_float(row.get("b2_p")),
                "bid2_volume": _to_float(row.get("b2_v")),
                "bid3_price": _to_float(row.get("b3_p")),
                "bid3_volume": _to_float(row.get("b3_v")),
                "bid4_price": _to_float(row.get("b4_p")),
                "bid4_volume": _to_float(row.get("b4_v")),
                "bid5_price": _to_float(row.get("b5_p")),
                "bid5_volume": _to_float(row.get("b5_v")),
                "ask1_price": _to_float(row.get("a1_p")),
                "ask1_volume": _to_float(row.get("a1_v")),
                "ask2_price": _to_float(row.get("a2_p")),
                "ask2_volume": _to_float(row.get("a2_v")),
                "ask3_price": _to_float(row.get("a3_p")),
                "ask3_volume": _to_float(row.get("a3_v")),
                "ask4_price": _to_float(row.get("a4_p")),
                "ask4_volume": _to_float(row.get("a4_v")),
                "ask5_price": _to_float(row.get("a5_p")),
                "ask5_volume": _to_float(row.get("a5_v")),
                "current_price": _to_float(row.get("price")),
            }
        except Exception:
            return None

    def _fetch_orderbook_sina(self, code: str, market: str) -> Optional[Dict[str, Any]]:
        """Sina 行情兜底盘口（仅买一卖一 + 现价）。"""
        symbol = self._sina_symbol(code, market)
        if not symbol:
            return None
        try:
            text = self._read_http(
                f"https://hq.sinajs.cn/list={symbol}",
                encoding="gbk",
                headers={"Referer": "https://finance.sina.com.cn/"},
            )
            payload = text.split('="', 1)[1].rsplit('"', 1)[0]
            fields = payload.split(",")
            if market == "港股":
                # Sina HK: name(0), en(1), open(2), prev_close(3), high(4), low(5),
                #             current(6), change(7), change%(8), bid1(9), ask1(10), ...
                price = _to_float(fields[6] if len(fields) > 6 else None)
                bid1 = _to_float(fields[9] if len(fields) > 9 else None)
                ask1 = _to_float(fields[10] if len(fields) > 10 else None)
            else:
                # Sina A: name(0), open(1), prev_close(2), current(3),
                #           high(4), low(5), bid1(11), ask1(21), ...
                price = _to_float(fields[3] if len(fields) > 3 else None)
                bid1 = _to_float(fields[11] if len(fields) > 11 else None)
                ask1 = _to_float(fields[21] if len(fields) > 21 else None)
            if not price or price <= 0:
                return None
            return {
                "bid1_price": bid1, "bid1_volume": None,
                "bid2_price": None, "bid2_volume": None,
                "bid3_price": None, "bid3_volume": None,
                "bid4_price": None, "bid4_volume": None,
                "bid5_price": None, "bid5_volume": None,
                "ask1_price": ask1, "ask1_volume": None,
                "ask2_price": None, "ask2_volume": None,
                "ask3_price": None, "ask3_volume": None,
                "ask4_price": None, "ask4_volume": None,
                "ask5_price": None, "ask5_volume": None,
                "current_price": price,
            }
        except Exception:
            return None

    def _fetch_orderbook_akshare(self, code: str) -> Optional[Dict[str, Any]]:
        try:
            if not self._ensure_akshare():
                return None
            df = self._ak.stock_bid_ask_em(symbol=code)
            if df is None or df.empty:
                return None
            data_dict = {}
            for _, row in df.iterrows():
                item = row.get("item", row.get("名称", ""))
                value = row.get("value", row.get("数值", 0))
                data_dict[item] = value
            return {
                "bid1_price": _to_float(data_dict.get("buy_1")),
                "bid1_volume": _to_float(data_dict.get("buy_1_vol")),
                "bid2_price": _to_float(data_dict.get("buy_2")),
                "bid2_volume": _to_float(data_dict.get("buy_2_vol")),
                "bid3_price": _to_float(data_dict.get("buy_3")),
                "bid3_volume": _to_float(data_dict.get("buy_3_vol")),
                "bid4_price": _to_float(data_dict.get("buy_4")),
                "bid4_volume": _to_float(data_dict.get("buy_4_vol")),
                "bid5_price": _to_float(data_dict.get("buy_5")),
                "bid5_volume": _to_float(data_dict.get("buy_5_vol")),
                "ask1_price": _to_float(data_dict.get("sell_1")),
                "ask1_volume": _to_float(data_dict.get("sell_1_vol")),
                "ask2_price": _to_float(data_dict.get("sell_2")),
                "ask2_volume": _to_float(data_dict.get("sell_2_vol")),
                "ask3_price": _to_float(data_dict.get("sell_3")),
                "ask3_volume": _to_float(data_dict.get("sell_3_vol")),
                "ask4_price": _to_float(data_dict.get("sell_4")),
                "ask4_volume": _to_float(data_dict.get("sell_4_vol")),
                "ask5_price": _to_float(data_dict.get("sell_5")),
                "ask5_volume": _to_float(data_dict.get("sell_5_vol")),
                "current_price": _to_float(data_dict.get("最新")),
            }
        except Exception:
            return None

    # ================================================================
    # 10. 汇率 — Yahoo → 内置兜底
    # ================================================================

    def get_fx_rate(self, from_currency: str, to_currency: str = "CNY") -> float:
        """获取单个货币对的汇率。

        数据源优先级: Sina → Yahoo → 内置兜底
        """
        if from_currency == to_currency:
            return 1.0

        cache_key = f"{from_currency}{to_currency}"
        if cache_key in self._fx_cache:
            return self._fx_cache[cache_key]

        # 1. Sina 汇率（国内可用，免费）
        sina_symbols = {"HKDCNY": "fx_shkdcny", "USDCNY": "fx_susdcny"}
        sina_code = sina_symbols.get(cache_key)
        if sina_code:
            try:
                url = f"https://hq.sinajs.cn/list={sina_code}"
                text = self._read_http(url, encoding="gbk", headers={"Referer": "https://finance.sina.com.cn/"})
                payload = text.split('="', 1)[1].rsplit('"', 1)[0]
                fields = payload.split(",")
                # Sina FX: 名称(0), 最新价(1), 今开(2), 昨收(3), ...
                rate = _to_float(fields[1] if len(fields) > 1 else None, None)
                if rate and 0.5 < rate < 50:
                    self._fx_cache[cache_key] = rate
                    self._save_fx_fallback()
                    return rate
            except Exception:
                pass

        # 2. Yahoo
        yahoo_symbol = f"{from_currency}{to_currency}=X"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(yahoo_symbol)}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result = (payload.get("chart") or {}).get("result") or []
            meta = result[0].get("meta", {}) if result else {}
            price = _to_float(meta.get("regularMarketPrice") or meta.get("previousClose"))
            if 0.5 < price < 50:  # 汇率合理范围
                self._fx_cache[cache_key] = price
                self._save_fx_fallback()
                return price
        except Exception:
            pass

        # 3. 兜底：从落盘文件读取上次成功获取的汇率。
        #    消除硬编码值因汇率波动而过期的问题。
        #    文件不存在时才用绝对兜底（首次运行无网络场景）。
        file_fb = self._load_fx_fallback()

        if from_currency == "HKD" and to_currency == "CNY":
            # HKD 通过 USDCNY / USDHKD 交叉汇率；USDHKD 取联系汇率中位数 7.83
            usdcny = (self._fx_cache.get("USDCNY")
                      or file_fb.get("USDCNY")
                      or 7.25)  # 绝对兜底：文件也不存在时
            return round(usdcny / 7.83, 4)

        if from_currency == "USD" and to_currency == "CNY":
            return (self._fx_cache.get("USDCNY")
                    or file_fb.get("USDCNY")
                    or 7.25)

        return 1.0

    def _save_fx_fallback(self) -> None:
        """将当前汇率缓存落盘，供后续兜底使用。"""
        try:
            with open(self._fx_fallback_path, "w") as f:
                json.dump(self._fx_cache, f)
        except Exception:
            pass

    def _load_fx_fallback(self) -> Dict[str, float]:
        """从落盘文件加载上次成功获取的汇率。"""
        try:
            with open(self._fx_fallback_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def resolve_fx_rates(
        self, base_currency: str, configured_rates: Dict[str, Any], currencies: List[str],
    ) -> Dict[str, float]:
        """批量解析汇率（兼容 portfolio_strategy 的 FxRateProvider 接口）。"""
        rates: Dict[str, float] = {}
        for currency in sorted(set(currencies + [base_currency])):
            if currency == base_currency:
                rates[currency] = 1.0
                continue
            configured = _to_float(configured_rates.get(currency), None)
            if configured:
                rates[currency] = configured
                continue
            rates[currency] = self.get_fx_rate(currency, base_currency)
        return rates

    # ================================================================
    # 11. 行业表现 — MongoDB 聚合
    # ================================================================

    def get_sector_performance(self, lookback_days: int = 20) -> List[Dict[str, Any]]:
        """获取行业涨跌表现排名。"""
        if self.mongo_available:
            return self.mongo.get_sector_performance_fast(lookback_days)
        return []

    # ================================================================
    # 12. 市场情绪 — 指数涨跌
    # ================================================================

    def get_market_sentiment(self) -> Dict[str, Any]:
        """获取 A 股整体市场情绪（基于指数涨跌 + 北向资金）。"""
        indices = {
            "上证指数": "000001",
            "创业板指": "399006",
            "沪深300": "000300",
        }
        sentiments = {}
        for name, code in indices.items():
            bars = self.get_bars(code, "A股", days=30).get("bars", [])
            if len(bars) < 5:
                sentiments[name] = {"available": False}
                continue
            closes = [b["close"] for b in bars if b.get("close", 0) > 0]
            if len(closes) < 5:
                sentiments[name] = {"available": False}
                continue
            ret_5d = self._return_pct(closes, 5)
            ret_20d = self._return_pct(closes, 20)
            vol_20d = self._volatility(closes, 20)
            sentiments[name] = {
                "available": True,
                "latest_close": closes[0],
                "return_5d": round(ret_5d, 2) if ret_5d is not None else None,
                "return_20d": round(ret_20d, 2) if ret_20d is not None else None,
                "volatility_20d": round(vol_20d, 2) if vol_20d is not None else None,
            }

        # 北向资金
        north_flow = self._get_north_bound_flow()
        # 融资融券
        margin = self._get_margin_data()
        # 全市场主力资金
        mkt_mf = self._get_market_moneyflow()
        # 行业资金流（东方财富实时）
        ind_mf = self._get_industry_moneyflow()

        # 综合情绪判断：用 z-score（return / vol）替代固定阈值，
        # 避免高波动指数（如创业板 8%+）误判、低波动指数（如上证 2%）太迟钝。
        available = [s for s in sentiments.values() if s.get("available")]
        if not available:
            return {"available": False, "indices": sentiments, "north_bound": north_flow}

        z_scores = []
        for s in available:
            ret = s.get("return_20d")
            vol = s.get("volatility_20d")
            if ret is not None and vol and vol > 0:
                z_scores.append(ret / vol)

        avg_20d = sum(s.get("return_20d") or 0 for s in available) / len(available)

        if z_scores:
            avg_z = sum(z_scores) / len(z_scores)
            if avg_z > 1:
                mood = "bullish"
            elif avg_z < -1:
                mood = "bearish"
            else:
                mood = "neutral"
        else:
            mood = "neutral"

        return {"available": True, "mood": mood, "avg_return_20d": round(avg_20d, 2),
                "avg_z_score": round(avg_z if z_scores else 0, 2),
                "indices": sentiments,
                "north_bound": north_flow, "margin": margin,
                "market_moneyflow": mkt_mf,
                "industry_moneyflow": ind_mf}

    def _get_north_bound_flow(self) -> Dict[str, Any]:
        """获取近期北向/南向成交净买额。

        数据源优先级：
        1. MongoDB stock_hsgt_flow（自存，东方财富 RPT_MUTUAL_QUOTA，每日14:55导入）
        2. Tushare moneyflow_hsgt（兜底，但字段含义存疑，仅提供买入额）
        """
        # 主源：MongoDB 自存数据
        try:
            mongo_result = self._get_hsgt_from_mongo()
            if mongo_result.get("available"):
                return mongo_result
        except Exception:
            pass

        # 兜底：Tushare
        return self._get_hsgt_from_tushare()

    def _get_hsgt_from_mongo(self) -> Dict[str, Any]:
        """从 MongoDB stock_hsgt_flow 读取近期净买额（东方财富数据）。"""
        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "config_complete.yaml",
            )
            mongodb_config = _load_mongodb_config(config_path)
            store = MongoFactorDataStore(
                mongodb_config,
                readonly=True,
                ensure_indexes=False,
            )
            coll = store.db[store.collections.get("hsgt_flow", "stock_hsgt_flow")]

            rows = list(coll.find(
                {"direction": {"$in": ["北向", "南向"]}},
                {"_id": 0, "trade_date": 1, "mutual_type": 1, "direction": 1, "net_buy_amt": 1},
            ).sort("trade_date", -1).limit(20))

            if not rows:
                return {"available": False, "reason": "MongoDB stock_hsgt_flow 无数据"}

            # 按日期聚合：北向 = 沪股通(001) + 深股通(003)，南向 = 港股通沪(002) + 港股通深(004)
            from collections import defaultdict
            daily: Dict[str, Dict[str, float]] = defaultdict(lambda: {"north": 0.0, "south": 0.0})
            for r in rows:
                d = r["trade_date"]
                amt = float(r.get("net_buy_amt", 0) or 0)
                mt = str(r.get("mutual_type", ""))
                if mt in ("001", "003"):  # 沪股通/深股通 → 北向
                    daily[d]["north"] += amt
                elif mt in ("002", "004"):  # 港股通沪/港股通深 → 南向
                    daily[d]["south"] += amt

            sorted_dates = sorted(daily.keys(), reverse=True)
            latest_date = sorted_dates[0] if sorted_dates else ""

            # 数据新鲜度：最新日期距今天数
            age_days = None
            if latest_date:
                try:
                    latest_dt = datetime.strptime(latest_date, "%Y-%m-%d")
                    age_days = (_utc_now() - latest_dt.replace(tzinfo=timezone.utc)).days
                except Exception:
                    pass

            # 近5日（按自然日取最近5个有数据的交易日）
            recent_dates = sorted_dates[:5]
            north_5d = round(sum(daily[d]["north"] for d in recent_dates), 2)
            south_5d = round(sum(daily[d]["south"] for d in recent_dates), 2)

            return {
                "available": True,
                "source": "mongodb_eastmoney",
                "latest_date": latest_date,
                "age_days": age_days,
                "north_5d_buy": north_5d,
                "south_5d_buy": south_5d,
                "_note": "成交净买额(net flow)，东方财富数据源",
            }
        except Exception as exc:
            return {"available": False, "reason": f"MongoDB查询失败: {exc}"}

    def _get_hsgt_from_tushare(self) -> Dict[str, Any]:
        """Tushare 兜底：moneyflow_hsgt 买入成交额（非净买入，仅作参考）。"""
        try:
            pro = self._get_tushare_pro()
            if pro is None:
                return {"available": False, "reason": "Tushare 不可用"}
            today = _utc_now().strftime("%Y%m%d")
            start = (_utc_now() - __import__("datetime").timedelta(days=30)).strftime("%Y%m%d")
            df = pro.moneyflow_hsgt(start_date=start, end_date=today)
            if df is None or df.empty:
                return {"available": False, "reason": "无北向资金数据"}
            df = df.sort_values("trade_date", ascending=True)
            recent = df.tail(10)
            north = [_to_float(v, None) for v in recent.get("north_money", [])]
            south = [_to_float(v, None) for v in recent.get("south_money", [])]
            valid_n = [f / 10000 for f in north if f is not None]
            valid_s = [f / 10000 for f in south if f is not None]
            if not valid_n and not valid_s:
                return {"available": False, "reason": "近期无成交数据"}
            n5 = valid_n[-5:] if len(valid_n) >= 5 else valid_n
            s5 = valid_s[-5:] if len(valid_s) >= 5 else valid_s
            latest_date = str(df.iloc[-1].get("trade_date", ""))
            return {
                "available": True,
                "source": "tushare_fallback",
                "latest_date": latest_date,
                "north_5d_buy": round(sum(n5), 2) if n5 else 0,
                "south_5d_buy": round(sum(s5), 2) if s5 else 0,
                "_note": "Tushare兜底，买入成交额(gross buy)，非净买入",
            }
        except Exception as exc:
            return {"available": False, "reason": str(exc)}

    def _get_margin_data(self) -> Dict[str, Any]:
        """融资融券余额（Tushare margin）。"""
        try:
            pro = self._get_tushare_pro()
            if pro is None:
                return {"available": False}
            today = _utc_now().strftime("%Y%m%d")
            df = pro.margin(trade_date=today)
            if df is None or df.empty:
                return {"available": False}
            r = df.iloc[0]
            return {
                "available": True, "source": "tushare",
                "rzye": _to_float(r.get("rzye"), None),  # 融资余额(亿)
                "rqye": _to_float(r.get("rqye"), None),  # 融券余额(亿)
                "rzmre": _to_float(r.get("rzmre"), None),  # 融资买入额(亿)
                "date": str(r.get("trade_date", "")),
            }
        except Exception:
            return {"available": False}

    def _get_market_moneyflow(self) -> Dict[str, Any]:
        """全市场主力资金流向 — MongoDB优先，无数据时实时拉tushare API。"""
        # 先查 MongoDB
        if self.mongo_available:
            try:
                docs = list(self.mongo.db["market_moneyflow"].find(
                    {}, sort=[("trade_date", -1)], limit=10))
                if docs:
                    docs.sort(key=lambda x: x["trade_date"])
                    net_main = []
                    dates = []
                    for d in docs:
                        net = _to_float(d.get("net_amount"), 0) or 0
                        net_main.append(net / 1e8)  # 转换为亿
                        dates.append(str(d.get("trade_date", "")))
                    n5 = net_main[-5:] if len(net_main) >= 5 else net_main
                    d5 = dates[-5:] if len(dates) >= 5 else dates
                    total5 = round(sum(n5), 2) if n5 else 0
                    pos_days = sum(1 for f in n5 if f > 0) if n5 else 0
                    return {
                        "available": True, "source": "mongodb",
                        "net_main_5d": total5,
                        "pos_days": f"{pos_days}/{len(n5)}",
                        "label": "主力流入" if total5 > 0 else ("主力流出" if total5 < 0 else "主力平衡"),
                        "latest_date": d5[-1] if d5 else "",
                    }
            except Exception:
                pass

        # MongoDB无数据 → 实时查tushare API
        try:
            import requests, yaml
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "config", "config_complete.yaml")
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            token = (cfg.get("data_sources") or {}).get("tushare", {}).get("token", "")
            if not token:
                return {"available": False, "reason": "无 tushare token"}
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
            resp = requests.post(
                "https://api.tushare.pro",
                json={
                    "api_name": "moneyflow_mkt_dc",
                    "token": token,
                    "params": {"start_date": start, "end_date": end},
                    "fields": "trade_date,net_amount,close_sh,pct_change_sh",
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"available": False, "reason": data.get("msg", "")}
            items = data.get("data", {}).get("items", [])
            if not items:
                return {"available": False, "reason": "API 返回空"}
            items.sort(key=lambda x: x[0])
            net5 = []
            for item in items[-5:]:
                net5.append(_to_float(item[1]) / 1e8 if len(item) > 1 else 0)
            total5 = round(sum(net5), 2)
            pos_days = sum(1 for f in net5 if f > 0)
            # 存回MongoDB，下次直接用
            try:
                for item in items:
                    self.mongo.db["market_moneyflow"].update_one(
                        {"trade_date": item[0]},
                        {"$set": {
                            "trade_date": item[0],
                            "net_amount": _to_float(item[1]),
                            "close_sh": _to_float(item[2]) if len(item) > 2 else 0,
                            "pct_change_sh": _to_float(item[3]) if len(item) > 3 else 0,
                            "source": "tushare", "updated_at": datetime.now(),
                        }},
                        upsert=True)
            except Exception:
                pass
            return {
                "available": True, "source": "tushare实时",
                "net_main_5d": total5,
                "pos_days": f"{pos_days}/{len(net5)}",
                "label": "主力流入" if total5 > 0 else ("主力流出" if total5 < 0 else "主力平衡"),
                "latest_date": items[-1][0] if items else "",
            }
        except Exception as exc:
            return {"available": False, "reason": str(exc)}

    def _get_industry_moneyflow(self) -> Dict[str, Any]:
        """行业资金流 — 从 stock_signals 缓存中的个股 moneyflow_net 按行业汇总。

        数据源：Tushare moneyflow API → precompute_history 缓存到 stock_factors。
        优势：不依赖外部网络API，直接读本地MongoDB，每次review必可用。
        """
        if not self.mongo_available:
            return {"available": False, "reason": "MongoDB 不可用"}

        try:
            from collections import defaultdict

            # 从 stock_signals 拿所有有 moneyflow_net 的股票
            pipeline = [
                {"$match": {
                    "computed_at": {"$gte": (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")},
                    "moneyflow_net": {"$exists": True, "$ne": 0},
                }},
                {"$project": {"code": 1, "moneyflow_net": 1}},
            ]
            stocks = list(self.mongo.db["stock_signals"].aggregate(pipeline))
            if len(stocks) < 50:
                return {"available": False,
                        "reason": f"近期资金流数据不足（仅{len(stocks)}只），请运行 precompute_history.py --date today"}

            codes = [s["code"] for s in stocks]
            mf_map = {s["code"]: s["moneyflow_net"] for s in stocks}

            # 批量查行业
            ind_map = {}
            for doc in self.mongo.db[self.mongo.collections["basic_info"]].find(
                {"code": {"$in": codes}},
                {"code": 1, "industry": 1},
            ):
                ind_map[doc["code"]] = doc.get("industry", "未知")

            # 按行业汇总（万元→亿元）
            ind_flow = defaultdict(lambda: {"net": 0.0, "cnt": 0})
            for code, mf in mf_map.items():
                ind = ind_map.get(code, "未知")
                ind_flow[ind]["net"] += mf
                ind_flow[ind]["cnt"] += 1

            ranked = sorted(ind_flow.items(), key=lambda x: x[1]["net"], reverse=True)
            top_in = []
            top_out = []
            for ind, d in ranked:
                flow = round(d["net"] / 1e4, 2)  # 万元→亿
                if flow == 0:
                    continue
                entry = {"industry": ind, "net_flow": flow, "stock_count": d["cnt"]}
                if flow > 0:
                    top_in.append(entry)
                else:
                    top_out.append(entry)

            # 取实际数据日期
            data_date = stocks[0].get("computed_at", "") if stocks else ""
            return {
                "available": True,
                "source": "Tushare→stock_signals缓存",
                "data_date": data_date,
                "stocks_with_data": len(stocks),
                "top_inflow": top_in[:10],
                "top_outflow": top_out[-10:][::-1] if top_out else [],
            }
        except Exception as exc:
            return {"available": False, "reason": str(exc)}

    def _get_tushare_pro(self):
        """懒加载 Tushare pro 接口。"""
        if hasattr(self, "_ts_pro"):
            return self._ts_pro
        try:
            import yaml
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "config", "config_complete.yaml")
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            token = ((cfg.get("data_sources") or {}).get("tushare") or {}).get("token", "")
            if not token:
                self._ts_pro = None
                return None
            import tushare as ts
            ts.set_token(token)
            self._ts_pro = ts.pro_api()
            return self._ts_pro
        except Exception:
            self._ts_pro = None
            return None

    # ================================================================
    # 13. Tushare 专属信号（需付费 token）
    # ================================================================

    def get_tushare_signals(self, code: str, market: str = "A股") -> Dict[str, Any]:
        """获取个股 Tushare 专属信号：回购、股东人数、一致预期。"""
        pro = self._get_tushare_pro()
        result = {"available": False}
        if pro is None:
            return result

        # 港股：走南向持仓数据
        if market != "A股":
            result["available"] = True
            try:
                df = pro.hk_hold(ts_code=f"{code}.HK", exchange="HK",
                                 start_date="20260401", end_date=_utc_now().strftime("%Y%m%d"))
                if df is not None and len(df) >= 2:
                    latest = df.iloc[0]
                    prev = df.iloc[1]
                    ratio_now = _to_float(latest.get("ratio"), None)
                    ratio_prev = _to_float(prev.get("ratio"), None)
                    result["hk_hold"] = {
                        "vol": _to_float(latest.get("vol"), None),
                        "ratio": ratio_now,
                    }
                    if ratio_now is not None and ratio_prev is not None and ratio_prev > 0:
                        result["hk_hold"]["ratio_change"] = round(ratio_now - ratio_prev, 3)
                return result
            except Exception:
                result["available"] = True  # 部分可用
                return result

        ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"
        result["available"] = True

        # 1. 近期回购（A股）
        try:
            df = pro.repurchase(ts_code=ts_code, start_date="20250101",
                                end_date=_utc_now().strftime("%Y%m%d"))
            if df is not None and not df.empty:
                result["buyback"] = {
                    "count": len(df),
                    "latest_vol": _to_float(df.iloc[0].get("vol"), None),
                    "latest_date": str(df.iloc[0].get("ann_date", "")),
                }
        except Exception:
            pass

        # 2. 股东人数变化（筹码集中度）
        try:
            df = pro.stk_holdernumber(ts_code=ts_code)
            if df is not None and len(df) >= 2:
                latest = df.iloc[0]["holder_num"]
                prev = df.iloc[1]["holder_num"]
                if prev > 0:
                    change = (latest - prev) / prev * 100
                    result["holder_change"] = round(change, 1)
                    result["holder_num"] = latest
        except Exception:
            pass

        # 3. 一致预期（取最新一期）
        try:
            df = pro.forecast(ts_code=ts_code)
            if df is not None and not df.empty:
                df = df.sort_values("end_date", ascending=False)
                r = df.iloc[0]
                result["forecast"] = {
                    "type": r.get("type", ""),
                    "end_date": str(r.get("end_date", "")),
                    "p_change_min": _to_float(r.get("p_change_min"), None),
                    "p_change_max": _to_float(r.get("p_change_max"), None),
                }
        except Exception:
            pass

        # 4. 个股融资融券（margin_detail）
        try:
            mdf = pro.margin_detail(ts_code=ts_code,
                                    start_date=(_utc_now() - __import__("datetime").timedelta(days=7)).strftime("%Y%m%d"),
                                    end_date=_utc_now().strftime("%Y%m%d"))
            if mdf is not None and len(mdf) >= 2:
                latest = mdf.iloc[0]
                prev = mdf.iloc[1]
                rzye_now = _to_float(latest.get("rzye"), 0) or 0
                rzye_prev = _to_float(prev.get("rzye"), 0) or 0
                rzmre = _to_float(latest.get("rzmre"), 0) or 0
                rzche = _to_float(latest.get("rzche"), 0) or 0
                rqye = _to_float(latest.get("rqye"), 0) or 0
                result["margin"] = {
                    "rzye": round(rzye_now / 1e4, 1),  # 元→万元
                    "rzye_change": round((rzye_now - rzye_prev) / rzye_prev * 100, 1) if rzye_prev > 0 else None,
                    "net_buy": round((rzmre - rzche) / 1e4, 1),  # 净买入万元
                    "rqye": round(rqye / 1e4, 1),
                    "date": str(latest.get("trade_date", "")),
                }
        except Exception:
            pass

        # 5. 财务指标（fina_indicator — 最新一期 ROE/ROA/毛利率/净利率）
        try:
            fdf = pro.fina_indicator(ts_code=ts_code)
            if fdf is not None and not fdf.empty:
                r = fdf.iloc[0]
                result["financial"] = {
                    "roe": _to_float(r.get("roe"), None),
                    "roa": _to_float(r.get("roa"), None),
                    "gross_margin": _to_float(r.get("grossprofit_margin"), None),
                    "net_margin": _to_float(r.get("netprofit_margin"), None),
                    "eps": _to_float(r.get("eps"), None),
                    "end_date": str(r.get("end_date", "")),
                }
        except Exception:
            pass

        return result

    # ================================================================
    # 14. 数据状态汇总
    # ================================================================

    def get_data_status(self, code: str, market: str = "A股") -> Dict[str, Any]:
        """一站式检查某只股票所有数据维度的可用性和新鲜度。

        Returns:
            {dimension: {"available": bool, "data_date": str, "stale": bool, "source": str}}
        """
        status = {}

        # 价格
        price = self.get_price(code, market)
        status["price"] = {"available": price["available"], "source": price.get("source"),
                           "data_date": price.get("data_date"), "stale": price.get("stale", False)}

        # 快照
        snap = self.get_quote_snapshot(code, market)
        status["quote"] = {"available": snap["available"], "source": snap.get("source"),
                           "data_date": snap.get("data_date"), "stale": snap.get("stale", False)}

        # K线
        bars = self.get_bars(code, market, days=5)
        status["bars"] = {"available": len(bars.get("bars", [])) > 0, "source": bars.get("source"),
                          "data_date": bars.get("data_date"), "stale": bars.get("stale", False),
                          "count": len(bars.get("bars", []))}

        # 财务
        fin = self.get_financial(code)
        status["financial"] = {"available": fin["available"], "data_date": fin.get("data_date"),
                               "stale": fin.get("stale", False)}

        # 基础信息
        info = self.get_stock_info(code)
        status["stock_info"] = {"available": info["available"], "data_date": info.get("data_date"),
                                "stale": info.get("stale", False)}

        # 新闻
        news = self.get_news(code, limit=1)
        status["news"] = {"available": len(news) > 0, "count": len(news)}

        # 资金流向
        mf = self.get_moneyflow(code)
        status["moneyflow"] = {"available": mf["available"], "source": mf.get("source"),
                               "data_date": (mf.get("data") or {}).get("trade_date") if mf.get("data") else None}

        # 盘口
        ob = self.get_orderbook(code, market)
        status["orderbook"] = {"available": ob["available"], "source": ob.get("source")}

        # 汇总
        dims = [k for k, v in status.items() if v.get("available")]
        stale_dims = [k for k, v in status.items() if v.get("stale")]
        status["_summary"] = {
            "total_dimensions": len(status),
            "available": len(dims),
            "stale": len(stale_dims),
            "stale_dimensions": stale_dims,
        }
        return status

    # ================================================================
    # 工具
    # ================================================================

    def _unavailable(self, reason: str) -> Dict[str, Any]:
        return {
            "available": False, "reason": reason,
            "supporting_factors": [], "risk_factors": [], "missing_data": [reason],
        }
