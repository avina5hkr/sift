import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../../lib/token'
import SettingsPage from '../SettingsPage'
import WizardPage from './index'

type Handler = (body: unknown) => unknown | { __status: number; body: unknown }
type Routes_ = Record<string, Handler>

const DEVICE = {
  state: 'connected_usb',
  message: 'Connected over USB.',
  serial: null,
  model: 'Test phone',
  android_version: '14',
  battery_pct: 80,
  free_bytes: 1,
  total_bytes: 2,
  link_mbps: null,
  usb2_hint: false,
  device_uuid: 'u',
  reason: null,
}
const STATUS = {
  app: { version: '0.1.0', schema_version: 1, config_warnings: [] },
  device: DEVICE,
  ssd: null,
  mac_folder: { path: '/Users/a/PhoneBackup', exists: false, free_bytes: 1e12 },
  jobs: [],
}
const STATE = {
  complete: false,
  phone: { uuid: 'u', model: 'Test phone', last4: '0001', label: null },
  mac_root: '~/PhoneBackup',
  device_label: 'Phone',
  suggested_label: 'Test phone',
  ssd_volume_uuid: '',
}
const TREE = {
  phone_connected: true,
  scanning: false,
  scan_job_id: null,
  sets: [
    { name: 'Camera', enabled: true, paths: ['/sdcard/DCIM'], files: 1200, bytes: 41_200_000_000 },
    { name: 'Downloads', enabled: false, paths: ['/sdcard/Download'], files: 30, bytes: 2_000_000_000 },
  ],
}

let calls: { method: string; path: string; body: unknown }[]

function mockApi(overrides: Routes_ = {}) {
  calls = []
  const routes: Routes_ = {
    'GET /api/status': () => STATUS,
    'GET /api/setup/state': () => STATE,
    ...overrides,
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      const path = url.split('?')[0]
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      calls.push({ method, path: url, body })
      const handler = routes[`${method} ${path}`]
      if (!handler) return new Response(JSON.stringify({ code: 'x', message: `no mock for ${method} ${url}`, detail: null }), { status: 500 })
      const out = handler(body) as { __status?: number; body?: unknown }
      if (out && typeof out === 'object' && '__status' in out)
        return new Response(JSON.stringify(out.body), { status: out.__status })
      return new Response(JSON.stringify(out), { status: 200 })
    }),
  )
}
const fail = (status: number, code: string, message: string) => ({ __status: status, body: { code, message, detail: null } })
const posted = (path: string) => calls.filter((c) => c.method === 'POST' && c.path === path)

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderWizard(initial = '/setup') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/setup" element={<WizardPage />} />
          <Route path="/backup" element={<h1>Backup page</h1>} />
          <Route path="/settings/:page" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const click = async (name: string | RegExp) => userEvent.click(await screen.findByRole('button', { name }))

async function goTo(title: string | RegExp, ...clicks: (string | RegExp)[]) {
  for (const c of clicks) await click(c)
  expect(await screen.findByRole('heading', { level: 2, name: title })).toBeInTheDocument()
}

test('welcome says what Sift does and that nothing leaves the Mac', async () => {
  mockApi()
  renderWizard()
  expect(await screen.findByRole('heading', { level: 2, name: 'Welcome to Sift' })).toBeInTheDocument()
  expect(screen.getByText(/Everything stays on this Mac/)).toBeInTheDocument()
  expect(screen.getByText('Step 1 of 8: Welcome')).toBeInTheDocument()
  expect(screen.getByRole('listitem', { current: 'step' })).toHaveTextContent('1. Welcome')
})

test('the heading of each step takes the focus', async () => {
  mockApi()
  renderWizard()
  const welcome = await screen.findByRole('heading', { level: 2, name: 'Welcome to Sift' })
  await waitFor(() => expect(welcome).toHaveFocus())
  await click('Get started')
  const usb = await screen.findByRole('heading', { level: 2, name: 'Turn on USB debugging' })
  await waitFor(() => expect(usb).toHaveFocus())
})

