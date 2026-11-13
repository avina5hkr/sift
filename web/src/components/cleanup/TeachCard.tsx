import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { apiFetch } from '../../lib/api'
import { formatCount } from '../../lib/format'
import type { ClustersResponse } from '../../lib/types'

const BOX = 'rounded-lg border border-neutral-200 p-4 dark:border-neutral-800'
const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'

/** Teach the app (groups to label) and a words search box. */
export function TeachCard() {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const clusters = useQuery({
    queryKey: ['cleanup', 'clusters'],
    queryFn: () => apiFetch<ClustersResponse>('/api/cleanup/clusters'),
    retry: false,
  })
  const groups = clusters.data?.clusters.length
  const search = () => {
    const text = q.trim()
    if (text) navigate(`/cleanup/search?q=${encodeURIComponent(text)}`)
  }
  return (
    <>
      {groups !== undefined && (
        <section aria-label="Teach the app" className={`${BOX} flex flex-wrap items-center gap-4`}>
          <div className="min-w-0 flex-1">
            <h2 className="font-medium">Teach the app</h2>
            <p className="text-sm text-neutral-600 dark:text-neutral-400">
              {groups === 0
                ? 'No groups of look-alike pictures yet.'
                : `${formatCount(groups)} group${groups === 1 ? '' : 's'} of look-alike pictures. Mark each one Junk or Keep so Sift learns what you want gone.`}
            </p>
          </div>
          <button type="button" className={BTN} onClick={() => navigate('/cleanup/teach')}>
            Open
          </button>
        </section>
      )}
      <section aria-label="Search" className={BOX}>
        <form
          className="flex flex-wrap items-center gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            search()
          }}
        >
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm font-medium">
            Search your pictures by words
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="for example: good morning flowers"
              className="rounded-md border border-neutral-300 bg-transparent px-3 py-1.5 font-normal dark:border-neutral-700"
            />
          </label>
          <button type="submit" className={`${BTN} self-end`}>
            Search
          </button>
        </form>
      </section>
    </>
  )
}
