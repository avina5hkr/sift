import { useEffect } from 'react'
import { dismissToast, useEventStore, type Toast } from '../lib/events'

const SHOW_MS = 8000

const TONES: Record<Toast['level'], string> = {
  info: 'border-neutral-300 bg-white text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100',
  warning: 'border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100',
  error: 'border-red-300 bg-red-50 text-red-950 dark:border-red-800 dark:bg-red-950 dark:text-red-100',
}

function ToastCard({ toast }: { toast: Toast }) {
  useEffect(() => {
    const t = setTimeout(() => dismissToast(toast.id), SHOW_MS)
    return () => clearTimeout(t)
  }, [toast.id])
  return (
    <div
      data-testid="toast"
      className={`pointer-events-auto flex items-start gap-3 rounded-lg border p-3 text-sm shadow-lg ${TONES[toast.level]}`}
    >
      <div className="min-w-0 flex-1">
        <p className="font-medium">{toast.title}</p>
        {toast.message && <p className="mt-0.5">{toast.message}</p>}
      </div>
      <button
        type="button"
        onClick={() => dismissToast(toast.id)}
        className="shrink-0 rounded px-2 py-0.5 hover:bg-black/5 dark:hover:bg-white/10"
      >
        Dismiss
      </button>
    </div>
  )
}

/** Messages from the engine (the `toast` event), bottom right. A polite live region. */
export function Toasts() {
  const toasts = useEventStore((s) => s.toasts)
  return (
    <div
      role="status"
      aria-live="polite"
      aria-label="Messages"
      className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-96 max-w-[calc(100vw-2rem)] flex-col gap-2"
    >
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} />
      ))}
    </div>
  )
}
