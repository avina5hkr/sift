import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../lib/api'
import type { TaskEvent, TaskKind } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'

/**
 * Start a background task and follow it until it ends, keeping its newest state for the TaskLine.
 * `kind` is the kind of task this page runs: when the page is opened while one of that kind is
 * already running (it was started before the page was left), it is followed again.
 */
export function useTaskRun(path: string, kind: TaskKind, onDone: (task: TaskEvent) => void, body?: unknown) {
  const [task, setTask] = useState<TaskEvent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const onDoneRef = useRef(onDone)
  useEffect(() => {
    onDoneRef.current = onDone
  })

  const follow = async (first: TaskEvent) => {
    let t = first
    setTask(t)
    while (alive.current && t.state === 'running') {
      await new Promise((r) => setTimeout(r, 400))
      const next = await apiFetch<TaskEvent | null>('/api/tasks/current')
      if (!alive.current) return
      if (!next) break
      t = next
      setTask(t)
    }
    if (t.state === 'done' && alive.current) onDoneRef.current(t)
  }
  const run = async (begin: () => Promise<TaskEvent>) => {
    setError(null)
    setBusy(true)
    try {
      await follow(await begin())
    } catch (e) {
      if (alive.current) setError(plain(e))
    } finally {
      if (alive.current) {
        setBusy(false)
        setStopping(false)
      }
    }
  }
  const start = async () => {
    setTask(null)
    await run(() => post<TaskEvent>(path, body))
  }

  useEffect(() => {
    void (async () => {
      let current: TaskEvent | null = null
      try {
        current = await apiFetch<TaskEvent | null>('/api/tasks/current')
      } catch {
        return
      }
      if (alive.current && current && current.kind === kind && current.state === 'running') await run(() => Promise.resolve(current))
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const stop = async () => {
    setStopping(true)
    try {
      await post('/api/tasks/stop')
    } catch (e) {
      setError(plain(e))
    }
  }
  return { task, error, busy, stopping, start, stop }
}
