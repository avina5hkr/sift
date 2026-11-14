import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { useRef, useState, type ChangeEvent, type KeyboardEvent } from 'react'
import { Link } from 'react-router'
import { PictureViewer, type ViewItem } from '../components/cleanup/PictureViewer'
import { apiFetch, withToken } from '../lib/api'
import { baseName, formatCount } from '../lib/format'
import type { Cluster, ClusterLabel, ClusterSection, ClusterTag, ClustersResponse, ImportResult, PictureItem, Suggestion } from '../lib/types'
import { plain, post } from './Wizard/request'

const TAGS: { value: ClusterTag; text: string }[] = [
  { value: 'greeting', text: 'Greeting' },
  { value: 'quote', text: 'Quote' },
  { value: 'meme', text: 'Meme' },
  { value: 'devotional', text: 'Devotional' },
  { value: 'other', text: 'Other' },
]
const BTN = 'rounded-md border px-3 py-1.5 text-sm font-medium'
const ON = {
  junk: 'border-red-700 bg-red-700 text-white',
  keep: 'border-green-700 bg-green-700 text-white',
}
const OFF = 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'

const PAGE = 24
const SOURCE: Record<Suggestion['source'], string> = {
  'built-in': 'built in',
  'built-in + your teaching': 'built in and your teaching',
  'your teaching': 'from your teaching',
}
const CHIP = {
  junk: 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100',
  keep: 'border-green-300 bg-green-50 text-green-900 dark:border-green-700 dark:bg-green-950 dark:text-green-100',
  none: 'border-neutral-300 bg-neutral-100 text-neutral-800 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-200',
}
const SMALL_BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800'

type Viewing = { items: ViewItem[]; index: number }

function Thumb({ id, onClick, label }: { id: number; onClick: () => void; label: string }) {
  return (
    <button type="button" aria-label={label} onClick={onClick} className="block aspect-square cursor-zoom-in overflow-hidden rounded bg-neutral-100 p-0 dark:bg-neutral-900">
      <img src={withToken(`/api/media/${id}/thumb`)} alt="" loading="lazy" decoding="async" draggable={false} className="h-full w-full object-cover" />
    </button>
  )
}

function SuggestionChip({ suggestion, current, onAccept }: { suggestion: Suggestion; current: ClusterLabel | null; onAccept: (label: ClusterLabel) => void }) {
  const word = suggestion.label === 'junk' ? 'Junk' : suggestion.label === 'keep' ? 'Keep' : 'Not sure'
  const tone = suggestion.label ?? 'none'
  return (
    <p className={`flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-2 py-1 text-sm ${CHIP[tone]}`}>
      <span className="min-w-0 flex-1">
        Sift suggests: {word} — {suggestion.reason}
        <span className="ml-2 text-xs opacity-75">({SOURCE[suggestion.source] ?? suggestion.source})</span>
      </span>
      {suggestion.label && current !== suggestion.label && (
        <button type="button" onClick={() => onAccept(suggestion.label as ClusterLabel)} className="rounded border border-current px-2 py-0.5 text-xs font-medium">
          Accept
        </button>
      )}
    </p>
  )
}

type CardProps = {
  cluster: Cluster
  onLabel: (cluster: Cluster, label: ClusterLabel, tag: ClusterTag | '') => void
  onView: (v: Viewing) => void
  onMove: (delta: number) => void
  /** The label to show (the owner's press, before the page is reloaded). */
  label: ClusterLabel | null
}

