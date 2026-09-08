import React, { useCallback, useEffect, useState } from 'react'
import { getAccount, getTrades, placeOrder, updatePosition } from '../api.js'
import { fmt, money, signedPct, signedCls } from '../format.js'
import StockSearch from '../components/StockSearch.jsx'

const COMMISSION_RATE = 0.00025
const STAMP_TAX_RATE = 0.001

/* 持仓风控/逻辑设置条（止盈 / 止损 / 持有逻辑；输入留空=不动，勾选清除=清空） */
function PositionSettings({ pos, saving, onSave, onClose }) {
  const [sl, setSl] = useState(pos.stop_loss_price == null ? '' : String(pos.stop_loss_price))
  const [tp, setTp] = useState(pos.take_profit_price == null ? '' : String(pos.take_profit_price))
  const [th, setTh] = useState(pos.thesis || '')
  const [clrSl, setClrSl] = useState(false)
  const [clrTp, setClrTp] = useState(false)
  const [clrTh, setClrTh] = useState(false)
  const [err, setErr] = useState('')

  function save() {
    const patch = {}
    if (clrSl) patch.stop_loss_price = null
    else if (sl.trim() !== '') {
      const v = parseFloat(sl)
      if (!(v > 0)) return setErr('止损价需为正数')
      patch.stop_loss_price = v
    }
    if (clrTp) patch.take_profit_price = null
    else if (tp.trim() !== '') {
      const v = parseFloat(tp)
      if (!(v > 0)) return setErr('止盈价需为正数')
      patch.take_profit_price = v
    }
    if (clrTh) patch.thesis = null
    else if (th.trim() !== '') patch.thesis = th.trim()
    onSave(patch)
  }

  const rowCls = { display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0' }
  const lbl = { width: 56, fontSize: 13, color: 'var(--text-dim)', flexShrink: 0 }
  const num = { width: 110 }
  return (
    <div className="setting-bar">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <b style={{ fontSize: 14 }}>⚙ 设置风控 / 持有逻辑</b>
        <span className="tag blue">{pos.name} {pos.code}</span>
        <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>现价 {money(pos.current_price)} · 成本 {money(pos.cost_price)}</span>
        <span style={{ marginLeft: 'auto' }}>
          <button className="btn sm ghost" onClick={onClose} disabled={saving}>收起</button>
        </span>
      </div>
      <div style={rowCls}>
        <span style={lbl}>止损价</span>
        <input type="number" step="0.01" style={num} value={sl} placeholder="留空不动" onChange={(e) => { setSl(e.target.value); setClrSl(false) }} />
        {pos.stop_loss_price != null && (
          <label style={{ fontSize: 12, color: 'var(--text-dim)', display: 'flex', gap: 4, alignItems: 'center' }}>
            <input type="checkbox" checked={clrSl} onChange={(e) => setClrSl(e.target.checked)} /> 清除
          </label>
        )}
        <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>当前 {pos.stop_loss_price != null ? money(pos.stop_loss_price) : '未设'}</span>
      </div>
      <div style={rowCls}>
        <span style={lbl}>止盈价</span>
        <input type="number" step="0.01" style={num} value={tp} placeholder="留空不动" onChange={(e) => { setTp(e.target.value); setClrTp(false) }} />
        {pos.take_profit_price != null && (
          <label style={{ fontSize: 12, color: 'var(--text-dim)', display: 'flex', gap: 4, alignItems: 'center' }}>
            <input type="checkbox" checked={clrTp} onChange={(e) => setClrTp(e.target.checked)} /> 清除
          </label>
        )}
        <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>当前 {pos.take_profit_price != null ? money(pos.take_profit_price) : '未设'}</span>
      </div>
      <div style={rowCls}>
        <span style={lbl}>持有逻辑</span>
        <input
          style={{ flex: 1, minWidth: 0, maxWidth: 420 }}
          value={th}
          placeholder="买入/持有的核心理由（写入组合决策卡 thesis）"
          onChange={(e) => { setTh(e.target.value); setClrTh(false) }}
        />
        {pos.thesis && (
          <label style={{ fontSize: 12, color: 'var(--text-dim)', display: 'flex', gap: 4, alignItems: 'center', flexShrink: 0 }}>
            <input type="checkbox" checked={clrTh} onChange={(e) => setClrTh(e.target.checked)} /> 清空
          </label>
        )}
      </div>
      {err && <div className="error" style={{ marginBottom: 6 }}>{err}</div>}
      <div style={{ marginTop: 6, display: 'flex', gap: 10 }}>
        <button className="btn sm" onClick={save} disabled={saving}>{saving ? '保存中…' : '保存设置'}</button>
        <button className="btn sm ghost" onClick={onClose} disabled={saving}>取消</button>
        <span style={{ fontSize: 12, color: 'var(--text-dim)', alignSelf: 'center' }}>保存后同步到组合全景：决策卡 / 止损提醒即时生效</span>
      </div>
    </div>
  )
}

export default function TradePage() {
  const [acct, setAcct] = useState(null)
  const [trades, setTrades] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  // 下单面板状态
  const [stock, setStock] = useState(null) // {code,name,close}
  const [side, setSide] = useState('buy')
  const [qty, setQty] = useState('')
  const [toast, setToast] = useState(null)
  const [orderErr, setOrderErr] = useState(null)

  // 持仓设置条状态
  const [setting, setSetting] = useState(null) // 选中的持仓 pos（含 code/name/stop…）
  const [savingPos, setSavingPos] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [a, t] = await Promise.all([getAccount(), getTrades(50)])
      setAcct(a)
      setTrades(t.trades || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function submit() {
    if (!stock) {
      setOrderErr('请先选择股票')
      return
    }
    const n = parseInt(qty, 10)
    if (!n || n <= 0) {
      setOrderErr('请输入有效的买入数量')
      return
    }
    setBusy(true)
    setOrderErr(null)
    setToast(null)
    try {
      const r = await placeOrder(stock.code, side, n)
      setToast(
        `${r.side === 'buy' ? '买入' : '卖出'}成功：${r.name} ${r.quantity} 股 @ ${r.price}，费用 ${r.fee}，现金余额 ${money(r.cash)}${r.note ? ' · ' + r.note : ''}`,
      )
      setQty('')
      setStock(null)
      await load()
    } catch (e) {
      setOrderErr(e.message)
    } finally {
      setBusy(false)
    }
  }

  function pickFromPos(p, wantSide) {
    setStock({ code: p.code, name: p.name, close: p.current_price })
    setSide(wantSide)
    if (wantSide === 'sell') setQty(String(p.available_qty || 0))
    else setQty('')
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  async function savePositionSetting(patch) {
    if (!setting) return
    if (Object.keys(patch).length === 0) {
      setToast('没有需要保存的改动（输入留空表示不动）')
      return
    }
    setSavingPos(true)
    setToast(null)
    try {
      await updatePosition(setting.code, patch)
      setToast(`已更新 ${setting.name}（${setting.code}）的风控/逻辑设置`)
      setSetting(null)
      await load()
    } catch (e) {
      setToast(null)
      setOrderErr(e.message)
    } finally {
      setSavingPos(false)
    }
  }

  const positions = acct?.positions || []
  const n = parseInt(qty, 10) || 0
  const price = stock ? Number(stock.close) || 0 : 0
  const amount = price * n
  const commission = amount * COMMISSION_RATE
  const tax = side === 'sell' ? amount * STAMP_TAX_RATE : 0
  const cashAfter = acct
    ? side === 'buy'
      ? acct.cash - amount - commission
      : acct.cash + amount - commission - tax
    : null
  const sellOk = side === 'sell' && stock ? (positions.find((p) => p.code === stock.code)?.available_qty || 0) >= n : true

  return (
    <>
      <div className="page-head">
        <div>
          <h2>模拟交易</h2>
          <div className="desc">初始虚拟资金 100 万 · 按现价即时成交 · T+1 · 佣金万2.5 + 印花税千1（仅卖出）</div>
        </div>
        <button className="btn sm ghost" onClick={load} disabled={loading}>🔄 刷新</button>
      </div>

      {error && <div className="error">{error}</div>}
      {toast && <div className="toast-ok">✅ {toast}</div>}

      <div className="stat-grid">
        <div className="stat accent"><div className="t">总资产</div><div className="v">{money(acct?.total_assets)}</div><div className="s">初始 {money(acct?.initial_cash)}</div></div>
        <div className="stat"><div className="t">可用现金</div><div className="v fmt">{money(acct?.cash)}</div><div className="s">现金占比 {(acct && acct.total_assets) ? fmt((acct.cash / acct.total_assets) * 100) : '—'}%</div></div>
        <div className="stat"><div className="t">持仓市值</div><div className="v fmt">{money(acct?.position_value)}</div><div className="s">{positions.length} 只</div></div>
        <div className="stat">
          <div className="t">浮动盈亏</div>
          <div className={`v ${signedCls(acct?.unrealized_pnl)}`}>{money(acct?.unrealized_pnl)}</div>
          <div className={`s ${signedCls(acct?.unrealized_pnl_pct)}`}>{signedPct(acct?.unrealized_pnl_pct)}</div>
        </div>
      </div>

      <div className="trade-top">
        {/* 左：持仓明细 */}
        <div>
          <div className="sect">
            <div className="sect-title">当前持仓（{positions.length}）</div>
            {loading && !acct ? <div className="loading-block"><div className="spinner" style={{ margin: '0 auto 12px' }} />加载账户……</div> : positions.length === 0 ? (
              <div className="empty">空仓 —— 右侧搜索并买入第一只股票</div>
            ) : (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>代码 / 名称</th><th>持仓</th><th className="num">可卖</th>
                    <th className="num">成本</th><th className="num">现价</th>
                    <th className="num">市值</th><th className="num">盈亏</th><th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.code} className={setting?.code === p.code ? 'row-sel' : ''}>
                      <td>
                        <b>{p.name}</b><span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>{p.code}</span>
                        <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 2 }}>
                          {p.stop_loss_price != null ? <span className="sl-flag">止 {money(p.stop_loss_price)}</span> : null}
                          {p.take_profit_price != null ? <span className="tp-flag">盈 {money(p.take_profit_price)}</span> : null}
                          {!p.stop_loss_price && !p.take_profit_price && !p.thesis ? <span>未设止盈止损</span> : null}
                          {p.thesis ? <span style={{ marginLeft: 6 }}>{p.thesis}</span> : null}
                        </div>
                      </td>
                      <td className="num fmt">{fmt(p.quantity, 0)}</td>
                      <td className="num fmt">{fmt(p.available_qty, 0)}</td>
                      <td className="num fmt">{money(p.cost_price)}</td>
                      <td className="num fmt">{money(p.current_price)}</td>
                      <td className="num fmt">{money(p.market_value)}</td>
                      <td className={`num fmt ${signedCls(p.unrealized_pnl_pct)}`}>
                        {money(p.unrealized_pnl)}<br /><span style={{ fontSize: 11 }}>{signedPct(p.unrealized_pnl_pct)}</span>
                      </td>
                      <td>
                        <button className="btn sm buy" style={{ marginRight: 4 }} onClick={() => pickFromPos(p, 'buy')}>买</button>
                        <button className="btn sm sell" disabled={!p.available_qty} onClick={() => pickFromPos(p, 'sell')}>卖</button>
                        <button
                          className="btn sm ghost"
                          title="设置止盈/止损/持有逻辑"
                          onClick={() => setSetting(setting?.code === p.code ? null : p)}
                        >
                          ⚙ 设置
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {setting && (
              <PositionSettings
                pos={setting}
                saving={savingPos}
                onSave={savePositionSetting}
                onClose={() => setSetting(null)}
              />
            )}
          </div>
        </div>

        {/* 右：下单面板 */}
        <div className="sect order-card" style={{ position: 'sticky', top: 72 }}>
          <div className="sect-title">下单</div>
          <div className="side-switch" style={{ marginBottom: 12 }}>
            <button className={`buy ${side === 'buy' ? 'on' : ''}`} onClick={() => setSide('buy')}>买入</button>
            <button className={`sell ${side === 'sell' ? 'on' : ''}`} onClick={() => setSide('sell')}>卖出</button>
          </div>
          <div className="field">
            <label>股票</label>
            <StockSearch
              onPick={(s) => {
                setStock(s)
                setOrderErr(null)
                setToast(null)
                setQty(side === 'sell' ? String(positions.find((p) => p.code === s.code)?.available_qty || '') : '')
              }}
            />
          </div>
          {stock && (
            <div className="field" style={{ fontSize: 13, color: 'var(--text-dim)' }}>
              已选：<b style={{ color: 'var(--text)' }}>{stock.name}</b>（{stock.code}）
              <span style={{ float: 'right' }}>现价 <b style={{ color: 'var(--accent)' }}>{stock.close}</b></span>
            </div>
          )}
          <div className="field">
            <label>数量（股）</label>
            <input value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ''))} placeholder={side === 'sell' ? '最多可卖当前可用数' : '例如 100'} inputMode="numeric" />
          </div>
          {stock && n > 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-dim)', background: 'var(--card-2)', borderRadius: 8, padding: '9px 12px', marginBottom: 12, lineHeight: 1.8 }}>
              {side === 'buy' ? (
                <>成交额 <b>{money(amount)}</b> · 佣金 <b>{money(commission)}</b> · 合计 <b>{money(amount + commission)}</b></>
              ) : (
                <>成交额 <b>{money(amount)}</b> · 佣金 <b>{money(commission)}</b> · 印花税 <b>{money(tax)}</b> · 到手 <b>{money(amount - commission - tax)}</b></>
              )}
              <br />
              成交后现金 ≈ <b className={cashAfter != null && cashAfter < 0 ? 'num-down' : ''}>{money(cashAfter)}</b>
            </div>
          )}
          {orderErr && <div className="error" style={{ marginBottom: 12 }}>{orderErr}</div>}
          <button className={`btn ${side === 'buy' ? 'buy' : 'sell'}`} style={{ width: '100%' }} onClick={submit} disabled={busy || !stock || n <= 0 || (side === 'sell' && !sellOk)}>
            {busy ? '委托中……' : side === 'buy' ? `买入${stock ? ' ' + stock.name : ''}` : `卖出${stock ? ' ' + stock.name : ''}`}
          </button>
          {side === 'sell' && stock && !sellOk && (
            <div style={{ fontSize: 12, color: 'var(--warn)', marginTop: 8, textAlign: 'center' }}>
              可卖数量不足（T+1：当日买入次日才能卖）
            </div>
          )}
        </div>
      </div>

      <div className="sect">
        <div className="sect-title">成交流水（最近 {trades.length} 笔）</div>
        {trades.length === 0 ? <div className="empty">暂无成交记录</div> : (
          <table className="tbl">
            <thead>
              <tr>
                <th>时间</th><th>方向</th><th>代码 / 名称</th>
                <th className="num">价格</th><th className="num">数量</th><th className="num">金额</th>
                <th className="num">佣金</th><th className="num">印花税</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => (
                <tr key={i}>
                  <td style={{ color: 'var(--text-dim)' }}>{t.traded_at}</td>
                  <td>
                    <span className="side-dot buy" style={{ background: t.side === 'buy' ? 'var(--up)' : '#0e7490' }} />
                    <b style={{ color: t.side === 'buy' ? 'var(--up)' : '#0e7490' }}>{t.side === 'buy' ? '买入' : '卖出'}</b>
                  </td>
                  <td><b>{t.name}</b><span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>{t.code}</span></td>
                  <td className="num fmt">{money(t.price)}</td>
                  <td className="num fmt">{fmt(t.quantity, 0)}</td>
                  <td className="num fmt">{money(t.amount)}</td>
                  <td className="num fmt">{money(t.fee)}</td>
                  <td className="num fmt">{money(t.tax)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  )
}
