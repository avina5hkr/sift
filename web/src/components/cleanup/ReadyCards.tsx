import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes, formatDay } from '../../lib/format'
import type { ReadySet } from '../../lib/types'
import { DeleteFlow } from '../delete/DeleteFlow'
import { useDeleteRun } from '../delete/useDeleteRun'

const KEY = 'sift.readyHidden'
const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800'

function hiddenBefore(): string[] {
  try {
    const v: unknown = JSON.parse(sessionStorage.getItem(KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

/** "Ready to free": backed-up, confirmed sets whose files can be deleted from the phone. */
export function ReadyCards() {
  const ready = useQuery({
    queryKey: ['cleanup', 'ready'],
    queryFn: () => apiFetch<ReadySet[]>('/api/cleanup/ready'),
    retry: false,
  })
  const flow = useDeleteRun()
  const [hidden, setHidden] = useState<string[]>(hiddenBefore)
  const hide = (name: string) => {
    const next = [...hidden, name]
    setHidden(next)
    try {
      sessionStorage.setItem(KEY, JSON.stringify(next))
    } catch {
      // hidden for this screen only
    }
  }
  const shown = (ready.data ?? []).filter((s) => !hidden.includes(s.set_name))
  return (
    <>
      {shown.map((s) => (
        <section key={s.set_name} aria-label={`Ready to free: ${s.set_name}`} className="flex flex-wrap items-center gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
          <p className="min-w-0 flex-1 text-sm">
            <span className="font-medium">Ready to free: {formatBytes(s.bytes)} in {s.set_name}</span> (confirmed {formatDay(s.confirmed_at)})
          </p>
          <button type="button" disabled={flow.phase !== 'idle'} onClick={() => void flow.start({ source: 'analysis', sets: [s.set_name] })} className={BTN}>
            Review and delete
          </button>
          <button type="button" onClick={() => hide(s.set_name)} className={BTN}>
            Not now
          </button>
        </section>
      ))}
      <DeleteFlow flow={flow} />
    </>
  )
}
