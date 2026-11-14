import { useEffect, useRef, type ReactNode } from 'react'

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), a[href], select, textarea, summary, [tabindex]:not([tabindex="-1"])'

type Props = {
  label: string
  /** Esc and the backdrop-less close. Leave out when the dialog must not be dismissed (a task is running). */
  onClose?: () => void
  children: ReactNode
}

/** A modal dialog: focus moves in and stays in (Tab wraps), Esc closes, focus returns where it was. */
export function Modal({ label, onClose, children }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  useEffect(() => {
    close.current = onClose
  })

  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    const el = box.current
    if (el) (el.querySelector<HTMLElement>('[data-autofocus]') ?? el.querySelector<HTMLElement>(FOCUSABLE) ?? el).focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && close.current) {
        e.preventDefault()
        close.current()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      before?.focus?.()
    }
  }, [])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'Tab' || !box.current) return
    const items = [...box.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
    const first = items[0]
    const last = items[items.length - 1]
    if (!first || !last) {
      e.preventDefault()
      return
    }
    const at = document.activeElement
    if (e.shiftKey && (at === first || at === box.current)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && at === last) {
      e.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="flex max-h-full w-full max-w-xl flex-col gap-3 overflow-auto rounded-lg border border-neutral-200 bg-[var(--bg)] p-5 shadow-xl outline-none dark:border-neutral-700"
      >
        {children}
      </div>
    </div>
  )
}
