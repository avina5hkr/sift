import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { apiFetch } from '../../lib/api'
import { formatBytes, formatCount } from '../../lib/format'
import type { JunkStatus, ReviewGroup, ReviewItem, ReviewSort } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'
import { DeleteFlow } from '../delete/DeleteFlow'
import { DeleteSelectedButton } from '../delete/DeleteSelectedButton'
import { useDeleteRun } from '../delete/useDeleteRun'
import { Chip } from '../Chip'
import { Lightbox } from './Lightbox'
import { Tile } from './Tile'
import { useReview } from './useReview'

const SORTS: { value: ReviewSort; label: string }[] = [
  { value: 'score', label: 'Most likely junk first' },
  { value: 'size', label: 'Biggest first' },
  { value: 'date', label: 'Newest first' },
]
const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'
const MUTED = 'text-neutral-600 dark:text-neutral-400'

type Review = ReturnType<typeof useReview>

function JunkRow({
  group,
  review,
  onToggle,
  onOpen,
  onNotJunk,
  busy,
  focusedId,
}: {
  group: ReviewGroup
  review: Review
  onToggle: (item: ReviewItem) => void
  onOpen: (item: ReviewItem) => void
  onNotJunk: (group: ReviewGroup) => void
  busy: boolean
  focusedId: number | null
}) {
  return (
    <section data-testid="junk-item" aria-label="Suggested file" className="flex flex-wrap items-start gap-4 border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
      <div className="flex flex-wrap gap-2">
        {group.copies.map((c) => (
          <Tile key={c.id} item={c} reason={group.reasons?.[0]?.text} checked={review.isChecked(c)} focused={focusedId === c.id} onToggle={onToggle} onOpen={onOpen} />
        ))}
      </div>
      <div className="flex min-w-0 flex-1 flex-col items-start gap-2">
        <p className="text-sm">
          {formatBytes(group.reclaimable)}
          {group.copies.length > 1 && <span className={MUTED}> · {group.copies.length} copies</span>}
        </p>
        <div className="flex flex-wrap gap-1.5">
          {(group.reasons ?? []).map((r) => (
            <Chip key={r.code}>{r.text}</Chip>
          ))}
        </div>
        <button type="button" disabled={busy} onClick={() => onNotJunk(group)} className={BTN}>
          Not junk
        </button>
      </div>
    </section>
  )
}

