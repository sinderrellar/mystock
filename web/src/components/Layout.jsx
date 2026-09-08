import React from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'

const NAV = [
  { to: '/portfolio', ico: '📊', label: '组合全景', group: '总览' },
  { to: '/sector', ico: '🛰️', label: '行业雷达', group: '行情' },
  { to: '/buyplan', ico: '🎯', label: '明日选股', group: '行情' },
  { to: '/trade', ico: '💰', label: '模拟交易', group: '交易' },
  { to: '/research/library', ico: '📚', label: '研究库', group: '研究' },
  { to: '/research/chat', ico: '💬', label: '投资助手', group: '研究' },
]

const GROUP_TITLE = { 总览: '总览', 行情: '行情 · 选股', 交易: '模拟交易', 研究: '深度研究' }

export default function Layout() {
  const { user, logout } = useAuth()
  const loc = useLocation()
  const nav = useNavigate()
  const cur = NAV.find((n) => loc.pathname.startsWith(n.to)) || NAV[0]

  function handleLogout() {
    logout()
    nav('/login', { replace: true })
  }

  const groups = [...new Set(NAV.map((n) => n.group))]
  return (
    <div className="console">
      <aside className="sidebar">
        <div className="brand">
          <span className="logo">MS</span> mystock
        </div>
        <div className="brand-sub">统一投资工作台</div>
        <nav className="side-nav">
          {groups.map((g) => (
            <React.Fragment key={g}>
              <div className="nav-label">{GROUP_TITLE[g] || g}</div>
              {NAV.filter((n) => n.group === g).map((n) => (
                <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? 'active' : '')}>
                  <span className="ico">{n.ico}</span>
                  {n.label}
                </NavLink>
              ))}
            </React.Fragment>
          ))}
        </nav>
        <div className="side-foot">
          <div className="u">{user?.user_id || ''}</div>
          {user?.email && (
            <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user.email}</div>
          )}
          <button onClick={handleLogout}>退出登录</button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="mod-title">
            {cur.label}
            <small>mystock · 模拟账户驱动组合全景</small>
          </div>
          <div className="userbox">
            <span>👤 {user?.user_id || ''}</span>
          </div>
        </header>
        <div className="content">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
