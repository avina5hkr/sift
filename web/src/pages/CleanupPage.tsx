import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { ReasonButton } from '../components/backup/ReasonButton'
import { TaskLine } from '../components/backup/TaskLine'
import { StorageBar } from '../components/cleanup/StorageBar'
import { apiFetch } from '../lib/api'
import { useEventStore } from '../lib/events'
import { formatAgo, formatBytes, formatCount } from '../lib/format'
import type { CleanupSummary, TaskEvent } from '../lib/types'
import { plain, post } from './Wizard/request'

export default function CleanupPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const summary = useQuery({
    queryKey: ['cleanup', 'summary'],
    queryFn: () => apiFetch<CleanupSummary>('/api/cleanup/summary'),
  })
  const streamTask = useEventStore((s) => s.task)
  const currentTask = useQuery({
    queryKey: ['backup', 'task'],
    queryFn: () => apiFetch<TaskEvent | null>('/api/tasks/current'),
  })
  const task = streamTask ?? currentTask.data ?? null
  const running = task?.state === 'running'
  const analysing = running && task.kind === 'analyze'
  const [message, setMessage] = useState<string | null>(null)

  const settled = () => {
    void qc.invalidateQueries({ queryKey: ['cleanup'] })
    void qc.invalidateQueries({ queryKey: ['backup', 'task'] })
  }
  const start = useMutation({
    mutationFn: () => post<TaskEvent>('/api/cleanup/analyze'),
    onMutate: () => setMessage(null),
    onSuccess: (t) => useEventStore.setState({ task: t }),
    onError: (e) => setMessage(plain(e)),
    onSettled: settled,
  })
  const stop = useMutation({
    mutationFn: () => post('/api/cleanup/cancel'),
    onError: (e) => setMessage(plain(e)),
    onSettled: settled,
  })

  const data = summary.data
  if (!data) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Cleanup</h1>
        <p className="mt-2 text-neutral-600 dark:text-neutral-400">
          {summary.error ? plain(summary.error) : 'Loading…'}
        </p>
      </div>
    )
  }

  const reason = !data.has_backup
    ? 'Back up your phone first. Cleanup works from your backup.'
    : running && !analysing
      ? 'Another task is running. Wait for it to finish or stop it.'
      : start.isPending
        ? 'Starting…'
        : null
  const card = data.cards.find((c) => c.tab === 'exact')

  return (
    <div className="flex max-w-3xl flex-col gap-5 p-6">
      <h1 className="text-xl font-semibold">Cleanup</h1>

      <section aria-label="Phone storage" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
        <h2 className="mb-3 font-medium">What is on your phone</h2>
        <StorageBar storage={data.storage} />
      </section>

      <section aria-label="Analyze" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          {!analysing && (
            <ReasonButton primary reason={reason} onClick={() => start.mutate()}>
              {data.analyzed_at ? 'Analyze again' : 'Analyze'}
            </ReasonButton>
          )}
          <p className="text-sm text-neutral-600 dark:text-neutral-400">
            {data.analyzed_at == null
              ? 'Not analysed yet. Analyze looks through your backup for copies of the same file. It never changes your phone or your backup.'
              : `Analysed ${formatAgo(data.analyzed_at)}${
                  data.new_since > 0 ? `, ${formatCount(data.new_since)} new file${data.new_since === 1 ? '' : 's'}` : ''
                }.`}
          </p>
        </div>
        {analysing && task && <TaskLine task={task} stopping={stop.isPending} onStop={() => stop.mutate()} />}
        {message && (
          <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
            {message}
          </p>
        )}
      </section>

      {data.analyzed_at != null && card && (
        <section aria-label="Exact duplicates" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
          <div className="flex flex-wrap items-center gap-4">
            <div className="min-w-0 flex-1">
              <h2 className="font-medium">Exact duplicates</h2>
              <p className="text-sm text-neutral-600 dark:text-neutral-400">
                {card.groups === 0
                  ? 'No exact copies found.'
                  : `${formatCount(card.groups)} group${card.groups === 1 ? '' : 's'}, ${formatCount(card.files)} extra cop${
                      card.files === 1 ? 'y' : 'ies'
                    }, ${formatBytes(card.bytes)} to free`}
              </p>
            </div>
            <ReasonButton
              reason={card.groups === 0 ? 'There are no duplicates to review.' : null}
              onClick={() => navigate('/cleanup/review')}
            >
              Review
            </ReasonButton>
          </div>
        </section>
      )}
    </div>
  )
}
