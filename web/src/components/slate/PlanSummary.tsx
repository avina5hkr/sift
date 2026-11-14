import { formatBytes, formatCount } from '../../lib/format'
import type { SlatePlan, SlateTotal } from '../../lib/types'

const files = (t: SlateTotal) => `${formatCount(t.files)} ${t.files === 1 ? 'file' : 'files'} (${formatBytes(t.bytes)})`

/** The running totals of the plan. */
export function PlanSummary({ plan }: { plan: SlatePlan }) {
  return (
    <div aria-label="Plan totals" role="group" className="flex flex-col gap-1 text-sm">
      <p className="font-medium">
        Remove {files(plan.remove)}. Keep {files(plan.keep)}.
      </p>
      <p className="text-neutral-600 dark:text-neutral-400">
        {files(plan.protected)} protected and always kept. {files(plan.never)} in folders Sift never deletes from.
        {plan.gaps.files > 0 && ` ${files(plan.gaps)} to remove still need a backup.`}
      </p>
    </div>
  )
}
