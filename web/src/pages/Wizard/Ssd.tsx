import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import type { SsdVolume } from '../../lib/types'
import { Button, ErrorNote, Hint, StepHeading, type StepProps } from './ui'
import { plain, post } from './request'
import { ChooseFolderButton } from '../../components/ChooseFolderButton'

type Volumes = { volumes: SsdVolume[]; selected_uuid: string; folder?: string }
type SsdFolderSaved = { volume_name: string; folder: string; root: string }
export type SsdAccess = { state: string; message: string }

export function Ssd({ next, settings = false }: StepProps & { settings?: boolean }) {
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['setup', 'volumes'],
    queryFn: () => apiFetch<Volumes>('/api/setup/volumes'),
    retry: false,
  })
  const [picked, setPicked] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [access, setAccess] = useState<SsdAccess | null>(null)
  // After choosing a drive: one small test write, so a macOS prompt or a refusal shows here and
  // not halfway through the first backup. Setup goes on once Sift may write to the drive.
  const check = useMutation({
    mutationFn: () => post<SsdAccess>('/api/setup/ssd-access', {}),
    onMutate: () => setAccess({ state: 'checking', message: 'Checking that Sift may write to the drive…' }),
    onSuccess: (r) => {
      setAccess(r)
      if (r.state === 'ok') next()
    },
    onError: (e) => setAccess({ state: 'error', message: plain(e) }),
  })
  const save = useMutation({
    mutationFn: async (uuid: string | null) => {
      try {
        return await post<Volumes>('/api/setup/ssd', { volume_uuid: uuid })
      } catch (e) {
        // the current SSD holds backups: stopping its use needs a clear yes
        if (e instanceof Error && 'code' in e && (e as { code?: string }).code === 'ssd_has_backups') {
          const ok = window.confirm(`${plain(e)}\n\nStop using this SSD?`)
          if (ok) return post<Volumes>('/api/setup/ssd', { volume_uuid: uuid, confirm_retire: true })
        }
        throw e
      }
    },
    onMutate: () => setError(null),
    onSuccess: (r) => {
      if (r.selected_uuid) check.mutate()
      else next()
    },
    onError: (e) => setError(plain(e)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['setup', 'volumes'] }),
  })
  const [savedFolder, setSavedFolder] = useState<string | null>(null)
  const saveFolder = useMutation({
    mutationFn: (path: string) => post<SsdFolderSaved>('/api/setup/ssd-folder', { path }),
    onMutate: () => setError(null),
    onSuccess: (r) => {
      setSavedFolder(`${r.folder} on ${r.volume_name}`)
      check.mutate()
    },
    onError: (e) => setError(plain(e)),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['setup', 'volumes'] }),
  })

  const volumes = list.data?.volumes ?? []
  const current = picked ?? list.data?.selected_uuid ?? null
  const chosen = volumes.find((v) => v.uuid === current)
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      {settings ? (
        <h2 className="text-lg font-semibold">Backup folder on the SSD</h2>
      ) : (
        <StepHeading>An SSD for a second copy (optional)</StepHeading>
      )}
      <p>
        If you have an external drive, Sift can keep a second copy of your backup on it. You can skip this and add a
        drive later.
      </p>
      <Hint>
        The first time Sift writes to an external drive, macOS asks whether Sift may use removable volumes. Choose Allow,
        or the second copy cannot be made. Sift checks this as soon as you choose the drive.
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
      {list.data?.selected_uuid && (
        <p className="text-sm">
          Backup folder on the drive:{' '}
          <span className="font-mono">{savedFolder ?? (list.data.folder || 'a folder named like the Mac backup folder, at the top of the drive')}</span>
        </p>
      )}
      <ErrorNote message={error} />
      {access && (
        <p
          role="status"
          aria-live="polite"
          className={
            access.state === 'ok'
              ? 'text-sm text-green-800 dark:text-green-300'
              : access.state === 'checking' || access.state === 'waiting'
                ? 'text-sm'
                : 'text-sm text-amber-800 dark:text-amber-300'
          }
        >
          {access.message}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <ChooseFolderButton
          start="/Volumes"
          label="Choose a folder on the drive…"
          onPicked={(path) => saveFolder.mutate(path)}
        />
        <Button primary disabled={!chosen || save.isPending} onClick={() => save.mutate(chosen?.uuid ?? null)}>
          Use this drive
        </Button>
        <Button disabled={save.isPending} onClick={() => save.mutate(null)}>
          {settings ? 'Use no SSD' : 'Skip for now'}
        </Button>
        <Button disabled={list.isFetching} onClick={() => void list.refetch()}>
          Look again
        </Button>
        {list.data?.selected_uuid && (
          <Button disabled={check.isPending} onClick={() => check.mutate()}>
            Check access
          </Button>
        )}
        {!settings && access && access.state !== 'ok' && access.state !== 'checking' && (
          <Button onClick={next}>Continue anyway</Button>
        )}
      </div>
    </section>
  )
}
