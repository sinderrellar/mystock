import React, { useEffect, useRef, useState } from 'react'
import { searchStocks } from '../api.js'

export default function StockSearch({ onPick, placeholder = '输入代码或名称搜索，如 300750 / 宁德' }) {
  const [q, setQ] = useState('')
  const [items, setItems] = useState([])
  const [open, setOpen] = useState(false)
  const timer = useRef()
  const boxRef = useRef()

  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => {
    function onDoc(e) {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  useEffect(() => {
    if (!q.trim()) {
      setItems([])
      return
    }
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      searchStocks(q.trim())
        .then((r) => {
          setItems(r.items || [])
          setOpen(true)
        })
        .catch(() => setItems([]))
    }, 250)
  }, [q])

  function choose(s) {
    setQ(`${s.name}  ${s.code}`)
    setOpen(false)
    if (onPick) onPick(s)
  }

  return (
    <div className="search-box" ref={boxRef}>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => items.length && setOpen(true)}
        placeholder={placeholder}
      />
      {open && items.length > 0 && (
        <div className="search-drop">
          {items.map((s) => (
            <div key={s.code} className="s-item" onMouseDown={(e) => { e.preventDefault(); choose(s) }}>
              <span>
                <b>{s.name}</b>
                <span style={{ color: 'var(--text-dim)', marginLeft: 8 }}>{s.code}</span>
                <span style={{ color: 'var(--text-dim)', marginLeft: 8, fontSize: 12 }}>{s.industry || ''}</span>
              </span>
              <span style={{ color: 'var(--text-dim)' }}>
                现价 <b style={{ color: 'var(--text)' }}>{s.close}</b>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
