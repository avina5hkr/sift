import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatSpeed } from '../../lib/format'
import type { ReaderTestResult, SetsTree } from '../../lib/types'
import { useStatus } from '../../lib/useStatus'
import { Button, ErrorNote, StepHeading } from './ui'
import { plain, post } from './request'

export function Done({ finish }: { finish: (startBackup: string[] | null) => Promise<void> }) {
  const status = useStatus()
  const connected = status.data?.device?.state === 'connected_usb' || status.data?.device?.state === 'connected_wifi'
  const tree = useQuery({ queryKey: ['setup', 'sets'], queryFn: () => apiFetch<SetsTree>('/api/setup/sets') })
  const names = (tree.data?.sets ?? []).filter((s) => s.enabled).map((s) => s.name)
  const [error, setError] = useState<string | null>(null)
  const [speed, setSpeed] = useState<ReaderTestResult | null>(null)
  const test = useMutation({
    mutationFn: () => post<ReaderTestResult>('/api/setup/reader-test'),
    onMutate: () => {
      setError(null)
      setSpeed(null)
    },
    onSuccess: setSpeed,
    onError: (e) => setError(plain(e)),
  })
  const go = useMutation({
    mutationFn: (start: string[] | null) => finish(start),
    onMutate: () => setError(null),
    onError: (e) => setError(plain(e)),
  })
  const busy = test.isPending || go.isPending
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>You are ready</StepHeading>
      <p>
        Sift is set up. If you like, it can first time how fast your phone and cable are, so that it can tell you how
        long a backup will take. It reads some of your biggest files and throws the data away: nothing is copied or
        saved. It takes about ten seconds.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button disabled={!connected || busy} onClick={() => test.mutate()}>
          {test.isPending ? 'Testing…' : 'Test my phone’s speed'}
        </Button>
        {!connected && <span className="text-sm">Connect your phone to run the test.</span>}
      </div>
      {speed && (
        <p role="status">
          {speed.tested && speed.speed_bps != null
            ? `Your phone read about ${formatSpeed(speed.speed_bps)} using ${speed.readers} at a time. ${speed.message}`
            : speed.message}
        </p>
      )}
      <ErrorNote message={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Button primary disabled={!connected || names.length === 0 || busy} onClick={() => go.mutate(names)}>
          {go.isPending ? 'Starting…' : 'Start my first backup'}
        </Button>
        <Button disabled={busy} onClick={() => go.mutate(null)}>
          Finish without backing up
        </Button>
      </div>
      <p className="text-sm">You can run this setup again any time from Settings.</p>
    </section>
  )
}
