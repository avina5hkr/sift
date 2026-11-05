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
