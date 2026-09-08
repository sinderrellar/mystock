import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import {
  getResearchReports,
  getResearchReport,
  createResearchTask,
  getResearchTask,
  getResearchProcess,
  getDebateSessions,
  getDebateSession,
  createDebateSession,
  sendDebateMessage,
} from '../api.js'
import MarkdownView from '../components/MarkdownView.jsx'
import StockSearch from '../components/StockSearch.jsx'
import usePolling from '../hooks/usePolling.js'

const TYPE_FILTERS = [
  { key: '', label: '全部' },
  { key: 'investment_research', label: '公司研究' },
  { key: 'industry_research', label: '行业研究' },
  { key: 'stock_value_analyse', label: '估值分析' },
  { key: 'chip_analysis', label: '筹码分析' },
  { key: 'verdict', label: '综合研判' },
  { key: 'other', label: '其他' },
]

function freshTag(r) {
  if (r.validity_days == null) return null
  return r.fresh ? <span className="tag green">新鲜</span> : <span className="tag orange">过期</span>
}

export default function ResearchPage() {
  const loc = useLocation()
  const tab = loc.pathname.endsWith('/chat') ? 'chat' : 'library'
  const title = tab === 'chat' ? '投资助手' : '研究库'
  const desc = tab === 'chat'
    ? '对话式研究：发起疑问 → 多轮对话 → 按需触发后台研究（证据：研究报告 + 定量数据）'
    : '浏览 reports/ 研究报告，按类型 / 有效期筛选'

  return (
    <>
      <div className="page-head">
        <div>
          <h2>{title}</h2>
          <div className="desc">{desc}</div>
        </div>
      </div>

      {tab === 'library' ? <LibraryPanel /> : <ChatPanel />}
    </>
  )
}

/* ── 发起研究（用户输入指令入口） ── */
const TRIGGER_TYPES = [
  { key: 'investment_research', label: '公司研究', hint: '搜索并选择标的股票，绑定代码后带定量数据底座' },
  { key: 'industry_research', label: '行业研究', hint: '输入行业名，如 半导体 / 新能源 / 医药' },
  { key: 'stock_value_analyse', label: '估值分析', hint: '搜索并选择标的股票，DCF + 护城河 11 维评分' },
  { key: 'verdict', label: '综合研判', hint: '对当前组合 + 新鲜报告做三层研判（无需输入）' },
]

