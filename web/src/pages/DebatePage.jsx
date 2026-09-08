import React, { useState } from 'react'
import IdeaInput from '../components/IdeaInput.jsx'
import DebateView from '../components/DebateView.jsx'
import PageView from '../components/PageView.jsx'
import RefinePanel from '../components/RefinePanel.jsx'
import { submitDebate, refinePage, getDemo } from '../api.js'

const STAGES = ['输入想法', '多流派辩论', '投资页面', '自然语言微调']

export default function DebatePage() {
  const [stage, setStage] = useState('input') // input | debating | result
  const [code, setCode] = useState('')
  const [result, setResult] = useState(null)
  const [page, setPage] = useState(null)
  const [error, setError] = useState(null)
  const [refining, setRefining] = useState(false)

  async function handleSubmit(nextIdea, nextCode, nextMarket) {
    setCode(nextCode)
    setError(null)
    setStage('debating')
    setResult(null)
    setPage(null)
    try {
      const data = await submitDebate(nextIdea, nextCode, nextMarket)
      setResult(data)
      setPage(data.page)
      setStage('result')
    } catch (e) {
      setError(e.message)
      setStage('input')
    }
  }

  async function handleRefine(instruction) {
    if (!page) return
    setRefining(true)
    setError(null)
    try {
      const newPage = await refinePage(page, instruction, code)
      setPage(newPage)
    } catch (e) {
      setError(e.message)
    } finally {
      setRefining(false)
    }
  }

  async function handleLoadDemo() {
    setError(null)
    setStage('debating')
    try {
      const data = await getDemo()
      setResult(data)
      setPage(data.page)
      setStage('result')
    } catch (e) {
      setError(e.message)
      setStage('input')
    }
  }

  const stepIndex = stage === 'input' ? 0 : stage === 'debating' ? 1 : 2

  return (
    <div className="debate-wrap">
      <div className="page-head">
        <div>
          <h2>投资智辩</h2>
          <div className="desc">输入一条投资想法 → 价值 / 成长 / 趋势三流派用真实财经数据辩论 → 生成可验证投资页面</div>
        </div>
      </div>

      <div className="steps">
        {STAGES.map((s, i) => (
          <div
            key={s}
            className={`step ${i === stepIndex ? 'active' : ''} ${i < stepIndex ? 'done' : ''}`}
          >
            {s}
          </div>
        ))}
      </div>

      {error && <div className="error">{error}</div>}

      {stage === 'input' && (
        <>
          <IdeaInput onSubmit={handleSubmit} />
          <div className="card" style={{ textAlign: 'center' }}>
            <button className="btn secondary" onClick={handleLoadDemo}>
              🚀 查看演示（宁德时代 300750 · 已跑好的三流派辩论）
            </button>
            <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8 }}>
              演示数据来自之前真实跑过的宁德时代辩论，秒开；输入想法则触发实时辩论（约 2-5 分钟）
            </div>
          </div>
        </>
      )}

      {stage === 'debating' && (
        <div className="loading">
          <div className="spinner" />
          <span>正在组织三流派辩论（价值 / 成长 / 趋势）与事实核查…… 通常需要 2-5 分钟</span>
        </div>
      )}

      {stage === 'result' && result && (
        <>
          <DebateView result={result} />
          <PageView page={page} />
          <div className="card">
            <h2>自然语言微调</h2>
            <RefinePanel onRefine={handleRefine} refining={refining} />
          </div>
          <div className="card" style={{ textAlign: 'center' }}>
            <button className="btn secondary" onClick={handleLoadDemo}>
              🔄 再看一次演示
            </button>
            <button
              className="btn ghost"
              style={{ marginLeft: 8 }}
              onClick={() => {
                setStage('input')
                setResult(null)
                setPage(null)
              }}
            >
              ← 输入新想法
            </button>
          </div>
        </>
      )}
    </div>
  )
}
