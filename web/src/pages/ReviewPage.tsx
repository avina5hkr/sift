import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { GroupRow } from '../components/cleanup/GroupRow'
import { JunkReview } from '../components/cleanup/JunkReview'
import { Lightbox } from '../components/cleanup/Lightbox'
import { ReviewTabs } from '../components/cleanup/ReviewTabs'
import {
  copyTop,
  columnsFor,
  CELL_H,
  ESTIMATED_ROW_H,
  rowHeight,
} from '../components/cleanup/layout'
import { type ReviewTab, useReview } from '../components/cleanup/useReview'
import { DeleteFlow } from '../components/delete/DeleteFlow'
import { DeleteSelectedButton } from '../components/delete/DeleteSelectedButton'
import { useDeleteRun } from '../components/delete/useDeleteRun'
import { formatBytes, formatCount } from '../lib/format'
import type { ReviewGroup, ReviewItem, ReviewSort } from '../lib/types'
import { buildOffsets, visibleRange } from '../lib/windowing'

const SORTS: { value: ReviewSort; label: string }[] = [
  { value: 'size', label: 'Biggest saving first' },
  { value: 'date', label: 'Newest first' },
  { value: 'group', label: 'Most copies first' },
]
const FALLBACK_W = 1000
const FALLBACK_H = 700

export default function ReviewPage() {
  const [params, setParams] = useSearchParams()
  const wanted = params.get('tab')
  const tab: ReviewTab = wanted === 'near' || wanted === 'similar' || wanted === 'junk' ? wanted : 'exact'
  const [sort, setSort] = useState<ReviewSort>('size')
  const onTab = (t: ReviewTab) => setParams(t === 'exact' ? {} : { tab: t })
  if (tab === 'junk') return <JunkReview tabs={<ReviewTabs tab={tab} onTab={onTab} />} />
  return (
    <Review
      key={`${tab}-${sort}`}
      tab={tab}
      onTab={onTab}
      sort={sort}
      onSort={setSort}
    />
  )
}

