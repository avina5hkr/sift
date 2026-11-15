import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, Navigate, NavLink, useParams } from 'react-router'
import { BTN, MUTED } from '../components/apps/ui'
import { FieldRow } from '../components/settings/fields'
import { fromForm, idOf, sameForm, toForm } from '../lib/settingsForm'
import type { Form, SettingField } from '../lib/settingsForm'
import { SpeedTest } from '../components/settings/SpeedTest'
import { WifiSection } from '../components/settings/WifiSection'
import { apiFetch } from '../lib/api'
import { useStatus } from '../lib/useStatus'
import { PAGE_TITLES } from '../lib/settingsLabels'
import type { SetupState } from '../lib/types'
import { MacFolder } from './Wizard/MacFolder'
import { Ssd } from './Wizard/Ssd'
import { StorageLocations } from '../components/settings/StorageLocations'
import { SwitchPhone } from './Wizard/SwitchPhone'
import { plain } from './Wizard/request'

type SettingsData = { pages: Record<string, SettingField[]>; warnings: string[] }

const BASE = 'rounded-md px-3 py-2 text-sm'

export default function SettingsPage() {
  const { page = 'general' } = useParams()
  if (!PAGE_TITLES.some(([id]) => id === page)) return <Navigate to="/settings/general" replace />
  const title = PAGE_TITLES.find(([id]) => id === page)?.[1] ?? ''
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-6 md:flex-row">
      <nav aria-label="Settings pages" className="shrink-0 md:w-44">
        <ul className="flex flex-wrap gap-1 md:flex-col">
          {PAGE_TITLES.map(([id, label]) => (
            <li key={id}>
              <NavLink
                to={`/settings/${id}`}
                className={({ isActive }) =>
                  `block ${BASE} ${isActive ? 'bg-neutral-200 font-medium dark:bg-neutral-800' : 'hover:bg-neutral-100 dark:hover:bg-neutral-900'}`
                }
              >
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <div className="min-w-0 flex-1">
        <h1 className="text-2xl font-semibold">{title}</h1>
        <PageForm key={page} page={page} />
      </div>
    </div>
  )
}

function PageForm({ page }: { page: string }) {
  const qc = useQueryClient()
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => apiFetch<SettingsData>('/api/settings'), retry: false })
  const [draft, setDraft] = useState<Record<string, Form>>({})
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const fields = settings.data?.pages[page] ?? []
  const byId = new Map(fields.map((f) => [idOf(f), f]))

  const save = useMutation({
    mutationFn: (changes: { section: string; key: string; value: unknown }[]) =>
      apiFetch<SettingsData>('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes }),
      }),
    onSuccess: (data) => {
      qc.setQueryData(['settings'], data)
      void qc.invalidateQueries({ queryKey: ['status'] })
      setDraft({})
      setSaved(true)
    },
    onError: (e) => setError(plain(e)),
  })

  const change = (f: SettingField, next: Form) => {
    setSaved(false)
    setDraft((d) => {
      const copy = { ...d }
      if (sameForm(next, toForm(f, f.value))) delete copy[idOf(f)]
      else copy[idOf(f)] = next
      return copy
    })
  }
  const onSave = () => {
    setError(null)
    const changes: { section: string; key: string; value: unknown }[] = []
    for (const [id, form] of Object.entries(draft)) {
      const f = byId.get(id)
      if (!f) continue
      const r = fromForm(f, form)
      if ('error' in r) return setError(r.error)
      changes.push({ section: f.section, key: f.key, value: r.value })
    }
    if (changes.length) save.mutate(changes)
  }
  const dirty = Object.keys(draft).length

  return (
    <>
      {settings.error && (
        <p role="alert" className="mt-3 text-red-700 dark:text-red-400">
          {plain(settings.error)}
        </p>
      )}
      {page === 'destinations' && <Folders />}
      {page === 'general' && <GeneralLinks />}
      {page === 'general' && <StorageLocations />}
      {page === 'general' && <About />}
      {fields.length > 0 && (
        <form
          className="mt-2"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            onSave()
          }}
        >
          {fields.map((f) => (
            <FieldRow key={idOf(f)} field={f} form={draft[idOf(f)] ?? toForm(f, f.value)} onChange={(next) => change(f, next)} />
          ))}
          <div className="mt-4 flex items-center gap-3">
            <button type="submit" className={BTN} disabled={dirty === 0 || save.isPending}>
              {save.isPending ? 'Saving…' : 'Save'}
            </button>
            {saved && dirty === 0 && <span role="status" className="text-sm text-green-700 dark:text-green-400">Saved.</span>}
          </div>
          {error && (
            <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-400">
              {error}
            </p>
          )}
        </form>
      )}
      {page === 'phone' && (
        <>
          <SpeedTest />
          <WifiSection />
          <section aria-labelledby="switch-title" className="mt-6 flex flex-col gap-2">
            <h2 id="switch-title" className="text-lg font-semibold">
              Switch phone
            </h2>
            <SwitchPhone />
          </section>
        </>
      )}
      {page === 'advanced' && (
        <section aria-labelledby="tools-title" className="mt-6 flex flex-col gap-2">
          <h2 id="tools-title" className="text-lg font-semibold">
            Diagnostics
          </h2>
          <p className={MUTED}>The activity log, and a zip of logs and settings to share when something goes wrong.</p>
          <p>
            <Link to="/logs" className={`${BTN} inline-block`}>
              Open the logs and export diagnostics
            </Link>
          </p>
        </section>
      )}
    </>
  )
}

function Folders() {
  const setup = useQuery({ queryKey: ['setup', 'state'], queryFn: () => apiFetch<SetupState>('/api/setup/state') })
  return (
    <section aria-label="Backup locations" className="mt-4 flex flex-col gap-8">
      {setup.data && <MacFolder setup={setup.data} settings next={() => void setup.refetch()} />}
      <Ssd settings next={() => undefined} />
    </section>
  )
}

function GeneralLinks() {
  return (
    <section aria-labelledby="setup-title" className="mt-4 flex flex-col gap-2">
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
  )
}

function About() {
  const status = useStatus()
  const version = status.data?.app.version
  const [error, setError] = useState<string | null>(null)
  const openLogs = useMutation({
    mutationFn: () => apiFetch<void>('/api/logs/open', { method: 'POST' }),
    onMutate: () => setError(null),
    onError: (e) => setError(plain(e)),
  })
  return (
    <section aria-labelledby="about-title" className="mt-6 flex flex-col gap-2">
      <h2 id="about-title" className="flex items-center gap-2 text-lg font-semibold">
        <img src="/favicon.svg" alt="" width={32} height={32} className="h-8 w-8" />
        About Sift
      </h2>
      <p>Version {version ?? '…'}</p>
      <p>No automatic updates: a new version is a new .dmg.</p>
      <p>
        To remove Sift: Drag Sift to the Bin, then delete ~/Library/Application Support/Sift and ~/Library/Caches/Sift. Your
        backups are never touched.
      </p>
      <p>
        <button type="button" className={BTN} disabled={openLogs.isPending} onClick={() => openLogs.mutate()}>
          Open logs folder
        </button>
      </p>
      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
    </section>
  )
}
