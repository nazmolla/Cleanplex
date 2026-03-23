import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Film, Scissors, Users, Settings } from 'lucide-react'

const nav = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/library', icon: Film, label: 'Library' },
  { to: '/segments', icon: Scissors, label: 'Segments' },
  { to: '/users', icon: Users, label: 'Users' },
  { to: '/settings', icon: Settings, label: 'Settings' },
]

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 bg-plex-dark border-r border-plex-border flex flex-col">
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
      <main className="flex-1 overflow-y-auto bg-plex-darker p-6">
        {children}
      </main>
    </div>
  )
}
