import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { initToken } from '../lib/token'
import type {
  BackupOverview,
  BackupSummary,
  FailuresResponse,
  JobEvent,
  JobStateName,
  StatusResponse,
} from '../lib/types'
import BackupPage from './BackupPage'

const DEVICE = {
  state: 'connected_usb',
  message: 'Connected.',
  serial: null,
  model: 'Test phone',
  android_version: '14',
  battery_pct: 80,
  free_bytes: 32_000_000_000,
  total_bytes: 100_000_000_000,
  link_mbps: null,
  usb2_hint: false,
  device_uuid: null,
  reason: null,
} as const

function status(device: Partial<StatusResponse['device'] & object> | null = {}, free = 500_000_000_000): StatusResponse {
  return {
    app: { version: '0.1.0', schema_version: 1, config_warnings: [] },
    device: device === null ? null : ({ ...DEVICE, ...device } as StatusResponse['device']),
    ssd: null,
    mac_folder: { path: '/Users/a/PhoneBackup', exists: true, free_bytes: free },
    jobs: [],
  }
}

const SETS = [
  { name: 'Camera', enabled: true, on_phone: 120, backed_up: 100, pending: 20 },
  { name: 'Screenshots', enabled: true, on_phone: 10, backed_up: 10, pending: 0 },
  { name: 'Downloads', enabled: false, on_phone: 0, backed_up: 0, pending: 0 },
]

function job(state: JobStateName, extra: Partial<JobEvent> = {}): JobEvent {
  return {
    job_id: 7,
    type: 'backup',
    state,
    sets: ['Camera', 'Screenshots'],
    dry_run: false,
    files_done: 12_430,
    bytes_done: 41_200_000_000,
    files_total: 41_982,
    bytes_total: 128_700_000_000,
    duplicates_skipped: 1_204,
    failed: 3,
    current: 'DCIM/Camera/clip.mp4',
    current_files: [
      { path: 'DCIM/Camera/clip.mp4', size: 312_000_000, done: 199_680_000 },
      { path: 'WhatsApp Video/VID-1.mp4', size: 18_000_000, done: 2_160_000 },
    ],
    speed_bps: 38_000_000,
    eta_s: 41 * 60,
    eta_basis: 'measured',
    pause_requested: false,
    stopped: false,
    error: null,
    started_at: 1_000,
    finished_at: null,
    ...extra,
  }
}

const SUMMARY: BackupSummary = {
  job_id: 7,
  sets: ['Camera'],
  outcome: 'done',
  started_at: 1_700_000_000,
  finished_at: 1_700_000_125,
  duration_s: 125,
  new_files: 55,
  duplicates_skipped: 9,
  failed: 2,
  moved_links: 0,
  changed_files: 0,
  bytes: 2_500_000_000,
  warnings: ['Could not read a folder; skipped.'],
  error: null,
}

const FAILURES: FailuresResponse = {
  count: 2,
  files: [
    { id: 1, path: '/sdcard/DCIM/a.jpg', size: 2_000, set_name: 'Camera', attempts: 3, reason: 'The phone stopped answering.' },
    { id: 2, path: '/sdcard/DCIM/b.jpg', size: 4_000, set_name: 'Camera', attempts: 1, reason: 'The file changed while copying.' },
  ],
}

type World = {
  status: StatusResponse
  overview: BackupOverview
  summary: BackupSummary | null
  failures: FailuresResponse
  failPost?: Record<string, { status: number; code: string; message: string }>
}
let world: World
let calls: { method: string; path: string; body: unknown }[]

function reply(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  useEventStore.setState({ job: null, jobAt: 0, toasts: [] })
  world = {
    status: status(),
    overview: { phone_connected: true, sets: SETS, recent: [], pending_scan: null, job: null },
    summary: null,
    failures: { count: 0, files: [] },
  }
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      const body = init.body ? JSON.parse(init.body as string) : undefined
      calls.push({ method, path: url, body })
      const failed = world.failPost?.[url]
      if (failed && method === 'POST') {
        return reply({ code: failed.code, message: failed.message, detail: null }, failed.status)
      }
      if (url === '/api/status') return reply(world.status)
      if (url === '/api/backup/overview') return reply(world.overview)
      if (url === '/api/backup/summary') return reply(world.summary)
      if (url === '/api/backup/failures') return reply(world.failures)
      if (method === 'POST') return reply({ job_id: 8 })
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <BackupPage />
    </QueryClientProvider>,
  )
}

