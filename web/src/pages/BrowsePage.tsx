import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { FolderStatus, StatusBadge } from '../components/browse/StatusBadge'
import { Thumb } from '../components/browse/Thumb'
import { ReasonButton } from '../components/backup/ReasonButton'
import { DeleteFlow } from '../components/delete/DeleteFlow'
import { useDeleteRun } from '../components/delete/useDeleteRun'
import { apiFetch } from '../lib/api'
import { STATUS_LABEL, folderCounts, isUnbacked } from '../lib/browse'
import { formatBytes, formatCount, shortPath } from '../lib/format'
import type { BrowseFile, BrowseFolder, BrowsePage as Page, BrowseStatus, SmartList, SmartListPage } from '../lib/types'
import { plain } from './Wizard/request'

type Sort = 'size' | 'name' | 'date'
const ROOT = '/sdcard'
const FIELD = 'rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700'
const MUTED = 'text-neutral-600 dark:text-neutral-400'
const KINDS: [string, string][] = [
  ['', 'Any type'],
  ['image', 'Photos'],
  ['video', 'Videos'],
  ['audio', 'Audio'],
  ['doc', 'Documents'],
  ['other', 'Other'],
]

const plural = (n: number, one: string, many: string) => `${formatCount(n)} ${n === 1 ? one : many}`
const day = (epoch: number) => new Date(epoch * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

function Bar({ size, max }: { size: number; max: number }) {
  const pct = max > 0 ? Math.max(2, Math.round((size / max) * 100)) : 0
  return (
    <span aria-hidden="true" className="mt-1 block h-1 w-24 rounded bg-neutral-100 dark:bg-neutral-900">
      <span className="block h-1 rounded bg-blue-500" style={{ width: `${pct}%` }} />
    </span>
  )
}

export default function BrowsePage() {
  const qc = useQueryClient()
  const [params] = useSearchParams()
  const [path, setPath] = useState(() => params.get('path') ?? ROOT)
  const [listKey, setListKey] = useState<string | null>(() => params.get('list'))
  const [sort, setSort] = useState<Sort>('size')
  const [search, setSearch] = useState('')
  const [kind, setKind] = useState('')
  const [olderDays, setOlderDays] = useState('')
  const [minMb, setMinMb] = useState('')
  const [status, setStatus] = useState(() => params.get('status') ?? '')
  const q = useDebounced(search.trim(), 300)
  const older = useDebounced(olderDays, 300)
  const minimum = useDebounced(minMb, 300)

  const [ids, setIds] = useState<Set<number>>(() => new Set())
  const [folders, setFolders] = useState<Set<string>>(() => new Set())
  const flow = useDeleteRun({
    onFinished: () => {
      setIds(new Set())
      setFolders(new Set())
      void qc.invalidateQueries({ queryKey: ['browse'] })
    },
  })

  const lists = useQuery({ queryKey: ['browse', 'lists'], queryFn: () => apiFetch<SmartList[]>('/api/browse/lists') })

  const query = useMemo(() => {
    const p = new URLSearchParams({ path, sort })
    if (q) p.set('q', q)
    if (kind) p.set('kind', kind)
    if (Number(older) > 0) p.set('older_days', String(Math.floor(Number(older))))
    if (Number(minimum) > 0) p.set('min_bytes', String(Math.round(Number(minimum) * 1_000_000)))
    if (status) p.set('status', status)
    return p.toString()
  }, [path, sort, q, kind, older, minimum, status])

  const level = useInfiniteQuery({
    queryKey: ['browse', 'level', query],
    enabled: listKey == null,
    initialPageParam: 0,
    queryFn: ({ pageParam }) => apiFetch<Page>(`/api/browse?${query}&offset=${pageParam}`),
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, p) => n + p.files.length, 0)
      return loaded < last.total_files ? loaded : undefined
    },
  })
  const smart = useInfiniteQuery({
    queryKey: ['browse', 'list', listKey],
    enabled: listKey != null,
    initialPageParam: 0,
    queryFn: ({ pageParam }) => apiFetch<SmartListPage>(`/api/browse/lists/${listKey}?offset=${pageParam}`),
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, p) => n + p.files.length, 0)
      return loaded < last.total_files ? loaded : undefined
    },
  })

  const active = listKey != null ? smart : level
  const first = level.data?.pages[0]
  const dirs: BrowseFolder[] = listKey == null ? (first?.folders ?? []) : []
  const files: BrowseFile[] = (active.data?.pages ?? []).flatMap((p) => p.files)
  const total = active.data?.pages[0]?.total_files ?? 0
  const max = Math.max(0, ...dirs.map((d) => d.size), ...files.map((f) => f.size))
  const listTitle = listKey != null ? (smart.data?.pages[0]?.title ?? lists.data?.find((l) => l.key === listKey)?.title ?? '') : ''

  const covered = (file: BrowseFile) => [...folders].some((f) => file.path.startsWith(`${f}/`))
  const toggleFile = (id: number) =>
    setIds((s) => {
      const n = new Set(s)
      if (!n.delete(id)) n.add(id)
      return n
    })
  const toggleFolder = (p: string) =>
    setFolders((s) => {
      const n = new Set(s)
      if (!n.delete(p)) n.add(p)
      return n
    })
  const open = (p: string) => {
    setListKey(null)
    setPath(p)
  }
  const sortHead = (key: Sort, label: string) => (
    <th scope="col" aria-sort={listKey == null && sort === key ? 'ascending' : undefined} className="py-2 pr-3 font-medium">
      <button type="button" disabled={listKey != null} onClick={() => setSort(key)} className="font-medium hover:underline disabled:no-underline">
        {label}
        {listKey == null && sort === key ? ' ↓' : ''}
      </button>
    </th>
  )

  const nothing = ids.size === 0 && folders.size === 0
  const reason = flow.phase !== 'idle' ? 'Working…' : nothing ? 'Tick the files or folders you want to delete first.' : null
  const selected = [plural(ids.size, 'file', 'files'), plural(folders.size, 'folder', 'folders')].join(' and ')
  const crumbs = first?.crumbs ?? []

  return (
    <div className="flex min-h-full">
      <aside aria-label="Smart lists" className="w-60 shrink-0 border-r border-neutral-200 p-3 dark:border-neutral-800">
        <h2 className="mb-2 text-sm font-semibold">Smart lists</h2>
        <ul className="flex flex-col gap-1">
          {(lists.data ?? []).map((l) => (
            <li key={l.key}>
              <button
                type="button"
                aria-pressed={listKey === l.key}
                onClick={() => setListKey(listKey === l.key ? null : l.key)}
                className={`w-full rounded-md px-2 py-1.5 text-left text-sm ${listKey === l.key ? 'bg-neutral-200 dark:bg-neutral-800' : 'hover:bg-neutral-100 dark:hover:bg-neutral-900'}`}
              >
                <span className="block">{l.title}</span>
                <span className={`block text-xs ${MUTED}`}>
                  {plural(l.files, 'file', 'files')}, {formatBytes(l.bytes)}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-sm">
          <Link to="/archive" className="underline">
            Archive
          </Link>
        </p>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col gap-3 p-6 pb-0">
        <h1 className="text-xl font-semibold">Browse</h1>

        {listKey == null ? (
          <nav aria-label="Folder" className="flex flex-wrap items-center gap-1 text-sm">
            {crumbs.length === 0 && <span>Phone storage</span>}
            {crumbs.map((c, i) => {
              const label = i === 0 ? 'Phone storage' : c.slice(c.lastIndexOf('/') + 1)
              return (
                <span key={c} className="flex items-center gap-1">
                  {i > 0 && <span aria-hidden="true">/</span>}
                  {i === crumbs.length - 1 ? (
                    <span aria-current="page" className="font-medium">
                      {label}
                    </span>
                  ) : (
                    <button type="button" onClick={() => open(c)} className="underline">
                      {label}
                    </button>
                  )}
                </span>
              )
            })}
          </nav>
        ) : (
          <p className="flex items-center gap-2 text-sm">
            <button type="button" onClick={() => setListKey(null)} className="underline">
              Back to folders
            </button>
            <span className="font-medium">{listTitle}</span>
          </p>
        )}

        {listKey == null && (
          <div className="flex flex-wrap items-center gap-2">
            <input aria-label="Search by name" placeholder="Search by name" value={search} onChange={(e) => setSearch(e.target.value)} className={`${FIELD} w-48`} />
            <select aria-label="Type" value={kind} onChange={(e) => setKind(e.target.value)} className={FIELD}>
              {KINDS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1 text-sm">
              Older than
              <input type="number" min={0} aria-label="Older than (days)" value={olderDays} onChange={(e) => setOlderDays(e.target.value)} className={`${FIELD} w-20`} />
              days
            </label>
            <label className="flex items-center gap-1 text-sm">
              At least
              <input type="number" min={0} aria-label="At least (MB)" value={minMb} onChange={(e) => setMinMb(e.target.value)} className={`${FIELD} w-20`} />
              MB
            </label>
            <select aria-label="Backup status" value={status} onChange={(e) => setStatus(e.target.value)} className={FIELD}>
              <option value="">Any backup status</option>
              {(Object.keys(STATUS_LABEL) as BrowseStatus[]).map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABEL[s]}
                </option>
              ))}
            </select>
          </div>
        )}

        {active.error && (
          <p role="alert" className="text-sm">
            {plain(active.error)}
          </p>
        )}
        {listKey == null && first && (
          <p className={`text-sm ${MUTED}`}>
            {formatBytes(first.total_size)} here. Sizes are from the last scan of the phone.
          </p>
        )}
        {active.data && dirs.length === 0 && files.length === 0 && <p className={MUTED}>Nothing to show here.</p>}

        {(dirs.length > 0 || files.length > 0) && (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 dark:border-neutral-800">
                <th scope="col" className="w-8 py-2">
                  <span className="sr-only">Select</span>
                </th>
                {sortHead('name', 'Name')}
                {sortHead('size', 'Size')}
                <th scope="col" className="py-2 pr-3 font-medium">Items</th>
                {sortHead('date', 'Modified')}
                <th scope="col" className="py-2 pr-3 font-medium">Backup</th>
                <th scope="col" className="py-2 font-medium">Kind</th>
              </tr>
            </thead>
            <tbody>
              {dirs.map((d) => {
                const { backed } = folderCounts(d.status)
                const filtering = Boolean(q || kind || Number(older) > 0 || Number(minimum) > 0 || status)
                const blocked = (d.files > 0 && backed === 0) || filtering
                return (
                  <tr key={d.path} className="border-b border-neutral-100 dark:border-neutral-900">
                    <td className="py-1.5">
                      <input
                        type="checkbox"
                        aria-label={`Select folder ${d.name}`}
                        title={filtering ? 'Clear the filters to select a whole folder' : blocked ? 'Not backed up yet' : undefined}
                        disabled={blocked}
                        checked={folders.has(d.path)}
                        onChange={() => toggleFolder(d.path)}
                      />
                    </td>
                    <td className="py-1.5 pr-3">
                      <button type="button" onClick={() => open(d.path)} className="flex items-center gap-2 font-medium hover:underline">
                        <span aria-hidden="true">📁</span>
                        {d.name}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3">
                      {formatBytes(d.size)}
                      <Bar size={d.size} max={max} />
                    </td>
                    <td className="py-1.5 pr-3">{formatCount(d.files)}</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{day(d.mtime)}</td>
                    <td className="py-1.5 pr-3">
                      <FolderStatus status={d.status} />
                    </td>
                    <td className={`py-1.5 ${MUTED}`}>Folder</td>
                  </tr>
                )
              })}
              {files.map((f) => {
                const unbacked = isUnbacked(f.status)
                const inFolder = !unbacked && covered(f)
                return (
                  <tr key={f.id} className="border-b border-neutral-100 dark:border-neutral-900">
                    <td className="py-1.5">
                      <input
                        type="checkbox"
                        aria-label={`Select ${f.name}`}
                        title={unbacked ? 'Not backed up yet' : inFolder ? 'In a selected folder' : undefined}
                        disabled={unbacked || inFolder}
                        checked={inFolder || ids.has(f.id)}
                        onChange={() => toggleFile(f.id)}
                      />
                    </td>
                    <td className="py-1.5 pr-3">
                      <span className="flex items-center gap-2">
                        <Thumb file={f} />
                        <span className="min-w-0">
                          <span className="block truncate" title={f.path}>
                            {f.name}
                          </span>
                          {listKey != null && <span className={`block truncate text-xs ${MUTED}`}>{shortPath(f.path)}</span>}
                        </span>
                      </span>
                    </td>
                    <td className="py-1.5 pr-3">
                      {formatBytes(f.size)}
                      <Bar size={f.size} max={max} />
                    </td>
                    <td className="py-1.5 pr-3">1</td>
                    <td className="py-1.5 pr-3 whitespace-nowrap">{day(f.mtime)}</td>
                    <td className="py-1.5 pr-3">
                      <StatusBadge status={f.status} />
                    </td>
                    <td className={`py-1.5 ${MUTED}`}>{f.kind}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        {active.hasNextPage && (
          <div className="flex items-center gap-3 pb-2">
            <button
              type="button"
              disabled={active.isFetchingNextPage}
              onClick={() => void active.fetchNextPage()}
              className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800"
            >
              Show more
            </button>
            <span className={`text-sm ${MUTED}`}>
              {formatCount(files.length)} of {formatCount(total)} files shown
            </span>
          </div>
        )}

        <div className="sticky bottom-0 -mx-6 mt-auto flex flex-wrap items-center gap-3 border-t border-neutral-200 bg-[var(--bg)] px-6 py-3 dark:border-neutral-800">
          <span className="text-sm">{selected} selected</span>
          <ReasonButton
            primary
            placement="above"
            reason={reason}
            onClick={() =>
              void flow.start({
                source: 'browse',
                ids: [...ids],
                folders: [...folders],
                filters: { q, kind, older_days: Math.floor(Number(older) || 0), min_bytes: Math.round((Number(minimum) || 0) * 1_000_000), status },
              })
            }
          >
            Delete selected
          </ReasonButton>
          {!nothing && (
            <button
              type="button"
              onClick={() => {
                setIds(new Set())
                setFolders(new Set())
              }}
              className="text-sm underline"
            >
              Clear
            </button>
          )}
        </div>
      </div>
      <DeleteFlow flow={flow} />
    </div>
  )
}
