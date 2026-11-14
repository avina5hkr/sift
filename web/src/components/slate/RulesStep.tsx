import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { MUTED, SLATE_BTN, SLATE_FIELD, SLATE_PRIMARY } from '../../lib/slate'
import type { SlatePlan, SlateRules } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'

export const DEFAULT_RULES: SlateRules = {
  keep_folders: ['/sdcard/Documents'],
  keep_newer_than_days: 30,
  keep_labelled: true,
  keep_camera: false,
  patterns: [],
}

type Props = { plan: SlatePlan | null; onPlan: (p: SlatePlan) => void }

/** Step 1: what to keep. Everything else is planned for removal. */
export function RulesStep({ plan, onPlan }: Props) {
  const start = plan?.rules ?? DEFAULT_RULES
  const [folders, setFolders] = useState(start.keep_folders)
  const [draft, setDraft] = useState('')
  const [days, setDays] = useState(String(start.keep_newer_than_days))
  const [labelled, setLabelled] = useState(start.keep_labelled)
  const [camera, setCamera] = useState(start.keep_camera)
  const [patterns, setPatterns] = useState(start.patterns.join('\n'))
  const [message, setMessage] = useState<string | null>(null)

  const make = useMutation({
    mutationFn: () =>
      post<SlatePlan>('/api/slate/plan', {
        keep_folders: folders,
        keep_newer_than_days: Math.max(0, Math.floor(Number(days) || 0)),
        keep_labelled: labelled,
        keep_camera: camera,
        patterns: patterns.split('\n').map((p) => p.trim()).filter(Boolean),
      }),
    onMutate: () => setMessage(null),
    onSuccess: onPlan,
    onError: (e) => setMessage(plain(e)),
  })

  const add = () => {
    const f = draft.trim().replace(/\/+$/, '')
    if (f && !folders.includes(f)) setFolders([...folders, f])
    setDraft('')
  }

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault()
        if (!make.isPending) make.mutate()
      }}
    >
      <fieldset className="flex flex-col gap-2">
        <legend className="font-medium">Folders to keep</legend>
        <p className={`text-sm ${MUTED}`}>Everything inside these folders stays on the phone.</p>
        <ul className="flex flex-col gap-1">
          {folders.map((f) => (
            <li key={f} className="flex items-center gap-2 text-sm">
              <span className="break-all">{f}</span>
              <button type="button" aria-label={`Remove ${f} from the folders to keep`} onClick={() => setFolders(folders.filter((x) => x !== f))} className={SLATE_BTN}>
                Remove
              </button>
            </li>
          ))}
          {folders.length === 0 && <li className={`text-sm ${MUTED}`}>No folders kept.</li>}
        </ul>
        <div className="flex items-center gap-2">
          <input
            aria-label="Folder to keep"
            placeholder="/sdcard/Documents"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                add()
              }
            }}
            className={`${SLATE_FIELD} w-72`}
          />
          <button type="button" onClick={add} className={SLATE_BTN}>
            Add folder
          </button>
        </div>
      </fieldset>

      <label className="flex items-center gap-2 text-sm">
        Keep files newer than
        <input type="number" min={0} aria-label="Keep files newer than (days)" value={days} onChange={(e) => setDays(e.target.value)} className={`${SLATE_FIELD} w-20`} />
        days <span className={MUTED}>(0 = off)</span>
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={labelled} onChange={(e) => setLabelled(e.target.checked)} />
        Keep pictures I marked keep
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={camera} onChange={(e) => setCamera(e.target.checked)} />
        Keep camera originals
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Keep paths matching (one per line)
        <textarea
          rows={3}
          placeholder="/sdcard/Download/*.pdf"
          value={patterns}
          onChange={(e) => setPatterns(e.target.value)}
          className={`${SLATE_FIELD} w-full max-w-md font-mono`}
        />
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked disabled readOnly />
        Protected files are always kept (documents, IDs, receipts, screenshots)
      </label>

      {message && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {message}
        </p>
      )}
      <div>
        <button type="submit" disabled={make.isPending} className={SLATE_PRIMARY}>
          {make.isPending ? 'Making the plan…' : 'Make the plan'}
        </button>
      </div>
    </form>
  )
}
