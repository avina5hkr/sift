import type { BrowseFolder, BrowseStatus } from './types'

export const STATUS_LABEL: Record<BrowseStatus, string> = {
  mac_ssd: 'Mac and SSD',
  mac_only: 'Mac only',
  ssd_only: 'SSD only',
  not_backed_up: 'Not backed up',
  changed: 'Changed',
}

/** Files that cannot be deleted from the phone yet: no backup copy to rely on. */
export const isUnbacked = (s: BrowseStatus) => s === 'not_backed_up' || s === 'changed'

export function folderCounts(status: BrowseFolder['status']) {
  const backed = (status.mac_ssd ?? 0) + (status.mac_only ?? 0) + (status.ssd_only ?? 0)
  const not = (status.not_backed_up ?? 0) + (status.changed ?? 0)
  return { backed, not }
}
