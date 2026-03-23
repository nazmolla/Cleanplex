import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { UserCircle2, ShieldCheck, ShieldOff } from 'lucide-react'

interface User {
  username: string
  thumb: string
  enabled: boolean
}

export default function UsersPage() {
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})

  useEffect(() => {
    api.get<{ users: User[] }>('/api/users').then(d => {
      setUsers(d.users)
      setLoading(false)
    })
  }, [])

  const toggle = async (username: string, enabled: boolean) => {
    setSaving(s => ({ ...s, [username]: true }))
    try {
      await api.put(`/api/users/${encodeURIComponent(username)}`, { enabled })
      setUsers(us => us.map(u => u.username === username ? { ...u, enabled } : u))
    } finally {
      setSaving(s => ({ ...s, [username]: false }))
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-gray-100 mb-2">Users</h1>
      <p className="text-sm text-gray-500 mb-6">
        Toggle content filtering per Plex account. Accounts with filtering enabled will have inappropriate scenes skipped automatically.
      </p>

      {loading ? (
        <div className="text-gray-500 text-sm">Loading users...</div>
      ) : users.length === 0 ? (
        <div className="bg-plex-card border border-plex-border rounded-xl p-8 text-center text-gray-500 text-sm">
          No users found. Make sure Plex is configured in Settings.
        </div>
      ) : (
        <div className="bg-plex-card border border-plex-border rounded-xl divide-y divide-plex-border">
          {users.map(user => (
            <div key={user.username} className="flex items-center gap-4 px-5 py-4">
              {user.thumb ? (
                <img
                  src={user.thumb}
                  alt={user.username}
                  className="w-10 h-10 rounded-full bg-plex-border object-cover flex-shrink-0"
                  onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                />
              ) : (
                <div className="w-10 h-10 rounded-full bg-plex-border flex items-center justify-center flex-shrink-0">
                  <UserCircle2 size={22} className="text-gray-500" />
                </div>
              )}

              <div className="flex-1 min-w-0">
                <p className="font-medium text-gray-100">{user.username}</p>
                <p className="text-xs text-gray-500 mt-0.5 flex items-center gap-1">
                  {user.enabled
                    ? <><ShieldCheck size={11} className="text-green-400" /> Filtering active</>
                    : <><ShieldOff size={11} className="text-gray-600" /> Filtering off</>
                  }
                </p>
              </div>

              {/* Toggle */}
              <button
                onClick={() => toggle(user.username, !user.enabled)}
                disabled={saving[user.username]}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50 ${
                  user.enabled ? 'bg-plex-orange' : 'bg-plex-border'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                    user.enabled ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
