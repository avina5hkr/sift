import type { ReactNode } from 'react'

type Tone = 'neutral' | 'ok' | 'warn'

const TONES: Record<Tone, string> = {
  neutral: 'bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300',
  ok: 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300',
  warn: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-300',
}

type Props = {
  children: ReactNode
  tone?: Tone
  title?: string
  /** When given, the chip is a real button; otherwise it is plain text. */
  onClick?: () => void
}

export function Chip({ children, tone = 'neutral', title, onClick }: Props) {
  const cls = `inline-flex items-center rounded-full px-3 py-1 text-sm ${TONES[tone]}`
  if (onClick) {
    return (
      <button type="button" title={title} onClick={onClick} className={`${cls} cursor-pointer hover:brightness-95`}>
        {children}
      </button>
    )
  }
  return (
    <span title={title} className={cls}>
      {children}
    </span>
  )
}