const posts = () => calls.filter((c) => c.method === 'POST')
const backUpNow = () => screen.getAllByRole('button', { name: 'Back up now' })[0]
const stateText = () => screen.getByTestId('screen-state')

async function ready() {
  renderPage()
  await waitFor(() => {
    for (const path of ['/api/status', '/api/backup/overview', '/api/backup/summary', '/api/backup/failures']) {
      expect(calls.some((c) => c.path === path)).toBe(true)
    }
  })
  // let the answers be rendered
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20))
  })
}

// -- the states --------------------------------------------------------------------------------

test('Not connected: says so, and Back up now is disabled with the reason', async () => {
  world.status = status({ state: 'no_device', message: 'No phone found.' })
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Not connected'))
  expect(screen.getByText('No phone connected')).toBeInTheDocument()
  expect(backUpNow()).toHaveAttribute('aria-disabled', 'true')
  expect(backUpNow()).toHaveAccessibleDescription('Connect your phone to start a backup.')
})

test('Wrong phone: the chip and the reason say it is a different phone', async () => {
  world.status = status({ state: 'wrong_device' })
  await ready()
  await waitFor(() => expect(screen.getByText('A different phone is connected')).toBeInTheDocument())
  expect(backUpNow()).toHaveAccessibleDescription(/different phone/)
})

test('Ready: the phone summary, sets with counts, last backup time', async () => {
  world.summary = SUMMARY
  world.overview.recent = [{ path: '/sdcard/DCIM/x.jpg', size: 3_000_000, at: 1_700_000_100 }]
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Ready'))
  expect(screen.getByText('Test phone, USB, 32% free')).toBeInTheDocument()
  expect(backUpNow()).not.toHaveAttribute('aria-disabled')
  expect(screen.getByText(/120 on the phone, 100 backed up on the Mac, 20 waiting/)).toBeInTheDocument()
  expect(screen.getByText('/sdcard/DCIM/x.jpg')).toBeInTheDocument()
  expect(await screen.findByText(/Last backup: \d+ \w+ \d\d:\d\d/)).toBeInTheDocument()
})

test('Ready with nothing backed up yet', async () => {
  await ready()
  expect(await screen.findByText('No backup yet')).toBeInTheDocument()
  expect(screen.getByText('No files copied yet.')).toBeInTheDocument()
})

test('a dry run (the wizard size scan) does not show as a backup on this screen', async () => {
  world.summary = SUMMARY
  world.overview.job = job('scanning', { job_id: 99, dry_run: true })
  useEventStore.setState({ job: job('done', { job_id: 99, dry_run: true }) })
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Ready'))
  expect(stateText()).not.toHaveTextContent('Looking through your phone')
  expect(screen.queryByRole('progressbar', { name: 'Overall progress' })).not.toBeInTheDocument()
  expect(backUpNow()).not.toHaveAttribute('aria-disabled')
})

test('Scanning: an unfinished bar and no speed yet', async () => {
  world.overview.job = job('scanning', { files_done: 0, bytes_done: 0, files_total: null, bytes_total: null, speed_bps: null, eta_s: null })
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Looking through your phone'))
  expect(screen.getByRole('progressbar', { name: 'Overall progress' })).not.toHaveAttribute('aria-valuenow')
})

