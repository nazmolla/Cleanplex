import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Film, BarChart2, Settings } from 'lucide-react'
import { Toaster } from './Toast'
import { useT } from '../i18n'
import HayaaBanner from './HayaaBanner'
import hayaaIcon from '../assets/hayaa-icon-64.png'

export default function Layout({ children }: { children: React.ReactNode }) {
  const { t } = useT()

  const nav = [
    { to: '/', icon: LayoutDashboard, label: t('nav_dashboard') },
    { to: '/browse', icon: Film, label: t('nav_browse') },
    { to: '/analytics', icon: BarChart2, label: t('nav_analytics') },
    { to: '/settings', icon: Settings, label: t('nav_settings') },
  ]

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar — desktop only */}
      <aside className="hidden md:flex w-56 flex-shrink-0 bg-hayaa-dark border-e border-hayaa-border flex-col">
        <div className="p-5 border-b border-hayaa-border">
          <NavLink to="/settings?section=about" className="flex items-center gap-2.5">
            <img src={hayaaIcon} alt="" className="h-8 w-8 rounded-lg" />
            <div className="leading-tight">
              <div className="text-hayaa-gold font-bold text-lg tracking-wide">{t('brand_name')}</div>
              <div className="text-xs text-muted">{t('brand_formerly')}</div>
            </div>
          </NavLink>
        </div>
        <nav aria-label={t('nav_main')} className="flex-1 p-3 space-y-1">
          {nav.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-hayaa-gold/20 text-hayaa-gold'
                    : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
                }`
              }
            >
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-hayaa-border">
          <div className="px-1 text-xs text-muted">v0.2.5</div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto bg-hayaa-darker p-4 md:p-6 pb-20 md:pb-6">
        <HayaaBanner />
        {children}
      </main>

      {/* Bottom nav — mobile only */}
      <nav aria-label={t('nav_main')} className="fixed bottom-0 inset-x-0 z-40 flex md:hidden bg-hayaa-dark border-t border-hayaa-border pb-[env(safe-area-inset-bottom)]">
        {nav.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-1 py-2 text-xs font-medium transition-colors ${
                isActive ? 'text-hayaa-gold' : 'text-muted'
              }`
            }
          >
            <Icon size={20} />
            <span className="max-w-full truncate">{label}</span>
          </NavLink>
        ))}
      </nav>
      <Toaster />
    </div>
  )
}
