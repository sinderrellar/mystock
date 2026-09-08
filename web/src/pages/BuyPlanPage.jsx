import React, { useEffect, useMemo, useState } from 'react'
import { getBuyPlan, getWatchlist, addWatchlist, getBuyPlanPresets, saveBuyPlanPreset, removeBuyPlanPreset } from '../api.js'
import { fmt, money } from '../format.js'
import {
  DEFAULT_FILTERS, loadFilters, saveFilters, applyFilters,
  TREND_STATUSES, VOL_SIGNALS, STRATEGY_TAGS, BOUNDS, MARKETS,
  BUILTIN_PRESET_NAME,
} from '../buyplan.js'

const copyArr = (a) => (Array.isArray(a) ? [...a] : a)

const LAYER_KEYS = {
  screen: ['markets', 'minAmountWan', 'minMvYi', 'peMin', 'peMax', 'excludeSt', 'priceMin', 'priceMax'],
  trend: ['trendStatus', 'ret5Min', 'ret5Max', 'ret20Min', 'ret20Max', 'ret60Min', 'ret60Max', 'aboveMa20'],
  strategy: ['minMomentum', 'minCycle', 'minTurnaround', 'minComposite', 'strategyTags'],
  entry: ['rsiMin', 'rsiMax', 'distMin', 'distMax', 'volSignals'],
  rank: ['wMomentum', 'wCycle', 'wTurnaround', 'sortBy', 'topN', 'downgradeHeld'],
}

const SORT_OPTIONS = [
  { v: 'alpha', label: 'α 评分' },
  { v: 'momentum', label: '动量' },
  { v: 'cycle', label: '周期' },
  { v: 'turnaround', label: '拐点' },
  { v: 'composite', label: '综合因子' },
]

const tagColor = (tg) => (/低位|拐点|反转/.test(tg) ? 'orange' : /突破|共振/.test(tg) ? 'red' : 'blue')

/* 「＋观察池」行内小按钮 */
function WatchButton({ code, inPool, onAdded }) {
  const [state, setState] = useState(inPool ? 'done' : 'idle')
  useEffect(() => {
    setState(inPool ? 'done' : 'idle')
  }, [inPool])
  if (state === 'done') return <button className="btn sm ghost" disabled style={{ minWidth: 70 }}>✓ 已在池</button>
  return (
    <button
      className="btn sm ghost"
      disabled={state === 'busy'}
      onClick={async () => {
        setState('busy')
        try {
          await addWatchlist(code)
          setState('ok')
          setTimeout(() => onAdded(code), 900)
        } catch {
          setState('idle')
        }
      }}
      style={{ minWidth: 70 }}
    >
      {state === 'busy' ? '…' : state === 'ok' ? '✓ 已加' : '＋ 观察池'}
    </button>
  )
}

/* 横向漏斗 */
function Funnel({ head, stages }) {
  const nodes = [...head, ...stages]
  const max = Math.max(1, ...nodes.map((s) => s.count))
  return (
    <div className="funnel">
      {nodes.map((s, i) => {
        const prev = i > 0 ? nodes[i - 1].count : s.count
        const drop = prev - s.count
        return (
          <div className={`frow ${s.fixed ? 'fixed' : ''}`} key={s.key}>
            <span className="flabel">{s.label}</span>
            <div className="fbar">
              <div className="ffill" style={{ width: `${Math.round((s.count / max) * 100)}%` }} />
            </div>
            <span className="fcount">{s.count}</span>
            <span className="fdrop">{i > 0 && drop > 0 ? `↓ ${drop}` : ''}</span>
          </div>
        )
      })}
    </div>
  )
}

/* 滑杆控件 */
function Range({ label, value, min, max, step = 1, unit = '', onChange }) {
  return (
    <label className="fctl">
      <span className="fctl-name">{label}</span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      <span className="fctl-val">{value}{unit}</span>
    </label>
  )
}

