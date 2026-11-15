import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import { plain, post } from '../../pages/Wizard/request'

type Place = { path: string; exists: boolean; free_bytes: number | null }
type Storage = {
  data_folder: Place
  overridden: boolean
  settings_file: Place
  database: Place
  mac_backup: Place | null
  ssd: {
    configured: boolean
    connected: boolean
    name: string | null
    volume_id: string | null
    folder: string | null
    status: string | null
  }
}
type Which = 'data' | 'settings' | 'database' | 'mac' | 'ssd'

const MUTED = 'text-neutral-600 dark:text-neutral-400'

/** Where this Sift keeps its data, read-only (changes happen in Destinations). */
export function StorageLocations() {
  const q = useQuery({ queryKey: ['storage'], queryFn: () => apiFetch<Storage>('/api/storage') })
  const [error, setError] = useState<string | null>(null)
  const reveal = useMutation({
    mutationFn: (which: Which) => post('/api/storage/reveal', { which }),
    onMutate: () => setError(null),
    onError: (e) => setError(plain(e)),
  })
  const s = q.data
  const row = (label: string, which: Which, place: Place | null, extra?: string) => (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1">
      <dt className="w-40 shrink-0 font-medium">{label}</dt>
      <dd className="min-w-0 flex-1">
        {place ? (
          <>
            <span className="font-mono break-all">{place.path}</span>
            <span className={`ml-2 ${MUTED}`}>
              {place.exists ? '' : '(not there yet)'}
              {place.free_bytes != null && ` ${formatBytes(place.free_bytes)} free`}
              {extra && ` ${extra}`}
            </span>
          </>
        ) : (
          <span className={MUTED}>Not chosen</span>
        )}
      </dd>
      {place?.exists && (
        <button
          type="button"
          onClick={() => reveal.mutate(which)}
          aria-label={`Reveal ${label.toLowerCase()} in Finder`}
          className="rounded-md border border-neutral-300 px-2 py-0.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          Reveal in Finder
        </button>
      )}
    </div>
  )
  return (
    <section aria-labelledby="storage-title" className="mt-6 flex flex-col gap-2">
      <h2 id="storage-title" className="text-lg font-semibold">
        Storage locations
      </h2>
      <p className={`text-sm ${MUTED}`}>Where this Sift keeps its data. Change the backup folders in Destinations.</p>
      {q.isError && <p role="alert">{plain(q.error)}</p>}
      {s && (
        <dl className="text-sm">
          {row('Data folder', 'data', s.data_folder, s.overridden ? '(set by SIFT_HOME for this run)' : undefined)}
          {row('Settings file', 'settings', s.settings_file)}
          {row('Database', 'database', s.database)}
          {row('Mac backup folder', 'mac', s.mac_backup)}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-1" data-testid="storage-ssd">
            <dt className="w-40 shrink-0 font-medium">SSD</dt>
            <dd className="min-w-0 flex-1">
              {!s.ssd.configured ? (
                <span className={MUTED}>Not chosen</span>
              ) : (
                <>
                  {s.ssd.connected ? `${s.ssd.name ?? 'SSD'}, connected` : 'Not connected'}
                  {s.ssd.volume_id && <span className={`ml-2 ${MUTED}`}>(drive {s.ssd.volume_id}…)</span>}
                  {s.ssd.folder && <span className="ml-2 font-mono break-all">{s.ssd.folder}</span>}
                  {s.ssd.status && <span className="ml-2 font-medium text-amber-800 dark:text-amber-300">{s.ssd.status}</span>}
                </>
              )}
            </dd>
            {s.ssd.connected && s.ssd.folder && (
              <button
                type="button"
                onClick={() => reveal.mutate('ssd')}
                aria-label="Reveal the SSD folder in Finder"
                className="rounded-md border border-neutral-300 px-2 py-0.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
              >
                Reveal in Finder
              </button>
            )}
          </div>
        </dl>
      )}
      {error && <p role="alert" className="text-sm">{error}</p>}
    </section>
  )
}
