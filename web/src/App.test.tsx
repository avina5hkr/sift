import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import { useEventStore } from './lib/events'
import { initToken } from './lib/token'

const STATUS = {
  app: { version: '0.1.0', schema_version: 1, config_warnings: [] },
  device: null,
  ssd: null,
  mac_folder: { path: '/Users/a/PhoneBackup', exists: true, free_bytes: 1_000_000_000 },
  jobs: [],
}

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderApp(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('redirects / to the Backup page', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(STATUS), { status: 200 })))
  renderApp('/')
  expect(await screen.findByRole('heading', { name: 'Backup' })).toBeInTheDocument()
})

test('shows the server message when status fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ code: 'bad_token', message: 'This request is not from the Sift window.', detail: null }),
        { status: 401 },
      ),
    ),
  )
  renderApp('/backup')
  expect(await screen.findByText('This request is not from the Sift window.')).toBeInTheDocument()
})

test('shows config warnings in an amber banner under the status bar', async () => {
  const warnings = ['phone.adb_server_port: invalid value, using the default', 'bogus: unknown setting, ignored']
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ...STATUS, app: { ...STATUS.app, config_warnings: warnings } }), { status: 200 }),
    ),
  )
  renderApp('/backup')
  const banner = await screen.findByRole('status', { name: 'Some settings in config.toml were not used' })
  expect(within(banner).getByText('Some settings in config.toml were not used')).toBeInTheDocument()
  const items = within(banner).getAllByRole('listitem')
  expect(items.map((li) => li.textContent)).toEqual(warnings)
  expect(banner).toHaveClass('bg-amber-100', 'text-amber-950')
  expect(within(banner).queryByRole('button')).toBeNull()
})

test('shows no config banner when there are no warnings', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(STATUS), { status: 200 })))
  renderApp('/backup')
  await screen.findByRole('heading', { name: 'Backup' })
  expect(screen.queryByText('Some settings in config.toml were not used')).toBeNull()
})

test('a phone held by another tool shows the banner, and Retry posts to the retry endpoint', async () => {
  const held = {
    ...STATUS,
    device: {
      state: 'held_by_other',
      message: 'Another tool may be using the phone. Close it, then press Retry.',
      serial: null,
      model: null,
      android_version: null,
      battery_pct: null,
      free_bytes: null,
      total_bytes: null,
      link_mbps: null,
      usb2_hint: false,
      device_uuid: null,
      reason: null,
    },
  }
  const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
    Promise.resolve(
      url === '/api/devices/retry' ? new Response(null, { status: 204 }) : new Response(JSON.stringify(held), { status: 200 }),
    ),
  )
  vi.stubGlobal('fetch', fetchMock)
  renderApp('/backup')
  const banner = await screen.findByTestId('device-banner')
  expect(banner).toHaveTextContent('Another tool may be using the phone. Close it, then press Retry.')
  expect(screen.getByText('Phone busy in another tool')).toBeInTheDocument()
  await userEvent.click(within(banner).getByRole('button', { name: 'Retry' }))
  const call = fetchMock.mock.calls.find(([url]) => url === '/api/devices/retry')
  expect(call?.[1]?.method).toBe('POST')
  expect(new Headers(call?.[1]?.headers).get('x-sift-token')).toBe('abc')
})

test('a connected phone shows its chip and no device banner', async () => {
  const connected = { ...STATUS, device: { state: 'connected_usb', message: 'Connected over USB.', serial: 'S', model: 'TestPhone', android_version: '14', battery_pct: 87, free_bytes: 12_000_000_000, total_bytes: 128_000_000_000, link_mbps: 5000, usb2_hint: false, device_uuid: 'u', reason: null } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(connected), { status: 200 })))
  renderApp('/backup')
  expect(await screen.findByText('TestPhone · USB · 87% · 12.0 GB free')).toBeInTheDocument()
  expect(screen.queryByTestId('device-banner')).toBeNull()
})

const HELD = {
  ...STATUS,
  device: {
    state: 'held_by_other',
    message: 'Another tool may be using the phone. Close it, then press Retry.',
    serial: null,
    model: null,
    android_version: null,
    battery_pct: null,
    free_bytes: null,
    total_bytes: null,
    link_mbps: null,
    usb2_hint: false,
    device_uuid: null,
    reason: null,
  },
}

