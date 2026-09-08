import React, { createContext, useContext, useState, useCallback } from 'react'
import { getToken, getUser, setSession, clearSession } from './api.js'

const AuthCtx = createContext(null)

export function AuthProvider({ children }) {
  const [state, setState] = useState(() => ({
    token: getToken(),
    user: getUser(),
  }))

  const login = useCallback((payload) => {
    setSession(payload)
    setState({ token: payload.token, user: { user_id: payload.user_id, email: payload.email } })
  }, [])

  const logout = useCallback(() => {
    clearSession()
    setState({ token: '', user: null })
  }, [])

  return (
    <AuthCtx.Provider value={{ token: state.token, user: state.user, login, logout }}>
      {children}
    </AuthCtx.Provider>
  )
}

export const useAuth = () => useContext(AuthCtx)
