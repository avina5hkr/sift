import type { DeviceStatus, JobEvent } from './types'

export type ScreenState =
  | 'not_connected'
  | 'ready'
  | 'scanning'
  | 'confirm'
  | 'backing_up'
  | 'paused'
  | 'waiting_for_phone'
  | 'finished'
  | 'failed'

/** Less free space than this on the Mac and a backup cannot do anything useful. */
export const MIN_MAC_FREE_BYTES = 100_000_000

export const isConnected = (device: DeviceStatus | null | undefined) =>
  device?.state === 'connected_usb' || device?.state === 'connected_wifi'

export const isRunning = (job: JobEvent | null | undefined): job is JobEvent =>
  job != null && job.state !== 'done' && job.state !== 'failed'

export function screenState(device: DeviceStatus | null | undefined, job: JobEvent | null): ScreenState {
  if (job) {
    switch (job.state) {
      case 'queued':
      case 'scanning':
        return 'scanning'
      case 'awaiting_confirmation':
        return 'confirm'
      case 'pulling':
      case 'stopping':
        return 'backing_up'
      case 'paused':
        return 'paused'
      case 'waiting_for_phone':
        return 'waiting_for_phone'
      case 'failed':
        return 'failed'
      case 'done':
        return 'finished'
    }
  }
  return isConnected(device) ? 'ready' : 'not_connected'
}

type ReasonInput = {
  device: DeviceStatus | null | undefined
  job: JobEvent | null
  macFreeBytes: number | null | undefined
  /** How many sets this button would back up (0 means setup is not finished, or none ticked). */
  setCount: number
  noSetsReason?: string
}

/** Why Back up now cannot be used right now, in plain words, or null when it can. */
export function disabledReason({ device, job, macFreeBytes, setCount, noSetsReason }: ReasonInput): string | null {
  if (isRunning(job)) return 'A backup is already running.'
  if (device?.state === 'wrong_device') {
    return 'A different phone is connected. Connect the phone you set Sift up with.'
  }
  if (!isConnected(device)) {
    return device && device.state !== 'no_device' && device.message
      ? device.message
      : 'Connect your phone to start a backup.'
  }
  if (setCount === 0) return noSetsReason ?? 'Choose what to back up in Settings first.'
  if (macFreeBytes != null && macFreeBytes < MIN_MAC_FREE_BYTES) {
    return 'Your Mac is almost full. Free up some space, then try again.'
  }
  return null
}
