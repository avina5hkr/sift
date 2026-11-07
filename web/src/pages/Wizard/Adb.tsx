import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import type { AdbState } from '../../lib/types'
import { Button, ErrorNote, Hint, StepHeading, type StepProps } from './ui'
import { plain, post } from './request'

export function Adb({ next }: StepProps) {
  const qc = useQueryClient()
  const adb = useQuery({ queryKey: ['setup', 'adb'], queryFn: () => apiFetch<AdbState>('/api/setup/adb') })
  const [agreed, setAgreed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const download = useMutation({
    mutationFn: () => post<unknown>('/api/devices/adb/download', { consent: true }),
    onMutate: () => setError(null),
    onError: (e) => setError(plain(e)),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['setup', 'adb'] })
      void qc.invalidateQueries({ queryKey: ['status'] })
    },
  })

  const state = adb.data
  const ready = state?.status === 'ready'
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>Phone tools (adb)</StepHeading>
      {adb.isPending && <p role="status">Checking for adb…</p>}
      {adb.isError && <ErrorNote message={plain(adb.error)} />}
      {state && ready && (
        <p role="status" className="font-medium">
          adb is ready{state.version ? ` (version ${state.version})` : ''}. Nothing to download.
        </p>
      )}
      {state && !ready && (
        <>
          <p>
            Sift needs Google&apos;s Android platform-tools (a program called adb) to talk to your phone.{' '}
            {state.status === 'broken' ? 'The copy Sift found does not work.' : 'Sift could not find one on this Mac.'}
          </p>
          <p>
            Sift can download it from Google for you. It is about {formatBytes(state.download_bytes)} and comes from{' '}
            {state.download_host}. This is the only thing Sift ever downloads, and only with your permission.
          </p>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />
            <span>
              I agree to download Google&apos;s platform-tools (about {formatBytes(state.download_bytes)}) from{' '}
              {state.download_host}.
            </span>
          </label>
          <ErrorNote message={error} />
          <div>
            <Button primary disabled={!agreed || download.isPending} onClick={() => download.mutate()}>
              {download.isPending ? 'Downloading…' : 'Download platform-tools'}
            </Button>
          </div>
          <Hint>
            Already have adb? Set phone.adb_path in config.toml to its location instead, then reopen Sift.
          </Hint>
        </>
      )}
      <div>
        <Button primary disabled={!ready} onClick={next}>
          Continue
        </Button>
        {state && !ready && <span className="ml-3 text-sm">Continue is available once adb works.</span>}
      </div>
    </section>
  )
}
