import React from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth.jsx'
import Layout from './components/Layout.jsx'
import LoginPage from './pages/LoginPage.jsx'
import PortfolioPage from './pages/PortfolioPage.jsx'
import SectorPage from './pages/SectorPage.jsx'
import BuyPlanPage from './pages/BuyPlanPage.jsx'
import TradePage from './pages/TradePage.jsx'
import DebatePage from './pages/DebatePage.jsx'
import ResearchPage from './pages/ResearchPage.jsx'

function RequireAuth({ children }) {
  const { token } = useAuth()
  if (!token) return <Navigate to="/login" replace />
  return children
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/" element={<Navigate to="/portfolio" replace />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/sector" element={<SectorPage />} />
          <Route path="/buyplan" element={<BuyPlanPage />} />
          <Route path="/trade" element={<TradePage />} />
          <Route path="/debate" element={<DebatePage />} />
          <Route path="/research" element={<Navigate to="/research/library" replace />} />
          <Route path="/research/library" element={<ResearchPage />} />
          <Route path="/research/chat" element={<ResearchPage />} />
          <Route path="*" element={<Navigate to="/portfolio" replace />} />
        </Route>
      </Routes>
    </AuthProvider>
  )
}