function JunkList({ status, sort, showMaybe, onLabelled }: { status: JunkStatus; sort: ReviewSort; showMaybe: boolean; onLabelled: () => void }) {
  const junk = useReview(sort, 'junk')
  const maybe = useReview(sort, 'maybe', showMaybe)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [openId, setOpenId] = useState<number | null>(null)
  const [focusedId, setFocusedId] = useState<number | null>(null)

  const maybeIds = useMemo(() => new Set(maybe.copies.map((c) => c.id)), [maybe.copies])
  const items = useMemo(() => (showMaybe ? [...junk.flat.items, ...maybe.flat.items] : junk.flat.items), [showMaybe, junk.flat, maybe.flat])
  const hashes = useMemo(() => {
    const m = new Map<number, string>()
    for (const g of [...junk.groups, ...maybe.groups]) for (const c of g.copies) m.set(c.id, g.content_hash)
    return m
  }, [junk.groups, maybe.groups])
  const reviewOf = (item: ReviewItem) => (maybeIds.has(item.id) ? maybe : junk)
  const isChecked = (item: ReviewItem) => reviewOf(item).isChecked(item)
  const toggle = (item: ReviewItem) => reviewOf(item).setChecked([item.id], !isChecked(item))
  const open = useCallback((item: ReviewItem) => setOpenId(item.id), [])

  const notJunk = async (g: ReviewGroup, from: Review) => {
    setBusy(true)
    setError(null)
    try {
      await post('/api/labels', { hashes: [g.content_hash], label: 'keep', source: 'feedback' })
      await from.reload()
      onLabelled()
    } catch (e) {
      setError(plain(e))
    } finally {
      setBusy(false)
    }
  }

  const flow = useDeleteRun({
    onFinished: () => {
      void junk.reload()
      if (showMaybe) void maybe.reload()
    },
  })
  const startDelete = async () => {
    const ids = await junk.loadAllTicked()
    if (showMaybe) ids.push(...(await maybe.loadAllTicked()))
    await flow.start({ source: 'cleanup', ids })
  }

  // Keyboard: J/ArrowDown and K/ArrowUp move between files, Space ticks, Enter opens the picture.
  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined)
  useEffect(() => {
    keys.current = (e) => {
      if (openId != null || flow.phase !== 'idle' || e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement
      if (t.closest('input, textarea, select, [contenteditable="true"]')) return
      if (items.length === 0) return
      const at = focusedId == null ? -1 : items.findIndex((x) => x.id === focusedId)
      const key = e.key.toLowerCase()
      let next: number | null = null
      if (key === 'arrowdown' || key === 'j') next = Math.min(items.length - 1, at + 1)
      else if (key === 'arrowup' || key === 'k') next = Math.max(0, at - 1)
      else if (key === ' ' || key === 'enter') {
        const item = at >= 0 ? items[at] : undefined
        if (!item || t.closest('button, a, [role="button"]')) return
        if (key === ' ') toggle(item)
        else open(item)
        e.preventDefault()
        return
      } else return
      e.preventDefault()
      const item = items[next] as ReviewItem
      setFocusedId(item.id)
      document.getElementById(`tile-${item.id}`)?.scrollIntoView?.({ block: 'nearest' })
    }
  })
  useEffect(() => {
    const on = (e: KeyboardEvent) => keys.current(e)
    document.addEventListener('keydown', on)
    return () => document.removeEventListener('keydown', on)
  }, [])

  const shown = showMaybe ? [junk, maybe] : [junk]
  const count = shown.reduce((n, r) => n + r.totals.count, 0)
  const bytes = shown.reduce((n, r) => n + r.totals.bytes, 0)
  const loadError = junk.error ?? (showMaybe ? maybe.error : null) ?? error
  const openIndex = openId == null ? -1 : items.findIndex((x) => x.id === openId)
  const openItem = openIndex >= 0 ? (items[openIndex] as ReviewItem) : null

  const list = (r: Review, label: string, empty: string) => (
    <div role="group" aria-label={label}>
      {r.loaded && r.groups.length === 0 && <p className={`p-6 ${MUTED}`}>{empty}</p>}
      {r.groups.map((g) => (
        <JunkRow key={g.group_id} group={g} review={r} onToggle={toggle} onOpen={open} onNotJunk={(x) => void notJunk(x, r)} busy={busy} focusedId={focusedId} />
      ))}
      {r.more && (
        <div className="p-4">
          <button type="button" onClick={() => void r.loadMore()} className={BTN}>
            Show more ({formatCount(r.groups.length)} of {formatCount(r.total)})
          </button>
        </div>
      )}
    </div>
  )

  return (
    <>
      {loadError && (
        <div role="alert" className="flex items-center gap-3 border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          <span className="flex-1">{loadError}</span>
          <button type="button" onClick={() => { junk.clearError(); maybe.clearError(); setError(null) }} className="underline">
            Dismiss
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto" data-testid="review-scroller">
        {!junk.loaded && !junk.error && <p className={`p-6 ${MUTED}`}>Loading…</p>}
        {!status.precheck_allowed && status.suggested > 0 && (
          <p className="border-b border-neutral-200 px-4 py-2 text-sm dark:border-neutral-800">
            Suggestions are not ticked until you check a sample (<Link to="/cleanup/calibration" className="underline">Calibration</Link>).
          </p>
        )}
        {list(junk, 'Suggested junk', 'Nothing looks like junk right now.')}
        {showMaybe && (
          <>
            <h2 className="border-y border-neutral-200 bg-neutral-50 px-4 py-2 text-sm font-medium dark:border-neutral-800 dark:bg-neutral-900">
              Lower confidence
              <span className={`font-normal ${MUTED}`}> · Sift is less sure about these. They are never ticked for you.</span>
            </h2>
            {list(maybe, 'Lower confidence', 'Nothing here.')}
          </>
        )}
      </div>
      <footer className="flex flex-wrap items-center gap-3 border-t border-neutral-200 bg-[var(--bg)] px-4 py-3 dark:border-neutral-800">
        <p role="status" className="min-w-0 flex-1 text-sm">
          <span className="font-medium">
            {formatCount(count)} selected, {formatBytes(bytes)}
          </span>
        </p>
        <button type="button" onClick={() => junk.setAll(true)} className={BTN}>
          Select all suggested
        </button>
        <button
          type="button"
          onClick={() => {
            junk.setAll(false)
            if (showMaybe) maybe.setAll(false)
          }}
          className={BTN}
        >
          Select none
        </button>
        <DeleteSelectedButton count={count} bytes={bytes} busy={flow.phase !== 'idle'} onClick={() => void startDelete()} />
      </footer>
      <DeleteFlow flow={flow} />
      {openItem && (
        <Lightbox
          items={items}
          index={openIndex}
          checked={isChecked(openItem)}
          hashOf={(i) => hashes.get(i.id) ?? ''}
          onIndex={(i) => items[i] && setOpenId((items[i] as ReviewItem).id)}
          onClose={() => setOpenId(null)}
          onKeep={(i) => reviewOf(i).setChecked([i.id], false)}
        />
      )}
    </>
  )
}

/** The Junk tab: files Sift suggests deleting, why, and (below, on request) the lower-confidence ones. */
export function JunkReview({ tabs }: { tabs: ReactNode }) {
  const qc = useQueryClient()
  const status = useQuery({
    queryKey: ['cleanup', 'junk', 'status'],
    queryFn: () => apiFetch<JunkStatus>('/api/cleanup/junk/status'),
    retry: false,
  })
  const [sort, setSort] = useState<ReviewSort>('score')
  const [showMaybe, setShowMaybe] = useState(false)
  const data = status.data

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
        {tabs}
        {data && !data.cold_start && (
          <>
            <label className="ml-auto flex items-center gap-2 text-sm">
              <input type="checkbox" checked={showMaybe} onChange={(e) => setShowMaybe(e.target.checked)} />
              Show lower confidence{data.maybe > 0 ? ` (${formatCount(data.maybe)})` : ''}
            </label>
            <label className="flex items-center gap-2 text-sm">
              Sort by
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as ReviewSort)}
                className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
              >
                {SORTS.map((s) => (
                  <option key={s.value} value={s.value} className="text-black">
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>
      {!data && <p className={`p-6 ${MUTED}`}>{status.error ? plain(status.error) : 'Loading…'}</p>}
      {data?.cold_start && (
        <div className="flex max-w-xl flex-col gap-2 p-6">
          <h2 className="text-lg font-semibold">Teach the app first</h2>
          <p>
            Sift needs {formatCount(data.needed_each)} pictures marked Junk and {formatCount(data.needed_each)} marked Keep before it can suggest anything.
          </p>
          <p className={MUTED}>
            So far: {formatCount(data.junk_labels)} of {formatCount(data.needed_each)} Junk, {formatCount(data.keep_labels)} of {formatCount(data.needed_each)} Keep.
          </p>
          <Link to="/cleanup/teach" className="self-start underline">
            Teach the app
          </Link>
        </div>
      )}
      {data && !data.cold_start && (
        <JunkList
          key={sort}
          status={data}
          sort={sort}
          showMaybe={showMaybe}
          onLabelled={() => void qc.invalidateQueries({ queryKey: ['cleanup'] })}
        />
      )}
    </div>
  )
}
