import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { ActionDialog } from '../components/apps/ActionDialog'
import { SimpleConfirm } from '../components/apps/SimpleConfirm'
import { BTN, MUTED } from '../components/apps/ui'
import { apiFetch } from '../lib/api'
import { LARGE_BYTES, installerLabel, isWhatsApp, lastUsedLabel, sizeOrUnavailable, totalBytes, unlockHint } from '../lib/apps'
import { formatAgo, formatBytes } from '../lib/format'
import type { AppAction, AppRow, AppsExport, AppsList } from '../lib/types'
import { plain, post } from './Wizard/request'

type Filter = 'unused' | 'large' | 'sideloaded'
type Sort = 'size' | 'name'

const FILTERS: [Filter, string, (a: AppRow) => boolean][] = [
  ['unused', 'Unused 90+ days', (a) => a.unused],
  ['large', 'Large (over 500 MB)', (a) => (a.app_bytes ?? 0) > LARGE_BYTES],
  ['sideloaded', 'Not from Play Store', (a) => !a.from_play_store],
]

const bytes = (n: number | null) => sizeOrUnavailable(n, formatBytes)
const badge = (a: AppRow) => (a.protected ? (a.unprotected ? 'Unprotected' : 'Protected') : null)

export default function AppsPage() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['apps'], queryFn: () => apiFetch<AppsList>('/api/apps'), retry: false })
  const refetch = () => void qc.invalidateQueries({ queryKey: ['apps'] })

  const [filters, setFilters] = useState<Set<Filter>>(() => new Set())
  const [sort, setSort] = useState<Sort>('size')
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [dialog, setDialog] = useState<AppAction | null>(null)
  const [unprotecting, setUnprotecting] = useState<AppRow | null>(null)
  const [caches, setCaches] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const apps = useMemo(() => list.data?.apps ?? [], [list.data])
  const shown = useMemo(() => {
    const keep = FILTERS.filter(([k]) => filters.has(k))
    const rows = apps.filter((a) => keep.every(([, , test]) => test(a)))
    return rows.sort((a, b) => (sort === 'name' ? a.name.localeCompare(b.name) : totalBytes(b) - totalBytes(a) || a.name.localeCompare(b.name)))
  }, [apps, filters, sort])
  const suggested = useMemo(
    () => (list.data?.suggested ?? []).map((p) => apps.find((a) => a.package === p)).filter((a): a is AppRow => a != null),
    [list.data, apps],
  )
  const selected = apps.filter((a) => picked.has(a.package))
  const probes = list.data?.probes

  const toggle = (pkg: string) =>
    setPicked((s) => {
      const n = new Set(s)
      if (!n.delete(pkg)) n.add(pkg)
      return n
    })
  const toggleFilter = (k: Filter) =>
    setFilters((s) => {
      const n = new Set(s)
      if (!n.delete(k)) n.add(k)
      return n
    })

  const unprotect = useMutation({
    mutationFn: (v: { row: AppRow; again: boolean }) =>
      post<{ unprotected: boolean }>('/api/apps/unprotect', v.again ? { package: v.row.package, protect_again: true } : { package: v.row.package, confirm: true }),
    onSuccess: () => {
      setUnprotecting(null)
      setActionError(null)
      refetch()
    },
    onError: (e) => {
      setActionError(plain(e))
      setUnprotecting(null)
    },
  })
  const trim = useMutation({
    mutationFn: () => post<{ done: boolean }>('/api/apps/trim-caches', { confirm: true }),
    onSuccess: () => {
      setCaches(false)
      setNote('Caches freed.')
      refetch()
    },
  })
  const save = useMutation({
    mutationFn: () => post<AppsExport>('/api/apps/export', { apks: [] }),
    onSuccess: (out) => {
      setActionError(null)
      setNote(`App list saved to: ${out.app_list.join(', ')}`)
    },
    onError: (e) => setActionError(plain(e)),
  })

  const sortHead = (key: Sort, label: string) => (
    <th scope="col" aria-sort={sort === key ? (key === 'size' ? 'descending' : 'ascending') : undefined} className="py-2 pr-3 font-medium">
      <button type="button" onClick={() => setSort(key)} className="font-medium hover:underline">
        {label}
        {sort === key ? (key === 'size' ? ' ↓' : ' ↑') : ''}
      </button>
    </th>
  )
  const head = (label: string) => (
    <th scope="col" className="py-2 pr-3 font-medium">
      {label}
    </th>
  )

  const none = selected.length === 0
  const pickReason = none ? 'Tick the apps first.' : undefined

  return (
    <div className="flex min-h-full flex-col">
      <div className="flex min-w-0 flex-1 flex-col gap-3 p-6">
        <h1 className="text-xl font-semibold">Apps</h1>

        {list.error && (
          <p role="alert" className="text-sm">
            {plain(list.error)}
          </p>
        )}
        {actionError && (
          <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
            {actionError}
          </p>
        )}
        {note && (
          <p role="status" className="text-sm">
            {note}
          </p>
        )}
        {probes && !probes.packages && <p className={`text-sm ${MUTED}`}>The app list is unavailable on this phone.</p>}
        {probes && probes.packages && !probes.diskstats && <p className={`text-sm ${MUTED}`}>Sizes are unavailable on this phone.</p>}
        {probes && probes.packages && !probes.usagestats && <p className={`text-sm ${MUTED}`}>Last used is unavailable on this phone.</p>}

        {suggested.length > 0 && (
          <section aria-label="Suggested" className="rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
            <h2 className="mb-1 text-sm font-semibold">Suggested</h2>
            <p className={`mb-2 text-xs ${MUTED}`}>Unused for a long time, or large. Nothing is ticked for you.</p>
            <ul className="flex flex-wrap gap-2">
              {suggested.map((a) => (
                <li key={a.package}>
                  <button
                    type="button"
                    aria-pressed={picked.has(a.package)}
                    onClick={() => toggle(a.package)}
                    className={`rounded-md border px-2 py-1 text-sm ${picked.has(a.package) ? 'border-neutral-900 bg-neutral-200 dark:border-neutral-100 dark:bg-neutral-800' : 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'}`}
                  >
                    {a.name} <span className={MUTED}>{bytes(a.app_bytes)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
          {FILTERS.map(([k, label]) => (
            <button
              key={k}
              type="button"
              aria-pressed={filters.has(k)}
              onClick={() => toggleFilter(k)}
              className={`rounded-md border px-2 py-1 text-sm ${filters.has(k) ? 'border-neutral-900 bg-neutral-200 dark:border-neutral-100 dark:bg-neutral-800' : 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'}`}
            >
              {label}
            </button>
          ))}
        </div>

        {list.isPending && <p className={MUTED}>Reading the apps on your phone…</p>}
        {list.data && shown.length === 0 && <p className={MUTED}>No apps to show.</p>}

        {shown.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 dark:border-neutral-800">
                <th scope="col" className="w-8 py-2">
                  <span className="sr-only">Select</span>
                </th>
                {sortHead('name', 'Name')}
                {head('Package')}
                {sortHead('size', 'Size')}
                {head('Data')}
                {head('Cache')}
                {head('Last used')}
                {head('Installer')}
                {head('Status')}
              </tr>
            </thead>
            <tbody>
              {shown.map((a) => {
                const b = badge(a)
                const locked = a.protected && !a.unprotected
                return (
                  <tr key={a.package} className="border-b border-neutral-100 align-top dark:border-neutral-900">
                    <td className="py-1.5">
                      <input type="checkbox" aria-label={`Select ${a.name}`} checked={picked.has(a.package)} onChange={() => toggle(a.package)} />
                    </td>
                    <td className="py-1.5 pr-3 font-medium">{a.name}</td>
                    <td className={`py-1.5 pr-3 ${MUTED}`}>{a.package}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{bytes(a.app_bytes)}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{bytes(a.data_bytes)}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{bytes(a.cache_bytes)}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{lastUsedLabel(a, formatAgo)}</td>
                    <td className="py-1.5 pr-3">{installerLabel(a)}</td>
                    <td className="py-1.5">
                      {b && (
                        <span className="rounded bg-neutral-200 px-1.5 py-0.5 text-xs font-medium dark:bg-neutral-800">{b}</span>
                      )}
                      {locked && <span className={`mt-1 block text-xs ${MUTED}`}>{unlockHint(a)}</span>}
                      {a.protected && !isWhatsApp(a.package) && (
                        <button
                          type="button"
                          aria-label={`${a.unprotected ? 'Protect again' : 'Unprotect'} ${a.name}`}
                          disabled={unprotect.isPending}
                          onClick={() => (a.unprotected ? unprotect.mutate({ row: a, again: true }) : setUnprotecting(a))}
                          className="mt-1 block text-xs underline"
                        >
                          {a.unprotected ? 'Protect again' : 'Unprotect'}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-neutral-200 bg-[var(--bg)] px-6 py-3 dark:border-neutral-800">
        <span className={`mr-auto text-sm ${MUTED}`}>{none ? 'Nothing ticked.' : `${selected.length} ticked`}</span>
        <button type="button" disabled={none} title={pickReason} onClick={() => setDialog('uninstall')} className={BTN}>
          Uninstall selected
        </button>
        <button type="button" disabled={none} title={pickReason} onClick={() => setDialog('clear_data')} className={BTN}>
          Clear data
        </button>
        <button type="button" disabled={!list.data} onClick={() => setCaches(true)} className={BTN}>
          Clear all caches
        </button>
        <button type="button" disabled={!list.data || save.isPending} onClick={() => save.mutate()} className={BTN}>
          Save app list
        </button>
      </div>

      {dialog && (
        <ActionDialog
          action={dialog}
          rows={selected}
          onClose={() => setDialog(null)}
          onFinished={() => {
            setDialog(null)
            setPicked(new Set())
            refetch()
          }}
        />
      )}
      {unprotecting && (
        <SimpleConfirm
          label="Unprotect app"
          title={`Unprotect ${unprotecting.name}?`}
          confirmLabel="Unprotect"
          busy={unprotect.isPending}
          error={null}
          onConfirm={() => unprotect.mutate({ row: unprotecting, again: false })}
          onCancel={() => setUnprotecting(null)}
        >
          Codes or data in this app may not be recoverable after you uninstall it.
        </SimpleConfirm>
      )}
      {caches && (
        <SimpleConfirm
          label="Clear all caches"
          title="Clear all caches?"
          confirmLabel="Clear all caches"
          busy={trim.isPending}
          error={trim.error ? plain(trim.error) : null}
          danger={false}
          onConfirm={() => trim.mutate()}
          onCancel={() => {
            trim.reset()
            setCaches(false)
          }}
        >
          Android frees cache space for all apps. Apps keep their data.
        </SimpleConfirm>
      )}
    </div>
  )
}
