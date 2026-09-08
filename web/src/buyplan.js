// buyplan.js —— 明日选股分层漏斗筛选（纯函数，无 JSX）
// 从 /api/buyplan 返回的 universe（全市场基础信息）+ pool（有因子 ≤200）出发，
// 按「初筛 → 趋势 → 策略 → 入场 → 排名」顺序过滤 + 排序，供 BuyPlanPage 消费。

// 各字段滑杆范围（fullMin/fullMax 也用于「空值只在全放开时放行」的判断）
export const BOUNDS = {
  mvYi: [50, 2000],            // 市值(亿)
  amountWan: [5000, 500000],   // 成交额(万)
  pe: [0, 200],
  price: [0, 200],             // 现价(元)
  ret5: [-40, 40],             // 5 日收益 %
  ret20: [-80, 100],
  ret60: [-150, 300],
  dist: [-60, 120],            // 距 MA20 %
  rsi: [0, 100],
  score: [0, 1],
  weight: [0, 1],
}

export const TREND_STATUSES = ['趋势较强', '震荡分歧', '修复中', '趋势偏弱', '趋势中性']
export const VOL_SIGNALS = ['放量上涨', '缩量下跌', '放量滞涨', '缩量止跌', '量价正常']
export const STRATEGY_TAGS = ['动量突破', '周期共振', '低位拐点']
export const MARKETS = ['主板', '创业板', '科创板', '北交所']

export const DEFAULT_FILTERS = {
  // L0 初筛（可配置，替换原服务端硬编码：市值50亿/成交额5000万/PE0-200/板块3板）
  markets: [...MARKETS.slice(0, 3)], // 默认主板/创业板/科创板（与 v3 生效口径一致）
  excludeSt: true,
  priceMin: 0,
  priceMax: 200,
  minMvYi: 50,
  minAmountWan: 5000,
  peMin: 0,
  peMax: 200,
  // L2 趋势
  trendStatus: [...TREND_STATUSES],
  ret5Min: -40, ret5Max: 40,
  ret20Min: -80, ret20Max: 100,
  ret60Min: -150, ret60Max: 300,
  aboveMa20: false,
  // L3 策略
  minMomentum: 0,
  minCycle: 0.2,
  minTurnaround: 0,
  minComposite: 0,
  strategyTags: [...STRATEGY_TAGS],
  // L4 入场
  rsiMin: 0, rsiMax: 100,
  distMin: -60, distMax: 120,
  volSignals: [...VOL_SIGNALS],
  // L5 排名（w* 默认由页面按 market.alpha_weights 填充，此处 null）
  wMomentum: null, wCycle: null, wTurnaround: null,
  sortBy: 'alpha',
  topN: 15,
  downgradeHeld: true,
}

const clone = (o) => JSON.parse(JSON.stringify(o))

export function loadFilters() {
  try {
    const raw = localStorage.getItem('buyplan_filters')
    if (!raw) return clone(DEFAULT_FILTERS)
    return { ...clone(DEFAULT_FILTERS), ...JSON.parse(raw) }
  } catch {
    return clone(DEFAULT_FILTERS)
  }
}

export function saveFilters(f) {
  try { localStorage.setItem('buyplan_filters', JSON.stringify(f)) } catch { /* 忽略 */ }
}

// 「默认策略」= 系统原策略（v3 实际生效值：初筛 50亿/5000万/PE0-200、cycle≥0.20、
// 排名用 market.alpha_weights 权重 + top15 + 已持仓降权，趋势/入场/动量/拐点不筛）。
// 用户自定义预设存后端 MongoDB（按 user_id 隔离），见 api.js 的 buyplan preset 接口。
export const BUILTIN_PRESET_NAME = '默认策略'

// 数值归一：null/undefined/''/NaN → null
const num = (v) => {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isNaN(n) ? null : n
}

// 区间过滤：空值只在「滑杆处于全放开」时放行，收紧后空值视为不满足
const inRange = (v, min, max, fullMin, fullMax) => {
  if (v === null) return min <= fullMin && max >= fullMax
  return v >= min && v <= max
}

export function distToMA20(item) {
  const close = num(item.close)
  const ma20 = num(item.trend?.ma?.ma20)
  if (close === null || !ma20 || ma20 <= 0) return null
  return (close - ma20) / ma20 * 100
}

