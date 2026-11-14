import { useNavigate } from 'react-router'

const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'

/** The way into Clean slate: keep what you list, remove the rest. */
export function SlateCard() {
  const navigate = useNavigate()
  return (
    <section aria-label="Clean slate" className="flex flex-wrap items-center gap-4 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="min-w-0 flex-1">
        <h2 className="font-medium">Clean slate</h2>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Keep the folders and files you list and remove everything else from the phone, once it is backed up.
        </p>
      </div>
      <button type="button" className={BTN} onClick={() => navigate('/slate')}>
        Start
      </button>
    </section>
  )
}
