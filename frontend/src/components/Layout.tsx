import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Film, Scissors, BarChart2, Users, Settings } from 'lucide-react'

const nav = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/library', icon: Film, label: 'Library' },
  { to: '/segments', icon: Scissors, label: 'Segments' },
  { to: '/analytics', icon: BarChart2, label: 'Analytics' },
  { to: '/users', icon: Users, label: 'Users' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar — desktop only */}
      <aside className="hidden md:flex w-56 flex-shrink-0 bg-plex-dark border-r border-plex-border flex-col">
        <div className="p-5 border-b border-plex-border">
          <span className="text-plex-orange font-bold text-xl tracking-wide">Cleanplex</span>
        </div>
        <nav className="flex-1 p-3 space-y-1">
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
        <div className="p-4 border-t border-plex-border text-xs text-gray-600">v0.1.0</div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto bg-plex-darker p-4 md:p-6 pb-20 md:pb-6">
        {children}
      </main>

      {/* Bottom nav — mobile only */}
      <nav className="fixed bottom-0 left-0 right-0 z-50 flex md:hidden bg-plex-dark border-t border-plex-border">
        {nav.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-1 py-2 text-xs font-medium transition-colors ${
                isActive ? 'text-plex-orange' : 'text-gray-500'
              }`
            }
          >
            <Icon size={20} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