export function applyFilters(universe, pool, f) {
  const stages = []
  const uni = universe || []

  // L0 全市场（如实展示漏斗头部，含北交所/ST/停牌等全部标的）
  stages.push({ key: 'universe', label: '全市场', count: uni.length, fixed: true })

  // L0 初筛漏斗：板块 → 成交额 → 市值 → PE → 剔除ST → 价格（每级可配置）
  let s = uni.filter((u) => !f.markets || !f.markets.length || f.markets.includes(u.market || ''))
  stages.push({ key: 'board', label: '板块', count: s.length })

  s = s.filter((u) => inRange(num(u.latest_amount), f.minAmountWan, BOUNDS.amountWan[1], ...BOUNDS.amountWan))
  stages.push({ key: 'amount', label: '成交额', count: s.length })

  s = s.filter((u) => inRange(num(u.total_mv) / 1e8, f.minMvYi, BOUNDS.mvYi[1], ...BOUNDS.mvYi))
  stages.push({ key: 'mv', label: '市值', count: s.length })

  s = s.filter((u) => inRange(num(u.pe), f.peMin, f.peMax, ...BOUNDS.pe))
  stages.push({ key: 'pe', label: 'PE', count: s.length })

  if (f.excludeSt) s = s.filter((u) => !/ST/.test(String(u.name || '').toUpperCase()))
  s = s.filter((u) => inRange(num(u.close), f.priceMin, f.priceMax, ...BOUNDS.price))
  stages.push({ key: 'screen', label: 'ST/价格', count: s.length })

  // 有因子池 = 后端 enrich 后 ≤200 只 ∩ 初筛结果
  const screenedCodes = new Set(s.map((u) => u.code))
  let surv = (pool || []).filter((it) => screenedCodes.has(it.code))
  stages.push({ key: 'enriched', label: '有因子', count: surv.length, fixed: true })

  // L2 趋势
  surv = surv.filter((it) => {
    const t = it.trend || {}
    const rets = t.returns || {}
    const status = t.status
    if (status && f.trendStatus.length && !f.trendStatus.includes(status)) return false
    if (!inRange(num(rets.return_5d), f.ret5Min, f.ret5Max, ...BOUNDS.ret5)) return false
    if (!inRange(num(rets.return_20d), f.ret20Min, f.ret20Max, ...BOUNDS.ret20)) return false
    if (!inRange(num(rets.return_60d), f.ret60Min, f.ret60Max, ...BOUNDS.ret60)) return false
    if (f.aboveMa20) {
      const close = num(it.close)
      const ma20 = num(t.ma?.ma20)
      if (close === null || !ma20 || ma20 <= 0 || close <= ma20) return false
    }
    return true
  })
  stages.push({ key: 'trend', label: '趋势', count: surv.length })

  // L3 策略（策略标签：命中所选任一标签即留）
  surv = surv.filter((it) => {
    const fs = it.factor?.factor_scores || {}
    if (!inRange(num(fs.momentum), f.minMomentum, BOUNDS.score[1], ...BOUNDS.score)) return false
    if (!inRange(num(it.cycle_score), f.minCycle, BOUNDS.score[1], ...BOUNDS.score)) return false
    if (!inRange(num(it.turnaround_score), f.minTurnaround, BOUNDS.score[1], ...BOUNDS.score)) return false
    if (!inRange(num(it.factor?.composite_score), f.minComposite, BOUNDS.score[1], ...BOUNDS.score)) return false
    if (f.strategyTags.length) {
      const tags = it.strategy_tags || []
      if (!f.strategyTags.some((tg) => tags.includes(tg))) return false
    }
    return true
  })
  stages.push({ key: 'strategy', label: '策略', count: surv.length })

  // L4 入场
  surv = surv.filter((it) => {
    const ti = it.trend?.technical_indicators || {}
    if (!inRange(num(ti.rsi14), f.rsiMin, f.rsiMax, ...BOUNDS.rsi)) return false
    if (!inRange(distToMA20(it), f.distMin, f.distMax, ...BOUNDS.dist)) return false
    if (f.volSignals.length) {
      const sig = ti.volume_price_signal
      if (sig && !f.volSignals.includes(sig)) return false
    }
    return true
  })
  stages.push({ key: 'entry', label: '入场', count: surv.length })

  // L5 排名
  const wM = f.wMomentum ?? 0.45
  const wC = f.wCycle ?? 0.35
  const wT = f.wTurnaround ?? 0.20
  const wsum = (wM + wC + wT) || 1
  const scoreOf = (it) => {
    const fs = it.factor?.factor_scores || {}
    switch (f.sortBy) {
      case 'momentum': return num(fs.momentum) ?? 0
      case 'cycle': return num(it.cycle_score) ?? 0
      case 'turnaround': return num(it.turnaround_score) ?? 0
      case 'composite': return num(it.factor?.composite_score) ?? 0
      default:
        return ((num(fs.momentum) ?? 0) * wM + (num(it.cycle_score) ?? 0) * wC + (num(it.turnaround_score) ?? 0) * wT) / wsum
    }
  }

  const ranked = surv.map((it) => ({ ...it, _score: scoreOf(it) }))
  ranked.sort((a, b) => b._score - a._score)

  let finalList = ranked
  if (f.downgradeHeld) {
    finalList = ranked.map((it) => (it.in_portfolio ? { ...it, _score: it._score * 0.5 } : it))
    finalList.sort((a, b) => b._score - a._score)
  }
  finalList = finalList.slice(0, f.topN)

  stages.push({ key: 'rank', label: '入选', count: finalList.length })

  return { stages, survivors: finalList }
}
