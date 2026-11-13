import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes, formatCount } from '../../lib/format'
import type { SetsTree } from '../../lib/types'
import { Button, ErrorNote, StepHeading, type StepProps } from './ui'
import { plain, post } from './request'

const POLL_MS = 1500

export function WhatToBackUp({ next }: StepProps) {
  const qc = useQueryClient()
  const tree = useQuery({
    queryKey: ['setup', 'sets'],
    queryFn: () => apiFetch<SetsTree>('/api/setup/sets'),
    refetchInterval: (q) => (q.state.data?.scanning ? POLL_MS : false),
    retry: false,
  })
  const [ticked, setTicked] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const scan = useMutation({
    mutationFn: () => post<SetsTree>('/api/setup/scan'),
    onMutate: () => setError(null),
    onSuccess: (data) => qc.setQueryData(['setup', 'sets'], data),
    onError: (e) => setError(plain(e)),
  })
  const save = useMutation({
    mutationFn: (names: string[]) => post<SetsTree>('/api/setup/sets', { enabled: names }),
    onMutate: () => setError(null),
    onSuccess: (data) => {
      qc.setQueryData(['setup', 'sets'], data)
      next()
    },
    onError: (e) => setError(plain(e)),
  })

  const data = tree.data
  const sets = data?.sets ?? []
  const scanned = sets.some((s) => s.files != null)
  // Look through the phone once, by itself, the first time this step shows with a phone connected.
  const started = useRef(false)
  useEffect(() => {
    if (data && data.phone_connected && !data.scanning && !scanned && !started.current) {
      started.current = true
      scan.mutate()
    }
  }, [data, scanned, scan])

  const isOn = (name: string, enabled: boolean) => ticked[name] ?? enabled
  const selected = sets.filter((s) => isOn(s.name, s.enabled))
  const total = selected.reduce((t, s) => ({ files: t.files + (s.files ?? 0), bytes: t.bytes + (s.bytes ?? 0) }), {
    files: 0,
    bytes: 0,
  })
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>What to back up</StepHeading>
      <p>
        Tick the groups of files to copy. Sift looks through your phone to show how big each one is. Looking does not
        copy anything.
      </p>
      {tree.isPending && <p role="status">Loading…</p>}
      {tree.isError && <ErrorNote message={plain(tree.error)} />}
      {data && !data.phone_connected && (
        <p role="status">
          Connect your phone to see the sizes. You can still choose what to back up now, and the sizes appear when the
          phone is connected.
        </p>
      )}
      {data?.scanning && <p role="status">Looking through your phone…</p>}
      {sets.length > 0 && (
        <fieldset className="flex flex-col gap-3">
          <legend className="sr-only">Backup sets</legend>
          <ul className="flex flex-col gap-3">
            {sets.map((s) => (
              <li key={s.name}>
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={isOn(s.name, s.enabled)}
                    onChange={(e) => setTicked((t) => ({ ...t, [s.name]: e.target.checked }))}
                  />
                  <span className="flex-1">
                    <span className="font-medium">{s.name}</span>
                    <span className="ml-2 text-sm text-neutral-600 dark:text-neutral-400">
                      {s.files != null && s.bytes != null
                        ? `${formatCount(s.files)} files, ${formatBytes(s.bytes)}`
                        : data?.scanning
                          ? 'measuring…'
                          : 'size not known yet'}
                    </span>
                  </span>
                </label>
                <ul className="ml-7 mt-1 text-sm text-neutral-600 dark:text-neutral-400">
                  {s.paths.map((p) => (
                    <li key={p} className="font-mono break-all">
                      {p}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </fieldset>
      )}
      {scanned && (
        <p role="status" className="text-sm font-medium">
          Ticked: {formatCount(total.files)} files, {formatBytes(total.bytes)}
        </p>
      )}
      <ErrorNote message={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Button
          primary
          disabled={selected.length === 0 || save.isPending}
          onClick={() => save.mutate(selected.map((s) => s.name))}
        >
          Save and continue
        </Button>
        {selected.length === 0 && <span className="text-sm">Tick at least one group.</span>}
        <Button disabled={!data?.phone_connected || data?.scanning || scan.isPending} onClick={() => scan.mutate()}>
          Look again
        </Button>
      </div>
    </section>
  )
}
