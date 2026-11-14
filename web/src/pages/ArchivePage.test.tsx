import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import ArchivePage from './ArchivePage'

const LISTING = {
  dest: 1,
  path: '',
  folders: [{ name: 'DCIM', rel_path: 'DCIM', size: 5_000_000, files: 2 }],
  files: [
    { rel_path: 'a.jpg', name: 'a.jpg', size: 1000, content_hash: 'aa', phone_path: '/sdcard/a.jpg', on_phone: false },
    { rel_path: 'b.jpg', name: 'b.jpg', size: 2000, content_hash: 'bb', phone_path: '/sdcard/b.jpg', on_phone: true },
    { rel_path: 'c.jpg', name: 'c.jpg', size: 3000, content_hash: 'cc', phone_path: '/sdcard/c.jpg', on_phone: false },
  ],
}
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
      if (url === '/api/archive/destinations') return reply([{ id: 1, kind: 'mac', root: '/Backups/Mac' }, { id: 2, kind: 'ssd', root: '/Volumes/SSD' }])
      if (url.startsWith('/api/archive?')) return reply(LISTING)
      if (url === '/api/archive/restore') {
        return refuse
          ? reply({ code: 'restore_refused', message: 'The phone is not connected.', detail: null }, 409)
          : reply({ restored: 2, already_on_phone: 1 })
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
        <ArchivePage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

test('lists kept files, blocks the ones already on the phone, and posts the ticked paths', async () => {
  renderPage()
  expect(await screen.findByText('a.jpg')).toBeInTheDocument()
  expect(screen.getByText('DCIM')).toBeInTheDocument()
  expect(screen.getAllByText('Not on the phone')).toHaveLength(2)
  expect(screen.getByRole('cell', { name: 'On the phone' })).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Select b.jpg' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Restore to phone' })).toHaveAttribute('aria-disabled', 'true')
  await userEvent.click(screen.getByRole('checkbox', { name: 'Select a.jpg' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Select c.jpg' }))
  await userEvent.click(screen.getByRole('button', { name: 'Restore to phone' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/archive/restore')?.body).toEqual({ dest: 1, paths: ['a.jpg', 'c.jpg'] }))
  expect(await screen.findByText('Put 2 files back on the phone. 1 file was already there.')).toBeInTheDocument()
})

test('a refusal is shown in plain words', async () => {
  refuse = true
  renderPage()
  await userEvent.click(await screen.findByRole('checkbox', { name: 'Select a.jpg' }))
  await userEvent.click(screen.getByRole('button', { name: 'Restore to phone' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The phone is not connected.')
})

test('switching to the SSD and opening a folder reads that listing', async () => {
  renderPage()
  await screen.findByText('a.jpg')
  await userEvent.click(screen.getByRole('button', { name: 'SSD' }))
  await waitFor(() => expect(calls.some((c) => c.path === '/api/archive?dest=2&path=')).toBe(true))
  await userEvent.click(await screen.findByRole('button', { name: /DCIM/ }))
  await waitFor(() => expect(calls.some((c) => c.path === '/api/archive?dest=2&path=DCIM')).toBe(true))
})
