import { useState } from 'react'
import { plain, post } from '../pages/Wizard/request'

/** Opens the macOS folder dialog (through the app window) and hands back the chosen folder. */
export function ChooseFolderButton({
  start,
  onPicked,
  label = 'Choose…',
}: {
  start?: string
  onPicked: (path: string) => void
  label?: string
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function choose() {
    setBusy(true)
    setError(null)
    try {
      const { path } = await post<{ path: string | null }>('/api/setup/pick-folder', { start: start || null })
      if (path) onPicked(path)
    } catch (e) {
      setError(plain(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <span className="inline-flex flex-col gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={() => void choose()}
        className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
      >
        {busy ? 'Choosing…' : label}
      </button>
      {error && (
        <span role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </span>
      )}
    </span>
  )
}
