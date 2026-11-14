import { useNavigate } from 'react-router'

const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'

/** The way into the health report and the pre-reset checklist. */
export function HealthCard() {
  const navigate = useNavigate()
  return (
    <section aria-label="Health report" className="flex flex-wrap items-center gap-4 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="min-w-0 flex-1">
        <h2 className="font-medium">Health report</h2>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          See how the phone is doing: storage, battery, memory and more. It only reads the phone.
        </p>
      </div>
      <button type="button" className={BTN} onClick={() => navigate('/health')}>
        Open
      </button>
    </section>
  )
}
