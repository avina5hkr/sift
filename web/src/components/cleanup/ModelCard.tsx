import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { plain, post } from '../../pages/Wizard/request'
import { apiFetch } from '../../lib/api'
import { useEventStore } from '../../lib/events'
import { formatBytes } from '../../lib/format'
import type { ModelStatus, TaskEvent } from '../../lib/types'
import { ReasonButton } from '../backup/ReasonButton'

const BOX = 'rounded-lg border border-neutral-200 p-4 dark:border-neutral-800'

/** Smarter suggestions: the one-time, consented download of the picture model. */
export function ModelCard() {
  const qc = useQueryClient()
  const model = useQuery({
    queryKey: ['cleanup', 'models'],
    queryFn: () => apiFetch<ModelStatus>('/api/cleanup/models'),
  })
  const streamTask = useEventStore((s) => s.task)
  const currentTask = useQuery({
    queryKey: ['backup', 'task'],
    queryFn: () => apiFetch<TaskEvent | null>('/api/tasks/current'),
  })
  const task = streamTask ?? currentTask.data ?? null
  const [agreed, setAgreed] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const download = useMutation({
    mutationFn: () => post<TaskEvent>('/api/cleanup/models/download', { consent: true }),
    onMutate: () => setMessage(null),
    onSuccess: (t) => useEventStore.setState({ task: t }),
    onError: (e) => setMessage(plain(e)),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['cleanup'] })
      void qc.invalidateQueries({ queryKey: ['backup', 'task'] })
    },
  })

  const m = model.data
  if (!m) return null

  if (m.installed) {
    return (
      <section aria-label="Smarter suggestions" className={BOX}>
        <h2 className="font-medium">Smarter suggestions</h2>
        <p role="status" className="text-sm text-neutral-600 dark:text-neutral-400">
          The picture model is installed. Choose Analyze again to find look-alike pictures and to search by words.
        </p>
      </section>
    )
  }

  const downloading = task?.kind === 'model' && task.state === 'running'
  const otherTask = task?.state === 'running' && task.kind !== 'model'
  const reason = !agreed
    ? 'Tick “I agree to download it” first.'
    : otherTask
      ? 'Another task is running. Wait for it to finish or stop it.'
      : download.isPending
        ? 'Starting…'
        : null
  const failed = task?.kind === 'model' && task.state === 'failed'

  return (
    <section aria-label="Smarter suggestions" className={`${BOX} flex flex-col gap-3`}>
      <div>
        <h2 className="font-medium">Smarter suggestions</h2>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Download a small picture model (about {formatBytes(m.size_bytes)}). It finds look-alike pictures and lets you search your pictures by
          words. It runs on this Mac only. Your pictures never leave it.
        </p>
      </div>
      {downloading && task ? (
        <div role="status" className="flex flex-col gap-1 text-sm">
          <span>
            Downloading… {formatBytes(task.done)} of {formatBytes(task.total || m.size_bytes)}
          </span>
          <progress className="w-full" value={task.done} max={task.total || m.size_bytes} aria-label="Download progress" />
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />I agree to download it
          </label>
          <ReasonButton primary reason={reason} onClick={() => download.mutate()}>
            Download
          </ReasonButton>
        </div>
      )}
      {failed && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          The download did not finish.{task?.message ? ` ${task.message}` : ''} You can try again.
        </p>
      )}
      {message && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {message}
        </p>
      )}
    </section>
  )
}
