import { useEffect, useRef } from 'react'
import { formatBytes } from '../../lib/format'
import type { FailedFile } from '../../lib/types'

type Props = {
  files: FailedFile[] | undefined
  loading: boolean
  error: string | null
  onClose: () => void
  onRetry: () => void
  /** Retry is unavailable (a backup is running, or one is starting): the plain reason. */
  retryBlocked: string | null
}

/** The files that could not be copied, each with its reason in plain words, and Retry. */
export function FailedDrawer({ files, loading, error, onClose, onRetry, retryBlocked }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => closeRef.current?.focus(), [])

  return (
    <aside
      role="dialog"
      aria-modal="false"
      aria-labelledby="failed-title"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
      className="fixed inset-y-0 right-0 z-30 flex w-[28rem] max-w-full flex-col border-l border-neutral-200 bg-white p-4 shadow-xl dark:border-neutral-800 dark:bg-neutral-950"
    >
      <div className="flex items-center gap-3">
        <h2 id="failed-title" className="flex-1 text-lg font-semibold">
          Files that could not be copied
        </h2>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          className="rounded-md border border-neutral-300 px-3 py-1 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          Close
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-3 rounded-md bg-red-50 p-2 text-sm text-red-900 dark:bg-red-950 dark:text-red-100">
          {error}
        </p>
      )}
      <div className="mt-3 min-h-0 flex-1 overflow-auto">
        {loading && !files ? (
          <p className="text-sm text-neutral-600 dark:text-neutral-400">Loading…</p>
        ) : files && files.length === 0 ? (
          <p className="text-sm text-neutral-600 dark:text-neutral-400">Nothing failed. Every file was copied.</p>
        ) : (
          <ul className="flex flex-col gap-3">
            {files?.map((f) => (
              <li key={f.id} className="rounded-md border border-neutral-200 p-3 text-sm dark:border-neutral-800">
                <p className="font-mono break-all">{f.path}</p>
                <p className="mt-1 text-neutral-600 dark:text-neutral-400">{formatBytes(f.size)}</p>
                <p className="mt-1">{f.reason}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
      {files && files.length > 0 && (
        <div className="mt-3">
          <button
            type="button"
            aria-disabled={retryBlocked ? true : undefined}
            aria-describedby={retryBlocked ? 'retry-reason' : undefined}
            onClick={() => {
              if (!retryBlocked) onRetry()
            }}
            className={`rounded-md border border-neutral-900 bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 ${retryBlocked ? 'cursor-not-allowed opacity-50' : ''}`}
          >
            Retry these files
          </button>
          {retryBlocked && (
            <p id="retry-reason" className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
              {retryBlocked}
            </p>
          )}
        </div>
      )}
    </aside>
  )
}