function TriggerBar({ onDone, onEnqueued }) {
  const [rtype, setRtype] = useState('investment_research')
  const [industry, setIndustry] = useState('')
  const [stock, setStock] = useState(null) // {code, name}
  const [busy, setBusy] = useState(false)
  const [taskId, setTaskId] = useState(null)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const { value: task } = usePolling(
    () => (taskId ? getResearchTask(taskId) : Promise.resolve(null)),
    { interval: 3000, enabled: !!taskId, shouldStop: (t) => t && ['done', 'failed', 'interrupted'].includes(t.status) }
  )

  useEffect(() => {
    if (task && ['done', 'failed', 'interrupted'].includes(task.status)) {
      if (task.status === 'done') { setMsg(`✅ 研究完成 → ${task.report || ''}`); onDone() }
      else if (task.status === 'failed') { setErr(`研究失败：${task.error}`) }
      else { setErr('任务中断（服务可能重启）') }
      setTaskId(null)
      setBusy(false)
    }
  }, [task, onDone])

  function submit() {
    setErr('')
    setMsg('')
    const body = { type: rtype }
    if (rtype === 'verdict') {
      // 无需 target/code
    } else if (rtype === 'industry_research') {
      if (!industry.trim()) { setErr('请输入行业名，如「半导体」'); return }
      body.target = industry.trim()
    } else {
      if (!stock) { setErr('请先在上方搜索并选择标的股票'); return }
      body.target = stock.name
      body.code = stock.code
    }
    setBusy(true)
    createResearchTask(body)
      .then((r) => {
        if (r.skipped) {
          setMsg(`已复用新鲜报告：${r.report}（无需重复研究）`)
          setBusy(false)
          onDone()
        } else {
          setTaskId(r.task_id)
          setMsg(`任务已入队（${r.task_id.slice(0, 8)}…），后台运行约 2-9 分钟，完成后自动刷新列表`)
          if (onEnqueued) onEnqueued()
        }
      })
      .catch((e) => { setErr(e.message); setBusy(false) })
  }

  const t = TRIGGER_TYPES.find((x) => x.key === rtype)

  return (
    <div className="sect research-trigger">
      <div className="sect-title">发起研究</div>
      <div className="rt-row">
        <div className="rt-types">
          {TRIGGER_TYPES.map((x) => (
            <button
              key={x.key}
              className={`chip ${rtype === x.key ? 'on' : ''}`}
              onClick={() => { setRtype(x.key); setErr(''); setMsg('') }}
            >
              {x.label}
            </button>
          ))}
        </div>
        <div className="rt-hint muted">{t?.hint}</div>
      </div>
      <div className="rt-row">
        {rtype === 'verdict' ? (
          <span className="muted">对当前组合全景 + 有效期内新鲜报告做三层研判（事实 / 推断 / 建议）</span>
        ) : rtype === 'industry_research' ? (
          <input
            type="text"
            className="input"
            placeholder="输入行业名，如 半导体 / 新能源 / 医药"
            value={industry}
            onChange={(e) => setIndustry(e.target.value)}
          />
        ) : (
          <StockSearch onPick={(s) => setStock(s)} placeholder="搜索并选择标的股票，如 300750 / 宁德时代" />
        )}
        <button className="btn" onClick={submit} disabled={busy}>
          {busy ? (taskId ? '运行中…' : '提交中…') : '发起研究'}
        </button>
      </div>
      {stock && rtype !== 'industry_research' && rtype !== 'verdict' && (
        <div className="rt-selected"><span className="tag blue">{stock.name} {stock.code}</span></div>
      )}
      {msg && <div className="alert-box info" style={{ marginTop: 8 }}>{msg}</div>}
      {err && <div className="error" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  )
}

/* ── 研究过程（wecode 会话提炼） ── */
function ProcessPane({ sessionId, task, onTaskDone }) {
  const [taskState, setTaskState] = useState(task)
  const [process, setProcess] = useState(null)
  const [err, setErr] = useState(null)

  // 用 ref 保持最新回调，避免 onTaskDone 每次 render 变化导致轮询 effect 反复重建
  const onTaskDoneRef = useRef(onTaskDone)
  useEffect(() => { onTaskDoneRef.current = onTaskDone })

  const live = !!task

  // live：轮询任务状态 + 同步最新 session_id（排队→运行中时 session_id 才出现）
  useEffect(() => {
    if (!task) return
    let stop = false
    const check = () =>
      getResearchTask(task.task_id)
        .then((t) => {
          if (stop) return
          setTaskState(t)
          if (['done', 'failed', 'interrupted'].includes(t.status)) onTaskDoneRef.current?.(t)
        })
        .catch(() => {})
    check()
    const id = setInterval(check, 3000)
    return () => { stop = true; clearInterval(id) }
  }, [task])

  const sid = sessionId || taskState?.session_id

  // 拉取/轮询研究过程（用最新 session_id）
  useEffect(() => {
    if (!sid) return
    let stop = false
    const loadP = () =>
      getResearchProcess(sid)
        .then((r) => { if (!stop) setProcess(r) })
        .catch((e) => { if (!stop) setErr(e.message) })
    loadP()
    if (live) {
      const id = setInterval(loadP, 3000)
      return () => { stop = true; clearInterval(id) }
    }
    return () => { stop = true }
  }, [sid, live])

  const label = live
    ? (taskState?.status === 'running' ? '后台运行中，过程实时刷新' : '排队中，等待启动')
    : '研究过程记录'

  return (
    <div>
      {live && (
        <div className="report-meta">
          <b>{taskState?.type_label || taskState?.type || '研究'}：{taskState?.target || taskState?.code || '—'}</b>
          <span className="tag blue">研究中</span>
          <span className="muted">{label}</span>
        </div>
      )}
      {!sid ? (
        <div className="empty">任务已入队，等待 wecode 启动（约数秒）…</div>
      ) : !process ? (
        <div className="loading-block"><div className="spinner" style={{ margin: '0 auto 12px' }} />正在读取研究过程…</div>
      ) : process.exists === false ? (
        <div className="empty">会话文件尚未生成，稍候…</div>
      ) : process.markdown ? (
        <MarkdownView content={process.markdown} />
      ) : (
        <div className="empty">暂无过程记录</div>
      )}
      {err && <div className="error" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  )
}

/* ── 研究库 ── */
function LibraryPanel() {
  const [reports, setReports] = useState(null)
  const [inProgress, setInProgress] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState('')
  const [q, setQ] = useState('')
  const [openTask, setOpenTask] = useState(null)      // 选中的研究中任务对象
  const [openReport, setOpenReport] = useState(null)  // 选中的报告名
  const [reportSessionId, setReportSessionId] = useState(null)
  const [showProcess, setShowProcess] = useState(false)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(null)

  const load = useCallback(() => {
    setLoading(true)
    getResearchReports()
      .then((r) => {
        setReports(r.reports || [])
        setInProgress(r.in_progress || [])
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // 有研究中任务时轮询刷新（状态/session_id 更新、完成后移入报告列表）
  const hasActive = inProgress.length > 0
  useEffect(() => {
    if (!hasActive) return
    const id = setInterval(load, 3000)
    return () => clearInterval(id)
  }, [hasActive, load])

  const openReportDetail = useCallback(async (name, sessionId) => {
    setOpenReport(name)
    setOpenTask(null)
    setShowProcess(false)
    setDetailError(null)
    setReportSessionId(sessionId != null ? sessionId : ((reports || []).find((x) => x.name === name)?.session_id || null))
    setDetailLoading(true)
    setDetail(null)
    try {
      setDetail(await getResearchReport(name))
    } catch (e) {
      setDetail({ name, type: 'other', target: '', content: `加载失败：${e.message}` })
    } finally {
      setDetailLoading(false)
    }
  }, [reports])

  function openTaskDetail(task) {
    setOpenTask(task)
    setOpenReport(null)
    setShowProcess(false)
    setDetail(null)
    setDetailError(null)
  }

  function handleTaskDone(t) {
    load()
    if (t.status === 'done' && t.report) {
      openReportDetail(t.report, t.session_id)
    } else {
      setOpenTask(null)
      setOpenReport(null)
      setDetail(null)
      setDetailError(t.status === 'failed' ? `研究失败：${t.error || '未知错误'}` : '任务已中断（服务可能重启）')
    }
  }

  const filtered = useMemo(() => {
    let list = reports || []
    if (filter) list = list.filter((r) => r.type === filter)
    if (q) {
      const nq = q.toLowerCase()
      list = list.filter((r) => `${r.name} ${r.target}`.toLowerCase().includes(nq))
    }
    return list
  }, [reports, filter, q])

  return (
    <>
      <TriggerBar onDone={load} onEnqueued={load} />
      <div className="research-grid">
        <div className="sect">
          <div className="sect-title">
            研究报告库 <span className="muted">（{filtered.length} 份{inProgress.length > 0 ? ` + ${inProgress.length} 研究中` : ''}）</span>
          </div>
          <div className="research-toolbar">
            <input type="text" className="input" placeholder="搜索标的 / 文件名…" value={q} onChange={(e) => setQ(e.target.value)} />
            <div className="chip-row">
              {TYPE_FILTERS.map((f) => (
                <button key={f.key} className={`chip ${filter === f.key ? 'on' : ''}`} onClick={() => setFilter(f.key)}>{f.label}</button>
              ))}
            </div>
          </div>

          {loading && !reports ? (
            <div className="loading-block"><div className="spinner" style={{ margin: '0 auto 12px' }} />正在扫描 reports/ 研究报告…</div>
          ) : error && !reports ? (
            <><div className="error">{error}</div><button className="btn sm ghost" onClick={load}>重试</button></>
          ) : inProgress.length === 0 && filtered.length === 0 ? (
            <div className="empty">暂无匹配的研究报告</div>
          ) : (
            <div className="report-list">
              {inProgress.map((t) => (
                <button key={t.task_id} className={`report-item ${openTask?.task_id === t.task_id ? 'on' : ''}`} onClick={() => openTaskDetail(t)}>
                  <div className="ri-head">
                    <span className="ri-type">{t.type_label || t.type}</span>
                    <span className="tag blue">研究中</span>
                    <span className="muted ri-date">{t.status === 'running' ? '运行中' : '排队中'}</span>
                  </div>
                  <div className="ri-title">{t.target || t.code || '—'}</div>
                </button>
              ))}
              {filtered.map((r) => (
                <button key={r.name} className={`report-item ${openReport === r.name ? 'on' : ''}`} onClick={() => openReportDetail(r.name)}>
                  <div className="ri-head">
                    <span className="ri-type">{r.type_label || r.type}</span>
                    {freshTag(r)}
                    {r.date && <span className="muted ri-date">{r.date}</span>}
                  </div>
                  <div className="ri-title">
                    {r.target || r.name}
                    {r.age_days != null && <span className="muted"> · {r.age_days} 天前</span>}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="sect">
          <div className="sect-title">报告详情</div>
          {openTask ? (
            <ProcessPane task={openTask} onTaskDone={handleTaskDone} />
          ) : showProcess && reportSessionId ? (
            <>
              <button className="btn sm ghost" style={{ marginBottom: 8 }} onClick={() => setShowProcess(false)}>← 返回报告</button>
              <ProcessPane sessionId={reportSessionId} />
            </>
          ) : !openReport ? (
            detailError ? <div className="error">{detailError}</div> : <div className="empty">← 点击左侧报告查看内容</div>
          ) : detailLoading ? (
            <div className="loading-block"><div className="spinner" style={{ margin: '0 auto 12px' }} />加载报告…</div>
          ) : detail ? (
            <>
              <div className="report-meta">
                <b>{detail.target || detail.name}</b>
                {detail.type_label && <span className="tag blue">{detail.type_label}</span>}
                {detail.date && <span className="muted">{detail.date}</span>}
                {reportSessionId && (
                  <button className="btn sm ghost" style={{ marginLeft: 'auto' }} onClick={() => setShowProcess(true)}>📋 研究过程记录</button>
                )}
              </div>
              <MarkdownView content={detail.content} />
            </>
          ) : null}
        </div>
      </div>
    </>
  )
}

/* ── 投资助手（多轮对话 + 话题列表 + 证据底座 + 后台研究任务） ── */
function ChatPanel() {
  const [sessions, setSessions] = useState([])
  const [currentId, setCurrentId] = useState(null)
  const [session, setSession] = useState(null) // 当前会话全文（含 messages）
  const [input, setInput] = useState('')
  const [mode, setMode] = useState('normal') // 普通分析 / 深度分析（多智能体）
  const [web, setWeb] = useState(true) // 深度分析时是否允许子代理联网
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [pollTaskId, setPollTaskId] = useState(null)
  const flowRef = useRef(null)

  const loadSessions = useCallback(() => {
    getDebateSessions()
      .then((r) => setSessions(r.sessions || []))
      .catch((e) => setError(e.message))
  }, [])

  const loadSession = useCallback(async (id) => {
    try {
      const s = await getDebateSession(id)
      setSession(s)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    loadSessions()
  }, [loadSessions])

  // 轮询当前辩论任务，直到 done/failed/interrupted
  const { value: task } = usePolling(
    () => (pollTaskId ? getResearchTask(pollTaskId) : Promise.resolve(null)),
    {
      interval: 3000,
      enabled: !!pollTaskId,
      shouldStop: (t) => t && ['done', 'failed', 'interrupted'].includes(t.status),
    }
  )

  // 任务终态 → 重载会话（拿到 assistant 回复），停止轮询
  useEffect(() => {
    if (task && ['done', 'failed', 'interrupted'].includes(task.status)) {
      if (currentId) loadSession(currentId)
      if (task.status === 'failed' && task.error) setError(`助手回复失败：${task.error}`)
      setPollTaskId(null)
      loadSessions()
    }
  }, [task, currentId, loadSession, loadSessions])

  // 自动滚动到底部
  useEffect(() => {
    if (flowRef.current) flowRef.current.scrollTop = flowRef.current.scrollHeight
  }, [session?.messages])

  function openTopic(id) {
    setCurrentId(id)
    setError(null)
    loadSession(id)
  }

  function newTopic() {
    setCurrentId(null)
    setSession(null)
    setError(null)
    setInput('')
    setPollTaskId(null)
  }

  async function send() {
    const q = input.trim()
    if (!q || busy) return
    setBusy(true)
    setError(null)
    try {
      let sid = currentId
      let taskId
      if (!sid) {
        const r = await createDebateSession({ first_message: q, mode, web })
        sid = r.session_id
        setCurrentId(sid)
        taskId = r.task_id
      } else {
        const r = await sendDebateMessage(sid, q, mode, web)
        taskId = r.task_id
      }
      setInput('')
      setPollTaskId(taskId)
      // 立即刷新会话，展示刚发出的 user 消息（assistant 回复由轮询驱动）
      await loadSession(sid)
      loadSessions()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const messages = session?.messages || []
  const ev = session?.evidence || {}
  const reportCount = ev.reports?.length || 0
  const hasData = !!(ev.data && Object.keys(ev.data || {}).length > 0 && !ev.data._error)

  return (
    <div className="chat-wrap">
      {/* 左：话题列表 */}
      <div className="chat-side">
        <button className="btn sm" onClick={newTopic}>＋ 新话题</button>
        <div className="chat-topics">
          {sessions.length === 0 ? (
            <div className="empty" style={{ padding: '12px 4px' }}>暂无话题，点「＋ 新话题」发起第一问</div>
          ) : (
            sessions.map((s) => (
              <button
                key={s.session_id}
                className={`chat-topic ${currentId === s.session_id ? 'on' : ''}`}
                onClick={() => openTopic(s.session_id)}
              >
                <div className="ct-title">{s.title}</div>
                <div className="ct-meta">
                  {s.code ? <span className="tag blue">{s.code}</span> : <span className="muted">通用话题</span>}
                  <span className="muted">{s.message_count} 条 · {s.updated_at?.slice(5, 16) || ''}</span>
                </div>
              </button>
            ))
          )}
        </div>
      </div>

      {/* 右：对话区 */}
      <div className="chat-main">
        <div className="chat-head">
          <b>{session?.title || '新话题'}</b>
          {session?.code && <span className="tag blue">{session.code}</span>}
          {session?.code && (
            <span className="muted" style={{ fontSize: 12 }}>
              证据：{reportCount} 份报告{hasData ? ' + 定量数据' : ''}
            </span>
          )}
        </div>

        <div className="chat-flow" ref={flowRef}>
          {messages.length === 0 ? (
            <div className="empty">
              向「投资助手」提问，例如：<br />
              「我的宁德时代持仓还能拿吗？」「半导体行业为什么这么弱？」<br />
              （会自动识别问题里的股票/行业，并带上相关研究报告 + 定量数据作证据）
            </div>
          ) : (
            messages.map((m, i) => (
              <div key={i} className={`chat-msg ${m.role}`}>
                <div className="chat-bubble">
                  {m.role === 'assistant' && m.meta?.mode === 'deep' && (
                    <div className="tag blue" style={{ display: 'inline-block', marginBottom: 6 }}>🧠 深度分析</div>
                  )}
                  {m.role === 'assistant' ? <MarkdownView content={m.content} /> : m.content}
                </div>
              </div>
            ))
          )}
          {(busy || pollTaskId) && (
            <div className="chat-msg assistant">
              <div className="chat-bubble typing">
                <div className="spinner" style={{ width: 16, height: 16, borderWidth: 2 }} /> {mode === 'deep' ? '正在多智能体深度分析（约 5-10 分钟）…' : '正在组织回复（后台运行，约 1-3 分钟）…'}
              </div>
            </div>
          )}
        </div>

        <div className="chat-input-bar">
          <select
            className="input"
            style={{ width: 'auto' }}
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            disabled={busy}
            title="普通分析：单轮快速回答；深度分析：多智能体多角度分析（更慢）"
          >
            <option value="normal">普通分析</option>
            <option value="deep">深度分析（多智能体）</option>
          </select>
          {mode === 'deep' && (
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 13, color: 'var(--text-dim)', whiteSpace: 'nowrap', cursor: busy ? 'not-allowed' : 'pointer' }} title="允许子代理联网搜索（关闭则只用数据底座，更快）">
              <input type="checkbox" checked={web} onChange={(e) => setWeb(e.target.checked)} disabled={busy} />
              联网
            </label>
          )}
          <textarea
            className="chat-input"
            placeholder="输入你的疑问，回车发送…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            rows={2}
          />
          <button className="btn" onClick={send} disabled={busy || !input.trim()}>
            {busy ? '…' : '发送'}
          </button>
        </div>
        {error && <div className="error" style={{ margin: '8px 0 0' }}>{error}</div>}
      </div>
    </div>
  )
}
