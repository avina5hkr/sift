import type { QueryClient } from '@tanstack/react-query'
import { create } from 'zustand'
import { withToken } from './api'
import type { JobEvent, ReplicationEvent, TaskEvent, ToastEvent } from './types'

export type Toast = ToastEvent & { id: number }

type EventState = {
  connected: boolean
  last?: { name: string; data: unknown }
  /** The newest backup job seen in a `progress` event (also after it ends; never a dry run), else null. */
  job: JobEvent | null
  /** When `job` arrived (Date.now()), so a later answer from the server can outrank it. */
  jobAt: number
  toasts: Toast[]
  /** The newest long task (verify, adopt, ...) seen in a `task` event. */
  task: TaskEvent | null
  /** The newest finished Analyse phone run, so its card stays up after the next task starts. */
  analysisId: number | null
  /** The newest copy to the SSD seen in a `replication` event. */
  replication: ReplicationEvent | null
}

export const useEventStore = create<EventState>()(() => ({
  connected: false,
  job: null,
  jobAt: 0,
  toasts: [],
  task: null,
  analysisId: null,
  replication: null,
}))

export function dismissToast(id: number) {
  useEventStore.setState((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }))
}

let nextToastId = 1
const MAX_TOASTS = 4

function asToast(data: unknown): Toast | null {
  if (typeof data !== 'object' || data === null) return null
  const { level, title, message } = data as Partial<ToastEvent>
  if (typeof title !== 'string' || typeof message !== 'string') return null
  const known = level === 'info' || level === 'warning' || level === 'error'
  return { id: nextToastId++, level: known ? level : 'info', title, message }
}

/** A backup job from a `progress` event. A dry run (the wizard's size scan) copies nothing and
 * is not a backup, so it is left out: the Backup screen must not show it as one. */
function asJob(data: unknown): JobEvent | null {
  if (typeof data !== 'object' || data === null) return null
  const job = data as Partial<JobEvent>
  return typeof job.job_id === 'number' && typeof job.state === 'string' && job.type === 'backup' && job.dry_run !== true
    ? (job as JobEvent)
    : null
}

function asTask(data: unknown): TaskEvent | null {
  if (typeof data !== 'object' || data === null) return null
  const t = data as Partial<TaskEvent>
  return typeof t.kind === 'string' && typeof t.state === 'string' ? (t as TaskEvent) : null
}

const EVENT_NAMES = ['hello', 'device', 'job', 'progress', 'log', 'toast', 'ssd', 'task', 'replication'] as const

export function startEventStream(qc: QueryClient): () => void {
  const es = new EventSource(withToken('/api/events'))

  for (const name of EVENT_NAMES) {
    es.addEventListener(name, (e) => {
      let data: unknown = null
      try {
        data = JSON.parse((e as MessageEvent<string>).data)
      } catch {
        // keep going with null data; one bad frame should not stop the stream
      }
      useEventStore.setState(
        // After "hello" (first connect or a reconnect) the job the stream last told us about may
        // be out of date: forget it, and the server's answer (fetched below) takes its place.
        name === 'hello'
          ? { connected: true, last: { name, data }, job: null, jobAt: 0, task: null, replication: null }
          : { last: { name, data } },
      )
      // "hello" also arrives after a reconnect, so a missed device event heals at once.
      if (name === 'hello' || name === 'device' || name === 'job' || name === 'ssd') {
        void qc.invalidateQueries({ queryKey: ['status'] })
      }
      if (name === 'toast') {
        const toast = asToast(data)
        if (toast) useEventStore.setState((s) => ({ toasts: [...s.toasts, toast].slice(-MAX_TOASTS) }))
      }
      if (name === 'task') {
        const task = asTask(data)
        if (task) {
          useEventStore.setState({ task })
          if (task.state !== 'running') {
            // Problems, confirmations and the counts may all have changed.
            void qc.invalidateQueries({ queryKey: ['backup'] })
            const id = task.result?.analysis_id
            if (task.kind === 'analyse' && task.state === 'done' && typeof id === 'number') {
              useEventStore.setState({ analysisId: id })
            }
          }
        }
      }
      if (name === 'replication' && data && typeof data === 'object') {
        const replication = data as ReplicationEvent
        useEventStore.setState({ replication })
        if (replication.state === 'done') {
          void qc.invalidateQueries({ queryKey: ['status'] })
          void qc.invalidateQueries({ queryKey: ['backup'] })
        }
      }
      if (name === 'progress') {
        const job = asJob(data)
        if (job) {
          const before = useEventStore.getState().job
          useEventStore.setState({ job, jobAt: Date.now() })
          // The numbers arrive here; what else the screen shows is fetched again when the job
          // starts, changes state or ends.
          if (!before || before.job_id !== job.job_id || before.state !== job.state) {
            void qc.invalidateQueries({ queryKey: ['backup'] })
          }
        }
      }
      if (name === 'hello') void qc.invalidateQueries({ queryKey: ['backup'] })
    })
  }

  // EventSource reconnects by itself; "hello" marks us connected again.
  es.onerror = () => useEventStore.setState({ connected: false })

  return () => es.close()
}
