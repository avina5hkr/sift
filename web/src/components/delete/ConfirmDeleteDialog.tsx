import { useState } from 'react'
import { formatBytes, formatCount } from '../../lib/format'
import type { DeleteRun } from '../../lib/types'
import { Modal } from './Modal'

const NOTE_KEY = 'sift.whatsappDeleteNoteSeen'
const NOTE = "Chats keep their text, but deleted media shows as missing, and WhatsApp's storage screen may not shrink at once."

function noteSeen(): boolean {
  try {
    return localStorage.getItem(NOTE_KEY) === '1'
  } catch {
    return false
  }
}
function markNoteSeen() {
  try {
    localStorage.setItem(NOTE_KEY, '1')
  } catch {
    // not remembered: the note shows again next time
  }
}

type Props = {
  run: DeleteRun
  busy: boolean
  error: string | null
  /** An error that means the checks are out of date (the dialog then offers to run them again). */
  outdated: boolean
  onConfirm: (typed?: number) => void
  onCancel: () => void
  onRecheck: () => void
}

const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'
const PRIMARY =
  'rounded-md border border-neutral-900 bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-50 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300'

/** The last question before anything is deleted from the phone. */
export function ConfirmDeleteDialog({ run, busy, error, outdated, onConfirm, onCancel, onRecheck }: Props) {
  const [reviewed, setReviewed] = useState(false)
  const [typed, setTyped] = useState('')
  const [note] = useState(() => !noteSeen() && run.items.some((i) => i.check === 'ok' && /whatsapp/i.test(i.path)))
  const n = run.passed
  const typedOk = !run.typed_required || typed.trim() === String(n)
  const ready = reviewed && typedOk && !busy && !outdated
  const files = `${formatCount(n)} ${n === 1 ? 'file' : 'files'}`

  return (
    <Modal label="Delete from the phone" onClose={busy ? undefined : onCancel}>
      <h2 className="text-lg font-semibold">
        Delete {files} ({formatBytes(run.passed_bytes)}) from the phone?
      </h2>
      <p className="text-sm">
        Backups: {run.backups.length > 0 ? run.backups.join(', ') : 'none found'}
      </p>
      <p className="text-sm font-semibold">This cannot be undone on the phone.</p>
      {note && <p className="rounded-md bg-neutral-100 px-3 py-2 text-sm dark:bg-neutral-800">{NOTE}</p>}
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
        I have reviewed these files
      </label>
      {run.typed_required && (
        <label className="flex flex-col gap-1 text-sm">
          Type {n} to confirm
          <input
            inputMode="numeric"
            autoComplete="off"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            className="w-40 rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
          />
        </label>
      )}
      {error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {error}
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        {outdated && (
          <button type="button" onClick={onRecheck} className={PRIMARY}>
            Run the checks again
          </button>
        )}
        <button type="button" onClick={onCancel} disabled={busy} className={BTN}>
          Cancel
        </button>
        <button
          type="button"
          disabled={!ready}
          onClick={() => {
            if (note) markNoteSeen()
            onConfirm(run.typed_required ? n : undefined)
          }}
          className={PRIMARY}
        >
          Delete {files}
        </button>
      </div>
    </Modal>
  )
}