test('Backing up: files, bytes, percent, speed, ETA, current file, duplicates, failed', async () => {
  world.overview.job = job('pulling')
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Backing up'))
  expect(screen.getByText('File 12,430 of 41,982')).toBeInTheDocument()
  expect(screen.getByText('41.2 GB of 128.7 GB')).toBeInTheDocument()
  expect(screen.getByText('32%')).toBeInTheDocument()
  expect(screen.getByText('38.0 MB/s, about 41 min')).toBeInTheDocument()
  expect(screen.getByRole('progressbar', { name: 'Overall progress' })).toHaveAttribute('aria-valuenow', '32')
    expect(screen.getByText('Skipped 1,204 duplicates')).toBeInTheDocument()
  expect(screen.getByText(/3 failed/)).toBeInTheDocument()
  expect(screen.getByText('Camera, Screenshots')).toBeInTheDocument()
  expect(backUpNow()).toHaveAccessibleDescription('A backup is already running.')
})

test('live progress events update the numbers', async () => {
  world.overview.job = job('pulling')
  await ready()
  await screen.findByText('File 12,430 of 41,982')
  act(() => useEventStore.setState({ job: job('pulling', { files_done: 20_000 }) }))
  expect(await screen.findByText('File 20,000 of 41,982')).toBeInTheDocument()
})

test('Paused offers Resume; Waiting for phone tells you to plug it in', async () => {
  world.overview.job = job('paused')
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Paused'))
  await userEvent.click(screen.getByRole('button', { name: 'Resume' }))
  expect(posts().map((c) => c.path)).toContain('/api/backup/resume')
  act(() => useEventStore.setState({ job: job('waiting_for_phone') }))
  await waitFor(() => expect(stateText()).toHaveTextContent('Waiting for your phone'))
  expect(screen.getByText(/Plug your phone back in/)).toBeInTheDocument()
})

test('Pause and Stop post to the engine; Stop shows it is stopping', async () => {
  world.overview.job = job('pulling')
  await ready()
  await userEvent.click(await screen.findByRole('button', { name: 'Pause' }))
  await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(posts().map((c) => c.path)).toEqual(['/api/backup/pause', '/api/backup/stop'])
  act(() => useEventStore.setState({ job: job('stopping', { stopped: true }) }))
  await waitFor(() => expect(stateText()).toHaveTextContent('Stopping after this file'))
  expect(screen.getByRole('button', { name: 'Stop' })).toBeDisabled()
})

test('Finished: the summary card with the numbers and Open backup folder', async () => {
  world.summary = SUMMARY
  useEventStore.setState({ job: job('done', { finished_at: 1_125 }) })
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Finished'))
  const card = screen.getByRole('region', { name: 'Backup finished' })
  expect(within(card).getByText('55')).toBeInTheDocument()
  expect(within(card).getByText('9')).toBeInTheDocument()
  expect(within(card).getByText('2.5 GB')).toBeInTheDocument()
  expect(within(card).getByText('2 min 5 s')).toBeInTheDocument()
  expect(within(card).getByText('Could not read a folder; skipped.')).toBeInTheDocument()
  await userEvent.click(within(card).getByRole('button', { name: 'Open backup folder' }))
  expect(posts().map((c) => c.path)).toEqual(['/api/backup/open-folder'])
})

test('Open backup folder shows the engine\'s plain message when it cannot', async () => {
  world.summary = SUMMARY
  world.failPost = { '/api/backup/open-folder': { status: 409, code: 'no_backup_folder', message: 'The backup folder does not exist yet.' } }
  useEventStore.setState({ job: job('done') })
  await ready()
  await userEvent.click(await screen.findByRole('button', { name: 'Open backup folder' }))
  expect(await screen.findByText('The backup folder does not exist yet.')).toBeInTheDocument()
})

test('A stopped run says so', async () => {
  world.summary = { ...SUMMARY, outcome: 'stopped' }
  useEventStore.setState({ job: job('done', { stopped: true }) })
  await ready()
  expect(await screen.findByRole('region', { name: 'Backup stopped' })).toBeInTheDocument()
})

test('Discarded run: nothing was copied', async () => {
  world.summary = { ...SUMMARY, outcome: 'discarded', new_files: 0 }
  useEventStore.setState({ job: job('done') })
  await ready()
  expect(await screen.findByRole('region', { name: 'Nothing was copied' })).toBeInTheDocument()
})