function ClusterCard({ cluster, onLabel, onView, onMove, label }: CardProps) {
  const [tag, setTag] = useState<ClusterTag | ''>('')
  const [open, setOpen] = useState(false)
  const members = useQuery({
    queryKey: ['cleanup', 'clusters', cluster.id, 'members'],
    queryFn: () => apiFetch<PictureItem[]>(`/api/cleanup/clusters/${cluster.id}`),
    enabled: open,
  })

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    const t = e.target as HTMLElement
    if (['SELECT', 'INPUT', 'TEXTAREA'].includes(t.tagName) || e.ctrlKey || e.metaKey || e.altKey) return
    const key = e.key.toLowerCase()
    if (key === 'j' || key === 'k') {
      onLabel(cluster, key === 'j' ? 'junk' : 'keep', tag)
      onMove(1)
    }
    else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') onMove(1)
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') onMove(-1)
    else return
    e.preventDefault()
  }

  const sampleItems: ViewItem[] = cluster.sample_ids.slice(0, 12).map((id) => ({ id, name: `Picture ${id}` }))
  const memberItems: ViewItem[] = (members.data ?? []).map((m) => ({ id: m.id, name: baseName(m.path) }))

  return (
    <section
      aria-label={`Group of ${cluster.size} pictures`}
      data-cluster={cluster.id}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-4 focus:outline-2 focus:outline-offset-2 focus:outline-[var(--ring)] dark:border-neutral-800"
    >
      <div className="grid grid-cols-4 gap-1">
        {sampleItems.map((it, i) => (
          <Thumb key={it.id} id={it.id} label={`Open picture ${i + 1} of this group`} onClick={() => onView({ items: sampleItems, index: i })} />
        ))}
      </div>
      <p className="text-sm">
        {formatCount(cluster.size)} picture{cluster.size === 1 ? '' : 's'}, {formatCount(cluster.unique)} different
        {label && <span className={`ml-2 rounded px-2 py-0.5 text-xs font-semibold ${ON[label]}`}>{label === 'junk' ? 'Junk' : 'Keep'}</span>}
      </p>
      {cluster.sample_text && <p className="truncate text-sm text-neutral-600 dark:text-neutral-400">{cluster.sample_text}</p>}
      {cluster.suggestion && <SuggestionChip suggestion={cluster.suggestion} current={label} onAccept={(l) => onLabel(cluster, l, tag)} />}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" aria-pressed={label === 'junk'} onClick={() => onLabel(cluster, 'junk', tag)} className={`${BTN} ${label === 'junk' ? ON.junk : OFF}`}>
          Junk
        </button>
        <button type="button" aria-pressed={label === 'keep'} onClick={() => onLabel(cluster, 'keep', tag)} className={`${BTN} ${label === 'keep' ? ON.keep : OFF}`}>
          Keep
        </button>
        <label className="flex items-center gap-1 text-sm">
          <span className="text-neutral-600 dark:text-neutral-400">Kind</span>
          <select
            value={tag}
            onChange={(e) => setTag(e.target.value as ClusterTag | '')}
            className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
          >
            <option value="">Not sure</option>
            {TAGS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.text}
              </option>
            ))}
          </select>
        </label>
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="ml-auto text-sm underline">
          {open ? 'Hide' : `Show all ${formatCount(cluster.size)}`}
        </button>
      </div>
      {open && (
        <div>
          {members.isPending && <p className="text-sm text-neutral-600 dark:text-neutral-400">Loading…</p>}
          {members.error && (
            <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
              {plain(members.error)}
            </p>
          )}
          <div className="grid grid-cols-6 gap-1">
            {memberItems.map((it, i) => (
              <Thumb key={it.id} id={it.id} label={`Open ${it.name}`} onClick={() => onView({ items: memberItems, index: i })} />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

const today = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export default function TeachPage() {
  const [section, setSection] = useState<ClusterSection>('to_mark')
  const [offset, setOffset] = useState(0)
  // Not live: the page is fetched when the owner changes page or tab, or presses Refresh, never after a label.
  const clusters = useQuery({
    queryKey: ['cleanup', 'clusters', 'page', section, offset],
    queryFn: () => apiFetch<ClustersResponse>(`/api/cleanup/clusters?section=${section}&offset=${offset}&limit=${PAGE}`),
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    placeholderData: keepPreviousData,
  })
  const [viewing, setViewing] = useState<Viewing | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // What the owner pressed on this page, shown at once: "section:offset:id" -> label.
  const [shown, setShown] = useState<Record<string, ClusterLabel>>({})
  const listRef = useRef<HTMLDivElement>(null)
  const topRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const slot = (id: number) => `${section}:${offset}:${id}`

  const label = useMutation({
    mutationFn: (v: { cluster: Cluster; label: ClusterLabel; tag: ClusterTag | ''; key: string }) =>
      post('/api/labels', { cluster_id: v.cluster.id, label: v.label, ...(v.tag ? { tag: v.tag } : {}) }),
    onMutate: (v) => {
      setMessage(null)
      setShown((old) => ({ ...old, [v.key]: v.label }))
    },
    onError: (e, v) => {
      setShown((old) => {
        const rest = { ...old }
        delete rest[v.key]
        return rest
      })
      setMessage(plain(e))
    },
  })

  const goTo = (next: ClusterSection, at: number) => {
    setSection(next)
    setOffset(at)
    setMessage(null)
    topRef.current?.scrollIntoView?.()
  }
  const refresh = () => {
    setMessage(null)
    void clusters.refetch().then(() => setShown({}))
  }

  const move = (from: number, delta: number) => {
    const cards = listRef.current?.querySelectorAll<HTMLElement>('[data-cluster]')
    cards?.[from + delta]?.focus()
  }

  const save = async () => {
    setMessage(null)
    try {
      const body = await apiFetch<unknown>('/api/labels/export')
      const url = URL.createObjectURL(new Blob([JSON.stringify(body, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `sift-teaching-${today()}.json`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setMessage(plain(e))
    }
  }

  const load = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setMessage(null)
    setNotice(null)
    let parsed: unknown
    try {
      parsed = JSON.parse(await file.text())
    } catch {
      setMessage('That file is not a Sift teaching file.')
      return
    }
    try {
      const r = await post<ImportResult>('/api/labels/import', parsed)
      setNotice(
        `Loaded ${formatCount(r.applied)} label${r.applied === 1 ? '' : 's'}; ${formatCount(r.waiting)} wait${r.waiting === 1 ? 's' : ''} for their pictures to be backed up.`,
      )
      refresh()
    } catch (err) {
      setMessage(plain(err))
    }
  }

  const data = clusters.data
  if (!data) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Teach the app</h1>
        <p className="mt-2 text-neutral-600 dark:text-neutral-400">{clusters.error ? plain(clusters.error) : 'Loading…'}</p>
        <Link to="/cleanup" className="mt-3 inline-block underline">
          Back to Cleanup
        </Link>
      </div>
    )
  }
  const percent = data.images > 0 ? Math.round((data.covered / data.images) * 100) : 0
  const groups = data.to_mark + data.marked
  const total = data[section]
  const first = data.offset + 1
  const last = data.offset + data.clusters.length
  const changing = clusters.isPlaceholderData
  const shownLabel = (c: Cluster) => shown[slot(c.id)] ?? c.label
  const suggested = data.clusters.filter((c) => c.suggestion?.label).length

  const acceptAll = () => {
    const todo = data.clusters.filter((c) => c.suggestion?.label && c.suggestion.label !== shownLabel(c))
    if (todo.length === 0) return
    if (!window.confirm(`Label ${todo.length} group${todo.length === 1 ? '' : 's'} on this page as Sift suggests?`)) return
    for (const c of todo) label.mutate({ cluster: c, label: c.suggestion?.label as ClusterLabel, tag: '', key: slot(c.id) })
  }

  const tab = (value: ClusterSection, text: string, count: number) => (
    <button
      type="button"
      role="tab"
      aria-selected={section === value}
      onClick={() => section !== value && goTo(value, 0)}
      className={`rounded-md border px-3 py-1.5 text-sm font-medium ${
        section === value ? 'border-neutral-900 bg-neutral-900 text-white dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900' : OFF
      }`}
    >
      {text} ({formatCount(count)})
    </button>
  )
  const pager = (
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" disabled={changing || offset === 0} onClick={() => goTo(section, Math.max(0, offset - PAGE))} className={SMALL_BTN}>
        Previous page
      </button>
      <button type="button" disabled={changing || last >= total} onClick={() => goTo(section, offset + PAGE)} className={SMALL_BTN}>
        Next page
      </button>
      {total > 0 && (
        <span className="text-sm text-neutral-600 dark:text-neutral-400">
          Showing {formatCount(first)}–{formatCount(last)} of {formatCount(total)}
        </span>
      )}
    </div>
  )

  return (
    <div ref={topRef} className="flex max-w-5xl flex-col gap-4 p-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Teach the app</h1>
        <Link to="/cleanup" className="text-sm underline">
          Back to Cleanup
        </Link>
      </div>
      <p>
        {formatCount(groups)} group{groups === 1 ? '' : 's'}, covering {percent}% of images
      </p>
      <p className="text-sm text-neutral-600 dark:text-neutral-400">
        Mark each group Junk or Keep. With a group selected, press J for Junk or K for Keep, and the arrow keys to move between groups. A marked group stays
        where it is until you change page or press Refresh.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => void save()} className={SMALL_BTN}>
          Save my teaching to a file
        </button>
        <button type="button" onClick={() => fileRef.current?.click()} className={SMALL_BTN}>
          Load teaching from a file
        </button>
        <input ref={fileRef} type="file" accept="application/json,.json" aria-label="Teaching file" onChange={(e) => void load(e)} className="hidden" />
      </div>
      <p className="text-sm text-neutral-600 dark:text-neutral-400">Your teaching stays on this Mac. Save it to a file to use it in another Sift install.</p>
      {notice && (
        <p role="status" className="text-sm text-green-800 dark:text-green-300">
          {notice}
        </p>
      )}
      {message && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {message}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <div role="tablist" aria-label="Groups" className="flex gap-2">
          {tab('to_mark', 'To mark', data.to_mark)}
          {tab('marked', 'Marked', data.marked)}
        </div>
        <button type="button" onClick={refresh} disabled={clusters.isFetching} className={`${SMALL_BTN} ml-auto`}>
          Refresh
        </button>
        <button type="button" onClick={acceptAll} disabled={suggested === 0} className={SMALL_BTN}>
          Accept all suggestions on this page
        </button>
      </div>
      {data.clusters.length > 0 && pager}
      {data.clusters.length === 0 ? (
        <p className="text-neutral-600 dark:text-neutral-400">
          {groups === 0
            ? 'No groups yet. Download the picture model and choose Analyze on the Cleanup screen first.'
            : section === 'to_mark'
              ? 'Every group is marked.'
              : 'No group is marked yet.'}
        </p>
      ) : (
        <div ref={listRef} aria-busy={changing} className={`grid gap-4 md:grid-cols-2 ${changing ? 'opacity-60' : ''}`}>
          {data.clusters.map((c, i) => (
            <ClusterCard
              key={c.id}
              cluster={c}
              label={shownLabel(c)}
              onLabel={(cl, l, t) => label.mutate({ cluster: cl, label: l, tag: t, key: slot(cl.id) })}
              onView={setViewing}
              onMove={(d) => move(i, d)}
            />
          ))}
        </div>
      )}
      {data.clusters.length > 0 && pager}
      {viewing && (
        <PictureViewer items={viewing.items} index={viewing.index} onIndex={(index) => setViewing({ ...viewing, index })} onClose={() => setViewing(null)} />
      )}
    </div>
  )
}
