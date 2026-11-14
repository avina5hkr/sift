import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import ChatBackupsPage from './ChatBackupsPage'

const base = { app: 'WhatsApp', folder: 'Databases', on_phone: false, drives: ['mac', 'ssd'], why_kept: null }
const COPIES = [
  { ...base, content_hash: 'n1', name: 'msgstore-new.db.crypt15', size: 5_000_000, date: 1_731_000_000, newest: true, deletable: false, why_kept: 'Newest, kept' },
  { ...base, content_hash: 'o1', name: 'msgstore-old1.db.crypt15', size: 4_000_000, date: 1_730_000_000, newest: false, deletable: true },
  { ...base, content_hash: 'o2', name: 'msgstore-old2.db.crypt15', size: 3_000_000, date: 1_729_000_000, newest: false, deletable: true, drives: ['mac'] },
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
      if (url === '/api/archive/chat-backups') return reply({ copies: COPIES })
      if (url === '/api/archive/chat-backups/delete') {
        return refuse
          ? reply({ code: 'delete_refused', message: 'Plug in every backup drive first.', detail: null }, 409)
          : reply({ deleted: 2, failed: 0, freed_bytes: 7_000_000 })
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
        <ChatBackupsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}
const deleteCall = () => calls.find((c) => c.path === '/api/archive/chat-backups/delete')

test('lists rows with kept notes; kept rows have no checkbox', async () => {
  renderPage()
  expect(await screen.findByText('msgstore-new.db.crypt15')).toBeInTheDocument()
  expect(screen.getByText('Newest, kept')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'WhatsApp, Databases' })).toBeInTheDocument()
  expect(screen.getAllByText('Mac and SSD')).toHaveLength(2)
  expect(screen.getByText('Mac')).toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: 'Select msgstore-new.db.crypt15' })).toBeNull()
  expect(screen.getAllByRole('checkbox')).toHaveLength(2)
})

test('delete sends the hashes only after the confirm', async () => {
  renderPage()
  await userEvent.click(await screen.findByRole('button', { name: 'Select all older' }))
  expect(screen.getByText('2 selected, 7.0 MB')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
  expect(deleteCall()).toBeUndefined()
  expect(screen.getByText('This deletes these chat backups from your Mac and SSD backup folders. It cannot be undone.')).toBeInTheDocument()
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))
  await waitFor(() => expect(deleteCall()?.body).toEqual({ hashes: ['o1', 'o2'], confirm: true }))
  expect(await screen.findByText(/^Deleted 2, freed/)).toBeInTheDocument()
})

test('a refusal is shown in the dialog', async () => {
  refuse = true
  renderPage()
  await userEvent.click(await screen.findByRole('checkbox', { name: 'Select msgstore-old1.db.crypt15' }))
  await userEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
  await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Plug in every backup drive first.')
})
