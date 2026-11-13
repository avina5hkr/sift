import { useId, useState, type ReactNode } from 'react'

type Props = {
  children: ReactNode
  onClick: () => void
  /** Why the button cannot be used right now; null means it can. */
  reason: string | null
  primary?: boolean
  label?: string
  /** Where the tooltip opens; `above` for a button at the bottom of the screen. */
  placement?: 'below' | 'above'
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
export function ReasonButton({ children, onClick, reason, primary = false, label, placement = 'below' }: Props) {
  const reasonId = useId()
  // Escape hides the tooltip until the pointer or focus leaves (WCAG 1.4.13); the reason stays
  // the button's accessible description.
  const [dismissed, setDismissed] = useState(false)
  return (
    <span
      className="group relative inline-block"
      onKeyDown={(e) => {
        if (e.key === 'Escape') setDismissed(true)
      }}
      onMouseLeave={() => setDismissed(false)}
      onBlur={() => setDismissed(false)}
    >
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
          style={dismissed ? { display: 'none' } : undefined}
          className={`pointer-events-none absolute left-0 z-20 hidden ${placement === 'above' ? 'bottom-full mb-1' : 'top-full mt-1'} w-64 rounded-md bg-neutral-900 px-3 py-2 text-sm font-normal text-white shadow-lg group-focus-within:block group-hover:block dark:bg-neutral-100 dark:text-neutral-900`}
        >
          {reason}
        </span>
      )}
    </span>
  )
}