test('Retry is disabled while its request is pending, then works again', async () => {
  let finish: (r: Response) => void = () => undefined
  const pending = new Promise<Response>((resolve) => (finish = resolve))
  const fetchMock = vi.fn((url: string) =>
    url === '/api/devices/retry' ? pending : Promise.resolve(new Response(JSON.stringify(HELD), { status: 200 })),
  )
  vi.stubGlobal('fetch', fetchMock)
  renderApp('/backup')
  const banner = await screen.findByTestId('device-banner')
  const button = within(banner).getByRole('button', { name: 'Retry' })
  await userEvent.click(button)
  expect(button).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Retry connecting to the phone' })).toBeDisabled()
  await userEvent.click(button) // a second click while pending sends nothing
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/devices/retry')).toHaveLength(1)
  finish(new Response(null, { status: 204 }))
  await waitFor(() => expect(button).toBeEnabled())
})

test('a failed Retry shows the plain server message in the banner', async () => {
  const body = { code: 'no_tracker', message: 'Sift is not looking for a phone right now.', detail: null }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      Promise.resolve(
        url === '/api/devices/retry'
          ? new Response(JSON.stringify(body), { status: 409 })
          : new Response(JSON.stringify(HELD), { status: 200 }),
      ),
    ),
  )
  renderApp('/backup')
  const banner = await screen.findByTestId('device-banner')
  await userEvent.click(within(banner).getByRole('button', { name: 'Retry' }))
  expect(await within(banner).findByRole('alert')).toHaveTextContent('Sift is not looking for a phone right now.')
  expect(within(banner).getByRole('button', { name: 'Retry' })).toBeEnabled()
})

test('a Retry that cannot reach the engine shows a plain message, not an error dump', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      url === '/api/devices/retry'
        ? Promise.reject(new TypeError('Failed to fetch'))
        : Promise.resolve(new Response(JSON.stringify(HELD), { status: 200 })),
    ),
  )
  renderApp('/backup')
  const banner = await screen.findByTestId('device-banner')
  await userEvent.click(within(banner).getByRole('button', { name: 'Retry' }))
  const alert = await within(banner).findByRole('alert')
  expect(alert).toHaveTextContent('Sift could not reach its engine. Try again in a moment.')
  expect(alert).not.toHaveTextContent('Failed to fetch')
})

test('/settings opens the General settings page', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      Promise.resolve(new Response(JSON.stringify(url === '/api/settings' ? { pages: { general: [] }, warnings: [] } : STATUS), { status: 200 })),
    ),
  )
  renderApp('/settings')
  expect(await screen.findByRole('heading', { level: 1, name: 'General' })).toBeInTheDocument()
  expect(screen.getByRole('navigation', { name: 'Settings pages' })).toBeInTheDocument()
})

test('shows messages from the engine as toasts', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(STATUS), { status: 200 })))
  renderApp('/logs')
  useEventStore.setState({ toasts: [{ id: 99, level: 'info', title: 'Backup finished', message: 'All done.' }] })
  expect(await screen.findByText('Backup finished')).toBeInTheDocument()
  useEventStore.setState({ toasts: [] })
})

function stubApi(setupComplete: boolean | 'fails') {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/setup/state') {
        if (setupComplete === 'fails') return new Response(JSON.stringify({ code: 'x', message: 'nope', detail: null }), { status: 500 })
        return new Response(
          JSON.stringify({
            complete: setupComplete,
            phone: null,
            mac_root: '~/PhoneBackup',
            device_label: 'Phone',
            suggested_label: 'Phone',
            ssd_volume_uuid: '',
          }),
          { status: 200 },
        )
      }
      return new Response(JSON.stringify(STATUS), { status: 200 })
    }),
  )
}

test('the first run opens the setup wizard instead of the Backup screen', async () => {
  stubApi(false)
  renderApp('/')
  expect(await screen.findByRole('heading', { name: 'Set up Sift' })).toBeInTheDocument()
  expect(screen.getByRole('heading', { level: 2, name: 'Welcome to Sift' })).toBeInTheDocument()
})

test('after setup the app opens the Backup screen', async () => {
  stubApi(true)
  renderApp('/')
  expect(await screen.findByRole('heading', { name: 'Backup' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Set up Sift' })).toBeNull()
})

test('if the setup state cannot be read the app still opens the Backup screen', async () => {
  stubApi('fails')
  renderApp('/')
  expect(await screen.findByRole('heading', { name: 'Backup' })).toBeInTheDocument()
})

test('the wizard can be reopened at /setup after setup is finished', async () => {
  stubApi(true)
  renderApp('/setup')
  expect(await screen.findByRole('heading', { name: 'Set up Sift' })).toBeInTheDocument()
})
