import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../../lib/events'
import { initToken } from '../../lib/token'
import type { ModelStatus, TaskEvent } from '../../lib/types'
import { ModelCard } from './ModelCard'

let model: ModelStatus
let calls: { method: string; path: string; body: unknown }[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  useEventStore.setState({ task: null })
  model = { key: 'clip', installed: false, size_bytes: 350_000_000 }
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/cleanup/models') return reply(model)
      if (url === '/api/tasks/current') return reply(null)
      return reply({ kind: 'model', state: 'running', done: 0, total: 350_000_000, message: '', result: null })
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ModelCard />
    </QueryClientProvider>,
  )
}

test('not installed: Download stays off until the box is ticked, then sends consent', async () => {
  renderCard()
  const button = await screen.findByRole('button', { name: 'Download' })
  expect(screen.getByText(/350\.0 MB/)).toBeInTheDocument()
  expect(button).toHaveAttribute('aria-disabled', 'true')
  await userEvent.click(button)
  expect(calls.some((c) => c.method === 'POST')).toBe(false)

  await userEvent.click(screen.getByRole('checkbox', { name: 'I agree to download it' }))
  expect(button).not.toHaveAttribute('aria-disabled')
  await userEvent.click(button)
  await waitFor(() =>
    expect(calls).toContainEqual({ method: 'POST', path: '/api/cleanup/models/download', body: { consent: true } }),
  )
})

test('downloading shows progress in bytes', async () => {
  const task: TaskEvent = { kind: 'model', state: 'running', done: 175_000_000, total: 350_000_000, message: '', result: null }
  useEventStore.setState({ task })
  renderCard()
  expect(await screen.findByRole('status')).toHaveTextContent('Downloading… 175.0 MB of 350.0 MB')
  expect(screen.getByRole('progressbar', { name: 'Download progress' })).toHaveAttribute('value', '175000000')
  expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
})

test('installed says so and suggests Analyze again', async () => {
  model.installed = true
  renderCard()
  expect(await screen.findByText(/model is installed/)).toHaveTextContent('Analyze again')
  expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument()
})
