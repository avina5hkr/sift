import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { Checklist, ChecklistItem } from '../lib/types'
import { initToken } from '../lib/token'
import ChecklistPage from './ChecklistPage'

const item = (over: Partial<ChecklistItem>): ChecklistItem => ({
  id: 1, title: 'Item', how: 'tick', help: 'Help.', auto_ok: null, auto_detail: null, ticked: false, done_on: null, note: null, green: false, ...over,
})
let list: Checklist
let phone = true
let calls: { method: string; path: string; body: Record<string, unknown> | null }[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  phone = true
  list = {
    phone: true,
    items: [
      item({ id: 1, title: 'Full backup verified', how: 'auto', auto_ok: true, auto_detail: 'Both backups confirmed.', green: true }),
      item({ id: 4, title: 'Smart Switch backup done', how: 'tick' }),
      item({ id: 9, title: 'App reinstall list saved', how: 'auto', auto_ok: false, auto_detail: 'No app list yet.' }),
    ],
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const body = init.body ? (JSON.parse(init.body as string) as Record<string, unknown>) : null
      calls.push({ method: init.method ?? 'GET', path: url, body })
      if (url === '/api/reset-checklist' && !init.method) return reply({ ...list, phone })
      if (url === '/api/reset-checklist/4' && init.method === 'PUT') {
        list = { ...list, items: list.items.map((i) => (i.id === 4 ? { ...i, ...body, green: body?.ticked === true } : i)) }
        return reply(list)
      }
      if (url === '/api/reset-checklist/restore') {
        return reply({
          contacts: { phone_path: '/sdcard/contacts.vcf', cards: 1234 },
          reinstall: { path: '/Backups/apps.csv', apps: [{ name: 'Signal', package: 'org.signal', link: 'https://play.google.com/x' }, { name: 'Local', package: 'a.b', link: '' }] },
        })
      }
      if (url === '/api/reset-checklist/restore/contacts') return reply({ phone_path: '/sdcard/Download/contacts.vcf' })
      if (url === '/api/reset-checklist/export') return reply({ paths: ['/Backups/reports/list.md'], markdown: '# x' })
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
        <ChecklistPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('shows the intro, the count, green items and automatic details', async () => {
  renderPage()
  expect(await screen.findByText('1 of 3 done')).toBeInTheDocument()
  expect(screen.getByText(/Sift never resets the phone\. Use this list before a factory reset you do yourself\./)).toBeInTheDocument()
  expect(screen.getByText('Both backups confirmed.')).toBeInTheDocument()
  expect(screen.getAllByRole('img', { name: 'Done' })).toHaveLength(1)
  // automatic items have no tick box
  expect(screen.getAllByRole('checkbox')).toHaveLength(1)
})

test('without the phone, automatic items say to connect it', async () => {
  phone = false
  renderPage()
  await screen.findByText('1 of 3 done')
  expect(screen.getAllByText(/Connect the phone to check this/).length).toBeGreaterThan(0)
})

test('ticking sends a PUT and the count updates; the note saves on blur only', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByText('1 of 3 done')
  await user.click(screen.getByRole('checkbox', { name: 'I did this' }))
  await waitFor(() => expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ ticked: true, done_on: null, note: null }))
  expect(await screen.findByText('2 of 3 done')).toBeInTheDocument()
  const puts = () => calls.filter((c) => c.method === 'PUT').length
  await user.type(screen.getByLabelText('Note'), 'in the drawer')
  expect(puts()).toBe(1)
  await user.tab()
  await waitFor(() => expect(puts()).toBe(2))
  expect(calls.filter((c) => c.method === 'PUT')[1].body).toEqual({ ticked: true, done_on: null, note: 'in the drawer' })
})

test('sending contacts needs the confirm dialog, then says where the file went', async () => {
  const user = userEvent.setup()
  renderPage()
  expect(await screen.findByText(/1,234 contacts/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Play Store' })).toHaveAttribute('href', 'https://play.google.com/x')
  await user.click(screen.getByRole('button', { name: 'Send contacts to the phone' }))
  expect(calls.some((c) => c.path.endsWith('/restore/contacts'))).toBe(false)
  const dialog = await screen.findByRole('dialog', { name: 'Send contacts to the phone' })
  await user.click(within(dialog).getByRole('button', { name: 'Send contacts' }))
  expect(await screen.findByText(/\/sdcard\/Download\/contacts\.vcf/)).toBeInTheDocument()
  expect(screen.getByText(/Open it on the phone from Download to import/)).toBeInTheDocument()
  expect(calls.find((c) => c.path.endsWith('/restore/contacts'))?.body).toEqual({ confirm: true })
})

test('Export checklist shows the saved path', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByText('1 of 3 done')
  await user.click(screen.getByRole('button', { name: 'Export checklist' }))
  expect(await screen.findByText('/Backups/reports/list.md')).toBeInTheDocument()
})