test('Failed: the plain reason from the engine', async () => {
  useEventStore.setState({ job: job('failed', { error: 'There is not enough space on the Mac.' }) })
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Something went wrong'))
  expect(screen.getByRole('region', { name: 'The backup could not finish' })).toBeInTheDocument()
  expect(screen.getByText('There is not enough space on the Mac.')).toBeInTheDocument()
})

// -- Back up now: starting, and the disabled reasons ---------------------------------------------

test('Back up now starts the ticked sets; ticking changes what is sent', async () => {
  await ready()
  await userEvent.click(screen.getByRole('checkbox', { name: 'Screenshots' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Downloads' }))
  await userEvent.click(backUpNow())
  expect(posts()).toEqual([{ method: 'POST', path: '/api/backup/start', body: { sets: ['Camera', 'Downloads'] } }])
})

test('each set has its own Back up now; a switched-off set explains itself', async () => {
  await ready()
  await userEvent.click(screen.getByRole('button', { name: 'Back up now, Screenshots' }))
  expect(posts()[0].body).toEqual({ sets: ['Screenshots'] })
  expect(screen.getByRole('button', { name: 'Back up now, Downloads' })).toHaveAccessibleDescription(
    'This set is switched off in Settings.',
  )
})

test('a 409 from the engine is shown in its own words', async () => {
  world.failPost = { '/api/backup/start': { status: 409, code: 'job_conflict', message: 'A backup is already running.' } }
  await ready()
  await userEvent.click(backUpNow())
  expect(await screen.findByRole('alert')).toHaveTextContent('A backup is already running.')
})

test('no space on the Mac disables Back up now with that reason', async () => {
  world.status = status({}, 5_000_000)
  await ready()
  await waitFor(() => expect(backUpNow()).toHaveAccessibleDescription(/Mac is almost full/))
})

test('setup not finished: no sets, or none ticked', async () => {
  world.overview.sets = []
  await ready()
  await screen.findByText(/nothing to back up yet/)
  expect(backUpNow()).toHaveAccessibleDescription('Choose what to back up in Settings first.')
})

test('with every set unticked the reason says to tick one', async () => {
  await ready()
  await userEvent.click(screen.getByRole('checkbox', { name: 'Camera' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Screenshots' }))
  expect(backUpNow()).toHaveAccessibleDescription(/Tick at least one set/)
})

test('the disabled reason is reachable by keyboard, and activating the button does nothing', async () => {
  world.status = status({ state: 'no_device' })
  const user = userEvent.setup()
  await ready()
  await waitFor(() => expect(backUpNow()).toHaveAttribute('aria-disabled', 'true'))
  // Tab reaches the disabled button: it is aria-disabled, not removed from the tab order.
  await user.tab()
  expect(backUpNow()).toHaveFocus()
  const tip = within(backUpNow().parentElement as HTMLElement).getByRole('tooltip')
  expect(tip).toHaveTextContent('Connect your phone to start a backup.')
  expect(backUpNow()).toHaveAttribute('aria-describedby', tip.id)
  await user.keyboard('{Enter}')
  await user.keyboard(' ')
  expect(posts()).toEqual([])
})

test('Back up now works from the keyboard', async () => {
  const user = userEvent.setup()
  await ready()
  backUpNow().focus()
  await user.keyboard('{Enter}')
  expect(posts()[0].path).toBe('/api/backup/start')
})

// -- the mass-disappearance guard ----------------------------------------------------------------

const SCAN = {
  id: 3,
  device_id: 1,
  state: 'awaiting_confirmation',
  sets: ['Camera'],
  started_at: 1,
  finished_at: null,
  warnings: ['Camera had 500 files last time and now shows 20.'],
  disappearance: [],
}

test('the guard card warns in plain words; Scan again has the focus and Confirm does not', async () => {
  world.overview.job = job('awaiting_confirmation')
  world.overview.pending_scan = SCAN
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Waiting for your confirmation'))
  const card = screen.getByRole('alertdialog', { name: 'Lots of files have gone from your phone' })
  expect(within(card).getByText(/Camera had 500 files last time and now shows 20/)).toBeInTheDocument()
  expect(within(card).getByRole('button', { name: 'Scan again' })).toHaveFocus()
  expect(within(card).getByRole('button', { name: 'Confirm' })).not.toHaveFocus()
  expect(posts()).toEqual([])
})

test('Confirm needs a deliberate click, then confirms that scan only', async () => {
  world.overview.job = job('awaiting_confirmation')
  world.overview.pending_scan = SCAN
  const user = userEvent.setup()
  await ready()
  await screen.findByRole('alertdialog')
  await user.click(screen.getByRole('button', { name: 'Confirm' }))
  expect(posts()).toEqual([{ method: 'POST', path: '/api/backup/scan/3/confirm', body: undefined }])
})

test('pressing Enter straight away takes the safe choice, never Confirm', async () => {
  world.overview.job = job('awaiting_confirmation')
  world.overview.pending_scan = SCAN
  const user = userEvent.setup()
  await ready()
  await screen.findByRole('alertdialog')
  await user.keyboard('{Enter}')
  await waitFor(() => expect(posts().map((c) => c.path)).toEqual(['/api/backup/scan/3/discard']))
})

test('Scan again drops the held scan, then starts a fresh backup of the same sets', async () => {
  world.overview.job = job('awaiting_confirmation')
  world.overview.pending_scan = SCAN
  await ready()
  await userEvent.click(await screen.findByRole('button', { name: 'Scan again' }))
  await waitFor(() => expect(posts().map((c) => c.path)).toEqual(['/api/backup/scan/3/discard']))
  // The waiting backup ends once the scan is dropped.
  world.overview = { ...world.overview, job: null, pending_scan: null }
  act(() => useEventStore.setState({ job: job('done') }))
  await waitFor(() =>
    expect(posts().map((c) => c.path)).toEqual(['/api/backup/scan/3/discard', '/api/backup/start']),
  )
  expect(posts()[1].body).toEqual({ sets: ['Camera', 'Screenshots'] })
})

// -- failed files drawer -------------------------------------------------------------------------

test('the failed drawer lists each file with its reason and Retry starts a retry', async () => {
  world.failures = FAILURES
  const user = userEvent.setup()
  await ready()
  await user.click(await screen.findByRole('button', { name: 'show' }))
  const drawer = await screen.findByRole('dialog', { name: 'Files that could not be copied' })
  expect(within(drawer).getByText('/sdcard/DCIM/a.jpg')).toBeInTheDocument()
  expect(within(drawer).getByText('The phone stopped answering.')).toBeInTheDocument()
  expect(within(drawer).getByText('The file changed while copying.')).toBeInTheDocument()
  await user.click(within(drawer).getByRole('button', { name: 'Retry these files' }))
  expect(posts().map((c) => c.path)).toEqual(['/api/backup/retry'])
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})

test('the drawer takes the focus, Escape closes it and the focus goes back', async () => {
  world.failures = FAILURES
  const user = userEvent.setup()
  await ready()
  const trigger = await screen.findByRole('button', { name: 'show' })
  await user.click(trigger)
  expect(await screen.findByRole('button', { name: 'Close' })).toHaveFocus()
  await user.keyboard('{Escape}')
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  await waitFor(() => expect(trigger).toHaveFocus())
})

test('Retry in the drawer is blocked, with the reason, while a backup runs', async () => {
  world.failures = FAILURES
  world.overview.job = job('pulling')
  const user = userEvent.setup()
  await ready()
  await user.click(await screen.findByRole('button', { name: 'show' }))
  const retry = await screen.findByRole('button', { name: 'Retry these files' })
  expect(retry).toHaveAttribute('aria-disabled', 'true')
  expect(retry).toHaveAccessibleDescription(/A backup is running/)
  await user.click(retry)
  expect(posts()).toEqual([])
})

// -- fix round 1 ---------------------------------------------------------------------------------

test('Now copying lists up to three files with size and percent', async () => {
  world.overview.job = job('pulling')
  await ready()
  const list = await screen.findByRole('heading', { name: 'Now copying' })
  const items = within(list.parentElement as HTMLElement).getAllByRole('listitem')
  expect(items).toHaveLength(2)
  expect(items[0]).toHaveTextContent('DCIM/Camera/clip.mp4')
  expect(items[0]).toHaveTextContent('312.0 MB')
  expect(items[0]).toHaveTextContent('64%')
  expect(items[1]).toHaveTextContent('12%')
})

test('a stream that dropped mid-run does not leave the page stuck: the server says no job', async () => {
  // The stream said "pulling" a moment ago; the job has since ended and the overview says so.
  useEventStore.setState({ job: job('pulling'), jobAt: Date.now() - 60_000 })
  world.overview.job = null
  world.summary = SUMMARY
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Ready'))
  expect(useEventStore.getState().job).toBeNull()
  expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument()
  expect(backUpNow()).not.toHaveAttribute('aria-disabled')
  await userEvent.click(backUpNow())
  expect(posts()[0].path).toBe('/api/backup/start')
})

test('a job from the stream that the server also lists is kept', async () => {
  useEventStore.setState({ job: job('pulling', { files_done: 99 }), jobAt: Date.now() - 60_000 })
  world.overview.job = job('pulling', { files_done: 5 })
  await ready()
  expect(await screen.findByText('File 99 of 41,982')).toBeInTheDocument()
})

test('a reload after a run shows Ready and the last summary card', async () => {
  world.summary = SUMMARY
  await ready()
  await waitFor(() => expect(stateText()).toHaveTextContent('Ready'))
  const card = await screen.findByRole('region', { name: 'Backup finished' })
  expect(within(card).getByText('55')).toBeInTheDocument()
  await userEvent.click(within(card).getByRole('button', { name: 'Open backup folder' }))
  expect(posts().map((c) => c.path)).toEqual(['/api/backup/open-folder'])
})

test('with no phone connected the sets say to connect it instead of showing counts', async () => {
  world.overview.phone_connected = false
  world.overview.sets = SETS.map((s) => ({ ...s, on_phone: 0, backed_up: 0, pending: 0 }))
  await ready()
  expect(screen.getAllByText('Connect your phone to see what is on it.')).toHaveLength(3)
  expect(screen.queryByText(/0 on the phone/)).not.toBeInTheDocument()
})

test('a second click while the start request is in flight is refused', async () => {
  let release: () => void = () => undefined
  const original = globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) =>
      url === '/api/backup/start'
        ? new Promise<Response>((resolve) => {
            calls.push({ method: 'POST', path: url, body: JSON.parse(init?.body as string) })
            release = () => resolve(new Response(JSON.stringify({ job_id: 8 })))
          })
        : original(url, init),
    ),
  )
  await ready()
  await userEvent.click(backUpNow())
  await waitFor(() => expect(backUpNow()).toHaveAccessibleDescription('The backup is starting.'))
  await userEvent.click(backUpNow())
  expect(posts()).toHaveLength(1)
  release()
})

test('Escape hides the tooltip of a disabled button and the reason stays its description', async () => {
  world.status = status({ state: 'no_device' })
  const user = userEvent.setup()
  await ready()
  await user.tab()
  const tip = within(backUpNow().parentElement as HTMLElement).getByRole('tooltip')
  expect(tip).not.toHaveStyle({ display: 'none' })
  await user.keyboard('{Escape}')
  expect(tip).toHaveStyle({ display: 'none' })
  expect(backUpNow()).toHaveAccessibleDescription('Connect your phone to start a backup.')
})

test('Scan again keeps the focus while the answer is on its way', async () => {
  world.overview.job = job('awaiting_confirmation')
  world.overview.pending_scan = SCAN
  world.failPost = { '/api/backup/scan/3/discard': { status: 409, code: 'x', message: 'Not now.' } }
  const user = userEvent.setup()
  await ready()
  const again = await screen.findByRole('button', { name: 'Scan again' })
  await user.click(again)
  expect(again).not.toBeDisabled()
  expect(document.activeElement).toBe(again)
})
