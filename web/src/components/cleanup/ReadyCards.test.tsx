import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../../lib/events'
import { initToken } from '../../lib/token'
import { ReadyCards } from './ReadyCards'

let calls: { method: string; path: string; body: unknown }[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))
const confirmedAt = new Date(2026, 10, 7, 12).getTime() / 1000

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  sessionStorage.clear()
  useEventStore.setState({ task: null })
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/cleanup/ready') return reply([{ set_name: 'Camera', files: 900, bytes: 40_000_000_000, confirmed_at: confirmedAt }])
      if (url === '/api/delete/preflight') return reply({ run_id: 3, task: { kind: 'preflight', state: 'running', done: 0, total: 0, message: '', result: null } })
      if (url === '/api/tasks/current') return reply({ kind: 'preflight', state: 'running', done: 0, total: 0, message: '', result: null })
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderCards() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ReadyCards />
    </QueryClientProvider>,
  )
}

test('a ready set shows its size and date; Review and delete starts the checks for that set', async () => {
  renderCards()
  expect(await screen.findByText('Ready to free: 40.0 GB in Camera')).toBeInTheDocument()
  expect(screen.getByText(/confirmed 7 Nov/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Review and delete' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/delete/preflight')?.body).toEqual({ source: 'analysis', sets: ['Camera'] }))
  expect(await screen.findByText('Checking the files…')).toBeInTheDocument()
})

test('Not now hides the card for the session', async () => {
  const first = renderCards()
  await userEvent.click(await screen.findByRole('button', { name: 'Not now' }))
  expect(screen.queryByText(/Ready to free/)).not.toBeInTheDocument()
  first.unmount()
  renderCards()
  await waitFor(() => expect(calls.filter((c) => c.path === '/api/cleanup/ready')).toHaveLength(2))
  expect(screen.queryByText(/Ready to free/)).not.toBeInTheDocument()
})
