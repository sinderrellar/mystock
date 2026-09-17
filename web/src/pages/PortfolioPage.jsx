import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getPortfolio, refreshPortfolio, getWatchlist, addWatchlist, removeWatchlist, createResearchTask } from '../api.js'
import { fmt, money, signedCls, signedPct, tagCls, statusText } from '../format.js'
import StockSearch from '../components/StockSearch.jsx'

function pnum(obj, path, def) {
  const seg = String(path).split('.')
  let o = obj
  for (const s of seg) {
    if (o == null) return def
    o = o[s]
  }
  return o == null ? def : o
}

/* 通用键值行（面板列表用） */
function KV({ l, v, cls, strong = true }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '4px 0', fontSize: 13, borderBottom: '1px solid var(--border)' }}>
      <span style={{ color: 'var(--text-dim)' }}>{l}</span>
      <span className={cls || ''} style={{ fontWeight: strong ? 600 : 400, textAlign: 'right' }}>{v}</span>
    </div>
  )
}

/* 进度条（percent 0-100；overflow=true 时超 100 按 100 显示） */
function Bar({ pct, cls = '', color }) {
  const w = Math.min(Math.max(Number(pct) || 0, 0), 100)
  return (
    <div className="pbar">
      <i style={{ width: `${w}%`, background: color || undefined }} className={cls} />
    </div>
  )
}

