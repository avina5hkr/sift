import { formatBytes, formatCount } from '../../lib/format'
import { MUTED, SLATE_CATEGORIES } from '../../lib/slate'
import type { Disposition, SlateCategory, SlatePlan } from '../../lib/types'

export type Dispositions = Partial<Record<SlateCategory, Disposition>>

/** What each kind of file does by default: junk is suggested for Quarantine, the rest stays in the backup. */
export const defaultDispositions = (): Dispositions => ({ junk: 'quarantine' })

type Props = { plan: SlatePlan; value: Dispositions; onChange: (v: Dispositions) => void }

/** Step 4: for each kind of file, what happens to its backup copy. */
export function DispositionStep({ plan, value, onChange }: Props) {
  const rows = SLATE_CATEGORIES.filter((c) => plan.categories[c.key].files > 0)
  if (rows.length === 0) return <p>There is nothing to remove.</p>
  return (
    <div className="flex flex-col gap-4">
      <p className={`text-sm ${MUTED}`}>For each kind of file, choose what happens to its copy in your backup after it is removed from the phone.</p>
      {rows.map(({ key, label }) => {
        const t = plan.categories[key]
        const chosen = value[key] ?? 'archive'
        return (
          <fieldset key={key} className="flex flex-col gap-1 rounded-lg border border-neutral-200 p-3 text-sm dark:border-neutral-800">
            <legend className="px-1 font-medium">
              {label} ({formatCount(t.files)} {t.files === 1 ? 'file' : 'files'}, {formatBytes(t.bytes)})
            </legend>
            <label className="flex items-center gap-2">
              <input type="radio" name={`disposition-${key}`} checked={chosen === 'archive'} onChange={() => onChange({ ...value, [key]: 'archive' })} />
              Keep in the backup (archive)
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name={`disposition-${key}`} checked={chosen === 'quarantine'} onChange={() => onChange({ ...value, [key]: 'quarantine' })} />
              Quarantine for 30 days
              {key === 'junk' && <span className={MUTED}>(suggested)</span>}
            </label>
          </fieldset>
        )
      })}
    </div>
  )
}
