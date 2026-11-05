// Mirrors GET /api/status (src/sift/server/routes/status.py).
export type StatusResponse = {
  app: { version: string; schema_version: number; config_warnings: string[] }
  device: null // typed model arrives in M1
  ssd: null // typed model arrives in M3
  mac_folder: { path: string; exists: boolean; free_bytes: number | null }
  jobs: unknown[]
}

// Shape of every API error body.
export type ApiErrorBody = { code: string; message: string; detail: unknown }