test('USB debugging lists the steps and the Samsung notes open for a Samsung phone', async () => {
  mockApi({ 'GET /api/status': () => ({ ...STATUS, device: { ...DEVICE, model: 'SM-X000' } }) })
  renderWizard()
  await goTo('Turn on USB debugging', 'Get started')
  expect(screen.getByText(/Tap Build number 7 times/)).toBeInTheDocument()
  expect(screen.getByText(/File transfer/)).toBeInTheDocument()
  await waitFor(() => expect(screen.getByText('Notes for your Samsung phone').closest('details')).toHaveAttribute('open'))
  expect(screen.getByText(/Auto Blocker/)).toBeVisible()
})

test('the Samsung notes stay closed for another phone', async () => {
  mockApi()
  renderWizard()
  await goTo('Turn on USB debugging', 'Get started')
  expect(screen.getByText('Do you have a Samsung phone?').closest('details')).not.toHaveAttribute('open')
})

const ADB_MISSING = { status: 'missing', version: null, message: 'adb was not found.', download_bytes: 15_000_000, download_host: 'dl.google.com' }

test('a missing adb shows the size and needs a ticked consent before it downloads', async () => {
  let ready = false
  mockApi({
    'GET /api/setup/adb': () => (ready ? { ...ADB_MISSING, status: 'ready', version: '35.0.2' } : ADB_MISSING),
    'POST /api/devices/adb/download': () => {
      ready = true
      return { path: null, version: '35.0.2', status: 'ready', message: 'ok' }
    },
  })
  renderWizard()
  await goTo('Phone tools (adb)', 'Get started', 'Continue')
  expect(await screen.findByText(/It is about 15.0 MB and comes from dl.google.com/)).toBeInTheDocument()
  const download = screen.getByRole('button', { name: 'Download platform-tools' })
  expect(download).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()
  await userEvent.click(screen.getByRole('checkbox', { name: /I agree to download Google/ }))
  expect(download).toBeEnabled()
  await userEvent.click(download)
  expect(await screen.findByText(/adb is ready \(version 35.0.2\)/)).toBeInTheDocument()
  expect(posted('/api/devices/adb/download').map((c) => c.body)).toEqual([{ consent: true }])
  expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()
})

test('a failed download says why in plain words', async () => {
  mockApi({
    'GET /api/setup/adb': () => ADB_MISSING,
    'POST /api/devices/adb/download': () => fail(502, 'adb_download_failed', 'The download did not finish.'),
  })
  renderWizard()
  await goTo('Phone tools (adb)', 'Get started', 'Continue')
  await userEvent.click(await screen.findByRole('checkbox', { name: /I agree/ }))
  await click('Download platform-tools')
  expect(await screen.findByRole('alert')).toHaveTextContent('The download did not finish.')
})