/* 多选 chips */
function ChipGroup({ label, options, selected, onToggle }) {
  return (
    <div className="fctl">
      <span className="fctl-name">{label}</span>
      <div className="chip-group">
        {options.map((o) => (
          <button key={o} type="button" className={`chip ${selected.includes(o) ? 'on' : ''}`} onClick={() => onToggle(o)}>{o}</button>
        ))}
      </div>
    </div>
  )
}

/* 开关 */
function Toggle({ label, checked, onChange }) {
  return (
    <div className="fctl fctl-toggle">
      <span className="fctl-name">{label}</span>
      <button type="button" className={`tgl ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}>{checked ? '开' : '关'}</button>
    </div>
  )
}

/* 六维因子雷达 */
function Radar({ item }) {
  const v01 = (x) => {
    const n = Number(x)
    return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0
  }
  const fs = item?.factor?.factor_scores || {}
  const dims = [
    { key: '动量', v: v01(fs.momentum) },
    { key: '周期', v: v01(item?.cycle_score) },
    { key: '拐点', v: v01(item?.turnaround_score) },
    { key: '价值', v: v01(fs.value) },
    { key: '成长', v: v01(fs.growth) },
    { key: '质量', v: v01(fs.quality) },
  ]
  const cx = 130, cy = 130, R = 88, N = dims.length
  const pt = (i, r) => {
    const ang = (Math.PI * 2 * i) / N - Math.PI / 2
    return [cx + Math.cos(ang) * r, cy + Math.sin(ang) * r]
  }
  const ring = (frac) => dims.map((_, i) => pt(i, R * frac).map((n) => n.toFixed(1)).join(',')).join(' ')
  const dataPts = dims.map((d, i) => pt(i, R * d.v))
  const dataPoly = dataPts.map((p) => p.map((n) => n.toFixed(1)).join(',')).join(' ')

  return (
    <svg className="radar-svg" viewBox="0 0 260 260" role="img" aria-label="六维因子雷达">
      {[0.25, 0.5, 0.75, 1].map((fr) => (
        <polygon key={fr} points={ring(fr)} className="radar-ring" />
      ))}
      {dims.map((_, i) => {
        const [x, y] = pt(i, R)
        return <line key={i} x1={cx} y1={cy} x2={x} y2={y} className="radar-spoke" />
      })}
      <polygon points={dataPoly} className="radar-data" />
      {dataPts.map((p, i) => <circle key={i} cx={p[0]} cy={p[1]} r="3" className="radar-dot" />)}
      {dims.map((d, i) => {
        const [x, y] = pt(i, R + 20)
        return (
          <text key={d.key} x={x} y={y} className="radar-label" textAnchor="middle" dominantBaseline="middle">
            {d.key} {d.v.toFixed(2)}
          </text>
        )
      })}
    </svg>
  )
}

export default function BuyPlanPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filters, setFilters] = useState(() => loadFilters())
  const [selectedCode, setSelectedCode] = useState(null)
  const [wlCodes, setWlCodes] = useState(new Set())
  const [presets, setPresets] = useState([])
  const [presetBusy, setPresetBusy] = useState(false)
  const [presetMsg, setPresetMsg] = useState('')
  const [activePreset, setActivePreset] = useState(BUILTIN_PRESET_NAME)

  useEffect(() => {
    getBuyPlan(15)
      .then((d) => {
        setData(d)
        setFilters((prev) => {
          if (prev.wMomentum != null) return prev
          const w = d.market?.alpha_weights || {}
          return { ...prev, wMomentum: w.momentum ?? 0.45, wCycle: w.cycle ?? 0.35, wTurnaround: w.turnaround ?? 0.20 }
        })
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
    getWatchlist().then((r) => setWlCodes(new Set((r.items || []).map((w) => w.code)))).catch(() => {})
    getBuyPlanPresets().then((r) => setPresets(r.items || [])).catch(() => {})
  }, [])

  useEffect(() => { saveFilters(filters) }, [filters])

  const universe = data?.universe || []
  const pool = data?.pool || data?.recommendations || []
  const { stages, survivors } = useMemo(() => applyFilters(universe, pool, filters), [universe, pool, filters])

  if (loading && !data) return <div className="loading-block"><div className="spinner" style={{ margin: '0 auto 12px' }} />正在跑全市场选股漏斗……</div>
  if (error && !data) return (<><div className="error">{error}</div><button className="btn sm ghost" onClick={() => window.location.reload()}>重试</button></>)
  if (!data) return null

  const marketW = data.market?.alpha_weights || {}
  const baseWeights = { wMomentum: marketW.momentum ?? 0.45, wCycle: marketW.cycle ?? 0.35, wTurnaround: marketW.turnaround ?? 0.20 }
  // 系统默认策略（v3 实际生效值 + 当前市场 regime 的 alpha 权重），数组字段每次深拷贝避免共享引用
  const defaultFilters = () => ({
    ...DEFAULT_FILTERS,
    ...baseWeights,
    markets: [...DEFAULT_FILTERS.markets],
    trendStatus: [...DEFAULT_FILTERS.trendStatus],
    strategyTags: [...DEFAULT_FILTERS.strategyTags],
    volSignals: [...DEFAULT_FILTERS.volSignals],
  })
  const up = (patch) => { setFilters((p) => ({ ...p, ...patch })); setActivePreset('') }
  const toggleChip = (key, value) => {
    const cur = filters[key]
    up({ [key]: cur.includes(value) ? cur.filter((x) => x !== value) : [...cur, value] })
  }
  const resetLayer = (key) => {
    const patch = {}
    LAYER_KEYS[key].forEach((k) => { patch[k] = copyArr(DEFAULT_FILTERS[k]) })
    if (key === 'rank') Object.assign(patch, baseWeights)
    up(patch)
  }
  const resetAll = () => { setFilters(defaultFilters()); setActivePreset(BUILTIN_PRESET_NAME) }

  // ── 策略预设：加载 / 保存 / 删除（后端按用户存） ──
  const onPresetSelect = (name) => {
    if (!name) return
    if (name === BUILTIN_PRESET_NAME) {
      setFilters(defaultFilters())
      setActivePreset(BUILTIN_PRESET_NAME)
    } else {
      const p = presets.find((x) => x.name === name)
      if (p) {
        const loaded = { ...defaultFilters(), ...p.filters }
        // 旧预设若未存权重，回落到当前市场 regime 权重，避免滑杆显示 0
        if (loaded.wMomentum == null) loaded.wMomentum = baseWeights.wMomentum
        if (loaded.wCycle == null) loaded.wCycle = baseWeights.wCycle
        if (loaded.wTurnaround == null) loaded.wTurnaround = baseWeights.wTurnaround
        setFilters(loaded)
        setActivePreset(name)
      }
    }
  }
  const onSavePreset = async () => {
    const name = (typeof window !== 'undefined' && window.prompt) ? window.prompt('预设名称（同名覆盖）：', '') : null
    if (!name || !name.trim()) return
    const trimmed = name.trim()
    setPresetBusy(true)
    setPresetMsg('')
    try {
      await saveBuyPlanPreset(trimmed, filters)
      const r = await getBuyPlanPresets()
      setPresets(r.items || [])
      setActivePreset(trimmed)
      setPresetMsg(`已保存「${trimmed}」`)
    } catch (e) {
      setPresetMsg(`保存失败：${e.message}`)
    } finally {
      setPresetBusy(false)
    }
  }
  const onDeletePreset = async () => {
    if (!activePreset || activePreset === BUILTIN_PRESET_NAME) return
    const name = activePreset
    setPresetBusy(true)
    setPresetMsg('')
    try {
      await removeBuyPlanPreset(name)
      const r = await getBuyPlanPresets()
      setPresets(r.items || [])
      setFilters(defaultFilters())
      setActivePreset(BUILTIN_PRESET_NAME)
      setPresetMsg(`已删除「${name}」`)
    } catch (e) {
      setPresetMsg(`删除失败：${e.message}`)
    } finally {
      setPresetBusy(false)
    }
  }

  const head = []
  const sel = pool.find((x) => x.code === selectedCode)
  const regime = data.market?.regime || ''

  return (
    <>
      <div className="page-head">
        <div>
          <h2>明日选股</h2>
          <div className="desc">全市场 → 初筛 → 有因子 → 趋势 → 策略 → 入场 → 排名，调阈值实时筛选（{regime} 市场）</div>
        </div>
        <button className="btn sm ghost" onClick={resetAll}>重置全部</button>
      </div>

      <div className="preset-bar">
        <span className="fctl-name">策略预设</span>
        <select value={activePreset} onChange={(e) => onPresetSelect(e.target.value)}>
          <option value={BUILTIN_PRESET_NAME}>默认策略（系统）</option>
          {presets.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          {activePreset && activePreset !== BUILTIN_PRESET_NAME && !presets.some((p) => p.name === activePreset)
            ? <option value={activePreset}>{activePreset}</option> : null}
          {activePreset === '' && <option value="" disabled>自定义（未保存）</option>}
        </select>
        <button className="btn sm ghost" disabled={presetBusy} onClick={onSavePreset}>保存当前为预设</button>
        {activePreset && activePreset !== BUILTIN_PRESET_NAME && (
          <button className="btn sm ghost" disabled={presetBusy} onClick={onDeletePreset}>删除此预设</button>
        )}
        {presetMsg && <span className="extra" style={{ marginLeft: 0 }}>{presetMsg}</span>}
        <span className="extra">调任意参数即变为「自定义」；预设按用户存在数据库，换设备可读</span>
      </div>

      <div className="sect">
        <div className="sect-title">分层漏斗</div>
        <Funnel head={head} stages={stages} />
      </div>

      <div className="sect">
        <div className="sect-title">
          筛选面板
          <span className="extra">改动即实时生效，刷新后记住（本地保存）</span>
        </div>

        <div className="layer">
          <div className="layer-head"><b>L0 初筛</b><span className="layer-tip">全市场逐级漏斗，阈值可放宽/收紧</span><button className="btn sm ghost" onClick={() => resetLayer('screen')}>重置</button></div>
          <ChipGroup label="板块" options={MARKETS} selected={filters.markets} onToggle={(o) => toggleChip('markets', o)} />
          <div className="fctl-grid">
            <Range label="成交额下限(万)" value={filters.minAmountWan} min={BOUNDS.amountWan[0]} max={BOUNDS.amountWan[1]} step={500} onChange={(v) => up({ minAmountWan: v })} />
            <Range label="市值下限(亿)" value={filters.minMvYi} min={BOUNDS.mvYi[0]} max={BOUNDS.mvYi[1]} step={5} onChange={(v) => up({ minMvYi: v })} />
            <Range label="PE 下限" value={filters.peMin} min={0} max={200} step={1} onChange={(v) => up({ peMin: v })} />
            <Range label="PE 上限" value={filters.peMax} min={0} max={200} step={1} onChange={(v) => up({ peMax: v })} />
            <Range label="价格下限(元)" value={filters.priceMin} min={BOUNDS.price[0]} max={BOUNDS.price[1]} step={1} onChange={(v) => up({ priceMin: v })} />
            <Range label="价格上限(元)" value={filters.priceMax} min={BOUNDS.price[0]} max={BOUNDS.price[1]} step={1} onChange={(v) => up({ priceMax: v })} />
          </div>
          <Toggle label="剔除 ST" checked={filters.excludeSt} onChange={(v) => up({ excludeSt: v })} />
        </div>

        <div className="layer">
          <div className="layer-head"><b>L2 趋势</b><button className="btn sm ghost" onClick={() => resetLayer('trend')}>重置</button></div>
          <ChipGroup label="趋势状态" options={TREND_STATUSES} selected={filters.trendStatus} onToggle={(o) => toggleChip('trendStatus', o)} />
          <div className="fctl-grid">
            <Range label="5日收益下限%" value={filters.ret5Min} min={BOUNDS.ret5[0]} max={BOUNDS.ret5[1]} step={1} onChange={(v) => up({ ret5Min: v })} />
            <Range label="5日收益上限%" value={filters.ret5Max} min={BOUNDS.ret5[0]} max={BOUNDS.ret5[1]} step={1} onChange={(v) => up({ ret5Max: v })} />
            <Range label="20日收益下限%" value={filters.ret20Min} min={BOUNDS.ret20[0]} max={BOUNDS.ret20[1]} step={1} onChange={(v) => up({ ret20Min: v })} />
            <Range label="20日收益上限%" value={filters.ret20Max} min={BOUNDS.ret20[0]} max={BOUNDS.ret20[1]} step={1} onChange={(v) => up({ ret20Max: v })} />
            <Range label="60日收益下限%" value={filters.ret60Min} min={BOUNDS.ret60[0]} max={BOUNDS.ret60[1]} step={1} onChange={(v) => up({ ret60Min: v })} />
            <Range label="60日收益上限%" value={filters.ret60Max} min={BOUNDS.ret60[0]} max={BOUNDS.ret60[1]} step={1} onChange={(v) => up({ ret60Max: v })} />
          </div>
          <Toggle label="站上 MA20" checked={filters.aboveMa20} onChange={(v) => up({ aboveMa20: v })} />
        </div>

        <div className="layer">
          <div className="layer-head"><b>L3 策略</b><button className="btn sm ghost" onClick={() => resetLayer('strategy')}>重置</button></div>
          <div className="fctl-grid">
            <Range label="动量分下限" value={filters.minMomentum} min={0} max={1} step={0.01} onChange={(v) => up({ minMomentum: v })} />
            <Range label="周期分下限" value={filters.minCycle} min={0} max={1} step={0.01} onChange={(v) => up({ minCycle: v })} />
            <Range label="拐点分下限" value={filters.minTurnaround} min={0} max={1} step={0.01} onChange={(v) => up({ minTurnaround: v })} />
            <Range label="综合因子下限" value={filters.minComposite} min={0} max={1} step={0.01} onChange={(v) => up({ minComposite: v })} />
          </div>
          <ChipGroup label="策略标签（命中任一即留）" options={STRATEGY_TAGS} selected={filters.strategyTags} onToggle={(o) => toggleChip('strategyTags', o)} />
        </div>

        <div className="layer">
          <div className="layer-head"><b>L4 入场</b><button className="btn sm ghost" onClick={() => resetLayer('entry')}>重置</button></div>
          <div className="fctl-grid">
            <Range label="RSI 下限" value={filters.rsiMin} min={0} max={100} step={1} onChange={(v) => up({ rsiMin: v })} />
            <Range label="RSI 上限" value={filters.rsiMax} min={0} max={100} step={1} onChange={(v) => up({ rsiMax: v })} />
            <Range label="距MA20下限%" value={filters.distMin} min={BOUNDS.dist[0]} max={BOUNDS.dist[1]} step={1} onChange={(v) => up({ distMin: v })} />
            <Range label="距MA20上限%" value={filters.distMax} min={BOUNDS.dist[0]} max={BOUNDS.dist[1]} step={1} onChange={(v) => up({ distMax: v })} />
          </div>
          <ChipGroup label="量价信号" options={VOL_SIGNALS} selected={filters.volSignals} onToggle={(o) => toggleChip('volSignals', o)} />
        </div>

        <div className="layer">
          <div className="layer-head"><b>L5 排名</b><button className="btn sm ghost" onClick={() => resetLayer('rank')}>重置</button></div>
          <div className="fctl-grid">
            <Range label="动量权重" value={filters.wMomentum ?? 0} min={0} max={1} step={0.05} onChange={(v) => up({ wMomentum: v })} />
            <Range label="周期权重" value={filters.wCycle ?? 0} min={0} max={1} step={0.05} onChange={(v) => up({ wCycle: v })} />
            <Range label="拐点权重" value={filters.wTurnaround ?? 0} min={0} max={1} step={0.05} onChange={(v) => up({ wTurnaround: v })} />
            <Range label="Top N" value={filters.topN} min={5} max={50} step={1} onChange={(v) => up({ topN: v })} />
          </div>
          <div className="fctl-row">
            <label className="fctl">
              <span className="fctl-name">排序字段</span>
              <select value={filters.sortBy} onChange={(e) => up({ sortBy: e.target.value })}>
                {SORT_OPTIONS.map((o) => <option key={o.v} value={o.v}>{o.label}</option>)}
              </select>
            </label>
            <Toggle label="已持仓降权(×0.5)" checked={filters.downgradeHeld} onChange={(v) => up({ downgradeHeld: v })} />
          </div>
        </div>
      </div>

      <div className="sect">
        <div className="sect-title">存活候选 Top {survivors.length}</div>
        {survivors.length === 0 ? <div className="empty">无满足条件的候选 —— 放宽阈值试试</div> : (
          <table className="tbl">
            <thead>
              <tr>
                <th>代码 / 名称</th>
                <th>行业</th>
                <th className="num">现价</th>
                <th className="num">PE</th>
                <th className="num">动量</th>
                <th className="num">周期</th>
                <th className="num">拐点</th>
                <th>策略标签</th>
                <th className="num">评分</th>
              </tr>
            </thead>
            <tbody>
              {survivors.map((r) => {
                const fs = r.factor?.factor_scores || {}
                return (
                  <tr key={r.code} className={selectedCode === r.code ? 'sel' : ''} onClick={() => setSelectedCode(r.code)} style={{ cursor: 'pointer' }}>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <div>
                          <b>{r.name || r.code}</b>
                          {r.in_portfolio && <span className="tag gray" style={{ marginLeft: 6 }}>已持仓</span>}
                          <div style={{ color: 'var(--text-dim)', fontSize: 12, marginTop: 1 }}>
                            {r.code}
                            <WatchButton code={r.code} inPool={wlCodes.has(r.code)} onAdded={(c) => setWlCodes((prev) => new Set(prev).add(c))} />
                          </div>
                        </div>
                      </div>
                    </td>
                    <td>{r.industry || '—'}</td>
                    <td className="num">{money(r.close)}</td>
                    <td className="num">{fmt(r.pe, 1)}</td>
                    <td className="num">{fmt(fs.momentum, 2)}</td>
                    <td className="num">{fmt(r.cycle_score, 2)}</td>
                    <td className="num">{fmt(r.turnaround_score, 2)}</td>
                    <td>
                      {(r.strategy_tags || []).map((tg) => (
                        <span key={tg} className={`tag ${tagColor(tg)}`} style={{ marginRight: 4 }}>{tg}</span>
                      ))}
                    </td>
                    <td className="num">
                      <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <div className="pbar gray" style={{ width: 56 }}><i style={{ width: `${Math.max(0, Math.min(1, r._score || 0)) * 100}%` }} /></div>
                        <b>{fmt(r._score, 2)}</b>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 10 }}>
          ⚠️ 候选仅供研究参考，非投资建议；需结合定性研究（阶段3）与基本面验证后再决策。
        </div>
      </div>

      {sel && (
        <div className="sect radar-sect">
          <div className="sect-title">
            因子雷达 · {sel.name} {sel.code}
            <span className="extra">动量/周期/拐点/价值/成长/质量（0–1）</span>
          </div>
          <div className="radar-wrap">
            <Radar item={sel} />
          </div>
        </div>
      )}
    </>
  )
}
