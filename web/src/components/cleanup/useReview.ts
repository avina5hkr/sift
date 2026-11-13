import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../../lib/api'
import type { CandidatesPage, ReviewGroup, ReviewItem, ReviewSort } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'

export type ReviewTab = 'exact' | 'near' | 'similar'

type Position = { group: number; copy: number } // copy -1 is the keeper

/**
 * The Review screen's data: the groups loaded so far (a page at a time), which copies are ticked,
 * and the changes to the ticks. Every change is shown at once and saved through POST
 * /api/selection; if the save fails the change is undone and a message is set.
 */
export function useReview(sort: ReviewSort, TAB: ReviewTab = 'exact') {
  const [groups, setGroups] = useState<ReviewGroup[]>([])
  const [total, setTotal] = useState(0)
  const [more, setMore] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [sel, setSel] = useState<Map<number, boolean>>(() => new Map())
  const [error, setError] = useState<string | null>(null)

  const selRef = useRef(sel)
  const cursor = useRef<number | null>(0)
  const inflight = useRef<Promise<void> | null>(null)
  const epoch = useRef(0) // bumped around a bulk change, so a page read meanwhile is read again

  const commit = useCallback((m: Map<number, boolean>) => {
    selRef.current = m
    setSel(m)
  }, [])

  const loadMore = useCallback((): Promise<void> => {
    if (inflight.current) return inflight.current
    if (cursor.current == null) return Promise.resolve()
    const run = (async () => {
      try {
        for (;;) {
          const at = cursor.current
          const seen = epoch.current
          if (at == null) return
          const page = await apiFetch<CandidatesPage>(
            `/api/cleanup/candidates?tab=${TAB}&sort=${sort}&cursor=${at}`,
          )
          if (seen !== epoch.current) continue
          cursor.current = page.next_cursor
          const m = new Map(selRef.current)
          for (const g of page.groups) for (const c of g.copies) if (!m.has(c.id)) m.set(c.id, c.checked)
          commit(m)
          setGroups((prev) => (at === 0 ? page.groups : [...prev, ...page.groups]))
          setTotal(page.total_groups)
          setMore(page.next_cursor != null)
          setLoaded(true)
          return
        }
      } catch (e) {
        setError(plain(e))
      } finally {
        inflight.current = null
      }
    })()
    inflight.current = run
    return run
  }, [sort, commit, TAB])

  // Each sort is its own mount (the page keys it by sort), so this only ever loads the first page.
  useEffect(() => {
    void loadMore()
  }, [loadMore])

  const isChecked = useCallback((item: ReviewItem) => sel.get(item.id) ?? item.checked, [sel])

  const setChecked = useCallback(
    (ids: number[], checked: boolean) => {
      const now = selRef.current
      const changed = ids.filter((id) => now.get(id) !== checked)
      if (changed.length === 0) return
      const before = new Map(changed.map((id) => [id, now.get(id)]))
      const next = new Map(now)
      for (const id of changed) next.set(id, checked)
      commit(next)
      post('/api/selection', { ids: changed, checked }).catch((e: unknown) => {
        const undone = new Map(selRef.current)
        for (const id of changed) undone.set(id, before.get(id) ?? !checked)
        commit(undone)
        setError(plain(e))
      })
    },
    [commit],
  )

  /** Tick or untick every copy in the tab, also those not loaded yet (they load afterwards). */
  const setAll = useCallback(
    (checked: boolean) => {
      const before = selRef.current
      epoch.current += 1
      const next = new Map(before)
      for (const id of next.keys()) next.set(id, checked)
      commit(next)
      post('/api/selection', { all_in_tab: TAB, checked })
        .then(async () => {
          epoch.current += 1
          // The server now holds `checked` for every copy, including any page that loaded while
          // the request was on its way with the old values.
          const done = new Map(selRef.current)
          for (const id of done.keys()) done.set(id, checked)
          commit(done)
          let last: number | null
          do {
            last = cursor.current
            await loadMore()
          } while (cursor.current != null && cursor.current !== last)
        })
        .catch((e: unknown) => {
          epoch.current += 1
          commit(before)
          setError(plain(e))
        })
    },
    [commit, loadMore, TAB],
  )

  /** Every item in reading order (a group's keeper, then its copies) and where each one sits. */
  const flat = useMemo(() => {
    const items: ReviewItem[] = []
    const at = new Map<number, Position>()
    groups.forEach((g, gi) => {
      at.set(g.keeper.id, { group: gi, copy: -1 })
      items.push(g.keeper)
      g.copies.forEach((c, ci) => {
        at.set(c.id, { group: gi, copy: ci })
        items.push(c)
      })
    })
    return { items, at }
  }, [groups])

  const copies = useMemo(() => groups.flatMap((g) => g.copies), [groups])

  const totals = useMemo(() => {
    let count = 0
    let bytes = 0
    for (const c of copies) {
      if (sel.get(c.id) ?? c.checked) {
        count += 1
        bytes += c.size
      }
    }
    return { count, bytes }
  }, [copies, sel])

  return {
    groups,
    total,
    more,
    loaded,
    error,
    clearError: () => setError(null),
    loadMore,
    isChecked,
    setChecked,
    setAll,
    flat,
    copies,
    totals,
  }
}
