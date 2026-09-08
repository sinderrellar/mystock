import React, { useEffect, useState } from 'react'
import { getSector } from '../api.js'
import { fmt, signedPct, signedCls, tagCls, statusText } from '../format.js'

function leaderOf(item) {
  const ls = item._leaders
  if (!ls || ls.length === 0) return null
  const l = ls[0]
  return l
}

export default function SectorPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    getSector()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  if (loading && !data) return <div className="loading-block"><div className="spinner" style={{ margin: '0 auto 12px' }} />正在生成行业动量热力与苏醒雷达……</div>
  if (error && !data) {
    return (<><div className="error">{error}</div><button className="btn sm ghost" onClick={() => window.location.reload()}>重试</button></>)
  }
  if (!data) return null
  const heat = data.heatmap || []
  const radar = data.radar || []

  return (
    <>
      <div className="page-head">
        <div>
          <h2>行业雷达</h2>
          <div className="desc">动量热力 Top10（基于信号缓存重算）+ 苏醒雷达 Top5（超跌反转动量打分）</div>
        </div>
      </div>

      <div className="sect">
        <div className="sect-title">行业动量热力 <span className="extra">按 20 日涨幅与宽度评分</span></div>
        {heat.length === 0 ? <div className="empty">暂无数据</div> : (
          <table className="tbl">
            <thead>
              <tr>
                <th>行业</th><th>热度</th>
                <th className="num">5日</th><th className="num">20日</th><th className="num">60日</th>
                <th className="num">站上MA20</th><th className="num">RSI</th>
                <th>龙头</th><th className="num">PE中位</th>
              </tr>
            </thead>
            <tbody>
              {heat.map((r) => {
                const ldr = leaderOf(r)
                return (
                  <tr key={r.industry}>
                    <td style={{ fontWeight: 600 }}>{r.industry}</td>
                    <td><span className={`tag ${tagCls(r.status)}`}>{statusText(r.status)}</span></td>
                    <td className={`num ${signedCls(r.avg_ret_5d)}`}>{signedPct(r.avg_ret_5d)}</td>
                    <td className={`num ${signedCls(r.avg_ret_20d)}`}>{signedPct(r.avg_ret_20d)}</td>
                    <td className={`num ${signedCls(r.avg_ret_60d)}`}>{signedPct(r.avg_ret_60d)}</td>
                    <td className="num">{fmt(r.leader_above_ma20_pct)}%</td>
                    <td className="num">{fmt(r.avg_rsi, 1)}</td>
                    <td>{ldr ? <span>{ldr.name} <span className={`num ${signedCls(ldr.ret_20d)}`}>({fmt(ldr.ret_20d)}%)</span></span> : '—'}</td>
                    <td className="num">{fmt(r.pe_median, 1)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="sect">
        <div className="sect-title">苏醒雷达（超跌反转机会） <span className="extra">washout 出清 / 反转 / 龙头 / 估值 / 背离 五维</span></div>
        {radar.length === 0 ? <div className="empty">暂无苏醒信号</div> : (
          radar.map((r) => {
            const det = r.wake_up_detail || {}
            const ldr = leaderOf(r)
            const dims = [
              ['出清', det.washout], ['反转', det.reversal], ['龙头启动', det.leader],
              ['估值修复', det.valuation], ['动量背离', det.divergence], ['量价背离', det.volume_divergence],
            ]
            return (
              <div key={r.industry} style={{ borderBottom: '1px solid var(--border)', padding: '12px 0' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                  <span style={{ width: 76, fontWeight: 700 }}>{r.industry}</span>
                  <span className={`tag ${tagCls(r.wake_label || r.status)}`}>{statusText(r.wake_label || r.status)}</span>
                  <span style={{ flex: 1, minWidth: 120 }}>
                    <div className="pbar orange" style={{ display: 'inline-block', width: 160 }}><i style={{ width: `${Math.min((r.wake_up_score || 0) * 100, 100)}%` }} /></div>
                    <span style={{ marginLeft: 8 }} className="fmt">苏醒 {fmt((r.wake_up_score || 0) * 100, 0)}</span>
                  </span>
                  {ldr && <span style={{ fontSize: 13, color: 'var(--text-dim)' }}>龙头 {ldr.name}</span>}
                  <span style={{ fontSize: 13 }} className={`num ${signedCls(r.avg_ret_5d)}`}>5日 {signedPct(r.avg_ret_5d)}</span>
                </div>
                <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 6, paddingLeft: 88 }}>
                  {dims.map(([n, v]) => (
                    <span key={n} className="tag gray" title={`${n}: ${fmt((v || 0) * 100, 0)}%`}>
                      {n} <b>{fmt((v || 0) * 100, 0)}%</b>
                    </span>
                  ))}
                </div>
              </div>
            )
          })
        )}
      </div>
    </>
  )
}
