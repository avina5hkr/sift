import { formatBytes } from '../../lib/format'
import type { StorageCategory } from '../../lib/types'

const CATEGORIES: StorageCategory[] = ['Camera', 'WhatsApp images', 'WhatsApp video', 'Sent', 'Other']

const COLOURS: Record<StorageCategory, string> = {
  Camera: 'bg-blue-500',
  'WhatsApp images': 'bg-green-500',
  'WhatsApp video': 'bg-emerald-700',
  Sent: 'bg-amber-500',
  Other: 'bg-neutral-400',
}

type Props = { storage: Record<StorageCategory, number> }

/** What is on the phone, split into five kinds, as one bar and a legend with the sizes. */
export function StorageBar({ storage }: Props) {
  const total = CATEGORIES.reduce((sum, c) => sum + (storage[c] ?? 0), 0)
  return (
    <div>
      <div
        role="img"
        aria-label={`Phone storage by kind, ${formatBytes(total)} in all`}
        className="flex h-4 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800"
      >
        {total > 0 &&
          CATEGORIES.map((c) => {
            const bytes = storage[c] ?? 0
            return bytes > 0 ? (
              <div key={c} className={COLOURS[c]} style={{ width: `${(bytes / total) * 100}%` }} title={`${c}: ${formatBytes(bytes)}`} />
            ) : null
          })}
      </div>
      <ul className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
        {CATEGORIES.map((c) => (
          <li key={c} className="flex items-center gap-2">
            <span aria-hidden="true" className={`inline-block h-3 w-3 rounded-sm ${COLOURS[c]}`} />
            <span>{c}</span>
            <span className="text-neutral-600 dark:text-neutral-400">{formatBytes(storage[c] ?? 0)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
