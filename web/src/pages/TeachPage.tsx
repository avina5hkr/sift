import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState, type KeyboardEvent } from 'react'
import { Link } from 'react-router'
import { PictureViewer, type ViewItem } from '../components/cleanup/PictureViewer'
import { apiFetch, withToken } from '../lib/api'
import { baseName, formatCount } from '../lib/format'
import type { Cluster, ClusterLabel, ClusterTag, ClustersResponse, PictureItem } from '../lib/types'
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

type Viewing = { items: ViewItem[]; index: number }

function Thumb({ id, onClick, label }: { id: number; onClick: () => void; label: string }) {
  return (
    <button type="button" aria-label={label} onClick={onClick} className="block aspect-square cursor-zoom-in overflow-hidden rounded bg-neutral-100 p-0 dark:bg-neutral-900">
      <img src={withToken(`/api/media/${id}/thumb`)} alt="" loading="lazy" decoding="async" draggable={false} className="h-full w-full object-cover" />
    </button>
  )
}

type CardProps = {
  cluster: Cluster
  onLabel: (cluster: Cluster, label: ClusterLabel, tag: ClusterTag | '') => void
  onView: (v: Viewing) => void
  onMove: (delta: number) => void
}

function ClusterCard({ cluster, onLabel, onView, onMove }: CardProps) {
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
    if (key === 'j') onLabel(cluster, 'junk', tag)
    else if (key === 'k') onLabel(cluster, 'keep', tag)
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
        {cluster.label && (
          <span className={`ml-2 rounded px-2 py-0.5 text-xs font-semibold ${ON[cluster.label]}`}>{cluster.label === 'junk' ? 'Junk' : 'Keep'}</span>
        )}
      </p>
      {cluster.sample_text && <p className="truncate text-sm text-neutral-600 dark:text-neutral-400">{cluster.sample_text}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" aria-pressed={cluster.label === 'junk'} onClick={() => onLabel(cluster, 'junk', tag)} className={`${BTN} ${cluster.label === 'junk' ? ON.junk : OFF}`}>
          Junk
        </button>
        <button type="button" aria-pressed={cluster.label === 'keep'} onClick={() => onLabel(cluster, 'keep', tag)} className={`${BTN} ${cluster.label === 'keep' ? ON.keep : OFF}`}>
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

export default function TeachPage() {
  const qc = useQueryClient()
  const clusters = useQuery({
    queryKey: ['cleanup', 'clusters'],
    queryFn: () => apiFetch<ClustersResponse>('/api/cleanup/clusters'),
    retry: false,
  })
  const [viewing, setViewing] = useState<Viewing | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const label = useMutation({
    mutationFn: (v: { cluster: Cluster; label: ClusterLabel; tag: ClusterTag | '' }) =>
      post('/api/labels', { cluster_id: v.cluster.id, label: v.label, ...(v.tag ? { tag: v.tag } : {}) }),
    onMutate: () => setMessage(null),
    onSuccess: (_r, v) =>
      qc.setQueryData<ClustersResponse>(['cleanup', 'clusters'], (old) =>
        old && { ...old, clusters: old.clusters.map((c) => (c.id === v.cluster.id ? { ...c, label: v.label } : c)) },
      ),
    onError: (e) => setMessage(plain(e)),
  })

  const move = (from: number, delta: number) => {
    const cards = listRef.current?.querySelectorAll<HTMLElement>('[data-cluster]')
    cards?.[from + delta]?.focus()
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

  return (
    <div className="flex max-w-5xl flex-col gap-4 p-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Teach the app</h1>
        <Link to="/cleanup" className="text-sm underline">
          Back to Cleanup
        </Link>
      </div>
      <p>
        {formatCount(data.clusters.length)} group{data.clusters.length === 1 ? '' : 's'}, covering {percent}% of images
      </p>
      <p className="text-sm text-neutral-600 dark:text-neutral-400">
        Mark each group Junk or Keep. With a group selected, press J for Junk or K for Keep, and the arrow keys to move between groups.
      </p>
      {message && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {message}
        </p>
      )}
      {data.clusters.length === 0 ? (
        <p className="text-neutral-600 dark:text-neutral-400">
          No groups yet. Download the picture model and choose Analyze on the Cleanup screen first.
        </p>
      ) : (
        <div ref={listRef} className="grid gap-4 md:grid-cols-2">
          {data.clusters.map((c, i) => (
            <ClusterCard key={c.id} cluster={c} onLabel={(cl, l, t) => label.mutate({ cluster: cl, label: l, tag: t })} onView={setViewing} onMove={(d) => move(i, d)} />
          ))}
        </div>
      )}
      {viewing && (
        <PictureViewer
          items={viewing.items}
          index={viewing.index}
          onIndex={(index) => setViewing({ ...viewing, index })}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  )
}
