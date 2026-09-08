import React from 'react'

const TAG_MAP = {
  用户观点: 'user',
  AI分析: 'ai',
  客观数据: 'data',
}

export default function PageView({ page }) {
  if (!page) return null

  return (
    <div className="card">
      <h2>投资页面</h2>
      {page.title && <div className="page-title">{page.title}</div>}

      {page.core_judgment && (
        <div className="core-judgment">{page.core_judgment}</div>
      )}

      {page.summary && (
        <p style={{ fontSize: 14, color: 'var(--text-dim)' }}>{page.summary}</p>
      )}

      {(page.bull_case || page.bear_case) && (
        <div className="two-col" style={{ marginTop: 16 }}>
          <div className="bull">
            <h3>📈 支持要点</h3>
            <ul>
              {(page.bull_case || []).map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          </div>
          <div className="bear">
            <h3>📉 风险要点</h3>
            <ul>
              {(page.bear_case || []).map((b, i) => (
                <li key={i}>{b}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {(page.key_facts || []).length > 0 && (
        <>
          <h3>关键事实（可验证）</h3>
          <div className="facts-grid">
            {page.key_facts.map((f, i) => (
              <div key={i} className="fact-card">
                <div className="fc-label">{f.label}</div>
                <div className="fc-value">{f.value}</div>
                <div className="fc-source">{f.source}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {(page.risks || []).length > 0 && (
        <>
          <h3>风险提示</h3>
          <ul className="risk-list">
            {page.risks.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </>
      )}

      {page.source_tags && (
        <>
          <h3>内容来源标注</h3>
          <div className="source-tags">
            {Object.entries(page.source_tags).map(([tag, items]) => (
              <div key={tag} className={`source-tag ${TAG_MAP[tag] || 'ai'}`}>
                <span className="st-title">{tag}</span>
                <span className="st-items">
                  {Array.isArray(items) ? items.join('、') : items}
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      {page.disclaimer && <div className="disclaimer">{page.disclaimer}</div>}
    </div>
  )
}
