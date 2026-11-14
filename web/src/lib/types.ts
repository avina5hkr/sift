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

// Mirrors the SSD part of GET /api/status; the `ssd` event carries the same without safe_to_unplug.
export type SsdStatus = {
  configured: boolean
  connected: boolean
  name: string | null
  mount: string | null
  root: string | null
  free_bytes: number | null
  dest_id: number | null
  safe_to_unplug: boolean
}

// Mirrors GET /api/status (src/sift/server/routes/status.py).
export type StatusResponse = {
  app: { version: string; schema_version: number; config_warnings: string[] }
  device: DeviceStatus | null // null when no tracker runs
  ssd: SsdStatus | null // null when no SSD is set up
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
  | 'replicating'
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

// First-run wizard: mirrors src/sift/server/routes/setup.py.
export type SetupPhone = { uuid: string; model: string | null; last4: string | null; label: string | null }
export type SetupState = {
  complete: boolean
  phone: SetupPhone | null
  mac_root: string
  device_label: string
  suggested_label: string
  ssd_volume_uuid: string
}
export type AdbState = {
  status: 'ready' | 'missing' | 'broken'
  version: string | null
  message: string
  download_bytes: number
  download_host: string
}
export type AttachedPhone = {
  ref: string // opaque; the serial number never reaches the window, only its last 4 characters
  model: string | null
  last4: string
  ready: boolean
  waiting_for_allow: boolean
  stored: boolean
}
export type FolderCheck = {
  path: string
  ok: boolean
  problem: string | null
  exists: boolean
  free_bytes: number | null
}
export type FolderSaved = { mac_root: string; device_label: string; free_bytes: number | null }
export type SsdVolume = {
  name: string
  file_system: string
  uuid: string
  free_bytes: number | null
  total_bytes: number | null
  suitable: boolean
  warning: string | null
}
export type SetNode = {
  name: string
  enabled: boolean
  paths: string[]
  files: number | null // null until a scan has seen the set
  bytes: number | null
}
export type SetsTree = {
  phone_connected: boolean
  scanning: boolean
  scan_job_id: number | null
  sets: SetNode[]
}
export type ReaderTestResult = {
  tested: boolean
  readers: number | null
  speed_bps: number | null
  message: string
}

// The `task` event and TaskOut: one long job (verify, adopt, rebuild, confirm, analyse) at a time.
export type TaskKind = 'verify' | 'adopt' | 'rebuild' | 'confirm' | 'analyse' | 'analyze' | 'model' | 'preflight' | 'delete' | 'restore'
export type TaskEvent = {
  kind: TaskKind
  state: 'running' | 'done' | 'stopped' | 'failed'
  done: number
  total: number
  message: string
  result: Record<string, unknown> | null
}
// The `replication` event: copying to the SSD after a run.
export type ReplicationEvent = { state: 'running' | 'done'; copied: number; bytes: number; problems: number; failed: number }

// Mirrors GET /api/backup/problems.
export type Problem = {
  id: number
  kind: 'hash_mismatch' | 'missing_file' | 'older_copy_differs' | 'unreadable'
  destination: 'mac' | 'ssd' | null
  rel_path: string
  detail: string
  created_at: number
  repairable: boolean
}

// Mirrors GET /api/backup/confirmation?set=NAME.
export type Confirmation = {
  set_name: string
  report: {
    on_phone: number
    bytes: number
    verified: { mac?: number; ssd?: number }
    provisional: number
    not_backed_up: number
    failed: number
    phone_hash_mismatch: number
    phone_unchecked: number
    required: string[]
    confirmed: boolean
    missing: Record<string, number>
  }
  last_confirmed_at: number | null
  last_confirmed: boolean
  stale_reason: string | null
}

// Mirrors GET /api/analyse/phone/{id}.
export type AnalyseGroup = { files: number; bytes: number }
export type AnalyseSet = {
  verified: AnalyseGroup
  not_verified: AnalyseGroup
  not_backed_up: AnalyseGroup
  problems: AnalyseGroup
}
export type Analysis = {
  id: number
  sets: string[]
  state: string
  started_at: number | null
  finished_at: number | null
  result: {
    sets: Record<string, AnalyseSet>
    hashed_files: number
    hashed_bytes: number
    estimate_s: number
    warnings: string[]
  } | null
  error: string | null
}

// Mirrors GET /api/cleanup/summary.
export type StorageCategory = 'Camera' | 'WhatsApp images' | 'WhatsApp video' | 'Sent' | 'Other'
export type CleanupCard = { tab: string; groups: number; files: number; bytes: number }
export type CleanupSummary = {
  has_backup: boolean
  analyzed_at: number | null
  new_since: number
  storage: Record<StorageCategory, number>
  cards: CleanupCard[]
}

// Mirrors GET /api/cleanup/candidates.
export type ReviewItem = {
  id: number
  path: string
  size: number
  mtime: number
  origin: string
  kind: string
  width: number | null
  height: number | null
  checked: boolean
  role: string
}
export type ReviewGroup = {
  group_id: number
  content_hash: string
  /** Null for the Junk and Lower confidence tabs, which have no keeper. */
  keeper: ReviewItem | null
  copies: ReviewItem[]
  reclaimable: number
  score?: number | null
  reasons?: JunkReason[]
}
export type CandidatesPage = {
  tab: string
  groups: ReviewGroup[]
  total_groups: number
  next_cursor: number | null
}
export type ReviewSort = 'size' | 'date' | 'group' | 'score'
export type JunkReason = { code: string; text: string }

// Mirrors GET /api/cleanup/junk/status and the calibration routes.
export type CalibrationRecord = {
  created_at: number
  sample_size: number
  errors: number
  threshold: number
  model_version: number
  passed: boolean
}
export type JunkStatus = {
  junk_labels: number
  keep_labels: number
  protected_labels: number
  needed_each: number
  cold_start: boolean
  model_version: number
  t_suggest: number
  precheck_allowed: boolean
  never_precheck: boolean
  suggested: number
  maybe: number
  last_calibration: CalibrationRecord | null
  calibration_open: number
}
export type CalibrationItem = { content_hash: string; id: number; path: string; score: number; verdict: boolean | null }
export type CalibrationState = { items: CalibrationItem[]; marked: number; total: number; last: CalibrationRecord | null }
export type CalibrationMark = {
  finished: boolean
  passed?: boolean | null
  errors?: number | null
  sample_size?: number | null
  threshold?: number | null
}

// Mirrors GET /api/cleanup/models.
export type ModelStatus = { key: string; installed: boolean; size_bytes: number }

// Mirrors GET /api/cleanup/clusters, /clusters/{id} and /search.
export type ClusterLabel = 'junk' | 'keep'
export type ClusterTag = 'greeting' | 'quote' | 'meme' | 'devotional' | 'other'
export type Suggestion = {
  label: ClusterLabel | null
  reason: string
  source: 'built-in' | 'built-in + your teaching' | 'your teaching'
}
export type Cluster = {
  id: number
  size: number
  unique: number
  sample_ids: number[]
  sample_text: string | null
  label: ClusterLabel | null
  /** Members that have a label. */
  marked: number
  suggestion: Suggestion | null
}
export type ClusterSection = 'to_mark' | 'marked'
export type ClustersResponse = {
  images: number
  covered: number
  to_mark: number
  marked: number
  offset: number
  clusters: Cluster[]
}
// Mirrors POST /api/labels/import.
export type ImportResult = { read: number; applied: number; waiting: number }
export type PictureItem = { id: number; path: string; content_hash: string; score: number; label: ClusterLabel | null }

// Mirrors GET /api/delete/runs/{id}.
export type DeleteRunState = 'preflight' | 'ready' | 'blocked' | 'running' | 'done' | 'stopped' | 'failed'
export type DeleteItem = {
  id: number
  path: string
  size: number
  /** "ok" or a reason code. */
  check: string
  /** The reason in plain words (null for "ok"). */
  reason: string | null
  state: string
  error: string | null
}
export type DeleteRun = {
  id: number
  source: string
  state: DeleteRunState
  blocked_reason: string | null
  disposition: 'quarantine' | 'archive'
  typed_required: boolean
  passed: number
  passed_bytes: number
  deleted: number
  failed: number
  bytes_freed: number
  manifests: string[]
  backups: string[]
  backed_up_on?: string
  quarantine_days?: number
  items: DeleteItem[]
}
export type DeleteBody =
  | { source: 'cleanup'; ids: number[] }
  | { source: 'analysis'; sets: string[] }
  | {
      source: 'browse'
      ids: number[]
      folders: string[]
      filters?: { q: string; kind: string; older_days: number; min_bytes: number; status: string }
    }
  | { source: 'slate'; plan: number; dispositions: Record<string, Disposition>; fingerprint?: string }
/** What happens to the backup copy of a file deleted from Browse. */
export type Disposition = 'archive' | 'quarantine'

// Mirrors /api/slate/* (Clean slate).
export type SlateCategory = 'camera' | 'screenshots' | 'whatsapp' | 'documents' | 'junk' | 'other'
export type SlateTotal = { files: number; bytes: number }
export type SlateRules = {
  keep_folders: string[]
  keep_newer_than_days: number
  keep_labelled: boolean
  keep_camera: boolean
  patterns: string[]
}
export type SlatePlan = {
  id: number
  fingerprint?: string
  rules: SlateRules
  kept_by_hand: { files: number; folders: string[] }
  remove: SlateTotal
  keep: SlateTotal
  protected: SlateTotal
  never: SlateTotal
  gaps: SlateTotal
  gaps_no_set: SlateTotal
  categories: Record<SlateCategory, SlateTotal>
  gap_sets: string[]
}
export type SlateSide = 'remove' | 'keep' | 'protected'
export type SlateGroup = { folder: string; files: number; bytes: number; sample_ids: number[]; reasons: Record<string, number> }
export type SlateFile = {
  id: number
  path: string
  size: number
  mtime: number
  kind: string
  status: string
  reason: string
  category: SlateCategory
  gap: string | null
}
export type SlateFilesPage = { total_files: number; files: SlateFile[] }

// Mirrors GET /api/quarantine and GET /api/cleanup/ready.
export type QuarantineEntry = {
  id: number
  content_hash: string
  phone_path: string | null
  size: number
  moved_at: number
  purge_after: number
}
export type ReadySet = { set_name: string; files: number; bytes: number; confirmed_at: number }

// Mirrors GET /api/browse and /api/browse/lists.
export type BrowseStatus = 'mac_ssd' | 'mac_only' | 'ssd_only' | 'not_backed_up' | 'changed'
export type BrowseFolder = {
  name: string
  path: string
  size: number
  files: number
  mtime: number
  status: Partial<Record<BrowseStatus, number>>
}
export type BrowseFile = {
  id: number
  name: string
  path: string
  size: number
  mtime: number
  kind: string
  origin: string
  status: BrowseStatus
}
export type BrowsePage = {
  path: string
  crumbs: string[]
  folders: BrowseFolder[]
  files: BrowseFile[]
  total_files: number
  total_size: number
}
export type SmartList = { key: string; title: string; files: number; bytes: number }
export type SmartListPage = { key: string; title: string; total_files: number; files: BrowseFile[] }

// Mirrors GET /api/archive and /api/archive/destinations.
export type ArchiveDestination = { id: number; kind: 'mac' | 'ssd'; root: string }
export type ArchiveFolder = { name: string; rel_path: string; size: number; files: number }
export type ArchiveFile = {
  rel_path: string
  name: string
  size: number
  content_hash: string
  phone_path: string | null
  on_phone: boolean
}
export type ArchiveListing = { dest: number; path: string; folders: ArchiveFolder[]; files: ArchiveFile[] }

// Mirrors GET /api/apps and the /api/apps actions.
export type AppRow = {
  package: string
  name: string
  installer: string | null
  from_play_store: boolean
  version: string | null
  app_bytes: number | null
  data_bytes: number | null
  cache_bytes: number | null
  last_used: number | null
  protected: boolean
  unprotected: boolean
  unused: boolean
}
export type AppsProbes = { packages: boolean; diskstats: boolean; usagestats: boolean; versions: boolean }
export type AppsList = { apps: AppRow[]; suggested: string[]; probes: AppsProbes }
export type AppAction = 'uninstall' | 'clear_data'
export type AppUnlock = Record<string, { backup_done: boolean; typed_name: string }>
export type AppsPreflight = { allowed: string[]; refused: Record<string, string> }
export type AppsResult = { done: string[]; failed: Record<string, string>; app_list: string[] }
export type AppsExport = { app_list: string[]; apks?: { saved: number; failed: number } }
