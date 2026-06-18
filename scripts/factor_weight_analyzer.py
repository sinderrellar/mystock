#!/usr/bin/env python3
"""
因子权重 IC 分析器 — 用历史信号 + 未来收益计算因子有效性。

用法:
    # 分析过去 N 天因子 IC，输出建议权重
    python3 factor_weight_analyzer.py --forward 20

    # 对比建议权重 vs 当前 config 权重
    python3 factor_weight_analyzer.py --compare

原理:
    IC (Information Coefficient) = 因子分与未来收益的 Spearman 秩相关系数。
    IC > 0.05 → 因子有效；IC ≈ 0 → 无预测力；IC < 0 → 因子反向。
    权重 = IC_i / sum(IC)，IC 为负的因子权重归零。
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from factor_data_import_service import MongoFactorDataStore, _load_mongodb_config

FACTOR_NAMES = ["value", "growth", "quality", "momentum"]


def _rank_ic(x: List[float], y: List[float]) -> Optional[float]:
    """Spearman rank correlation between x and y, returns None if variance==0."""
    n = len(x)
    if n < 8:
        return None

    # Rank x
    x_ranked = sorted(range(n), key=lambda i: x[i])
    x_ranks = [0] * n
    for rank, idx in enumerate(x_ranked):
        x_ranks[idx] = rank + 1

    # Rank y
    y_ranked = sorted(range(n), key=lambda i: y[i])
    y_ranks = [0] * n
    for rank, idx in enumerate(y_ranked):
        y_ranks[idx] = rank + 1

    # Pearson on ranks
    mean_xr = sum(x_ranks) / n
    mean_yr = sum(y_ranks) / n
    cov = sum((x_ranks[i] - mean_xr) * (y_ranks[i] - mean_yr) for i in range(n))
    std_x = (sum((r - mean_xr) ** 2 for r in x_ranks) ** 0.5)
    std_y = (sum((r - mean_yr) ** 2 for r in y_ranks) ** 0.5)
    if std_x == 0 or std_y == 0:
        return None
    return cov / (std_x * std_y)


def compute_factor_ic(
    store: MongoFactorDataStore,
    start_date: str = "2026-01-01",
    end_date: Optional[str] = None,
    forward_days: int = 20,
) -> Dict[str, Any]:
    """计算每个因子在各历史日期的 IC。

    Returns:
        {"by_date": {date: {factor: ic}}, "summary": {factor: {mean_ic, win_rate, dates}}}
    """
    if end_date is None:
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    dates = sorted(store.db["stock_signals_history"].distinct("computed_at"))
    dates = [d for d in dates if start_date <= d <= end_date]
    if len(dates) < 3:
        return {"error": f"历史信号日期不足: {len(dates)} 天", "dates": dates}

    print(f"计算因子 IC: {dates[0]} ~ {dates[-1]}, {len(dates)} 天, forward={forward_days}天")

    by_date: Dict[str, Dict[str, Optional[float]]] = {}
    ic_history: Dict[str, List[float]] = defaultdict(list)

    for date in dates:
        # 取当天所有信号
        signals = list(store.db["stock_signals_history"].find(
            {"computed_at": date},
            {"code": 1, "factor": 1},
        ))
        if len(signals) < 50:
            continue

        # 构建 code → factor_scores 映射
        factor_by_code: Dict[str, Dict[str, float]] = {}
        for sig in signals:
            fs = (sig.get("factor") or {}).get("factor_scores", {})
            if all(fs.get(f) is not None for f in FACTOR_NAMES):
                factor_by_code[sig["code"]] = {f: fs[f] for f in FACTOR_NAMES}

        codes = list(factor_by_code.keys())
        if len(codes) < 50:
            continue

        # 取 forward_days 后的收盘价
        # 找到 forward_days 个交易日后的日期
        future_quotes = list(store.db[store.collections["daily_quotes"]].find(
            {"code": {"$in": codes}, "trade_date": {"$gt": date}, "period": "daily"},
            {"code": 1, "trade_date": 1, "close": 1},
        ).sort("trade_date", 1))

        # 按 code 分组，取第 forward_days 个交易日
        future_price: Dict[str, float] = {}
        code_day_count: Dict[str, int] = defaultdict(int)
        for q in future_quotes:
            c = q["code"]
            if code_day_count[c] >= forward_days:
                continue
            code_day_count[c] += 1
            if code_day_count[c] == forward_days:
                close = q.get("close", 0)
                if close and close > 0:
                    future_price[c] = float(close)

        # 取当天收盘价
        current_price: Dict[str, float] = {}
        for q in store.db[store.collections["daily_quotes"]].find(
            {"code": {"$in": codes}, "trade_date": date, "period": "daily"},
            {"code": 1, "close": 1},
        ):
            close = q.get("close", 0)
            if close and close > 0:
                current_price[q["code"]] = float(close)

        # 计算前行收益
        returns: Dict[str, float] = {}
        for code in codes:
            cp = current_price.get(code)
            fp = future_price.get(code)
            if cp and fp and cp > 0:
                returns[code] = (fp - cp) / cp

        if len(returns) < 50:
            continue

        # 对各因子算 IC
        day_ic: Dict[str, Optional[float]] = {}
        for factor in FACTOR_NAMES:
            xs, ys = [], []
            for code, ret in returns.items():
                fs = factor_by_code.get(code, {})
                score = fs.get(factor)
                if score is not None:
                    xs.append(score)
                    ys.append(ret)
            ic = _rank_ic(xs, ys)
            day_ic[factor] = ic
            if ic is not None:
                ic_history[factor].append(ic)

        by_date[date] = day_ic

    # 汇总
    summary = {}
    for factor in FACTOR_NAMES:
        ics = ic_history.get(factor, [])
        if ics:
            mean_ic = sum(ics) / len(ics)
            win_rate = sum(1 for ic in ics if ic > 0) / len(ics)
            summary[factor] = {
                "mean_ic": round(mean_ic, 4),
                "win_rate": round(win_rate, 4),
                "n_dates": len(ics),
                "ic_list": [round(ic, 4) for ic in ics],
            }
        else:
            summary[factor] = {"mean_ic": None, "win_rate": None, "n_dates": 0}

    return {"by_date": by_date, "summary": summary, "n_dates": len(dates)}


def suggest_weights(
    store: MongoFactorDataStore,
    start_date: str = "2026-01-01",
    end_date: Optional[str] = None,
    forward_days: int = 20,
    min_ic: float = 0.02,
) -> Dict[str, Any]:
    """基于 IC 建议因子权重。IC < min_ic 的因子权重归零。"""
    result = compute_factor_ic(store, start_date, end_date, forward_days)
    if "error" in result:
        return result

    summary = result["summary"]
    ics = {}
    for f in FACTOR_NAMES:
        mean_ic = summary[f].get("mean_ic") or 0
        ics[f] = max(mean_ic, 0)  # 负 IC 归零

    total_ic = sum(ics.values())
    if total_ic == 0:
        weights = {f: 0.25 for f in FACTOR_NAMES}  # 全无效时等权
    else:
        weights = {f: round(ics[f] / total_ic, 4) for f in FACTOR_NAMES}
        # 过滤低于 min_ic 的因子
        weights = {f: (round(ics[f] / total_ic, 4) if ics[f] >= min_ic else 0)
                   for f in FACTOR_NAMES}
        # 重新归一化
        total = sum(weights.values())
        if total > 0:
            weights = {f: round(w / total, 4) for f, w in weights.items()}
        else:
            weights = {f: 0.25 for f in FACTOR_NAMES}

    result["suggested_weights"] = weights
    result["forward_days"] = forward_days
    return result


def compare_with_config(
    store: MongoFactorDataStore,
    config_path: str,
    start_date: str = "2026-01-01",
    end_date: Optional[str] = None,
    forward_days: int = 20,
) -> None:
    """对比 IC 建议权重 vs config 中的 style_weights。"""
    suggestion = suggest_weights(store, start_date, end_date, forward_days)
    if "error" in suggestion:
        print(f"⚠️ {suggestion['error']}")
        return

    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    style_weights = cfg["pyramid_middle_layer"].get("style_weights", {})

    sw = suggestion["suggested_weights"]
    summary = suggestion["summary"]

    print(f"\n{'='*70}")
    print(f"因子 IC 分析 ({suggestion.get('n_dates', 0)} 天, forward={forward_days}天)")
    print(f"{'='*70}")
    print(f"{'因子':<12} {'平均IC':>8} {'胜率':>8} {'IC建议':>8} {'default':>8}")
    print(f"{'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for f in FACTOR_NAMES:
        s = summary.get(f, {})
        mic = s.get("mean_ic")
        wr = s.get("win_rate")
        ic_str = f"{mic:.4f}" if mic is not None else "N/A"
        wr_str = f"{wr:.1%}" if wr is not None else "N/A"
        cfg_default = style_weights.get("default", {}).get(f, 0)
        print(f"{f:<12} {ic_str:>8} {wr_str:>8} {sw.get(f, 0):>8.4f} {cfg_default:>8.2f}")

    # 每个 style 对比
    for style_name, cfg_w in style_weights.items():
        if style_name == "default":
            continue
        print(f"\n--- {style_name} ---")
        print(f"{'因子':<12} {'config':>8} {'IC建议':>8} {'差值':>8}")
        for f in FACTOR_NAMES:
            cw = cfg_w.get(f, 0)
            iw = sw.get(f, 0)
            diff = iw - cw
            print(f"{f:<12} {cw:>8.2f} {iw:>8.4f} {diff:>+8.4f}")

    print(f"\n提示: IC 基于全市场（不按风格分群），风格权重对比仅供参考。")


def main():
    parser = argparse.ArgumentParser(description="因子权重 IC 分析器")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--forward", type=int, default=20, help="前看交易日数")
    parser.add_argument("--compare", action="store_true", help="对比 config 权重")
    args = parser.parse_args()

    config_path = os.path.join(PROJECT_ROOT, "config", "config_complete.yaml")
    mongodb_config = _load_mongodb_config(config_path)
    store = MongoFactorDataStore(mongodb_config)

    if args.compare:
        compare_with_config(store, config_path, args.start, args.end, args.forward)
    else:
        result = suggest_weights(store, args.start, args.end, args.forward)
        if "error" in result:
            print(f"⚠️ {result['error']}")
            return
        print(f"\n建议权重 (forward={args.forward}天):")
        for f in FACTOR_NAMES:
            print(f"  {f}: {result['suggested_weights'].get(f, 0):.4f}")


if __name__ == "__main__":
    main()
