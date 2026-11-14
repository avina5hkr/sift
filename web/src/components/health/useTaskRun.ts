import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../lib/api'
import type { TaskEvent } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'

/** Start a background task and follow it until it ends, keeping its newest state for the TaskLine. */
export function useTaskRun(path: string, onDone: (task: TaskEvent) => void) {
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

  const start = async () => {
    setError(null)
    setTask(null)
    setBusy(true)
    try {
      let t = await post<TaskEvent>(path)
      setTask(t)
      while (alive.current && t.state === 'running') {
        await new Promise((r) => setTimeout(r, 400))
        const next = await apiFetch<TaskEvent | null>('/api/tasks/current')
        if (!next) break
        t = next
        setTask(t)
      }
      if (t.state === 'done' && alive.current) onDone(t)
    } catch (e) {
      setError(plain(e))
    } finally {
      setBusy(false)
      setStopping(false)
    }
  }
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
