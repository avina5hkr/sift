import { useEffect, useRef } from 'react'
import type { ScanInfo } from '../../lib/types'

type Props = {
  scan: ScanInfo | null
  busy: boolean
  error: string | null
  onConfirm: () => void
  onScanAgain: () => void
}

/**
 * Many files are gone from the phone since the last backup. Sift waits for the owner. Scan again
 * is the safe choice and has the focus; Confirm needs a deliberate click and is never focused
 * for you.
 */
export function GuardCard({ scan, busy, error, onConfirm, onScanAgain }: Props) {
  const safeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => safeRef.current?.focus(), [])

  return (
    <section
      role="alertdialog"
      aria-modal="false"
      aria-labelledby="guard-title"
      aria-describedby="guard-text"
      className="rounded-lg border border-amber-400 bg-amber-50 p-4 text-amber-950 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100"
    >
      <h2 id="guard-title" className="text-lg font-semibold">
        Lots of files have gone from your phone
      </h2>
      <div id="guard-text" className="mt-2 text-sm">
        <p>
          Sift found far fewer files on your phone than at the last backup. That can happen if a
          folder was cleared by mistake, or if the phone did not show everything this time.
        </p>
        {scan && scan.warnings.length > 0 && (
          <ul className="mt-2 list-disc pl-5">
            {scan.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        )}
        <p className="mt-2">
          If you deleted them on purpose, choose Confirm. If you are not sure, choose Scan again.
          Nothing has been changed yet, and the copies already on your Mac are safe either way.
        </p>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-sm font-medium">
          {error}
        </p>
      )}
      <div className="mt-3 flex gap-2">
        <button
          ref={safeRef}
          type="button"
          aria-disabled={busy ? true : undefined}
          onClick={() => {
            if (!busy) onScanAgain()
          }}
          className={`rounded-md border border-neutral-900 bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 ${busy ? 'opacity-60' : ''}`}
        >
          Scan again
        </button>
        <button
          type="button"
          aria-disabled={busy || !scan ? true : undefined}
          onClick={() => {
            if (!busy && scan) onConfirm()
          }}
          className={`rounded-md border border-amber-700 px-3 py-1.5 text-sm font-medium hover:bg-amber-200 dark:border-amber-500 dark:hover:bg-amber-900 ${busy || !scan ? 'opacity-60' : ''}`}
        >
          Confirm
        </button>
      </div>
    </section>
  )
}