test('a working adb needs no download', async () => {
  mockApi({ 'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '35.0.2' }) })
  renderWizard()
  await goTo('Phone tools (adb)', 'Get started', 'Continue')
  expect(await screen.findByText(/Nothing to download/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Download platform-tools' })).toBeNull()
})

const PHONES = {
  phones: [
    { ref: 'r1', model: 'Alpha 1', last4: '0001', ready: true, waiting_for_allow: false, stored: false },
    { ref: 'r2', model: 'Beta 2', last4: '0002', ready: true, waiting_for_allow: false, stored: false },
  ],
}

test('with two phones the picker lists model and the last 4, and the choice is posted', async () => {
  mockApi({
    'GET /api/status': () => ({ ...STATUS, device: { ...DEVICE, state: 'no_device', message: 'Looking.' } }),
    'GET /api/setup/devices': () => PHONES,
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
    'POST /api/setup/phone': () => ({ uuid: 'u2', model: 'Beta 2', last4: '0002', label: null }),
  })
  renderWizard()
  await goTo('Connect your phone', 'Get started', 'Continue', 'Continue')
  const group = await screen.findByRole('group', { name: 'Which phone should Sift back up?' })
  expect(within(group).getAllByRole('radio').map((r) => r.parentElement?.textContent)).toEqual([
    'Alpha 1 ending 0001',
    'Beta 2 ending 0002',
  ])
  await userEvent.click(within(group).getByRole('radio', { name: /Beta 2/ }))
  await userEvent.click(within(group).getByRole('button', { name: 'Use this phone' }))
  await waitFor(() => expect(posted('/api/setup/phone').map((c) => c.body)).toEqual([{ ref: 'r2' }]))
  // a serial number never reaches the page
  expect(document.body.textContent).not.toMatch(/AAAA|BBBB|serial/i)
})

test('an unauthorised phone says what to tap', async () => {
  mockApi({
    'GET /api/status': () => ({ ...STATUS, device: { ...DEVICE, state: 'unauthorized', message: 'Unlock the phone.' } }),
    'GET /api/setup/devices': () => ({ phones: [] }),
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
  })
  renderWizard()
  await goTo('Connect your phone', 'Get started', 'Continue', 'Continue')
  expect(await screen.findByText(/Waiting for you to allow this Mac/)).toBeInTheDocument()
  expect(screen.getByText(/Always allow from this computer/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Skip for now' })).toBeEnabled()
})

async function toFolder() {
  mockApi({
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
    'GET /api/setup/devices': () => ({ phones: [PHONES.phones[0]] }),
    'GET /api/setup/folder': () => ({ path: '/Users/a/PhoneBackup', ok: true, problem: null, exists: false, free_bytes: 500_000_000_000 }),
    'POST /api/setup/mac-folder': (b) => ({ mac_root: (b as { path: string }).path, device_label: 'Test phone', free_bytes: 1 }),
  })
}

test('the backup folder shows free space and is saved with the phone name', async () => {
  await toFolder()
  renderWizard()
  await goTo('Choose the backup folder', 'Get started', 'Continue', 'Continue', 'Continue')
  expect(await screen.findByText('500.0 GB free here.')).toBeInTheDocument()
  expect(screen.getByLabelText('Backup folder')).toHaveValue('~/PhoneBackup')
  expect(screen.getByLabelText(/Name for this phone/)).toHaveValue('Test phone')
  await click('Save and continue')
  expect(await screen.findByRole('heading', { level: 2, name: /An SSD for a second copy/ })).toBeInTheDocument()
  expect(posted('/api/setup/mac-folder').map((c) => c.body)).toEqual([{ path: '~/PhoneBackup', label: 'Test phone' }])
})

test('a folder the server rejects shows its reason and cannot be saved', async () => {
  mockApi({
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
    'GET /api/setup/devices': () => ({ phones: [PHONES.phones[0]] }),
    'GET /api/setup/folder': () => ({ path: '/x', ok: false, problem: 'That is where Sift keeps its own data. Choose another folder.', exists: true, free_bytes: null }),
  })
  renderWizard()
  await goTo('Choose the backup folder', 'Get started', 'Continue', 'Continue', 'Continue')
  expect(await screen.findByText(/where Sift keeps its own data/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save and continue' })).toBeDisabled()
  expect(screen.getByLabelText('Backup folder')).toHaveAttribute('aria-invalid', 'true')
})

const VOLUMES = {
  volumes: [
    { name: 'Backup SSD', file_system: 'ExFAT', uuid: 'u-ssd', free_bytes: 700_000_000_000, total_bytes: 1e12, suitable: true, warning: null },
    { name: 'Old', file_system: 'MS-DOS FAT32', uuid: 'u-old', free_bytes: 5e9, total_bytes: 32e9, suitable: false, warning: 'This drive is formatted as MS-DOS FAT32.' },
  ],
  selected_uuid: '',
}

test('the SSD step lists drives with warnings, explains the macOS prompt, and can be skipped', async () => {
  mockApi({
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
    'GET /api/setup/devices': () => ({ phones: [PHONES.phones[0]] }),
    'GET /api/setup/folder': () => ({ path: '/p', ok: true, problem: null, exists: false, free_bytes: 1 }),
    'POST /api/setup/mac-folder': () => ({ mac_root: '~/PhoneBackup', device_label: 'Phone', free_bytes: 1 }),
    'GET /api/setup/volumes': () => VOLUMES,
    'POST /api/setup/ssd': () => VOLUMES,
    'GET /api/setup/sets': () => TREE,
  })
  renderWizard()
  await goTo('Choose the backup folder', 'Get started', 'Continue', 'Continue', 'Continue')
  await screen.findByText(/free here/)
  await goTo(/An SSD for a second copy/, 'Save and continue')
  expect(await screen.findByText('Backup SSD, ExFAT, 700.0 GB free')).toBeInTheDocument()
  expect(screen.getByText(/macOS asks whether Sift may use removable volumes/)).toBeInTheDocument()
  expect(screen.getByRole('note')).toHaveTextContent('formatted as MS-DOS FAT32')
  await click('Skip for now')
  expect(await screen.findByRole('heading', { level: 2, name: 'What to back up' })).toBeInTheDocument()
  expect(posted('/api/setup/ssd').map((c) => c.body)).toEqual([{ volume_uuid: null }])
})

test('choosing a drive posts its uuid', async () => {
  mockApi({
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
    'GET /api/setup/devices': () => ({ phones: [PHONES.phones[0]] }),
    'GET /api/setup/folder': () => ({ path: '/p', ok: true, problem: null, exists: false, free_bytes: 1 }),
    'POST /api/setup/mac-folder': () => ({ mac_root: '~/PhoneBackup', device_label: 'Phone', free_bytes: 1 }),
    'GET /api/setup/volumes': () => VOLUMES,
    'POST /api/setup/ssd': () => VOLUMES,
    'GET /api/setup/sets': () => TREE,
  })
  renderWizard()
  await goTo('Choose the backup folder', 'Get started', 'Continue', 'Continue', 'Continue')
  await screen.findByText(/free here/)
  await goTo(/An SSD for a second copy/, 'Save and continue')
  expect(screen.getByRole('button', { name: 'Use this drive' })).toBeDisabled()
  await userEvent.click(await screen.findByRole('radio', { name: /Backup SSD/ }))
  await click('Use this drive')
  await screen.findByRole('heading', { level: 2, name: 'What to back up' })
  expect(posted('/api/setup/ssd').map((c) => c.body)).toEqual([{ volume_uuid: 'u-ssd' }])
})

async function toSets(extra: Routes_ = {}) {
  mockApi({
    'GET /api/setup/adb': () => ({ ...ADB_MISSING, status: 'ready', version: '1' }),
    'GET /api/setup/devices': () => ({ phones: [PHONES.phones[0]] }),
    'GET /api/setup/folder': () => ({ path: '/p', ok: true, problem: null, exists: false, free_bytes: 1 }),
    'POST /api/setup/mac-folder': () => ({ mac_root: '~/PhoneBackup', device_label: 'Phone', free_bytes: 1 }),
    'GET /api/setup/volumes': () => ({ volumes: [], selected_uuid: '' }),
    'POST /api/setup/ssd': () => ({ volumes: [], selected_uuid: '' }),
    'GET /api/setup/sets': () => TREE,
    'POST /api/setup/sets': () => TREE,
    ...extra,
  })
  renderWizard()
  await goTo('Choose the backup folder', 'Get started', 'Continue', 'Continue', 'Continue')
  await screen.findByText(/free here/)
  await goTo(/An SSD/, 'Save and continue')
  await goTo('What to back up', 'Skip for now')
}

test('the sets tree shows sizes, the defaults are ticked, and the choice is saved', async () => {
  await toSets()
  expect(await screen.findByText('1,200 files, 41.2 GB')).toBeInTheDocument()
  expect(screen.getByText('/sdcard/DCIM')).toBeInTheDocument()
  const camera = screen.getByRole('checkbox', { name: /Camera/ })
  const downloads = screen.getByRole('checkbox', { name: /Downloads/ })
  expect(camera).toBeChecked()
  expect(downloads).not.toBeChecked()
  expect(screen.getByText('Ticked: 1,200 files, 41.2 GB')).toBeInTheDocument()
  await userEvent.click(downloads)
  expect(screen.getByText('Ticked: 1,230 files, 43.2 GB')).toBeInTheDocument()
  await click('Save and continue')
  expect(await screen.findByRole('heading', { level: 2, name: 'You are ready' })).toBeInTheDocument()
  expect(posted('/api/setup/sets').map((c) => c.body)).toEqual([{ enabled: ['Camera', 'Downloads'] }])
})

test('nothing ticked cannot be saved', async () => {
  await toSets()
  await userEvent.click(await screen.findByRole('checkbox', { name: /Camera/ }))
  expect(screen.getByRole('button', { name: 'Save and continue' })).toBeDisabled()
  expect(screen.getByText('Tick at least one group.')).toBeInTheDocument()
})

test('the first look through the phone starts by itself when no size is known', async () => {
  const unseen = { ...TREE, sets: TREE.sets.map((s) => ({ ...s, files: null, bytes: null })) }
  await toSets({
    'GET /api/setup/sets': () => unseen,
    'POST /api/setup/scan': () => ({ ...unseen, scanning: true, scan_job_id: 3 }),
  })
  await waitFor(() => expect(posted('/api/setup/scan')).toHaveLength(1))
  expect(await screen.findByText('Looking through your phone…')).toBeInTheDocument()
})

test('the done step offers the speed test, then starts the first backup with the ticked sets', async () => {
  await toSets({
    'POST /api/setup/reader-test': () => ({ tested: true, readers: 2, speed_bps: 38_000_000, message: 'Done.' }),
    'POST /api/setup/complete': () => ({ ...STATE, complete: true }),
    'POST /api/backup/start': () => ({ job_id: 5 }),
  })
  await click('Save and continue')
  await screen.findByRole('heading', { level: 2, name: 'You are ready' })
  await click(/Test my phone/)
  expect(await screen.findByText(/about 38.0 MB\/s using 2 at a time/)).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start my first backup' })).toBeEnabled())
  await click('Start my first backup')
  expect(await screen.findByRole('heading', { name: 'Backup page' })).toBeInTheDocument()
  expect(posted('/api/setup/complete')).toHaveLength(1)
  expect(posted('/api/backup/start').map((c) => c.body)).toEqual([{ sets: ['Camera'] }])
})

test('finishing without a backup only completes setup', async () => {
  await toSets({ 'POST /api/setup/complete': () => ({ ...STATE, complete: true }) })
  await click('Save and continue')
  await screen.findByRole('heading', { level: 2, name: 'You are ready' })
  await click('Finish without backing up')
  expect(await screen.findByRole('heading', { name: 'Backup page' })).toBeInTheDocument()
  expect(posted('/api/backup/start')).toHaveLength(0)
})

test('welcome can skip setup for someone who already set Sift up', async () => {
  mockApi({ 'POST /api/setup/complete': () => ({ ...STATE, complete: true }) })
  renderWizard()
  await screen.findByRole('heading', { level: 2, name: 'Welcome to Sift' })
  await click('I have already set Sift up')
  expect(await screen.findByRole('heading', { name: 'Backup page' })).toBeInTheDocument()
})

test('Back goes to the step before', async () => {
  mockApi()
  renderWizard()
  await goTo('Turn on USB debugging', 'Get started')
  await goTo('Welcome to Sift', 'Back')
  expect(screen.queryByRole('button', { name: 'Back' })).toBeNull()
})

// -- Settings: the entry points ---------------------------------------------------------------

test('Settings links back to the setup and switching phones needs the typed word', async () => {
  mockApi({
    'GET /api/settings': () => ({ pages: {}, warnings: [] }),
    'GET /api/setup/state': () => ({ ...STATE, complete: true }),
    'GET /api/setup/devices': () => ({
      phones: [{ ...PHONES.phones[0], stored: true }, PHONES.phones[1]],
    }),
    'POST /api/setup/switch-phone': () => ({ uuid: 'u2', model: 'Beta 2', last4: '0002', label: 'Beta 2' }),
  })
  renderWizard('/settings/phone')
  expect(await screen.findByText('Sift backs up Test phone ending 0001.')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Run the setup again' })).toBeNull()
  cleanup()
  renderWizard('/settings/general')
  expect(await screen.findByRole('link', { name: 'Run the setup again' })).toHaveAttribute('href', '/setup')
  cleanup()
  renderWizard('/settings/phone')
  expect(await screen.findByText('Sift backs up Test phone ending 0001.')).toBeInTheDocument()
  await click('Switch phone…')
  await userEvent.click(await screen.findByRole('radio', { name: /Beta 2/ }))
  const action = screen.getByRole('button', { name: 'Switch to this phone' })
  expect(action).toBeDisabled() // the word has not been typed
  await userEvent.type(screen.getByLabelText('Type SWITCH to confirm'), 'switch')
  expect(action).toBeEnabled()
  await userEvent.click(action)
  expect(
    await screen.findByText('Sift now backs up Beta 2 ending 0002. Its files go into the folder Beta 2.'),
  ).toBeInTheDocument()
  expect(screen.getByText(/gets its own folder inside your backup folder/)).toBeInTheDocument()
  expect(screen.queryByText(/give the new phone its own name/)).toBeNull()
  expect(posted('/api/setup/switch-phone').map((c) => c.body)).toEqual([{ ref: 'r2', confirmation: 'switch' }])
})

test('the server refusing a switch is shown in plain words', async () => {
  mockApi({
    'GET /api/settings': () => ({ pages: {}, warnings: [] }),
    'GET /api/setup/state': () => ({ ...STATE, complete: true }),
    'GET /api/setup/devices': () => PHONES,
    'POST /api/setup/switch-phone': () => fail(409, 'job_conflict', 'A backup is running. Stop it before switching phones.'),
    'GET /api/setup/volumes': () => ({ volumes: [], selected_uuid: '' }),
    'GET /api/setup/folder': () => ({ ok: true, path: '/x', problem: null, free_bytes: 1 }),
  })
  renderWizard('/settings/phone')
  await click('Switch phone…')
  await userEvent.click(await screen.findByRole('radio', { name: /Beta 2/ }))
  await userEvent.type(screen.getByLabelText('Type SWITCH to confirm'), 'SWITCH')
  await click('Switch to this phone')
  expect(await screen.findByRole('alert')).toHaveTextContent('A backup is running. Stop it before switching phones.')
})

test('Settings lets the owner choose the Mac and SSD backup folders with the folder dialog', async () => {
  const saved: unknown[] = []
  mockApi({
    'GET /api/settings': () => ({ pages: {}, warnings: [] }),
    'GET /api/setup/state': () => ({ ...STATE, complete: true }),
    'GET /api/setup/devices': () => PHONES,
    'GET /api/setup/folder': () => ({ ok: true, path: '/Users/me/Backups', problem: null, free_bytes: 5e11 }),
    'GET /api/setup/volumes': () => ({ volumes: [{ name: 'T7', file_system: 'ExFAT', uuid: 'u1', free_bytes: 1e12, total_bytes: 2e12, suitable: true, warning: null }], selected_uuid: 'u1', folder: '' }),
    'POST /api/setup/pick-folder': (body: unknown) =>
      (body as { start: string | null }).start === '/Volumes' ? { path: '/Volumes/T7/Backups' } : { path: '/Users/me/Backups' },
    'POST /api/setup/mac-folder': (body: unknown) => {
      saved.push(body)
      return { mac_root: '/Users/me/Backups', device_label: 'Phone', free_bytes: 1 }
    },
    'POST /api/setup/ssd-folder': (body: unknown) => {
      saved.push(body)
      return { volume_uuid: 'u1', volume_name: 'T7', folder: 'Backups', root: '/Volumes/T7/Backups', free_bytes: 1 }
    },
  })
  renderWizard('/settings/destinations')
  await click('Choose…')
  expect(await screen.findByDisplayValue('/Users/me/Backups')).toBeInTheDocument()
  expect(await screen.findByText(/free here/)).toBeInTheDocument() // the folder was checked
  await click('Save')
  expect(await screen.findByText('Saved.')).toBeInTheDocument()
  await click('Choose a folder on the drive…')
  expect(await screen.findByText('Backups on T7')).toBeInTheDocument()
  expect(saved).toEqual([
    expect.objectContaining({ path: '/Users/me/Backups' }),
    { path: '/Volumes/T7/Backups' },
  ])
})