/* ---------- 单只持仓卡片（可展开决策详情） ---------- */
function PositionCard({ p }) {
  const [open, setOpen] = useState(false)
  const ts = p.trend_signal || {}
  const ds = p.dynamic_stop || {}
  const dims = p.decision_dimensions || []
  const add = p.add_conditions?.conditions || []
  const trim = p.trim_conditions?.conditions || []
  const rule = p.rule_precheck || {}
  const pnl = p.unrealized_pnl
  const pnlPct = p.unrealized_pnl_pct
  const ma = ts.ma || {}

  return (
    <div className="poscard">
      <div className="phead" onClick={() => setOpen(!open)}>
        <div style={{ minWidth: 0 }}>
          <span className="pname">{p.name || p.code}</span>{' '}
          <span className="pcode">{p.code}</span>
        </div>
        {p.industry ? <span className={`tag ${tagCls(p.industry)}`}>{p.industry}</span> : null}
        <span className="tag blue">{p.market || 'A股'}</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 22, alignItems: 'center' }}>
          <span className="fmt">
            现价 <b>{money(p.current_price)}</b>
          </span>
          <span className="fmt">
            市值 <b>{money(p.market_value)}</b>
          </span>
          <span className={`fmt ${signedCls(pnlPct)}`}>
            盈亏 <b>{signedPct(pnlPct)}</b>
          </span>
          <span className="fmt">
            仓位 <b>{fmt(p.weight_pct)}%</b>
          </span>
        </span>
        <span className={`pexpand ${open ? 'open' : ''}`}>▾</span>
      </div>

      {open && (
        <div className="pbody">
          <div className="mgrid">
            <div className="mcell"><div className="l">成本价</div><div className="val">{money(p.cost_price)}</div></div>
            <div className="mcell"><div className="l">现价</div><div className="val">{money(p.current_price)}</div></div>
            <div className="mcell"><div className="l">数量</div><div className="val fmt">{fmt(p.quantity, 0)} 股</div></div>
            <div className="mcell"><div className="l">持仓市值</div><div className="val">{money(p.market_value)}</div></div>
            <div className="mcell"><div className="l">持仓成本</div><div className="val">{money(p.cost_value)}</div></div>
            <div className="mcell">
              <div className="l">浮动盈亏</div>
              <div className={`val ${signedCls(pnl)}`}>{money(pnl)}</div>
              <div className={`l ${signedCls(pnlPct)}`}>{signedPct(pnlPct)}</div>
            </div>
            <div className="mcell"><div className="l">PE / PB</div><div className="val fmt">{fmt(p.pe, 1)} / {fmt(p.pb, 2)}</div></div>
            {ds.price ? (
              <div className="mcell">
                <div className="l">动态止损（{ds.method || '动态'}）</div>
                <div className={`val ${ds.distance_pct != null && ds.distance_pct < 2 ? 'num-down' : ''}`}>{money(ds.price)}</div>
                <div className="l">距止损 {fmt(ds.distance_pct)}%</div>
              </div>
            ) : null}
            {p.take_profit_price ? (
              <div className="mcell"><div className="l">自设止盈</div><div className="val">{money(p.take_profit_price)}</div></div>
            ) : null}
            {p.stop_loss_price ? (
              <div className="mcell"><div className="l">自设止损</div><div className="val">{money(p.stop_loss_price)}</div></div>
            ) : null}
            {p.thesis ? (
              <div className="mcell"><div className="l">持有逻辑</div><div className="val" style={{ fontWeight: 400 }}>{p.thesis}</div></div>
            ) : null}
          </div>

          <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <span className="tag gray">趋势：{statusText(ts.status) || '—'}</span>
            {ma.ma20 ? <span className="tag gray">MA20 {money(ma.ma20)}</span> : null}
            {ma.ma5 ? <span className="tag gray">MA5 {money(ma.ma5)}</span> : null}
            {ma.ma60 ? <span className="tag gray">MA60 {money(ma.ma60)}</span> : null}
          </div>

          {(add.length || trim.length) && (
            <div style={{ marginTop: 10, display: 'flex', gap: 18, flexWrap: 'wrap' }}>
              <div>
                <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)' }}>加仓条件 {add.filter((c) => c.met).length}/{add.length}</div>
                <div className="facts-inline" style={{ marginTop: 4 }}>
                  {add.map((c, i) => (
                    <span key={i} className={`tag ${c.met ? 'green' : 'gray'}`}>
                      {c.met ? '✓' : '✗'} {c.name}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)' }}>止盈/减仓条件 {trim.filter((c) => c.met).length}/{trim.length}</div>
                <div className="facts-inline" style={{ marginTop: 4 }}>
                  {trim.map((c, i) => (
                    <span key={i} className={`tag ${c.met ? 'orange' : 'gray'}`}>
                      {c.met ? '✓' : '✗'} {c.name}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {dims.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 6 }}>七维决策卡</div>
              {dims.map((d, i) => (
                <div key={i} className="dim-item">
                  <div className="d-head">
                    <span className={`tag ${tagCls(d.status)}`}>{d.status}</span>
                    {d.name}
                  </div>
                  {d.conclusion && <div className="d-con">{d.conclusion}</div>}
                  {d.supporting_factors?.length > 0 && (
                    <div className="facts-inline" style={{ marginTop: 5 }}>
                      {d.supporting_factors.map((s, j) => (
                        <span key={j} className="tag green">{s}</span>
                      ))}
                    </div>
                  )}
                  {d.risk_factors?.length > 0 && (
                    <div className="facts-inline" style={{ marginTop: 5 }}>
                      {d.risk_factors.map((s, j) => (
                        <span key={j} className="tag orange">{s}</span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {rule.action && (
            <div className={`alert-box ${/止损|处理|警惕|风险/.test(rule.action) ? 'danger' : 'info'}`} style={{ marginTop: 8 }}>
              <b>{rule.action}</b> {rule.reason ? `—— ${rule.reason}` : ''}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ---------- 面板：诊断 + 目标进度 ---------- */
function DiagnosisCard({ d }) {
  const dx = d.diagnosis || {}
  const ss = dx.style_summary || {}
  const goal = d.goal_progress || {}
  const styles = [
    ['周期', ss.cyclical_pct],
    ['成长', ss.growth_pct],
    ['防守', ss.defensive_pct],
  ]
  const hasStyle = styles.some(([, v]) => Number(v) > 0)

  return (
    <div className="sect">
      <div className="sect-title">诊断</div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '4px 0 8px' }}>
        {dx.risk_tone ? <span className={`tag ${tagCls(dx.risk_tone)}`}>风险：{dx.risk_tone}</span> : null}
        {dx.cash_state ? <span className={`tag ${tagCls(dx.cash_state)}`}>现金：{dx.cash_state}</span> : null}
        {dx.goal_state ? <span className={`tag ${tagCls(dx.goal_state)}`}>目标：{dx.goal_state}</span> : null}
        <span className="tag gray">可动 {fmt(dx.available_to_deploy_pct)}%</span>
      </div>

      <div style={{ margin: '6px 0' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-dim)', marginBottom: 3 }}>
          <span>组合收益进度 → 目标 {fmt(goal.profit_target_pct)}%</span>
          <span>{fmt(goal.target_progress_pct)}%</span>
        </div>
        <Bar pct={goal.target_progress_pct} color="var(--accent)" />
      </div>
      <div style={{ margin: '6px 0 8px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-dim)', marginBottom: 3 }}>
          <span>回撤占用（限额 {fmt(goal.max_drawdown_pct)}%）</span>
          <span>{fmt(goal.drawdown_usage_pct)}%</span>
        </div>
        <Bar pct={goal.drawdown_usage_pct} color={Number(goal.drawdown_usage_pct) >= 80 ? 'var(--up)' : 'var(--trend)'} />
      </div>

      <KV l="组合收益" v={signedPct(goal.portfolio_pnl_pct)} cls={signedCls(goal.portfolio_pnl_pct)} />
      <KV l="距目标还差" v={`${fmt(goal.remaining_to_target_pct)}%`} />
      <KV l="剩余回撤空间" v={`${fmt(goal.remaining_drawdown_pct)}%`} />
      <KV l="可加仓预算" v={fmt(dx.available_to_deploy_pct) + '%'} strong={false} />

      {hasStyle && (
        <div style={{ marginTop: 8 }}>
          <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 2 }}>持仓风格占比</div>
          {styles.map(([name, v]) => (
            <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', fontSize: 12 }}>
              <span style={{ width: 30, color: 'var(--text-dim)' }}>{name}</span>
              <div className="pbar" style={{ flex: 1 }}><i style={{ width: `${Math.min(Math.max(Number(v) || 0, 0), 100)}%` }} /></div>
              <span style={{ width: 52, textAlign: 'right' }}>{fmt(v)}%</span>
            </div>
          ))}
        </div>
      )}

      {(dx.preferred_directions || []).length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 2 }}>建议方向</div>
          <div className="facts-inline">{(dx.preferred_directions || []).map((t, i) => <span key={i} className="tag green">✓ {t}</span>)}</div>
        </div>
      )}
      {(dx.avoid_directions || []).length > 0 && (
        <div style={{ marginTop: 6 }}>
          <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 2 }}>回避方向</div>
          <div className="facts-inline">{(dx.avoid_directions || []).map((t, i) => <span key={i} className="tag orange">✗ {t}</span>)}</div>
        </div>
      )}
      {dx.buy_budget ? <div style={{ marginTop: 8, fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.5 }}>💡 {dx.buy_budget}</div> : null}
      {(dx.notes || []).length > 0 && (
        <div style={{ marginTop: 6, fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>
          {(dx.notes || []).map((n, i) => <div key={i}>· {n}</div>)}
        </div>
      )}
    </div>
  )
}

/* ---------- 面板：市场情绪（含指数 / 北向南向） ---------- */
const MOOD_TEXT = { bullish: '情绪偏多', bearish: '情绪偏空', neutral: '情绪中性' }
function MarketSentimentCard({ d }) {
  const ms = d.market_sentiment || {}
  if (!ms.available) {
    return (
      <div className="sect">
        <div className="sect-title">市场情绪</div>
        <div className="empty">市场情绪数据暂不可用{ms.reason ? `：${ms.reason}` : ''}</div>
        {ms._note ? <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 4 }}>{ms._note}</div> : null}
      </div>
    )
  }
  const moodCls = ms.mood === 'bullish' ? 'num-up' : ms.mood === 'bearish' ? 'num-down' : ''
  const indices = ms.indices || {}
  const nb = ms.north_bound || {}
  return (
    <div className="sect">
      <div className="sect-title">
        市场情绪
        {ms.mood ? <span className="tag" style={{ marginLeft: 8 }}>{MOOD_TEXT[ms.mood] || ms.mood}</span> : null}
      </div>
      <div className="grid2" style={{ margin: '2px 0 4px' }}>
        <div className="stat" style={{ padding: '8px 4px' }}><div className="t">20日平均收益</div><div className={`v fmt ${signedCls(ms.avg_return_20d)}`}>{signedPct(ms.avg_return_20d)}</div></div>
        <div className="stat" style={{ padding: '8px 4px' }}><div className="t">情绪 z 分</div><div className={`v fmt ${signedCls(ms.avg_z_score)}`}>{signedPct(ms.avg_z_score)}</div><div className="s">收益/波动 标准化</div></div>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', margin: '6px 0 2px' }}>指数（现价 / 5日 / 20日）</div>
      {Object.entries(indices).map(([name, it]) => {
        const ok = it && it.available
        return (
          <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', fontSize: 13, borderBottom: '1px solid var(--border)' }}>
            <span style={{ width: 62, fontWeight: 600 }}>{name}</span>
            {ok ? (
              <>
                <span>现价 {fmt(it.latest_close)}</span>
                <span className={`${signedCls(it.return_5d)}`} style={{ width: 68, textAlign: 'right' }}>{signedPct(it.return_5d)} / 5日</span>
                <span className={`${signedCls(it.return_20d)}`} style={{ width: 70, textAlign: 'right' }}>{signedPct(it.return_20d)} / 20日</span>
                <span className="muted" style={{ marginLeft: 'auto' }}>波动 {fmt(it.volatility_20d)}%</span>
              </>
            ) : (
              <span style={{ color: 'var(--text-dim)' }}>数据不足</span>
            )}
          </div>
        )
      })}
      <div style={{ fontSize: 12, color: 'var(--text-dim)', margin: '8px 0 2px' }}>北向 / 南向资金（{nb.latest_date || '—'}）</div>
      {nb.available ? (
        <>
          <KV l="北向 5 日净买" v={nb.north_5d_buy ?? '—'} cls={/^-/.test(String(nb.north_5d_buy ?? '')) ? 'num-down' : 'num-up'} />
          <KV l="南向 5 日净买" v={nb.south_5d_buy ?? '—'} cls={/^-/.test(String(nb.south_5d_buy ?? '')) ? 'num-down' : 'num-up'} />
          <KV l="口径" v={nb.source || '—'} strong={false} />
        </>
      ) : (
        <div style={{ fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.5 }}>
          {nb._note || nb.reason || '北向/南向数据暂不可用'}
        </div>
      )}
    </div>
  )
}

/* ---------- 面板：大盘宽度 / 两融 / 资金流 ---------- */
function BreadthCard({ d }) {
  const mb = d.market_breadth || {}
  const corr = d.correlation || {}
  const ms = d.market_sentiment || {}

  const renderValue = (name, item) => {
    switch (name) {
      case '大盘宽度':
        return (
          <span>
            <b style={{ color: 'var(--accent)' }}>{item.width}</b>
            {' · '}站上MA20 <b>{item.above_ma20_pct}%</b> / MA60 <b>{item.above_ma60_pct}%</b>
            {' · 样本 '}{item.sample} 只
          </span>
        )
      case '组合相关性': {
        const pairs = item.pairs || []
        const head = pairs.slice(0, 3).map(p => `${p.pair} ${p.corr}`).join('、')
        return (
          <span>
            平均相关 <b>{fmt(item.avg_corr, 2)}</b>
            {head ? <> · 高相关：{head}</> : ' · 无显著高相关对'}
          </span>
        )
      }
      case '两融余额':
        return (
          <span>
            两融 <b>{fmt(item.rzrqye, 0)}亿</b>（融资 {fmt(item.rzye, 0)}亿 / 融券 {fmt(item.rqye, 0)}亿）
            <span style={{ color: 'var(--text-dim)' }}> · {item.date}</span>
          </span>
        )
      case '全市场主力资金':
        return (
          <span>
            <b className={signedCls(item.net_main_5d)}>{item.label} {fmt(Math.abs(item.net_main_5d), 0)}亿</b>
            {' · 近5日 '}{item.pos_days} 净流入
            <span style={{ color: 'var(--text-dim)' }}> · 截至 {item.latest_date}</span>
          </span>
        )
      case '行业资金流': {
        const inflow = (item.top_inflow || []).slice(0, 3).map(x => `${x.industry} +${fmt(x.net_flow, 1)}亿`).join('、')
        const outflow = (item.top_outflow || []).slice(0, 3).map(x => `${x.industry} ${fmt(Math.abs(x.net_flow), 1)}亿`).join('、')
        return (
          <span style={{ display: 'block', lineHeight: 1.6 }}>
            <div>流入：{inflow || '—'}</div>
            <div>流出：{outflow || '—'}</div>
          </span>
        )
      }
      default:
        return null
    }
  }

  const rows = [
    ['大盘宽度', mb, mb.available ? '' : mb.reason],
    ['组合相关性', corr, corr.available ? '' : (corr.reason || '需 ≥2 只持仓方可计算')],
    ['两融余额', ms.margin || {}, ms.margin?.available ? '' : (ms.margin?.reason || '—')],
    ['全市场主力资金', ms.market_moneyflow || {}, ms.market_moneyflow?.available ? '' : (ms.market_moneyflow?.reason || '—')],
    ['行业资金流', ms.industry_moneyflow || {}, ms.industry_moneyflow?.available ? '' : (ms.industry_moneyflow?.reason || '—')],
  ]
  return (
    <div className="sect">
      <div className="sect-title">大盘宽度 / 资金面</div>
      {rows.map(([name, item, reason]) => {
        const ok = item && item.available
        return (
          <div key={name} style={{ padding: '7px 0', fontSize: 13, borderBottom: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
              <span style={{ fontWeight: 600 }}>{name}</span>
              {!ok && <span className="tag gray" style={{ fontSize: 11 }}>暂不可用</span>}
            </div>
            <div style={{ fontSize: 12.5, marginTop: 3, color: ok ? 'var(--text)' : 'var(--text-dim)' }}>
              {ok ? renderValue(name, item) : `↳ ${reason || '数据暂不可用'}`}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/* ---------- 面板：周期 / 风格 / 市场暴露 ---------- */
function CycleExposureCard({ d }) {
  const cyc = d.cycle_summary || {}
  const styles = cyc.styles || []
  const styleRows = d.style_exposure || []
  const marketRows = d.market_exposure || []
  const assetRows = d.asset_type_exposure || []
  const noData = styles.length === 0 && styleRows.length === 0 && marketRows.length === 0 && assetRows.length === 0

  const groupRows = (rows, key) =>
    rows.map((r) => ({
      name: r[key],
      pct: r.weight_pct,
      extra: r.momentum_score != null ? fmt(r.momentum_score, 3) : null,
    }))

  return (
    <div className="sect">
      <div className="sect-title">周期 / 暴露</div>
      {noData ? (
        <div className="empty">暂无持仓，无暴露数据</div>
      ) : (
        <>
          {styles.length > 0 && (
            <>
              <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)', margin: '2px 0 4px' }}>行业动量分（sector_radar）</div>
              {styles.map((s) => (
                <div key={s.industry} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', fontSize: 12.5, borderBottom: '1px solid var(--border)' }}>
                  <span style={{ fontWeight: 600, width: 88 }}>{s.industry}</span>
                  <span className={`tag ${tagCls(s.status)}`}>{s.status}</span>
                  <span className="muted" style={{ marginLeft: 'auto' }}>{s.momentum_score != null ? `动量 ${fmt(s.momentum_score, 3)}` : '无数据'}</span>
                </div>
              ))}
            </>
          )}
          {styleRows.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)' }}>风格暴露（市值/成长属性）</div>
              {groupRows(styleRows, 'style').map((r) => (
                <div key={r.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', fontSize: 12 }}>
                  <span style={{ width: 88, color: 'var(--text-dim)' }}>{r.name}</span>
                  <div className="pbar" style={{ flex: 1 }}><i style={{ width: `${Math.min(Number(r.pct) || 0, 100)}%` }} /></div>
                  <span style={{ width: 52, textAlign: 'right' }}>{fmt(r.pct)}%</span>
                </div>
              ))}
            </div>
          )}
          {marketRows.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)' }}>市场暴露</div>
              <div className="facts-inline">{(groupRows(marketRows, 'market')).map((r) => <span key={r.name} className="tag blue">{r.name} {fmt(r.pct)}%</span>)}</div>
            </div>
          )}
          {assetRows.length > 0 && (
            <div style={{ marginTop: 6 }}>
              <div className="l" style={{ fontSize: 12, color: 'var(--text-dim)' }}>资产类型暴露</div>
              <div className="facts-inline">{(groupRows(assetRows, 'asset_type')).map((r) => <span key={r.name} className="tag gray">{r.name} {fmt(r.pct)}%</span>)}</div>
            </div>
          )}
          {cyc.summary ? <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>{cyc.summary}</div> : null}
        </>
      )}
    </div>
  )
}

/* ---------- 观察池（独立 Mongo 接口，可加可移） ---------- */
function WatchlistBox() {
  const [wl, setWl] = useState([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [picked, setPicked] = useState(null) // {code,name}
  const [thesis, setThesis] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    getWatchlist()
      .then((r) => setWl(r.items || []))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  function pick(s) {
    setPicked({ code: s.code, name: s.name })
    setThesis('')
  }
  async function doAdd() {
    if (!picked) return
    setBusy(true)
    setErr('')
    try {
      await addWatchlist(picked.code, thesis.trim())
      setPicked(null)
      setThesis('')
      setWl((await getWatchlist()).items || [])
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }
  async function doRemove(code) {
    try {
      await removeWatchlist(code)
      setWl((await getWatchlist()).items || [])
    } catch (e) {
      setErr(e.message)
    }
  }

  return (
    <div className="sect">
      <div className="sect-title">观察池（{wl.length}）</div>

      <div style={{ margin: '4px 0 8px' }}>
        <StockSearch onPick={pick} placeholder="搜标的加入观察池…" />
        {picked && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
            <span className="tag blue">{picked.name} {picked.code}</span>
            <input
              style={{ flex: 1, minWidth: 0 }}
              placeholder="关注逻辑（可选）"
              value={thesis}
              onChange={(e) => setThesis(e.target.value)}
            />
            <button className="btn sm" onClick={doAdd} disabled={busy}>{busy ? '…' : '＋ 加入'}</button>
            <button className="btn sm ghost" onClick={() => setPicked(null)}>取消</button>
          </div>
        )}
      </div>

      {err && <div className="error" style={{ margin: '4px 0' }}>{err}</div>}
      {!loading && wl.length === 0 ? (
        <div className="empty">观察池为空 —— 上框搜索后加入，用于跟踪候选标的</div>
      ) : (
        wl.map((w) => (
          <div key={w.code} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 0', borderBottom: '1px solid var(--border)', fontSize: 13 }}>
            <span style={{ minWidth: 0 }}>
              <b>{w.name}</b> <span style={{ color: 'var(--text-dim)' }}>{w.code}</span>
              {w.industry ? <span className={`tag ${tagCls(w.industry)}`} style={{ marginLeft: 6, fontSize: 10.5 }}>{w.industry}</span> : null}
            </span>
            {w.add_price ? <span className="muted" style={{ fontSize: 12 }}>入池价 {money(w.add_price)}</span> : null}
            <span className="muted" style={{ flex: 1, textAlign: 'right', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 12 }}>
              {w.thesis || ''}
            </span>
            <button className="link-del" onClick={() => doRemove(w.code)} title="移出观察池">✕</button>
          </div>
        ))
      )}
    </div>
  )
}

/* ---------- 组合全景页 ---------- */
export default function PortfolioPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [verdict, setVerdict] = useState({ state: 'idle', msg: '' }) // idle|submitting|queued|error

  const load = useCallback(async (force = false) => {
    setError(null)
    if (force) setRefreshing(true)
    else setLoading(true)
    try {
      const d = force ? await refreshPortfolio() : await getPortfolio()
      setData(d)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // 综合研判：组合全景 + 新鲜报告 → 后台跑三层研判，结果落研究库
  async function doVerdict() {
    setVerdict({ state: 'submitting', msg: '' })
    try {
      const r = await createResearchTask({ type: 'verdict' })
      if (r.skipped) {
        setVerdict({ state: 'queued', msg: `已有综合研判可复用：${r.report}` })
      } else {
        setVerdict({ state: 'queued', msg: `综合研判任务已提交（${r.task_id?.slice(0, 8)}…），后台运行约数分钟，完成后见研究库` })
      }
    } catch (e) {
      setVerdict({ state: 'error', msg: e.message })
    }
  }

  if (loading && !data) {
    return (
      <div className="loading-block">
        <div className="spinner" style={{ margin: '0 auto 12px' }} />
        正在生成组合全景（联网获取行情与因子，约 10-20 秒）……
      </div>
    )
  }
  if (error && !data) {
    return (
      <>
        <div className="error">{error}</div>
        <button className="btn sm ghost" onClick={() => load()}>重试</button>
      </>
    )
  }
  if (!data) return null

  const s = data.summary || {}
  const goal = s.goal_progress || {}
  const positions = data.positions || []
  const alerts = data.alerts || []
  const exposure = data.industry_exposure || []
  const gp = data.goal_progress || {}
  const cashPct = s.cash_pct
  const cash = s.cash

  return (
    <>
      <div className="page-head">
        <div>
          <h2>组合全景</h2>
          <div className="desc">数据源：模拟交易账户 → portfolio.yaml → review()（缓存 3 分钟）· {s.generated_at || ''}</div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn sm ghost" onClick={doVerdict} disabled={verdict.state === 'submitting'}>
            {verdict.state === 'submitting' ? '提交中…' : '🔬 智能研判'}
          </button>
          <button className="btn sm ghost" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? '重算中…' : '🔄 手动刷新'}
          </button>
        </div>
      </div>

      {verdict.msg && (
        <div className={`alert-box ${verdict.state === 'error' ? 'danger' : 'info'}`} style={{ marginBottom: 12 }}>
          {verdict.msg}
          {verdict.state === 'queued' && <Link to="/research/library" style={{ marginLeft: 8 }}>去研究库查看 →</Link>}
        </div>
      )}

      <div className="stat-grid">
        <div className="stat accent"><div className="t">总资产</div><div className="v">{money(s.total_assets)}</div><div className="s">含浮盈亏</div></div>
        <div className="stat"><div className="t">可用现金</div><div className="v fmt">{money(cash)}</div><div className="s">现金占比 {fmt(cashPct)}%</div></div>
        <div className="stat"><div className="t">持仓市值</div><div className="v fmt">{money(s.position_value)}</div><div className="s">{positions.length} 只持仓</div></div>
        <div className="stat">
          <div className="t">浮动盈亏</div>
          <div className={`v ${signedCls(s.unrealized_pnl)}`}>{money(s.unrealized_pnl)}</div>
          <div className={`s ${signedCls(s.unrealized_pnl_pct)}`}>{signedPct(s.unrealized_pnl_pct)}</div>
        </div>
        <div className="stat"><div className="t">风险偏好</div><div className="v" style={{ fontSize: 17 }}>{s.risk_profile || 'balanced'}</div><div className="s">券商 {s.broker || '—'}</div></div>
        <div className="stat">
          <div className="t">目标状态</div>
          <div className="v" style={{ fontSize: 16, color: 'var(--accent)' }}>{statusText(goal.state || gp.state)}</div>
          <div className="s">距目标 {goal.target_progress_pct != null ? `${goal.target_progress_pct}%` : '—'}</div>
        </div>
      </div>

      <div className="goal-banner">
        <span className="g-state">🎯 {statusText(goal.state || gp.state || '—')}</span>
        <span className="g-bias">{goal.action_bias || gp.action_bias || '—'}</span>
        {gp.interpretation ? <span className="g-bias" style={{ opacity: 0.8 }}>{gp.interpretation}</span> : null}
        {alerts.length === 0 && <span className="g-bias" style={{ opacity: 0.6 }}>无告警</span>}
      </div>

      {alerts.length > 0 && (
        <div className="sect" style={{ borderColor: 'rgba(226,54,44,.3)' }}>
          <div className="sect-title">告警 {alerts.length > 0 && <span className="extra">{alerts.length} 条</span>}</div>
          {alerts.map((a, i) => (
            <div key={i} className={`alert-box ${/预警|风险|止损|跌破|触发|清仓|大亏/.test(String(a.severity || a.level || a.text || '')) ? 'danger' : 'warn'}`}>
              {typeof a === 'string' ? a : `${a.text || a.message || ''} ${a.detail || ''}`}
            </div>
          ))}
        </div>
      )}

      <div className="sect">
        <div className="sect-title">
          持仓明细（{positions.length}） <span className="extra">点击卡片展开七维决策卡</span>
        </div>
        {positions.length === 0 ? (
          <div className="empty">暂无持仓 —— 去「模拟交易」买入第一笔，组合全景将实时反映</div>
        ) : (
          positions.map((p) => <PositionCard key={p.code} p={p} />)
        )}
      </div>

      <div className="grid2">
        <DiagnosisCard d={data} />
        <MarketSentimentCard d={data} />
      </div>
      <div className="grid2">
        <WatchlistBox />
        <div className="sect">
          <div className="sect-title">行业暴露</div>
          {exposure.length === 0 ? (
            <div className="empty">暂无持仓，无行业暴露</div>
          ) : (
            exposure.map((e) => (
              <div key={e.industry} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '7px 0', fontSize: 13 }}>
                <span style={{ width: 96, fontWeight: 600 }}>{e.industry}</span>
                <div className="pbar" style={{ flex: 1 }}><i style={{ width: `${Math.min((e.weight_pct || 0) / 30 * 100, 100)}%` }} /></div>
                <span className="fmt" style={{ width: 70, textAlign: 'right' }}>{fmt(e.weight_pct)}%</span>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="grid2">
        <CycleExposureCard d={data} />
        <BreadthCard d={data} />
      </div>
    </>
  )
}
