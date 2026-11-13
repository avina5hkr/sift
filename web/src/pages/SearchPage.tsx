import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { PictureViewer } from '../components/cleanup/PictureViewer'
import { ApiError, apiFetch, withToken } from '../lib/api'
import { baseName } from '../lib/format'
import type { PictureItem } from '../lib/types'
import { plain } from './Wizard/request'

export default function SearchPage() {
  const [params, setParams] = useSearchParams()
  const q = (params.get('q') ?? '').trim()
  const [viewing, setViewing] = useState<number | null>(null)

  const results = useQuery({
    queryKey: ['cleanup', 'search', q],
    queryFn: () => apiFetch<PictureItem[]>(`/api/cleanup/search?q=${encodeURIComponent(q)}`),
    enabled: q !== '',
    retry: false,
  })
  const noModel = results.error instanceof ApiError && results.error.code === 'no_model'
  const items = results.data ?? []

  return (
    <div className="flex max-w-5xl flex-col gap-4 p-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Search your pictures</h1>
        <Link to="/cleanup" className="text-sm underline">
          Back to Cleanup
        </Link>
      </div>
      <form
        role="search"
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          const next = String(new FormData(e.currentTarget).get('q') ?? '').trim()
          if (next) setParams({ q: next })
        }}
      >
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm font-medium">
          Search your pictures by words
          <input
            type="search"
            key={q}
            name="q"
            defaultValue={q}
            className="rounded-md border border-neutral-300 bg-transparent px-3 py-1.5 font-normal dark:border-neutral-700"
          />
        </label>
        <button type="submit" className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800">
          Search
        </button>
      </form>

      {q === '' && <p className="text-neutral-600 dark:text-neutral-400">Type a few words, like “good morning” or “birthday”, and press Search.</p>}
      {noModel && (
        <p role="status" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
          Search needs the picture model, which is not downloaded yet. Go to{' '}
          <Link to="/cleanup" className="underline">
            Cleanup
          </Link>
          , tick “I agree to download it” under Smarter suggestions, press Download, then choose Analyze again.
        </p>
      )}
      {results.error && !noModel && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {plain(results.error)}
        </p>
      )}
      {results.isPending && q !== '' && <p className="text-neutral-600 dark:text-neutral-400">Searching…</p>}
      {results.data && items.length === 0 && (
        <p role="status" className="text-neutral-600 dark:text-neutral-400">
          Nothing matched “{q}”. Try other words, or choose Analyze again on the Cleanup screen to include new pictures.
        </p>
      )}
      {items.length > 0 && (
        <ul aria-label="Results" className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
          {items.map((it, i) => (
            <li key={it.id}>
              <button
                type="button"
                aria-label={`Open ${baseName(it.path)}`}
                onClick={() => setViewing(i)}
                className="block aspect-square w-full cursor-zoom-in overflow-hidden rounded bg-neutral-100 p-0 dark:bg-neutral-900"
              >
                <img src={withToken(`/api/media/${it.id}/thumb`)} alt="" loading="lazy" decoding="async" draggable={false} className="h-full w-full object-cover" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {viewing !== null && (
        <PictureViewer
          items={items.map((it) => ({ id: it.id, name: baseName(it.path) }))}
          index={viewing}
          onIndex={setViewing}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  )
}
