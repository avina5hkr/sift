import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { plain, post } from '../../pages/Wizard/request'
import { BTN, MUTED } from '../apps/ui'

type Out = { tested: boolean; readers: number | null; speed_bps: number | null; message: string }

/** Connection test: reads some of the phone's biggest files for about ten seconds and reports MB/s. */
export function SpeedTest() {
  const [error, setError] = useState<string | null>(null)
  const test = useMutation({
    mutationFn: () => post<Out>('/api/setup/reader-test'),
    onMutate: () => setError(null),
    onError: (e) => setError(plain(e)),
  })
  const out = test.data
  return (
    <section aria-labelledby="speed-title" className="mt-6 flex flex-col gap-2">
      <h2 id="speed-title" className="text-lg font-semibold">
        Connection test
      </h2>
      <p className={MUTED}>Reads some big files from the phone for about ten seconds to see how fast this phone and cable are.</p>
      <div>
        <button type="button" className={BTN} disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? 'Testing…' : 'Test the connection'}
        </button>
      </div>
      {out && (
        <p role="status">
          {out.tested && out.speed_bps != null
            ? `${(out.speed_bps / 1_000_000).toFixed(1)} MB/s with ${out.readers} files read at once. `
            : ''}
          {out.message}
        </p>
      )}
      {error && <p role="alert" className="text-sm text-red-700 dark:text-red-400">{error}</p>}
    </section>
  )
}
