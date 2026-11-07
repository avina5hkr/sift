export type ConnectionState =
  | 'no_adb'
  | 'no_device'
  | 'unauthorized'
  | 'offline'
  | 'wrong_device'
  | 'held_by_other'
  | 'connected_usb'
  | 'connected_wifi'

// Mirrors DeviceStatusModel (src/sift/server/routes/status.py), the tracker's DeviceStatus.
export type DeviceStatus = {
  state: ConnectionState
  message: string // plain words from the tracker, shown to the person as is
  serial: string | null
  model: string | null
  android_version: string | null
  battery_pct: number | null
  free_bytes: number | null
  total_bytes: number | null
  link_mbps: number | null
  usb2_hint: boolean
  device_uuid: string | null
  reason: 'adb_missing' | 'adb_unavailable' | null // why the state is no_adb
}

// Mirrors GET /api/status (src/sift/server/routes/status.py).
export type StatusResponse = {
  app: { version: string; schema_version: number; config_warnings: string[] }
  device: DeviceStatus | null // null when no tracker runs
  ssd: null // typed model arrives in M3
  mac_folder: { path: string; exists: boolean; free_bytes: number | null }
  jobs: unknown[]
}

// Shape of every API error body.
export type ApiErrorBody = { code: string; message: string; detail: unknown }

export type JobStateName =
  | 'queued'
  | 'scanning'
  | 'awaiting_confirmation'
  | 'pulling'
  | 'paused'
  | 'waiting_for_phone'
  | 'stopping'
  | 'done'
  | 'failed'

export type CurrentFile = { path: string; size: number; done: number }

// Mirrors JobSnapshot.as_event (src/sift/core/jobs.py): the body of every `progress` event.
export type JobEvent = {
  job_id: number
  type: string
  state: JobStateName
  sets: string[]
  dry_run: boolean
  files_done: number
  bytes_done: number
  files_total: number | null
  bytes_total: number | null
  duplicates_skipped: number
  failed: number
  current: string | null
  current_files: CurrentFile[] // up to 3 files being copied now
  speed_bps: number | null
  eta_s: number | null
  eta_basis: 'measured' | 'stored' | 'reference'
  pause_requested: boolean
  stopped: boolean
  error: string | null
  started_at: number | null
  finished_at: number | null
}

export type ToastLevel = 'info' | 'warning' | 'error'
export type ToastEvent = { level: ToastLevel; title: string; message: string }

// Mirrors GET /api/backup/summary (src/sift/server/routes/backup.py).
export type BackupSummary = {
  job_id: number
  sets: string[]
  outcome: 'done' | 'stopped' | 'failed' | 'discarded'
  started_at: number
  finished_at: number
  duration_s: number
  new_files: number
  duplicates_skipped: number
  failed: number
  moved_links: number
  changed_files: number
  bytes: number
  warnings: string[]
  error: string | null
}

export type FailedFile = {
  id: number
  path: string
  size: number
  set_name: string | null
  attempts: number
  reason: string
}
export type FailuresResponse = { count: number; files: FailedFile[] }

export type ScanInfo = {
  id: number
  device_id: number | null
  state: string
  sets: string[]
  started_at: number | null
  finished_at: number | null
  warnings: string[]
  disappearance: Record<string, unknown>[]
}

export type SetStatus = {
  name: string
  enabled: boolean
  on_phone: number
  backed_up: number
  pending: number
}
export type RecentFile = { path: string; size: number; at: number }

// Mirrors GET /api/backup/overview.
export type BackupOverview = {
  phone_connected: boolean // false: the counts are empty because no phone is connected
  sets: SetStatus[]
  recent: RecentFile[]
  pending_scan: ScanInfo | null
  job: JobEvent | null
}
