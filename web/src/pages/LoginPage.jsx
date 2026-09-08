import React, { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import { authConfig, casValidate, devLogin } from '../api.js'

export default function LoginPage() {
  const { token, login } = useAuth()
  const nav = useNavigate()
  const loc = useLocation()
  const [cfg, setCfg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (token) {
      nav('/portfolio', { replace: true })
      return
    }
    authConfig()
      .then(setCfg)
      .catch((e) => setError(e.message))

    // 真实 CAS 回跳：/login?ticket=ST-xxx
    const ticket = new URLSearchParams(loc.search).get('ticket')
    if (ticket) {
      const service = window.location.origin + '/login'
      setBusy(true)
      casValidate(ticket, service)
        .then((d) => {
          login(d)
          nav('/portfolio', { replace: true })
        })
        .catch((e) => setError(`CAS 登录失败：${e.message}`))
        .finally(() => setBusy(false))
    }
  }, [token]) // eslint-disable-line react-hooks/exhaustive-deps

  function doDevLogin() {
    setBusy(true)
    setError('')
    devLogin()
      .then((d) => {
        login(d)
        nav('/portfolio', { replace: true })
      })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }

  function goCas() {
    if (!cfg?.cas_login_url) return
    const service = encodeURIComponent(window.location.origin + '/login')
    window.location.href = `${cfg.cas_login_url}?service=${service}`
  }

  const isDev = Boolean(cfg?.dev_user)
  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-brand">
          <span className="logo">MS</span>
          <b>mystock 投资工作台</b>
        </div>
        <div className="login-sub">
          组合全景 · 行业雷达 · 明日选股 · 模拟交易 · 投资智辩
          <br />
          登录采用新浪统一身份认证（CAS SSO）
        </div>
        {error && <div className="error">{error}</div>}
        {!cfg && !busy ? (
          <div className="loading">
            <div className="spinner" />
            <span>正在确认登录方式……</span>
          </div>
        ) : busy ? (
          <div className="loading">
            <div className="spinner" />
            <span>正在登录……</span>
          </div>
        ) : isDev ? (
          <button className="btn" onClick={doDevLogin}>
            ⚡ 开发模式登录（{cfg.dev_user}）
          </button>
        ) : (
          <button className="btn" onClick={goCas}>
            新浪统一身份登录 →
          </button>
        )}
        {!isDev && (
          <div className="login-hint">
            授权页面可能要求允许跳转，登录后自动回到本系统
            <br />
            会话有效期 12 小时
          </div>
        )}
        {isDev && <div className="login-hint">当前为本地联调模式，已注入 DEV_USER，跳过真实 CAS</div>}
      </div>
    </div>
  )
}
