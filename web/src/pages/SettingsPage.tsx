import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { apiFetch } from '../lib/api'
import type { SetupState } from '../lib/types'
import { MacFolder } from './Wizard/MacFolder'
import { Ssd } from './Wizard/Ssd'
import { SwitchPhone } from './Wizard/SwitchPhone'

// The full Settings screens are milestone M9; until then this page holds the setup entry points.
export default function SettingsPage() {
  const setup = useQuery({ queryKey: ['setup', 'state'], queryFn: () => apiFetch<SetupState>('/api/setup/state') })
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="mt-1 text-neutral-600 dark:text-neutral-400">The Settings screens arrive in milestone M9.</p>
      </header>
      <section aria-label="Backup locations" className="flex flex-col gap-8">
        {setup.data && <MacFolder setup={setup.data} settings next={() => void setup.refetch()} />}
        <Ssd settings next={() => undefined} />
      </section>
      <section aria-labelledby="setup-title" className="flex flex-col gap-2">
        <h2 id="setup-title" className="text-lg font-semibold">
          Setup
        </h2>
        <p>Go through the first-time setup again: USB debugging, adb, the backup folder, the SSD and what to back up.</p>
        <p>
          <Link to="/setup" className="underline">
            Run the setup again
          </Link>
        </p>
      </section>
      <section aria-labelledby="phone-title" className="flex flex-col gap-2">
        <h2 id="phone-title" className="text-lg font-semibold">
          Phone
        </h2>
        <SwitchPhone />
      </section>
    </div>
  )
}
