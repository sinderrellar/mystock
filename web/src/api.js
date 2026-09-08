// api.js —— mystock 统一控制台后端封装
// 认证：localStorage 存 session（ms_token / ms_user）。所有业务请求自动带 Authorization 头。

function resolveBase() {
  // 浏览器：取同源绝对地址，保证子路径部署也可用；无 window（Node 冒烟）回落相对路径
  if (typeof window !== 'undefined' && window.location && window.location.origin) {
    return `${window.location.origin}/api`
  }
  return '/api'
}
const BASE = resolveBase()
const TOKEN_KEY = 'ms_token'
const USER_KEY = 'ms_user'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}
export function setSession({ token, user_id, email }) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify({ user_id, email }))
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

function buildError(detail, status) {
  if (typeof detail === 'string') return new Error(detail)
  if (detail && typeof detail === 'object') {
    // FastAPI 422 校验错误：detail 是数组
    if (Array.isArray(detail)) {
      const msgs = detail.map((d) => `${d.loc?.slice(1).join('.') || '参数'}: ${d.msg}`)
      return new Error(msgs.join('；') || `请求失败 (${status})`)
    }
    return new Error(JSON.stringify(detail))
  }
  return new Error(`请求失败 (${status})`)
}

async function request(path, { method = 'GET', body, params, anon = false } = {}) {
  let url = BASE + path
  if (params) {
    const us = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== '') us.set(k, String(v))
    })
    const qs = us.toString()
    if (qs) url += `?${qs}`
  }
  const headers = {}
  const token = getToken()
  if (token && !anon) headers['Authorization'] = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    if (res.status === 401 && token && !anon && !path.startsWith('/auth/')) {
      clearSession()
      if (window.location.pathname !== '/login') window.location.href = '/login'
    }
    throw buildError(data.detail ?? data, res.status)
  }
  return data
}

// ── 认证 ──
export const authConfig = () => request('/auth/config', { anon: true })
export const devLogin = () => request('/auth/dev-login', { method: 'POST', anon: true })
export const casValidate = (ticket, service) =>
  request('/auth/validate', { params: { ticket, service }, anon: true })

// ── 模拟交易 ──
export const getAccount = () => request('/trade/account')
export const placeOrder = (code, side, quantity) =>
  request('/trade/order', { method: 'POST', body: { code, side, quantity } })
export const getTrades = (limit = 50) => request('/trade/trades', { params: { limit } })
export const searchStocks = (q) => request('/stock/search', { params: { q } })
// 观察池（独立轻量接口，即时显示）
export const getWatchlist = () => request('/trade/watchlist')
export const addWatchlist = (code, thesis = '') =>
  request('/trade/watchlist', { method: 'POST', body: { code, thesis } })
export const removeWatchlist = (code) =>
  request('/trade/watchlist/remove', { method: 'POST', body: { code } })
// 持仓止盈/止损/逻辑编辑（缺省字段不改；显式 null 清空）
export const updatePosition = (code, patch = {}) =>
  request('/trade/position/update', { method: 'POST', body: { code, ...patch } })

// ── 业务模块 ──
export const getPortfolio = () => request('/portfolio')
export const refreshPortfolio = () => request('/portfolio/refresh', { method: 'POST' })
export const getSector = () => request('/sector')
export const getBuyPlan = (top = 15) => request('/buyplan', { params: { top } })

// ── 明日选股策略预设（按用户存后端 MongoDB） ──
export const getBuyPlanPresets = () => request('/buyplan/presets')
export const saveBuyPlanPreset = (name, filters) =>
  request('/buyplan/presets', { method: 'POST', body: { name, filters } })
export const removeBuyPlanPreset = (name) =>
  request('/buyplan/presets/remove', { method: 'POST', body: { name } })

// ── 深度研究 ──
export const getResearchReports = (params = {}) => request('/research/reports', { params })
export const getResearchReport = (name) => request(`/research/reports/${encodeURIComponent(name)}`)
export const createResearchTask = (body) => request('/research/tasks', { method: 'POST', body })
export const getResearchTasks = (limit = 20) => request('/research/tasks', { params: { limit } })
export const getResearchTask = (taskId) => request(`/research/tasks/${taskId}`)
export const getResearchProcess = (sessionId) =>
  request(`/research/process/${encodeURIComponent(sessionId)}`)

// ── 投资智辩（保留，可带 token 亦无妨） ──
export const submitDebate = (idea, code, market = 'A股') =>
  request('/debate', { method: 'POST', body: { idea, code, market } })
export const refinePage = (page_json, instruction, code) =>
  request('/refine', { method: 'POST', body: { page_json, instruction, code } })
export const getDemo = () => request('/demo')
export const getData = (code, market = 'A股') =>
  request(`/data/${code}`, { params: { market } })

// ── 投资助手（多轮对话会话） ──
export const createDebateSession = (body) =>
  request('/debate/sessions', { method: 'POST', body })
export const getDebateSessions = () => request('/debate/sessions')
export const getDebateSession = (sessionId) =>
  request(`/debate/sessions/${encodeURIComponent(sessionId)}`)
export const sendDebateMessage = (sessionId, content, mode = 'normal', web = true) =>
  request(`/debate/sessions/${encodeURIComponent(sessionId)}/messages`, { method: 'POST', body: { content, mode, web } })

export default request
