import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import type { AppRow, AppsList } from '../lib/types'
import AppsPage from './AppsPage'

const row = (over: Partial<AppRow>): AppRow => ({
  package: 'com.example',
  name: 'Example',
  installer: 'com.android.vending',
  from_play_store: true,
  version: '1.0',
  app_bytes: 100_000_000,
  data_bytes: 20_000_000,
  cache_bytes: 1_000_000,
  last_used: Math.floor(Date.now() / 1000) - 12 * 86_400,
  protected: false,
  unprotected: false,
  unused: false,
  ...over,
})
const LIST: AppsList = {
  apps: [
    row({ package: 'com.big', name: 'Big Game', app_bytes: 900_000_000 }),
    row({ package: 'com.mystery', name: 'Mystery', installer: null, from_play_store: false, app_bytes: null, data_bytes: null, cache_bytes: null, last_used: null }),
    row({ package: 'com.bank', name: 'Authenticator', protected: true, app_bytes: 5_000_000 }),
    row({ package: 'com.whatsapp', name: 'WhatsApp', protected: true, app_bytes: 4_000_000 }),
    row({ package: 'com.old', name: 'Old App', unused: true, app_bytes: 3_000_000 }),
  ],
  suggested: ['com.old'],
  probes: { packages: true, diskstats: true, usagestats: false, versions: true },
}

let calls: { method: string; path: string; body: Record<string, unknown> | null }[]
let refusals: Record<string, string>
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  refusals = {}
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const body = init.body ? (JSON.parse(init.body as string) as Record<string, unknown>) : null
      calls.push({ method: init.method ?? 'GET', path: url, body })
      if (url === '/api/apps') return reply(LIST)
      if (url === '/api/apps/preflight') {
        const packages = body?.packages as string[]
        return reply({ allowed: packages.filter((p) => !(p in refusals)), refused: refusals })
      }
      if (url === '/api/apps/uninstall') return reply({ done: body?.packages, failed: {}, app_list: ['/Backups/.sift/apps/list.json'] })
      if (url === '/api/apps/unprotect') return reply({ unprotected: true })
      if (url === '/api/apps/trim-caches') return reply({ done: true })
      return reply({ code: 'nope', message: 'No.' }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AppsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const posts = (path: string) => calls.filter((c) => c.method === 'POST' && c.path === path)

test('the table shows unavailable values, the Protected badge, and the notes', async () => {
  renderPage()
  const mystery = (await screen.findByText('Mystery')).closest('tr') as HTMLElement
  expect(within(mystery).getAllByText('unavailable')).toHaveLength(4) // size, data, cache, last used
  expect(within(mystery).getByText('unknown')).toBeInTheDocument()
  const bank = screen.getByText('Authenticator').closest('tr') as HTMLElement
  expect(within(bank).getByText('Protected')).toBeInTheDocument()
  expect(within(bank).getByText('Unprotect it first to allow this.')).toBeInTheDocument()
  expect(within(screen.getByText('Big Game').closest('tr') as HTMLElement).getByText('12 days ago')).toBeInTheDocument()
  expect(screen.getByText('Last used is unavailable on this phone.')).toBeInTheDocument()
  // sorted by size, biggest first; unknown sizes last
  const names = screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[1].textContent)
  expect(names[0]).toBe('Big Game')
  expect(names[names.length - 1]).toBe('Mystery')
  // the suggested strip lists the app and ticks nothing
  expect(within(screen.getByRole('region', { name: 'Suggested' })).getByRole('button', { name: /Old App/ })).toHaveAttribute('aria-pressed', 'false')
})

test('a filter narrows the list', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByText('Mystery')
  await user.click(screen.getByRole('button', { name: 'Not from Play Store' }))
  expect(screen.queryByText('Big Game')).not.toBeInTheDocument()
  expect(screen.getByText('Mystery')).toBeInTheDocument()
})

test('the uninstall dialog shows refusals and sends the WhatsApp unlock', async () => {
  const user = userEvent.setup()
  refusals = { 'com.whatsapp': 'Confirm that your WhatsApp chat backup is complete.' }
  renderPage()
  await screen.findByText('Mystery')
  await user.click(screen.getByRole('checkbox', { name: 'Select WhatsApp' }))
  await user.click(screen.getByRole('button', { name: 'Uninstall selected' }))
  const dialog = await screen.findByRole('dialog', { name: 'Uninstall apps' })
  expect(await within(dialog).findByText(/Confirm that your WhatsApp chat backup is complete/)).toBeInTheDocument()
  expect(within(dialog).getByText(/This app cannot back it up/)).toBeInTheDocument()
  expect(within(dialog).getByText('The app list is saved to your backup folder first.')).toBeInTheDocument()
  const go = within(dialog).getByRole('button', { name: 'Uninstall 1 app' })
  expect(go).toBeDisabled()
  expect(go).not.toHaveFocus()

  refusals = {}
  await user.click(within(dialog).getByRole('checkbox', { name: 'My WhatsApp chat backup is complete' }))
  await user.type(within(dialog).getByLabelText('Type WhatsApp to confirm'), 'WhatsApp')
  await waitFor(() => expect(go).toBeEnabled())
  await user.click(go)
  await within(dialog).findByText('Uninstall finished')
  expect(posts('/api/apps/uninstall')[0].body).toEqual({
    action: 'uninstall',
    packages: ['com.whatsapp'],
    unlock: { 'com.whatsapp': { backup_done: true, typed_name: 'WhatsApp' } },
    confirm: true,
  })
  expect(within(dialog).getByText(/\/Backups\/\.sift\/apps\/list\.json/)).toBeInTheDocument()
})

test('Unprotect posts confirm', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByText('Mystery')
  expect(screen.queryByRole('button', { name: 'Unprotect WhatsApp' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Unprotect Authenticator' }))
  const dialog = screen.getByRole('dialog', { name: 'Unprotect app' })
  expect(within(dialog).getByText(/may not be recoverable/)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Unprotect' }))
  await waitFor(() => expect(posts('/api/apps/unprotect')).toHaveLength(1))
  expect(posts('/api/apps/unprotect')[0].body).toEqual({ package: 'com.bank', confirm: true })
})

test('Clear all caches posts confirm', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByText('Mystery')
  await user.click(screen.getByRole('button', { name: 'Clear all caches' }))
  const dialog = screen.getByRole('dialog', { name: 'Clear all caches' })
  expect(within(dialog).getByText(/Apps keep their data/)).toBeInTheDocument()
  await user.click(within(dialog).getByRole('button', { name: 'Clear all caches' }))
  await waitFor(() => expect(posts('/api/apps/trim-caches')).toHaveLength(1))
  expect(posts('/api/apps/trim-caches')[0].body).toEqual({ confirm: true })
})
