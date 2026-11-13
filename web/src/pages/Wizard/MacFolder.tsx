import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import type { FolderCheck, FolderSaved, SetupState } from '../../lib/types'
import { Button, ErrorNote, StepHeading, type StepProps } from './ui'
import { plain, post } from './request'

const DEBOUNCE_MS = 300

function useDebounced<T>(value: T, ms: number): T {
  const [shown, setShown] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setShown(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return shown
}

export function MacFolder({ next, setup }: StepProps & { setup: SetupState }) {
  const [path, setPath] = useState(setup.mac_root)
  const [label, setLabel] = useState(setup.phone?.label ?? setup.suggested_label)
  const [error, setError] = useState<string | null>(null)
  const typed = useDebounced(path, DEBOUNCE_MS)
  const check = useQuery({
    queryKey: ['setup', 'folder', typed],
    queryFn: () => apiFetch<FolderCheck>(`/api/setup/folder?path=${encodeURIComponent(typed)}`),
    retry: false,
  })
  const save = useMutation({
    mutationFn: () => post<FolderSaved>('/api/setup/mac-folder', { path, label }),
    onMutate: () => setError(null),
    onSuccess: next,
    onError: (e) => setError(plain(e)),
  })
  const result = check.data
  const current = typed === path
  const problem = current && result && !result.ok ? result.problem : null
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>Choose the backup folder</StepHeading>
      <p>
        Your phone&apos;s files are copied into this folder on your Mac. Pick a folder with plenty of free space. Sift
        creates it if it does not exist.
      </p>
      <div className="flex flex-col gap-1">
        <label htmlFor="mac-root" className="text-sm font-medium">
          Backup folder
        </label>
        <input
          id="mac-root"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          aria-describedby="mac-root-note"
          aria-invalid={problem ? true : undefined}
          className="rounded-md border border-neutral-300 bg-transparent px-3 py-1.5 font-mono text-sm dark:border-neutral-700"
        />
        <p id="mac-root-note" className="text-sm" aria-live="polite">
          {problem ??
            (current && result?.ok
              ? result.free_bytes != null
                ? `${formatBytes(result.free_bytes)} free here.`
                : 'This folder can be used.'
              : 'Checking…')}
        </p>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="device-label" className="text-sm font-medium">
          Name for this phone&apos;s folder
        </label>
        <input
          id="device-label"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          className="rounded-md border border-neutral-300 bg-transparent px-3 py-1.5 text-sm dark:border-neutral-700"
        />
        <p className="text-sm">Files go into a folder with this name inside the backup folder. Leave it as it is if unsure.</p>
      </div>
      <ErrorNote message={error} />
      <div>
        <Button primary disabled={!current || !result?.ok || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? 'Saving…' : 'Save and continue'}
        </Button>
      </div>
    </section>
  )
}
