import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import QuarantinePage from './QuarantinePage'

const ENTRIES = [
  { id: 1, content_hash: 'aa', phone_path: '/sdcard/DCIM/Camera/IMG_1.jpg', size: 2_000_000, moved_at: 1_700_000_000, purge_after: 1_702_592_000 },
  { id: 2, content_hash: 'bb', phone_path: '/sdcard/DCIM/Camera/IMG_2.jpg', size: 3_000_000, moved_at: 1_700_000_100, purge_after: 1_702_592_100 },
]
let calls: { method: string; path: string; body: unknown }[]
let refuse = false
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  refuse = false
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/quarantine') return reply(ENTRIES)
      if (url === '/api/quarantine/restore') {
        return refuse
          ? reply({ code: 'restore_refused', message: 'Plug in the drive that holds these files first.', detail: null }, 409)
          : reply({ restored: 1, pushed: 1 })
      }
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <QuarantinePage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

test('lists the files with both dates and posts the picked ids to restore', async () => {
  renderPage()
  expect(await screen.findByText('/sdcard/DCIM/Camera/IMG_1.jpg')).toBeInTheDocument()
  expect(screen.getByText('Deleted on')).toBeInTheDocument()
  expect(screen.getByText('Removed for good on')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Restore to phone' })).toHaveAttribute('aria-disabled', 'true')
  await userEvent.click(screen.getByRole('checkbox', { name: 'Select IMG_2.jpg' }))
  await userEvent.click(screen.getByRole('button', { name: 'Restore to phone' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/quarantine/restore')?.body).toEqual({ ids: [2], to: 'phone' }))
  expect(await screen.findByText('Restored 1 file. 1 put back on the phone.')).toBeInTheDocument()
})

test('Restore to backup only sends to backup, and a refusal is shown', async () => {
  refuse = true
  renderPage()
  await userEvent.click(await screen.findByRole('checkbox', { name: 'Select all' }))
  await userEvent.click(screen.getByRole('button', { name: 'Restore to backup only' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/quarantine/restore')?.body).toEqual({ ids: [1, 2], to: 'backup' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Plug in the drive that holds these files first.')
})
