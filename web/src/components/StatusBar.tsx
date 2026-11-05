import { formatBytes } from '../lib/format'
import { useEventStore } from '../lib/events'
import type { StatusResponse } from '../lib/types'
import { Chip } from './Chip'

type Props = {
  status: StatusResponse | undefined
  /** Each chip opens its Settings page; omit to show plain chips. */
  onOpenSettings?: () => void
}

function macFolderLabel(folder: StatusResponse['mac_folder']): string {
  const name = folder.path.split('/').filter(Boolean).pop() ?? folder.path
  return folder.free_bytes == null ? name : `${name} · ${formatBytes(folder.free_bytes)} free`
}

export function StatusBar({ status, onOpenSettings }: Props) {
  const connected = useEventStore((s) => s.connected)
  const folder = status?.mac_folder

  return (
    <header className="flex items-center gap-2 border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
      <Chip onClick={onOpenSettings}>{status?.device ? 'Phone connected' : 'No phone'}</Chip>
      <Chip onClick={onOpenSettings}>{status?.ssd ? 'SSD connected' : 'SSD not connected'}</Chip>
      <Chip onClick={onOpenSettings} title={folder?.path}>
        {folder ? macFolderLabel(folder) : 'Mac folder: checking'}
      </Chip>
      <span
        role="status"
        data-testid="events-status"
        data-connected={connected ? 'true' : 'false'}
        className="ml-auto inline-flex items-center gap-2 text-sm text-neutral-600 dark:text-neutral-400"
      >
        <span
          aria-hidden="true"
          className={`inline-block h-2.5 w-2.5 rounded-full ${connected ? 'bg-green-500' : 'bg-neutral-400'}`}
        />
        <span className="sr-only">{connected ? 'Live updates on' : 'Live updates reconnecting'}</span>
      </span>
    </header>
  )
}
