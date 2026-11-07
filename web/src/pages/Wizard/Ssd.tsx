import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import type { SsdVolume } from '../../lib/types'
import { Button, ErrorNote, Hint, StepHeading, type StepProps } from './ui'
import { plain, post } from './request'

type Volumes = { volumes: SsdVolume[]; selected_uuid: string }

export function Ssd({ next }: StepProps) {
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['setup', 'volumes'],
    queryFn: () => apiFetch<Volumes>('/api/setup/volumes'),
    retry: false,
  })
  const [picked, setPicked] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: (uuid: string | null) => post<Volumes>('/api/setup/ssd', { volume_uuid: uuid }),
    onMutate: () => setError(null),
    onSuccess: next,
    onError: (e) => setError(plain(e)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['setup', 'volumes'] }),
  })

  const volumes = list.data?.volumes ?? []
  const current = picked ?? list.data?.selected_uuid ?? null
  const chosen = volumes.find((v) => v.uuid === current)
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>An SSD for a second copy (optional)</StepHeading>
      <p>
        If you have an external drive, Sift can keep a second copy of your backup on it. You can skip this and add a
        drive later.
      </p>
      <Hint>
        The first time Sift writes to an external drive, macOS asks whether Sift may use removable volumes. Choose Allow,
        or the second copy cannot be made. Sift only looks at which drives are plugged in for now; copying to the drive
        comes later, so the status bar keeps saying &quot;SSD not connected&quot; until then.
      </Hint>
      {list.isPending && <p role="status">Looking for external drives…</p>}
      {list.isError && <ErrorNote message={plain(list.error)} />}
      {list.data && volumes.length === 0 && (
        <p role="status">No external drive is plugged in. Plug one in and press Look again, or skip this step.</p>
      )}
      {volumes.length > 0 && (
        <fieldset className="flex flex-col gap-2 rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
          <legend className="px-1 text-sm font-medium">Drives</legend>
          {volumes.map((v) => (
            <div key={v.uuid} className="flex flex-col gap-1">
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="ssd" checked={current === v.uuid} onChange={() => setPicked(v.uuid)} />
                <span>
                  {v.name}, {v.file_system}
                  {v.free_bytes != null && `, ${formatBytes(v.free_bytes)} free`}
                </span>
              </label>
              {v.warning && (
                <p role="note" className="ml-6 text-sm text-amber-800 dark:text-amber-300">
                  {v.warning}
                </p>
              )}
            </div>
          ))}
        </fieldset>
      )}
      <ErrorNote message={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Button primary disabled={!chosen || save.isPending} onClick={() => save.mutate(chosen?.uuid ?? null)}>
          Use this drive
        </Button>
        <Button disabled={save.isPending} onClick={() => save.mutate(null)}>
          Skip for now
        </Button>
        <Button disabled={list.isFetching} onClick={() => void list.refetch()}>
          Look again
        </Button>
      </div>
    </section>
  )
}
