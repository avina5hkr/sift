import type { ReactNode } from 'react'
import { Modal } from '../delete/Modal'
import { BTN, DANGER } from './ui'

type Props = {
  label: string
  title: string
  children: ReactNode
  confirmLabel: string
  busy: boolean
  error: string | null
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
}

/** A one-question confirm. Cancel has the focus, never the confirm button. */
export function SimpleConfirm({ label, title, children, confirmLabel, busy, error, danger = true, onConfirm, onCancel }: Props) {
  return (
    <Modal label={label} onClose={busy ? undefined : onCancel}>
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="text-sm">{children}</p>
      {error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" data-autofocus onClick={onCancel} disabled={busy} className={BTN}>
          Cancel
        </button>
        <button type="button" onClick={onConfirm} disabled={busy} className={danger ? DANGER : BTN}>
          {busy ? 'Working…' : confirmLabel}
        </button>
      </div>
    </Modal>
  )
}
