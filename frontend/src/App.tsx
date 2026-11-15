import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Browse from './pages/Browse'
import Analytics from './pages/Analytics'
import UsersPage from './pages/Users'
import SettingsPage from './pages/Settings'

export default function App() {
  return (
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
  )
}
