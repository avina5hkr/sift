import { useNavigate } from 'react-router'
import type { ReviewTab } from './useReview'

const TABS: { value: ReviewTab; label: string }[] = [
  { value: 'exact', label: 'Exact duplicates' },
  { value: 'near', label: 'Near-duplicates' },
  { value: 'similar', label: 'Similar shots' },
  { value: 'junk', label: 'Junk' },
]

/** The back button and the tab strip at the top of the Review screen. */
export function ReviewTabs({ tab, onTab }: { tab: ReviewTab; onTab: (t: ReviewTab) => void }) {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => navigate('/cleanup')} className="rounded-md px-2 py-1 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800">
        ‹ Cleanup
      </button>
      <div role="tablist" aria-label="What to review" className="flex gap-1">
        {TABS.map((t) => (
          <button
            key={t.value}
            role="tab"
            aria-selected={t.value === tab}
            type="button"
            onClick={() => onTab(t.value)}
            className={`rounded-md px-3 py-1.5 text-sm ${
              t.value === tab ? 'bg-neutral-200 font-medium dark:bg-neutral-800' : 'hover:bg-neutral-100 dark:hover:bg-neutral-800'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
    </>
  )
}
