import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react'

const BASE = 'rounded-md border px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50'
const LOOKS = {
  primary:
    'border-neutral-900 bg-neutral-900 text-white hover:bg-neutral-700 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300',
  plain: 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800',
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean }

export function Button({ primary = false, className = '', type = 'button', ...rest }: ButtonProps) {
  return <button type={type} className={`${BASE} ${primary ? LOOKS.primary : LOOKS.plain} ${className}`} {...rest} />
}

/** The step's heading takes the focus when the step appears, so a keyboard or screen-reader user lands on it. */
export function StepHeading({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null)
  useEffect(() => ref.current?.focus(), [])
  return (
    <h2 id="step-title" ref={ref} tabIndex={-1} className="text-xl font-semibold outline-none">
      {children}
    </h2>
  )
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <p
      role="alert"
      className="rounded-md border border-amber-300 bg-amber-100 px-3 py-2 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
    >
      {message}
    </p>
  )
}

export function Hint({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-800 dark:border-neutral-800 dark:bg-neutral-900 dark:text-neutral-200">
      {children}
    </p>
  )
}

export type StepProps = {
  /** Go to the next step. */
  next: () => void
}
