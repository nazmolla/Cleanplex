import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { api } from '../api/client'
import { useLiveRefresh } from '../api/events'
import { UserCircle2, ShieldCheck, ShieldOff, ChevronDown, ChevronRight } from 'lucide-react'
import CategoryMatrix from '../components/CategoryMatrix'
import { useT } from '../i18n'

interface User {
  username: string
  thumb: string
  enabled: boolean
}

/** Per-account filtering, shown as the Users section of Settings (#118). */
export function UsersPanel() {
  const { t } = useT()
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  // Only the expanded user's matrix is mounted, so switching users does not leave
  // several category requests in flight at once.
  const [expanded, setExpanded] = useState<string | null>(null)

  // Reloaded when any user's filter changes elsewhere, e.g. another tab (#89).
  const [liveTick, setLiveTick] = useState(0)
  useLiveRefresh(['users_changed'], () => setLiveTick(n => n + 1))
  useEffect(() => {
    api.get<{ users: User[] }>('/api/users').then(d => {
      setUsers(d.users)
      setLoading(false)
    })
  }, [liveTick])

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
    <div>
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide mb-4">{t('users_title')}</h2>
      <p className="text-xs text-muted mb-4">
        {t('users_subtitle')}
      </p>

      {loading ? (
        <div className="text-muted text-sm">{t('users_loading')}</div>
      ) : users.length === 0 ? (
        <div className="bg-hayaa-darker border border-hayaa-border rounded-xl p-8 text-center text-muted text-sm">
          {t('users_empty')}
        </div>
      ) : (
        <div className="bg-hayaa-darker border border-hayaa-border rounded-xl divide-y divide-hayaa-border">
          {users.map(user => (
            <div key={user.username}>
            {/* The whole row opens the category levels; the switch and chevron stop the click themselves. */}
            <div
              className="flex items-center gap-4 px-5 py-4 cursor-pointer hover:bg-white/[0.03]"
              onClick={() => setExpanded(e => (e === user.username ? null : user.username))}
            >
              {user.thumb ? (
                <img
                  src={user.thumb}
                  alt={user.username}
                  className="w-10 h-10 rounded-full bg-hayaa-border object-cover flex-shrink-0"
                  onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
                />
              ) : (
                <div className="w-10 h-10 rounded-full bg-hayaa-border flex items-center justify-center flex-shrink-0">
                  <UserCircle2 size={22} className="text-muted" />
                </div>
              )}

              <button
                onClick={e => { e.stopPropagation(); setExpanded(x => (x === user.username ? null : user.username)) }}
                aria-expanded={expanded === user.username}
                className="text-gray-400 hover:text-gray-300"
                aria-label={expanded === user.username ? t('users_hide_categories') : t('users_show_categories')}
              >
                {expanded === user.username ? <ChevronDown size={16} /> : <ChevronRight size={16} className="rtl:-scale-x-100" />}
              </button>

              <div className="flex-1 min-w-0">
                <p className="font-medium text-gray-100">{user.username}</p>
                <p className="text-xs text-muted mt-0.5 flex items-center gap-1">
                  {user.enabled
                    ? <><ShieldCheck size={11} className="text-green-400" /> {t('users_filtering_active')}</>
                    : <><ShieldOff size={11} className="text-faint" /> {t('users_filtering_off')}</>
                  }
                </p>
              </div>

              {/* Toggle */}
              <button
                role="switch"
                aria-checked={user.enabled}
                aria-label={t('users_toggle_aria', { name: user.username })}
                onClick={e => { e.stopPropagation(); toggle(user.username, !user.enabled) }}
                disabled={saving[user.username]}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors disabled:opacity-50 ${
                  user.enabled ? 'bg-hayaa-gold' : 'bg-hayaa-border'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                    user.enabled ? 'translate-x-6 rtl:-translate-x-6' : 'translate-x-1 rtl:-translate-x-1'
                  }`}
                />
              </button>
            </div>

            {expanded === user.username && (
              <div className="bg-plex-bg/40 border-t border-hayaa-border">
                <CategoryMatrix username={user.username} enabled={user.enabled} />
              </div>
            )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}


/** The old /users address: Users now lives under Settings. */
export default function UsersPage() {
  return <Navigate to="/settings?section=users" replace />
}
