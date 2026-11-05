import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from './App'
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
