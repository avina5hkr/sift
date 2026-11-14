import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { BTN, FIELD, MUTED } from '../components/apps/ui'
import { Chip } from '../components/Chip'
import { apiFetch } from '../lib/api'
import { useEventStore } from '../lib/events'
import { plain, post } from './Wizard/request'

type Entry = { ts: number | string | null; level: string; area: string; message: string }

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
const RANK = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
const MAX_ROWS = 1000
const tone = (level: string) => (level === 'ERROR' || level === 'CRITICAL' ? 'bad' : level === 'WARNING' ? 'warn' : 'neutral')

function when(ts: number | string | null): string {
  if (ts === null || ts === '') return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts) // the log file has seconds
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString()
}

export default function LogsPage() {
  const [level, setLevel] = useState('INFO')
  const [area, setArea] = useState('')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [liveState, setLive] = useState<{ key: string; rows: Entry[] }>({ key: '', rows: [] })
  const [paths, setPaths] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setQ(search), 300)
    return () => clearTimeout(t)
  }, [search])

  const params = new URLSearchParams({ level, area, q, limit: String(MAX_ROWS) })
  const logs = useQuery({
    queryKey: ['logs', level, area, q],
    queryFn: () => apiFetch<{ entries: Entry[] }>(`/api/logs?${params.toString()}`),
    retry: false,
  })

  // Lines that arrive after the table was read; a new filter reads the table again, so start over.
  const key = `${level}|${area}|${q}`
  const live = liveState.key === key ? liveState.rows : []
  useEffect(() => {
    return useEventStore.subscribe((state, prev) => {
      if (state.last === prev.last || state.last?.name !== 'log') return
      const d = state.last.data as Partial<Entry> | null
      if (!d || typeof d.message !== 'string') return
      const entry: Entry = { ts: new Date().toISOString(), level: String(d.level ?? 'INFO').toUpperCase(), area: String(d.area ?? ''), message: d.message }
      if (RANK.indexOf(entry.level) < RANK.indexOf(level)) return
      if (area && !entry.area.startsWith(area)) return
      if (q && !entry.message.toLowerCase().includes(q.toLowerCase())) return
      setLive((l) => ({ key, rows: [entry, ...(l.key === key ? l.rows : [])].slice(0, MAX_ROWS) }))
    })
  }, [level, area, q, key])

  const rows = useMemo(() => [...live, ...(logs.data?.entries ?? [])].slice(0, MAX_ROWS), [live, logs.data])

  const exportZip = useMutation({
    mutationFn: () => post<{ path: string; name: string }>('/api/diagnostics', { include_paths: paths }),
    onMutate: () => {
      setError(null)
      setSaved(null)
    },
    onSuccess: (r) => setSaved(r.name),
    onError: (e) => setError(plain(e)),
  })

  return (
    <div className="flex flex-col gap-4 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Logs</h1>
        <p className={MUTED}>What Sift has been doing, newest first. New lines appear as they happen.</p>
      </header>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Level
          <select className={FIELD} value={level} onChange={(e) => setLevel(e.target.value)}>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l === 'DEBUG' ? 'Everything' : l === 'INFO' ? 'Info and up' : l === 'WARNING' ? 'Warnings and up' : 'Errors only'}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Area
          <input className={`${FIELD} w-40`} placeholder="for example backup" value={area} onChange={(e) => setArea(e.target.value.trim())} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Search
          <input type="search" className={`${FIELD} w-64`} value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
      </div>

      {logs.error && (
        <p role="alert" className="text-red-700 dark:text-red-400">
          {plain(logs.error)}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-neutral-300 dark:border-neutral-700">
              <th className="py-1 pr-3">Time</th>
              <th className="py-1 pr-3">Level</th>
              <th className="py-1 pr-3">Area</th>
              <th className="py-1">Message</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e, i) => (
              <tr key={i} className="border-b border-neutral-100 align-top dark:border-neutral-900">
                <td className="whitespace-nowrap py-1 pr-3">{when(e.ts)}</td>
                <td className="py-1 pr-3">
                  <Chip tone={tone(e.level)}>{e.level}</Chip>
                </td>
                <td className="py-1 pr-3">{e.area}</td>
                <td className="break-words py-1 font-mono">{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {logs.data && rows.length === 0 && <p className={`py-3 ${MUTED}`}>No log lines match.</p>}
      </div>

      <section aria-labelledby="diag-title" className="flex flex-col gap-2 border-t border-neutral-200 pt-4 dark:border-neutral-800">
        <h2 id="diag-title" className="text-lg font-semibold">
          Diagnostics
        </h2>
        <p className={MUTED}>Saves a zip with the logs, the settings and the database version to your Downloads folder.</p>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={paths} onChange={(e) => setPaths(e.target.checked)} />
          Include file paths
        </label>
        <div>
          <button type="button" className={BTN} disabled={exportZip.isPending} onClick={() => exportZip.mutate()}>
            {exportZip.isPending ? 'Saving…' : 'Export diagnostics'}
          </button>
        </div>
        {saved && <p role="status">Saved {saved} in Downloads.</p>}
        {error && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-400">
            {error}
          </p>
        )}
      </section>
    </div>
  )
}
