import type { DeviceStatus } from '../lib/types'

// A Retry can change the outcome for these; the other blocking states follow the phone itself
// (tap Allow, plug in the right phone) and the tracker notices on its own.
const RETRY_STATES = new Set(['held_by_other', 'offline', 'no_adb'])

type Props = {
  device: DeviceStatus | null | undefined
  /** Look for the phone again (POST /api/devices/retry). */
  onRetry: () => void
  /** A Retry request is in flight: the button is disabled. */
  retrying?: boolean
  /** Plain-words reason the last Retry did not work; shown under the message. */
  error?: string | null
}

/** The banner under the status bar while the phone cannot be used: the tracker's own words and one action. */
export function DeviceBanner({ device, onRetry, retrying = false, error = null }: Props) {
  if (!device || device.state === 'connected_usb' || device.state === 'connected_wifi') return null
  return (
    <div
      role="status"
      data-testid="device-banner"
      className="flex items-center gap-3 border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
    >
      <div className="min-w-0 flex-1">
        <p>{device.message}</p>
        {error && (
          <p role="alert" className="mt-1 font-medium">
            {error}
          </p>
        )}
      </div>
      {RETRY_STATES.has(device.state) && (
        <button
          type="button"
          onClick={onRetry}
          disabled={retrying}
          className="shrink-0 rounded-md border border-amber-400 px-3 py-1 font-medium hover:bg-amber-200 disabled:opacity-60 dark:border-amber-700 dark:hover:bg-amber-900"
        >
          Retry
        </button>
      )}
    </div>
  )
}