function Review({
  tab,
  onTab,
  sort,
  onSort,
}: {
  tab: ReviewTab
  onTab: (t: ReviewTab) => void
  sort: ReviewSort
  onSort: (s: ReviewSort) => void
}) {
  const review = useReview(sort, tab)
  const { groups, flat, copies, isChecked, setChecked, setAll, loadMore } = review
  const flow = useDeleteRun({ onFinished: () => void review.reload() })
  const flowActive = flow.active
  const startDelete = async () => {
    const ids = await review.loadAllTicked()
    await flow.start({ source: 'cleanup', ids })
  }

  // -- the window: which rows are drawn
  const scroller = useRef<HTMLDivElement>(null)
  const [view, setView] = useState({ top: 0, width: FALLBACK_W, height: FALLBACK_H })
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    const measure = () =>
      setView((v) => ({ top: el.scrollTop, width: el.clientWidth || FALLBACK_W, height: el.clientHeight || FALLBACK_H }) satisfies typeof v)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const frame = useRef(0)
  const onScroll = () => {
    if (frame.current) return
    frame.current = requestAnimationFrame(() => {
      frame.current = 0
      const el = scroller.current
      if (el) setView((v) => ({ ...v, top: el.scrollTop }))
    })
  }
  useEffect(() => () => cancelAnimationFrame(frame.current), [])

  const cols = columnsFor(view.width)
  const offsets = useMemo(() => buildOffsets(groups.map((g) => rowHeight(g, cols))), [groups, cols])
  const unloaded = Math.max(0, review.total - groups.length)
  const loadedHeight = offsets[offsets.length - 1] as number
  const totalHeight = loadedHeight + unloaded * ESTIMATED_ROW_H
  const [start, end] = visibleRange(offsets, view.top, view.height)

  // More pages load as the end of what is loaded comes into view.
  useEffect(() => {
    if (review.more && view.top + view.height + 1500 >= loadedHeight) void loadMore()
  }, [review.more, view.top, view.height, loadedHeight, loadMore])

  // -- focus (a position in the list, not a DOM focus, so it survives rows leaving the window)
  const list = useRef<HTMLDivElement>(null)
  const [focusId, setFocusId] = useState<number | null>(null)
  const focused = focusId ?? flat.items[0]?.id ?? null
  const [openId, setOpenId] = useState<number | null>(null)

  const reveal = useCallback(
    (id: number) => {
      const el = scroller.current
      const at = flat.at.get(id)
      if (!el || !at) return
      const rowTop = offsets[at.group] as number
      const top = rowTop + (at.copy < 0 ? 0 : copyTop(at.copy, cols))
      const bottom = rowTop + (at.copy < 0 ? rowHeight(groups[at.group] as ReviewGroup, cols) : copyTop(at.copy, cols) + CELL_H)
      if (top < el.scrollTop) el.scrollTop = top - 8
      else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight + 8
      setView((v) => ({ ...v, top: el.scrollTop }))
    },
    [flat, offsets, cols, groups, setView],
  )
  const moveTo = useCallback(
    (item: ReviewItem | undefined) => {
      if (!item) return
      setFocusId(item.id)
      reveal(item.id)
    },
    [reveal, setFocusId],
  )

  // -- changing the ticks
  const anchor = useRef<number | null>(null)
  const toggle = useCallback(
    (item: ReviewItem, shift = false) => {
      setFocusId(item.id)
      list.current?.focus({ preventScroll: true })
      const target = !isChecked(item)
      const from = anchor.current
      if (shift && from != null) {
        const a = copies.findIndex((c) => c.id === from)
        const b = copies.findIndex((c) => c.id === item.id)
        if (a >= 0 && b >= 0) {
          const range = copies.slice(Math.min(a, b), Math.max(a, b) + 1)
          setChecked(range.map((c) => c.id), target)
          anchor.current = item.id
          return
        }
      }
      setChecked([item.id], target)
      anchor.current = item.id
    },
    [copies, isChecked, setChecked, setFocusId],
  )
  const selectCopies = useCallback((g: ReviewGroup, checked: boolean) => setChecked(g.copies.map((c) => c.id), checked), [setChecked])
  const open = useCallback(
    (item: ReviewItem) => {
      setFocusId(item.id)
      setOpenId(item.id)
    },
    [setFocusId, setOpenId],
  )
  const closeLightbox = useCallback(() => {
    setOpenId(null)
    setTimeout(() => list.current?.focus({ preventScroll: true }), 0)
  }, [setOpenId])

  // -- keys
  const neighbour = (id: number, key: string): ReviewItem | undefined => {
    const at = flat.at.get(id)
    if (!at) return flat.items[0]
    const i = flat.items.findIndex((x) => x.id === id)
    if (key === 'ArrowLeft') return flat.items[i - 1]
    if (key === 'ArrowRight') return flat.items[i + 1]
    const g = groups[at.group] as ReviewGroup
    if (key === 'ArrowDown') {
      if (at.copy < 0) return g.copies[0] ?? groups[at.group + 1]?.keeper ?? undefined
      return g.copies[at.copy + cols] ?? groups[at.group + 1]?.keeper ?? undefined
    }
    // ArrowUp
    if (at.copy < 0) {
      const p = groups[at.group - 1]
      return p ? (p.copies[p.copies.length - 1] ?? p.keeper ?? undefined) : undefined
    }
    return at.copy >= cols ? g.copies[at.copy - cols] : (g.keeper ?? undefined)
  }
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => undefined)
  const onKeyDown = (e: KeyboardEvent) => {
    if (openId != null || flowActive) return // the lightbox or the delete dialogs have the keys
    const t = e.target as HTMLElement
    if (t.closest('input, textarea, select')) return
    if (e.metaKey || e.ctrlKey) {
      const k = e.key.toLowerCase()
      if (k === 'a' || k === 'd') {
        e.preventDefault()
        setAll(k === 'a')
      }
      return
    }
    if (e.altKey) return
    if (!(list.current?.contains(t) || t === document.body)) return
    if (focused == null) return
    const item = flat.items.find((x) => x.id === focused)
    if (!item) return
    if (e.key === ' ' || e.key.toLowerCase() === 'x') {
      e.preventDefault()
      if (item.role !== 'keeper') toggle(item)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      open(item)
    } else if (e.key.startsWith('Arrow')) {
      e.preventDefault()
      moveTo(neighbour(item.id, e.key))
    }
  }
  useEffect(() => {
    keyHandler.current = onKeyDown
  })
  useEffect(() => {
    const on = (e: KeyboardEvent) => keyHandler.current(e)
    document.addEventListener('keydown', on)
    return () => document.removeEventListener('keydown', on)
  }, [])
  useEffect(() => {
    if (review.loaded) list.current?.focus({ preventScroll: true })
  }, [review.loaded])

  const openIndex = openId == null ? -1 : flat.items.findIndex((x) => x.id === openId)
  const hashOf = useCallback(
    (item: ReviewItem) => {
      const at = flat.at.get(item.id)
      return at ? (groups[at.group]?.content_hash ?? '') : ''
    },
    [flat, groups],
  )
  const { count, bytes } = review.totals

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
        <ReviewTabs tab={tab} onTab={onTab} />
        <label className="ml-auto flex items-center gap-2 text-sm">
          Sort by
          <select
            value={sort}
            onChange={(e) => onSort(e.target.value as ReviewSort)}
            className="rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value} className="text-black">
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {review.error && (
        <div role="alert" className="flex items-center gap-3 border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
          <span className="flex-1">{review.error}</span>
          <button type="button" onClick={review.clearError} className="underline">
            Dismiss
          </button>
        </div>
      )}

      <div
        ref={scroller}
        onScroll={onScroll}
        className="relative min-h-0 flex-1 overflow-auto"
        data-testid="review-scroller"
      >
        {!review.loaded && !review.error && <p className="p-6 text-neutral-600 dark:text-neutral-400">Loading…</p>}
        {review.loaded && groups.length === 0 && <p className="p-6 text-neutral-600 dark:text-neutral-400">No exact duplicates found.</p>}
        <div
          ref={list}
          tabIndex={0}
          role="group"
          aria-label="Duplicate groups"
          aria-activedescendant={focused != null ? `tile-${focused}` : undefined}
          className="relative outline-none"
          style={{ height: totalHeight }}
          data-testid="review-list"
        >
          {groups.slice(start, end).map((g, i) => (
            <GroupRow
              key={g.group_id}
              group={g}
              cols={cols}
              top={offsets[start + i] as number}
              height={(offsets[start + i + 1] as number) - (offsets[start + i] as number)}
              checked={isChecked}
              focusId={focused}
              onToggle={toggle}
              onOpen={open}
              onSelectCopies={selectCopies}
            />
          ))}
        </div>
      </div>

      <footer className="flex flex-wrap items-center gap-3 border-t border-neutral-200 bg-[var(--bg)] px-4 py-3 dark:border-neutral-800">
        <p role="status" className="min-w-0 flex-1 text-sm">
          <span className="font-medium">
            {formatCount(count)} selected, {formatBytes(bytes)}
          </span>
          <span className="text-neutral-600 dark:text-neutral-400">
            {review.more
              ? ` so far · ${formatCount(groups.length)} of ${formatCount(review.total)} groups loaded`
              : ` · ${formatCount(review.total)} group${review.total === 1 ? '' : 's'}`}
          </span>
        </p>
        <button
          type="button"
          onClick={() => setAll(true)}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          Select all in tab
        </button>
        <button
          type="button"
          onClick={() => setAll(false)}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          Select none
        </button>
        <DeleteSelectedButton count={count} bytes={bytes} busy={flow.phase !== 'idle'} onClick={() => void startDelete()} />
      </footer>
      <DeleteFlow flow={flow} />

      {openIndex >= 0 && (
        <Lightbox
          items={flat.items}
          index={openIndex}
          checked={isChecked(flat.items[openIndex] as ReviewItem)}
          hashOf={hashOf}
          onIndex={(i) => {
            const next = flat.items[i]
            if (next) {
              setOpenId(next.id)
              setFocusId(next.id)
              reveal(next.id)
            }
          }}
          onClose={closeLightbox}
          onKeep={(item) => setChecked([item.id], false)}
        />
      )}
    </div>
  )
}
