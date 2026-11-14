import { NavLink } from 'react-router'

const AREAS = [
  { to: '/backup', label: 'Backup' },
  { to: '/cleanup', label: 'Cleanup' },
  { to: '/browse', label: 'Browse' },
  { to: '/slate', label: 'Clean slate' },
  { to: '/apps', label: 'Apps' },
  { to: '/health', label: 'Health' },
  { to: '/archive', label: 'Archive' },
  { to: '/quarantine', label: 'Quarantine' },
  { to: '/logs', label: 'Logs' },
  { to: '/settings', label: 'Settings' },
]

export function Sidebar() {
  return (
    <nav
      aria-label="Main"
      className="w-[200px] shrink-0 border-r border-neutral-200 p-3 dark:border-neutral-800"
    >
      <ul className="flex flex-col gap-1">
        {AREAS.map(({ to, label }) => (
          <li key={to}>
            <NavLink
              to={to}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm ${
                  isActive
                    ? 'bg-neutral-200 font-medium dark:bg-neutral-800'
                    : 'hover:bg-neutral-100 dark:hover:bg-neutral-900'
                }`
              }
            >
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
