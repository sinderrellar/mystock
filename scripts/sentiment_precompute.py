#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sentiment_precompute.py —— 后台预计算持仓情绪信号（crontab 每交易日盘中跑）。

职责：
1. 枚举所有用户持仓文件 data/portfolio_<user>.yaml（跳过 example/.org 备份），按用户分组。
2. 对每只持仓调 StrategySignalCollector._event_signal()：拉新闻 + wecode 算情绪，
   写入 event_signal_cache（半小时分桶 key）。
3. 作为组合全景 review 的缓存预热器：cron 每半小时预热，review 通常秒回；
   缓存 miss 时 review 仍兜底实时算（wecode），保证不因 cron 缺失而「不可用」。

设计口径：
- 情绪是股票属性（新闻全局一致），缓存按「代码:市场:半小时桶」全局存，
  同一只股票被多用户持有只算一次；但枚举时按用户文件展开，确保覆盖所有用户持仓。
- 「解析到哪个算哪个」：单文件解析失败 / 单只计算失败只 skip 并打日志，不中断整批。
- ETF 在 _event_signal 内已跳过（不做个股新闻事件分析），此处自然落入 skip 计数。
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from strategy_signals import StrategySignalCollector  # noqa: E402


def _portfolio_files() -> List[str]:
    """data/portfolio_<user>.yaml（glob 天然排除 portfolio.yaml 默认空仓与 .example.yaml/.org）。"""
    return sorted(glob.glob(os.path.join(PROJECT_ROOT, "data", "portfolio_*.yaml")))


def _positions_from_file(path: str) -> List[Dict[str, Any]]:
    import yaml

    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    positions = cfg.get("positions") or []
    out: List[Dict[str, Any]] = []
    for p in positions:
        code = str(p.get("code", "") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": p.get("name", code),
            "market": p.get("market", "A股"),
            "asset_type": p.get("asset_type", "stock"),
        })
    return out


def main() -> None:
    collector = StrategySignalCollector()
    files = _portfolio_files()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] 情绪预计算开始，持仓文件 {len(files)} 个")

    seen: set = set()  # (code, market) 去重：同一股票多用户持有只算一次
    ok = 0
    skip = 0
    for path in files:
        user = os.path.basename(path).replace("portfolio_", "").replace(".yaml", "")
        try:
            positions = _positions_from_file(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] 解析失败 {path}: {exc}")
            continue
        if not positions:
            print(f"[user={user}] 0 只持仓")
            continue
        print(f"[user={user}] {len(positions)} 只持仓")
        for pos in positions:
            key = (pos["code"], pos["market"])
            if key in seen:
                print(f"  [dedup] {pos['code']}({pos['market']}) 已被其他用户计算，跳过")
                continue
            seen.add(key)
            try:
                res = collector._event_signal(pos)
            except Exception as exc:  # noqa: BLE001
                skip += 1
                print(f"  [skip] {pos['code']}({pos['market']}) 异常: {exc}")
                continue
            if res.get("available"):
                ok += 1
                score = res.get("sentiment_score")
                print(f"  [ok] {pos['code']}({pos['market']}) 情绪 {score}")
            else:
                skip += 1
                print(f"  [skip] {pos['code']}({pos['market']}) 不可用: {res.get('reason', '')[:80]}")

    print(f"[done] 情绪预计算完成：成功 {ok}，跳过/失败 {skip}，去重后标的 {len(seen)}")


if __name__ == "__main__":
    main()
