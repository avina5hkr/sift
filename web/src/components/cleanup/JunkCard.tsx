import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router'
import { apiFetch } from '../../lib/api'
import { formatCount } from '../../lib/format'
import type { JunkStatus } from '../../lib/types'

const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'

/** Junk suggestions (or what is still needed before there can be any), and the Calibration link. */
export function JunkCard() {
  const navigate = useNavigate()
  const status = useQuery({
    queryKey: ['cleanup', 'junk', 'status'],
    queryFn: () => apiFetch<JunkStatus>('/api/cleanup/junk/status'),
    retry: false,
  })
  const s = status.data
  if (!s) return null
  const text = s.cold_start
    ? 'Teach the app first: mark some groups Junk and some Keep, and Sift will start suggesting junk.'
    : s.suggested === 0
      ? 'Nothing looks like junk right now.'
      : `${formatCount(s.suggested)} file${s.suggested === 1 ? '' : 's'} Sift suggests deleting${
          s.precheck_on && !s.precheck_allowed ? '. Check a sample before they are ticked for you.' : '.'
        }`
  return (
    <section aria-label="Junk" className="flex flex-wrap items-center gap-4 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="min-w-0 flex-1">
        <h2 className="font-medium">Junk</h2>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">{text}</p>
      </div>
      {!s.cold_start && (
        <button type="button" className={BTN} onClick={() => navigate('/cleanup/calibration')}>
          Calibration
        </button>
      )}
      <button type="button" className={BTN} onClick={() => navigate(s.cold_start ? '/cleanup/teach' : '/cleanup/review?tab=junk')}>
        {s.cold_start ? 'Teach the app' : 'Review'}
      </button>
    </section>
  )
}
