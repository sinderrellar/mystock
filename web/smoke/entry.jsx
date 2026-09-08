// 渲染冒烟：真实挂载 App 路由，fetch 打真实后端（localhost:8000），轮询断言关键文案。
// 运行：node web/smoke/run.mjs   （后端需已启动 DEV_USER=zhongyue3）
import React, { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from '../src/auth.jsx'
import Layout from '../src/components/Layout.jsx'
import LoginPage from '../src/pages/LoginPage.jsx'
import PortfolioPage from '../src/pages/PortfolioPage.jsx'
import SectorPage from '../src/pages/SectorPage.jsx'
import BuyPlanPage from '../src/pages/BuyPlanPage.jsx'
import TradePage from '../src/pages/TradePage.jsx'
import DebatePage from '../src/pages/DebatePage.jsx'
import ResearchPage from '../src/pages/ResearchPage.jsx'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function RequireAuth({ children }) {
  const { token } = useAuth()
  if (!token) return <Navigate to="/login" replace />
  return children
}

function AppTest({ initialPath }) {
  return (
    <AuthProvider>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route path="/portfolio" element={<PortfolioPage />} />
            <Route path="/sector" element={<SectorPage />} />
            <Route path="/buyplan" element={<BuyPlanPage />} />
            <Route path="/trade" element={<TradePage />} />
            <Route path="/debate" element={<DebatePage />} />
            <Route path="/research/library" element={<ResearchPage />} />
            <Route path="/research/chat" element={<ResearchPage />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AuthProvider>
  )
}

let root = null
function mount(path) {
  if (root) {
    root.unmount()
    root = null
  }
  document.body.innerHTML = '<div id="root"></div>'
  root = createRoot(document.getElementById('root'))
  root.render(<AppTest initialPath={path} />)
}

const bodyHas = (s) => document.body.textContent.includes(s)

async function waitFor(fn, label, timeoutMs = 70000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    try {
      if (fn()) return
    } catch (e) {
      /* keep polling */
    }
    await sleep(300)
  }
  throw new Error(`[超时] 等待「${label}」失败。当前文本片段：${document.body.textContent.slice(0, 300)}`)
}

const results = []
function check(name, cond, extra = '') {
  results.push([name, !!cond])
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : '  ' + extra}`)
}

export async function main() {
  // ── 0. 登录页（未登录态） ──
  localStorage.clear()
  mount('/portfolio') // 应被 RequireAuth 弹到 /login
  await waitFor(() => bodyHas('开发模式登录'), '登录页出现开发模式登录按钮')
  await sleep(400)
  check('未登录访问受保护页被重定向到登录页', bodyHas('新浪统一身份认证') || bodyHas('开发模式登录'))

  // ── 1. 开发模式登录 ──
  localStorage.setItem('ms_token', 'smoke-dev')
  localStorage.setItem('ms_user', JSON.stringify({ user_id: 'zhongyue3', email: 'zhongyue3@sina.com.cn' }))

  // ── 2. 组合全景 ──
  mount('/portfolio')
  await waitFor(() => bodyHas('浮动盈亏'), '组合全景统计加载', 90000)
  await sleep(300)
  check('组合全景统计卡渲染', bodyHas('总资产') && bodyHas('浮动盈亏') && bodyHas('持仓明细'))
  check('组合全景展示持仓(宁德时代)', bodyHas('宁德时代') || bodyHas('暂无持仓'))
  const portfolioText = document.body.textContent

  // ── 3. 模拟交易 ──
  mount('/trade')
  await waitFor(() => bodyHas('当前持仓'), '模拟交易页加载', 30000)
  await sleep(300)
  check('模拟交易统计与持仓渲染', bodyHas('可用现金') && bodyHas('成交流水'))
  check('下单面板渲染(买入按钮)', bodyHas('买入') && bodyHas('初始虚拟资金'))
  const tradeText = document.body.textContent

  // ── 4. 行业雷达 ──
  mount('/sector')
  await waitFor(() => !bodyHas('正在生成') && bodyHas('行业动量热力'), '行业雷达热力加载', 40000)
  await sleep(300)
  check('行业雷达渲染(热力+苏醒)', bodyHas('苏醒雷达') && bodyHas('龙头'))

  // ── 5. 明日选股 ──
  mount('/buyplan')
  await waitFor(() => bodyHas('分层漏斗'), '明日选股漏斗加载', 40000)
  await sleep(300)
  check('明日选股漏斗+筛选+表格渲染', bodyHas('分层漏斗') && bodyHas('筛选面板') && bodyHas('策略标签'))

  // ── 6. 投资智辩 ──
  mount('/debate')
  await waitFor(() => bodyHas('输入你的投资想法'), '投资智辩加载')
  await sleep(300)
  check('投资智辩输入与演示渲染', bodyHas('查看演示'))

  // ── 7. 深度研究（研究库） ──
  mount('/research/library')
  await waitFor(() => bodyHas('研究报告库'), '研究库页加载')
  await sleep(300)
  check('研究库列表与类型过滤渲染', bodyHas('公司研究') && bodyHas('行业研究') && bodyHas('投资助手'))

  // ── 8. 投资助手（多轮对话） ──
  mount('/research/chat')
  await waitFor(() => bodyHas('新话题'), '投资助手页加载')
  await sleep(300)
  check('投资助手话题列表与输入框渲染', bodyHas('新话题') && bodyHas('输入你的疑问'))

  // 汇总
  const fails = results.filter(([, ok]) => !ok)
  console.log(`\n==== smoke 结果：${results.length - fails.length}/${results.length} PASS ====`)
  if (fails.length) {
    console.log('失败项：', fails.map(([n]) => n).join('；'))
    process.exitCode = 1
  }
  return { ok: fails.length === 0, portfolioHasName: portfolioText.includes('宁德时代'), tradeText: tradeText.includes('成交流水') }
}
