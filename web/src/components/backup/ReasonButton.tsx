import { useId, type ReactNode } from 'react'

type Props = {
  children: ReactNode
  onClick: () => void
  /** Why the button cannot be used right now; null means it can. */
  reason: string | null
  primary?: boolean
  label?: string
}

const BASE = 'rounded-md border px-3 py-1.5 text-sm font-medium'
const LOOKS = {
  primary:
    'border-neutral-900 bg-neutral-900 text-white hover:bg-neutral-700 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300',
  plain: 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800',
}

/**
 * A button that, when it cannot be used, stays focusable (aria-disabled, not `disabled`) and
 * says why: the reason is its accessible description and a tooltip that shows on hover and on
 * keyboard focus. Clicking it does nothing.
 */
export function ReasonButton({ children, onClick, reason, primary = false, label }: Props) {
  const reasonId = useId()
  return (
    <span className="group relative inline-block">
      <button
        type="button"
        aria-label={label}
        aria-disabled={reason ? true : undefined}
        aria-describedby={reason ? reasonId : undefined}
        onClick={() => {
          if (!reason) onClick()
        }}
        className={`${BASE} ${primary ? LOOKS.primary : LOOKS.plain} ${reason ? 'cursor-not-allowed opacity-50' : ''}`}
      >
        {children}
      </button>
      {reason && (
        <span
          id={reasonId}
          role="tooltip"
          className="pointer-events-none absolute top-full left-0 z-20 mt-1 hidden w-64 rounded-md bg-neutral-900 px-3 py-2 text-sm font-normal text-white shadow-lg group-focus-within:block group-hover:block dark:bg-neutral-100 dark:text-neutral-900"
        >
          {reason}
        </span>
      )}
    </span>
  )
}
