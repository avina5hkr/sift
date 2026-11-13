import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../../lib/api'
import { formatWhen } from '../../lib/format'
import type { Confirmation } from '../../lib/types'

/** Whether a set's backup has been confirmed: "Confirmed <date>", "Stale: <reason>" or "Not confirmed". */
export function ConfirmStatus({ set }: { set: string }) {
  const q = useQuery({
    queryKey: ['backup', 'confirmation', set],
    queryFn: () => apiFetch<Confirmation>(`/api/backup/confirmation?set=${encodeURIComponent(set)}`),
    retry: false,
  })
  const c = q.data
  let text = 'Not confirmed'
  if (c?.stale_reason) text = `Stale: ${c.stale_reason}`
  else if (c?.last_confirmed && c.last_confirmed_at != null) text = `Confirmed ${formatWhen(c.last_confirmed_at)}`
  return (
    <p className="text-sm text-neutral-600 dark:text-neutral-400" data-testid={`confirm-${set}`}>
      {text}
    </p>
  )
}
