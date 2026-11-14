import { formatCount } from '../../lib/format'
import { STATUS_LABEL, folderCounts } from '../../lib/browse'
import type { BrowseFolder, BrowseStatus } from '../../lib/types'

const LOOK: Record<BrowseStatus, string> = {
  mac_ssd: 'bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200',
  mac_only: 'bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200',
  ssd_only: 'bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200',
  not_backed_up: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200',
  changed: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200',
}

const PILL = 'inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium'

export function StatusBadge({ status }: { status: BrowseStatus }) {
  return <span className={`${PILL} ${LOOK[status]}`}>{STATUS_LABEL[status]}</span>
}

/** A folder's files at a glance: "120 backed up · 3 not". */
export function FolderStatus({ status }: { status: BrowseFolder['status'] }) {
  const { backed, not } = folderCounts(status)
  if (backed + not === 0) return <span className="text-xs text-neutral-500">Empty</span>
  return (
    <span className={`${PILL} ${not > 0 ? LOOK.not_backed_up : LOOK.mac_ssd}`}>
      {formatCount(backed)} backed up{not > 0 ? ` · ${formatCount(not)} not` : ''}
    </span>
  )
}
