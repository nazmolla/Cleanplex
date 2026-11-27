import { useEffect, useState } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { api } from './api/client'
import { I18nProvider } from './i18n'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Browse from './pages/Browse'
import Analytics from './pages/Analytics'
import UsersPage from './pages/Users'
import SettingsPage from './pages/Settings'
import Setup from './pages/Setup'

/** Sends a fresh install to the setup wizard (#245) until it finishes. */
function useSetupDone(): boolean | null {
  const [done, setDone] = useState<boolean | null>(null)
  useEffect(() => {
    api.get<{ done: boolean }>('/api/setup/status').then(s => setDone(s.done)).catch(() => setDone(true))
  }, [])
  return done
}

export default function App() {
  const { pathname } = useLocation()
  const setupDone = useSetupDone()
  if (pathname === '/setup') {
    return <I18nProvider><Setup /></I18nProvider>
  }
  if (setupDone === false) return <Navigate to="/setup" replace />
  return (
    <I18nProvider>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/browse" element={<Browse />} />
          {/* The three pages Browse replaced (#207): old links and bookmarks land on the matching view. */}
          <Route path="/library" element={<Navigate to="/browse" replace />} />
          <Route path="/segments" element={<Navigate to="/browse?view=segments" replace />} />
          <Route path="/markers" element={<Navigate to="/browse?view=markers" replace />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </Layout>
    </I18nProvider>
  )
}
