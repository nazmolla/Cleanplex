import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Film, BarChart2, Settings, Languages } from 'lucide-react'
import { Toaster } from './Toast'
import { useT } from '../i18n'

export default function Layout({ children }: { children: React.ReactNode }) {
  const { t, lang, setLang, toggleLang } = useT()

  const nav = [
    { to: '/', icon: LayoutDashboard, label: t('nav_dashboard') },
    { to: '/browse', icon: Film, label: t('nav_browse') },
    { to: '/analytics', icon: BarChart2, label: t('nav_analytics') },
    { to: '/settings', icon: Settings, label: t('nav_settings') },
  ]

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar — desktop only */}
      <aside className="hidden md:flex w-56 flex-shrink-0 bg-plex-dark border-e border-plex-border flex-col">
        <div className="p-5 border-b border-plex-border">
          <span className="text-plex-orange font-bold text-xl tracking-wide">Cleanplex</span>
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
                    ? 'bg-plex-orange/20 text-plex-orange'
                    : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
                }`
              }
            >
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-plex-border space-y-2">
          <div
            role="group"
            aria-label={t('lang_toggle_label')}
            className="flex p-0.5 gap-0.5 rounded-lg bg-black/25 border border-plex-border"
          >
            <button
              type="button"
              onClick={() => setLang('en')}
              aria-pressed={lang === 'en'}
              className={`flex-1 py-1 px-2 rounded-md text-xs font-medium transition-colors ${
                lang === 'en' ? 'bg-plex-orange text-black' : 'text-gray-400 hover:text-gray-100'
              }`}
            >
              {t('lang_english')}
            </button>
            <button
              type="button"
              onClick={() => setLang('ar')}
              aria-pressed={lang === 'ar'}
              className={`flex-1 py-1 px-2 rounded-md text-xs font-medium transition-colors ${
                lang === 'ar' ? 'bg-plex-orange text-black' : 'text-gray-400 hover:text-gray-100'
              }`}
            >
              {t('lang_arabic')}
            </button>
          </div>
          <div className="px-1 text-xs text-muted">v0.1.0</div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto bg-plex-darker p-4 md:p-6 pb-20 md:pb-6">
        {children}
      </main>

      {/* Bottom nav — mobile only */}
      <nav aria-label={t('nav_main')} className="fixed bottom-0 inset-x-0 z-40 flex md:hidden bg-plex-dark border-t border-plex-border pb-[env(safe-area-inset-bottom)]">
        {nav.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-1 py-2 text-xs font-medium transition-colors ${
                isActive ? 'text-plex-orange' : 'text-muted'
              }`
            }
          >
            <Icon size={20} />
            <span className="max-w-full truncate">{label}</span>
          </NavLink>
        ))}
        <button
          type="button"
          onClick={toggleLang}
          aria-label={t('lang_toggle_label')}
          className="flex-1 flex flex-col items-center justify-center gap-1 py-2 text-xs font-medium text-muted hover:text-gray-100 transition-colors"
        >
          <Languages size={20} />
          <span className="max-w-full truncate">{lang === 'ar' ? t('lang_english') : t('lang_arabic')}</span>
        </button>
      </nav>
      <Toaster />
    </div>
  )
}
