import React, { useState } from 'react'

const SCHOOLS = [
  { key: 'value', name: '价值投资派', en: 'Value' },
  { key: 'growth', name: '成长投资派', en: 'Growth' },
  { key: 'trend', name: '趋势投资派', en: 'Trend' },
]

function ArgumentList({ items, kind }) {
  if (!items || items.length === 0) return <p style={{ color: 'var(--text-dim)' }}>无</p>
  return (
    <div className="arg-list">
      {items.map((a, i) => (
        <div key={i} className={`arg-item ${kind}`}>
          <div className="point">{a.point}</div>
          {a.evidence && a.evidence.length > 0 && (
            <ul className="evidence">
              {a.evidence.map((e, j) => (
                <li key={j}>{e}</li>
              ))}
            </ul>
          )}
          {a.source && <div className="source">来源：{a.source}</div>}
        </div>
      ))}
    </div>
  )
}

export default function DebateView({ result }) {
  const [active, setActive] = useState('value')
  const school = result.schools.find((s) => s.school === active)
  const idea = result.idea || {}
  const factCheck = result.fact_check || {}
  const synthesis = result.synthesis || {}

  return (
    <>
      {/* 想法解析 */}
      <div className="card">
        <h2>想法解析</h2>
        <div className="thesis-box">
          <div className="label">核心判断</div>
          <div>{idea.core_claim || idea.original}</div>
        </div>
        {(idea.assumptions || []).length > 0 && (
          <>
            <h3>隐含假设</h3>
            <ul className="syn-list">
              {idea.assumptions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </>
        )}
        {(idea.falsifiable_points || []).length > 0 && (
          <>
            <h3>可证伪点（什么条件会证明它成立 / 破产）</h3>
            <ul className="syn-list">
              {idea.falsifiable_points.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          </>
        )}
        {idea.time_horizon && (
          <p style={{ fontSize: 13, color: 'var(--text-dim)' }}>
            时间维度：{idea.time_horizon}
          </p>
        )}
      </div>

      {/* 三流派辩论 */}
      <div className="card">
        <h2>三流派辩论</h2>
        <div className="tabs">
          {SCHOOLS.map((s) => (
            <div
              key={s.key}
              className={`tab ${s.key} ${active === s.key ? 'active' : ''}`}
              onClick={() => setActive(s.key)}
            >
              {s.name}
              <span className="school-en">{s.en}</span>
            </div>
          ))}
        </div>

        {school && (
          <div>
            <div className="thesis-box">
              <div className="label">{school.name}的核心判断</div>
              <div>{school.thesis}</div>
            </div>

            <h3 style={{ color: 'var(--value)' }}>✅ 支持该想法的论据</h3>
            <ArgumentList items={school.support} kind="support" />

            <h3 style={{ color: 'var(--danger)' }}>⚠️ 反对 / 质疑</h3>
            <ArgumentList items={school.oppose} kind="oppose" />

            {school.key_metric && (
              <div className="key-metric">
                <div className="km-title">该流派最看重的关键指标</div>
                <div className="km-value">
                  {Object.entries(school.key_metric).map(([k, v]) => (
                    <span key={k}>
                      {k}：{typeof v === 'string' ? v : JSON.stringify(v)}
                    </span>
                  ))}
                </div>
                {school.key_metric.解读 && (
                  <div className="km-note">{school.key_metric.解读}</div>
                )}
              </div>
            )}

            {school.confidence && (
              <span className={`confidence-badge ${school.confidence}`}>
                确信度：{school.confidence}
              </span>
            )}
          </div>
        )}
      </div>

      {/* 事实核查 */}
      <div className="card">
        <h2>财经数据验证（事实核查）</h2>
        {(factCheck.verified || []).map((f, i) => (
          <div key={i} className="fact-item">
            <span className={`fact-status ${f.status}`}>{f.status}</span>
            <div>
              <div>{f.claim}</div>
              {f.source && (
                <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>来源：{f.source}</div>
              )}
            </div>
          </div>
        ))}
        {(factCheck.corrections || []).length > 0 && (
          <>
            <h3 style={{ color: 'var(--warn)' }}>修正记录</h3>
            {factCheck.corrections.map((c, i) => (
              <div key={i} className="fact-item">
                <div>
                  <div>
                    <s style={{ color: 'var(--text-dim)' }}>{c.original}</s>
                  </div>
                  <div style={{ color: 'var(--warn)' }}>→ {c.correct}</div>
                  {c.reason && (
                    <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>{c.reason}</div>
                  )}
                </div>
              </div>
            ))}
          </>
        )}
      </div>

      {/* 综合研判 */}
      <div className="card">
        <h2>关键分歧汇总</h2>
        <div className="synthesis-grid">
          <div className="syn-col consensus">
            <h3>三派共识</h3>
            <ul className="syn-list">
              {(synthesis.consensus || []).map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
          <div className="syn-col disagreement">
            <h3>三派分歧</h3>
            <ul className="syn-list">
              {(synthesis.disagreement || []).map((d, i) => (
                <li key={i}>{d}</li>
              ))}
            </ul>
          </div>
        </div>

        {synthesis.disagreement_root && (
          <div className="verdict">
            <div className="label">分歧根因</div>
            <div>{synthesis.disagreement_root}</div>
          </div>
        )}
        {synthesis.verdict && (
          <div className="verdict">
            <div className="label">综合研判（事实层 → 推断层 → 建议研究方向）</div>
            <div>{synthesis.verdict}</div>
          </div>
        )}
      </div>
    </>
  )
}
