import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, apiFetch } from '../../lib/api'
import { useEventStore } from '../../lib/events'
import type { DeleteBody, DeleteRun, TaskEvent, TaskKind } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'

/** Where the delete flow is: nothing, the checks, the result of the checks, deleting, or the end. */
export type DeletePhase = 'idle' | 'error' | 'checking' | 'result' | 'deleting' | 'finished'

type Options = { onFinished?: () => void }

/**
 * One delete flow, shared by the review tabs, Analyse phone and Ready to free: start the checks,
 * follow them, show what passed, delete after a confirm, follow that, show how it ended.
 * The run is read from the engine only when a task ends (it lists every file).
 */
export function useDeleteRun({ onFinished }: Options = {}) {
  const qc = useQueryClient()
  const [runId, setRunId] = useState<number | null>(null)
  const [stage, setStage] = useState<'checks' | 'delete'>('checks')
  const [starting, setStarting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [hint, setHint] = useState(0)
  const lastBody = useRef<DeleteBody | null>(null)

  const kind: TaskKind = stage === 'checks' ? 'preflight' : 'delete'
  const stream = useEventStore((s) => s.task)
  const polled = useQuery({
    queryKey: ['delete', 'task', runId, stage],
    queryFn: () => apiFetch<TaskEvent | null>('/api/tasks/current'),
    enabled: runId != null,
    refetchInterval: (q) => {
      const t = q.state.data
      return t && t.kind === kind && t.state !== 'running' ? false : 1000
    },
  })
  const match = (t: TaskEvent | null | undefined) => (t && t.kind === kind ? t : null)
  const task = runId == null ? null : (match(polled.data) ?? match(stream))
  const ended = task != null && task.state !== 'running'

  const runQuery = useQuery({
    queryKey: ['delete', 'run', runId, stage],
    queryFn: () => apiFetch<DeleteRun>(`/api/delete/runs/${runId}`),
    enabled: runId != null && ended,
  })
  const run = runQuery.data ?? null

  let phase: DeletePhase = 'idle'
  if (starting) phase = 'checking'
  else if (runId == null) phase = error ? 'error' : 'idle'
  else if (stage === 'checks') phase = !run ? 'checking' : 'result'
  else phase = run ? 'finished' : 'deleting'
  // The checks ended without a verdict (the task failed): say so rather than wait for ever.
  const checksFailed = stage === 'checks' && run != null && run.state === 'preflight'

  // When the deleting ends, tell the screens to read again.
  const finishedRef = useRef(onFinished)
  useEffect(() => {
    finishedRef.current = onFinished
  })
  const done = phase === 'finished'
  useEffect(() => {
    if (!done) return
    void qc.invalidateQueries({ queryKey: ['cleanup'] })
    void qc.invalidateQueries({ queryKey: ['status'] })
    void qc.invalidateQueries({ queryKey: ['backup'] })
    finishedRef.current?.()
  }, [done, qc])

  const fail = (e: unknown) => {
    setError(plain(e))
    setErrorCode(e instanceof ApiError ? e.code : null)
  }

  const start = useCallback(
    async (body: DeleteBody) => {
      lastBody.current = body
      setError(null)
      setErrorCode(null)
      setRunId(null)
      setStage('checks')
      setConfirming(false)
      setExecuting(false)
      setHint(body.source === 'cleanup' ? body.ids.length : 0)
      setStarting(true)
      try {
        const started = await post<{ run_id: number; task: TaskEvent }>('/api/delete/preflight', body)
        useEventStore.setState({ task: started.task })
        setRunId(started.run_id)
      } catch (e) {
        fail(e)
      } finally {
        setStarting(false)
      }
    },
    [],
  )

  const stop = useCallback(async () => {
    setStopping(true)
    try {
      await post('/api/tasks/stop')
    } catch (e) {
      fail(e)
    } finally {
      setStopping(false)
    }
  }, [])

  const execute = useCallback(
    async (typed?: number) => {
      if (runId == null) return
      setExecuting(true)
      setError(null)
      setErrorCode(null)
      try {
        const t = await post<TaskEvent>('/api/delete/execute', {
          run_id: runId,
          confirm: true,
          ...(typed != null ? { typed_count: typed } : {}),
        })
        useEventStore.setState({ task: t })
        setHint(run?.passed ?? 0)
        setStage('delete')
        setConfirming(false)
      } catch (e) {
        fail(e)
      } finally {
        setExecuting(false)
      }
    },
    [runId, run],
  )

  const close = useCallback(() => {
    setRunId(null)
    setStage('checks')
    setConfirming(false)
    setError(null)
    setErrorCode(null)
  }, [])

  return {
    phase,
    active: phase !== 'idle',
    run,
    task,
    /** How many files the checks cover, as far as is known. */
    total: task && task.total > 0 ? task.total : hint,
    error,
    errorCode,
    checksFailed,
    confirming,
    executing,
    stopping,
    start,
    stop,
    execute,
    close,
    openConfirm: () => {
      setError(null)
      setErrorCode(null)
      setConfirming(true)
    },
    closeConfirm: () => {
      setConfirming(false)
      setError(null)
      setErrorCode(null)
    },
    /** Run the checks again on the same files. */
    recheck: () => {
      if (lastBody.current) void start(lastBody.current)
    },
  }
}

export type DeleteRunApi = ReturnType<typeof useDeleteRun>
