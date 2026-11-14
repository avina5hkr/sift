import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '../lib/api'
import { useEventStore } from '../lib/events'
import { BTN } from './apps/ui'
import { Modal } from './delete/Modal'

type Crash = { report: string; summary: string }

/** The crash dialog (spec 14): an uncaught exception wrote a report; say so, offer the logs folder. */
export function CrashDialog() {
  const [crash, setCrash] = useState<Crash | null>(null)
  const last = useEventStore((s) => s.last)
  const crashEvent = last?.name === 'crash' ? last : null

  const check = useCallback(async () => {
    try {
      const r = await apiFetch<{ crash: Crash | null }>('/api/crash')
      if (r?.crash?.report) setCrash(r.crash)
    } catch {
      // No answer from the engine is not a reason to show a dialog.
    }
  }, [])

  // On start, and again whenever a `crash` event arrives on the stream.
  useEffect(() => {
    void check()
  }, [check, crashEvent])

  const close = () => {
    const report = crash?.report
    setCrash(null)
    if (report) {
      void apiFetch<void>('/api/crash/seen', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ report }),
      }).catch(() => undefined)
    }
  }
  const openLogs = () => void apiFetch<void>('/api/logs/open', { method: 'POST' }).catch(() => undefined)

  if (!crash) return null
  return (
    <Modal label="Sift hit a problem" onClose={close}>
      <h2 className="text-lg font-semibold">Sift hit a problem</h2>
      <p className="text-sm">Sift wrote a report about it. Your backups are safe.</p>
      {crash.summary && (
        <p className="max-h-40 overflow-auto font-mono text-xs break-words text-neutral-600 dark:text-neutral-400">{crash.summary}</p>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={openLogs} className={BTN}>
          Open logs
        </button>
        <button type="button" data-autofocus onClick={close} className={BTN}>
          Close
        </button>
      </div>
    </Modal>
  )
}
