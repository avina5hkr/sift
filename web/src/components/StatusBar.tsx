import { formatBytes } from '../lib/format'
import { useEventStore } from '../lib/events'
import type { DeviceStatus, StatusResponse } from '../lib/types'
import { Chip } from './Chip'

type Props = {
  status: StatusResponse | undefined
  /** Each chip opens its Settings page; omit to show plain chips. */
  onOpenSettings?: () => void
  /** Look for the phone again (POST /api/devices/retry); shown next to a phone held by another tool. */
  onRetry?: () => void
}

const USB2_TIP = 'Use a USB 3 cable in a direct port for faster backups.'
const USB2_TIP_ID = 'usb2-tip'

type PhoneChipView = { text: string; tone: 'neutral' | 'ok' | 'warn'; usb2: boolean; retry: boolean }

function phoneChip(device: DeviceStatus | null | undefined): PhoneChipView {
  const view = (text: string, tone: PhoneChipView['tone'], extra: Partial<PhoneChipView> = {}) => ({
    text,
    tone,
    usb2: false,
    retry: false,
    ...extra,
  })
  switch (device?.state) {
    case 'connected_usb':
    case 'connected_wifi': {
      const parts = [
        device.model ?? 'Phone',
        device.state === 'connected_usb' ? 'USB' : 'Wi-Fi',
        ...(device.battery_pct == null ? [] : [`${device.battery_pct}%`]),
        ...(device.free_bytes == null ? [] : [`${formatBytes(device.free_bytes)} free`]),
      ]
      return view(parts.join(' · '), 'ok', { usb2: device.usb2_hint })
    }
    case 'unauthorized':
      return view('Tap Allow on the phone', 'warn')
    case 'offline':
      return view('Phone not responding', 'warn')
    case 'held_by_other':
      return view('Phone busy in another tool', 'warn', { retry: true })
    case 'wrong_device':
      return view('A different phone is connected', 'warn')
    case 'no_adb':
      return view('adb not installed', 'warn')
    default:
      return view('No phone', 'neutral')
  }
}

function macFolderLabel(folder: StatusResponse['mac_folder']): string {
  const name = folder.path.split('/').filter(Boolean).pop() ?? folder.path
  return folder.free_bytes == null ? name : `${name} · ${formatBytes(folder.free_bytes)} free`
}

export function StatusBar({ status, onOpenSettings, onRetry }: Props) {
  const connected = useEventStore((s) => s.connected)
  const folder = status?.mac_folder
  const phone = phoneChip(status?.device)

  return (
    <header className="flex items-center gap-2 border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
      <Chip onClick={onOpenSettings} tone={phone.tone}>
        {phone.text}
      </Chip>
      {phone.usb2 && (
        <>
          <span
            title={USB2_TIP}
            aria-describedby={USB2_TIP_ID}
            className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-300"
          >
            USB 2
          </span>
          <span id={USB2_TIP_ID} className="sr-only">
            {USB2_TIP}
          </span>
        </>
      )}
      {phone.retry && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          aria-label="Retry connecting to the phone"
          className="rounded-full border border-neutral-300 px-3 py-1 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          Retry
        </button>
      )}
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
