import React, { useState } from 'react'

const EXAMPLES = [
  { idea: '我觉得宁德时代未来一年能涨到 500 元，翻倍空间', code: '300750', market: 'A股' },
  { idea: '茅台估值已经很便宜，现在买入长期持有', code: '600519', market: 'A股' },
  { idea: '腾讯被低估了，现在是不错的买入机会', code: '00700', market: '港股' },
]

export default function IdeaInput({ onSubmit }) {
  const [idea, setIdea] = useState('')
  const [code, setCode] = useState('')
  const [market, setMarket] = useState('A股')

  function handleSubmit(e) {
    e.preventDefault()
    if (!idea.trim() || !code.trim()) return
    onSubmit(idea.trim(), code.trim(), market)
  }

  return (
    <div className="card">
      <h2>输入你的投资想法</h2>
      <form className="idea-form" onSubmit={handleSubmit}>
        <div>
          <label>投资想法（一句话或一段话）</label>
          <textarea
            value={idea}
            onChange={(e) => setIdea(e.target.value)}
            placeholder="例如：我觉得宁德时代未来一年能涨到 500 元，翻倍空间"
            required
          />
        </div>
        <div className="row">
          <div>
            <label>股票代码</label>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="例如：300750"
              required
            />
          </div>
          <div style={{ flex: 0.5 }}>
            <label>市场</label>
            <input
              value={market}
              onChange={(e) => setMarket(e.target.value)}
              placeholder="A股 / 港股"
            />
          </div>
          <button type="submit" className="btn" disabled={!idea.trim() || !code.trim()}>
            开始智辩
          </button>
        </div>
      </form>

      <div style={{ marginTop: 16 }}>
        <label>试试这些例子：</label>
        <div className="examples">
          {EXAMPLES.map((ex) => (
            <span
              key={ex.code}
              className="example-chip"
              onClick={() => {
                setIdea(ex.idea)
                setCode(ex.code)
                setMarket(ex.market)
              }}
            >
              {ex.idea.slice(0, 18)}…（{ex.code}）
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
