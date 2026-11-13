import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { initToken } from '../lib/token'
import type { CleanupSummary, TaskEvent } from '../lib/types'
import CleanupPage from './CleanupPage'

const SUMMARY: CleanupSummary = {
  has_backup: true,
  analyzed_at: null,
  new_since: 0,
  storage: { Camera: 41_200_000_000, 'WhatsApp images': 3_000_000_000, 'WhatsApp video': 5_500_000_000, Sent: 100_000_000, Other: 900_000_000 },
  cards: [{ tab: 'exact', groups: 0, files: 0, bytes: 0 }],
}

let summary: CleanupSummary
let task: TaskEvent | null
let calls: { method: string; path: string }[]
let startFails = false
let junkStatus: unknown = null

const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  useEventStore.setState({ task: null, toasts: [] })
  summary = structuredClone(SUMMARY)
  task = null
  startFails = false
  junkStatus = null
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      calls.push({ method, path: url })
      if (url === '/api/cleanup/summary') return reply(summary)
      if (url === '/api/tasks/current') return reply(task)
      if (url === '/api/cleanup/junk/status' && junkStatus) return reply(junkStatus)
      if (url === '/api/cleanup/analyze' && startFails) {
        return reply({ code: 'task_running', message: 'Another task is already running.', detail: null }, 409)
      }
      if (method === 'POST') return reply(task)
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/cleanup']}>
        <Routes>
          <Route path="/cleanup" element={<CleanupPage />} />
          <Route path="/cleanup/review" element={<p>The review screen</p>} />
          <Route path="/cleanup/calibration" element={<p>The calibration screen</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const analyzeButton = () => screen.findByRole('button', { name: /^Analyze/ })

test('before any backup, Analyze is disabled and says why', async () => {
  summary.has_backup = false
  renderPage()
  const button = await analyzeButton()
  expect(button).toHaveAttribute('aria-disabled', 'true')
  expect(button).toHaveAccessibleDescription(/Back up your phone first/)
  await userEvent.click(button)
  expect(calls.some((c) => c.method === 'POST')).toBe(false)
})

test('shows the storage by the five kinds with their sizes', async () => {
  renderPage()
  await analyzeButton()
  for (const [name, size] of [
    ['Camera', '41.2 GB'],
    ['WhatsApp images', '3.0 GB'],
    ['WhatsApp video', '5.5 GB'],
    ['Sent', '100.0 MB'],
    ['Other', '900.0 MB'],
  ]) {
    const row = screen.getByText(name, { selector: 'li span' }).closest('li') as HTMLElement
    expect(row).toHaveTextContent(size as string)
  }
  expect(screen.getByText(/Not analysed yet/)).toBeInTheDocument()
  expect(screen.queryByText('Exact duplicates')).not.toBeInTheDocument()
})

test('Analyze starts the analysis', async () => {
  renderPage()
  await userEvent.click(await analyzeButton())
  await waitFor(() => expect(calls).toContainEqual({ method: 'POST', path: '/api/cleanup/analyze' }))
})

test('a refused start shows the reason', async () => {
  startFails = true
  renderPage()
  await userEvent.click(await analyzeButton())
  expect(await screen.findByRole('alert')).toHaveTextContent('Another task is already running.')
})

test('while analysing it shows progress and Stop', async () => {
  task = { kind: 'analyze', state: 'running', done: 30, total: 120, message: '', result: null }
  renderPage()
  const status = await screen.findByRole('status')
  expect(status).toHaveTextContent('Looking for duplicates · 30 of 120')
  expect(screen.queryByRole('button', { name: /^Analyze/ })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
  await waitFor(() => expect(calls).toContainEqual({ method: 'POST', path: '/api/cleanup/cancel' }))
})

test('another running task keeps Analyze off, with the reason', async () => {
  task = { kind: 'verify', state: 'running', done: 1, total: 2, message: '', result: null }
  renderPage()
  const button = await analyzeButton()
  expect(button).toHaveAttribute('aria-disabled', 'true')
  expect(button).toHaveAccessibleDescription(/Another task is running/)
})

test('after analysis: how old it is, new files, and the Exact duplicates card with Review', async () => {
  summary.analyzed_at = Math.floor(Date.now() / 1000) - 3 * 86_400 - 60
  summary.new_since = 214
  summary.cards = [{ tab: 'exact', groups: 12, files: 30, bytes: 2_500_000_000 }]
  renderPage()
  expect(await screen.findByText('Analysed 3 days ago, 214 new files.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Analyze again' })).toBeInTheDocument()
  const card = screen.getByRole('region', { name: 'Exact duplicates' })
  expect(card).toHaveTextContent('12 groups, 30 extra copies, 2.5 GB to free')
  await userEvent.click(screen.getByRole('button', { name: 'Review' }))
  expect(await screen.findByText('The review screen')).toBeInTheDocument()
})

test('after analysis with nothing found, Review is disabled', async () => {
  summary.analyzed_at = Math.floor(Date.now() / 1000) - 10
  renderPage()
  expect(await screen.findByText('No exact copies found.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Review' })).toHaveAttribute('aria-disabled', 'true')
})

test('the Junk card shows the suggested count and links to Calibration', async () => {
  junkStatus = { cold_start: false, suggested: 42, maybe: 3, precheck_allowed: false, needed_each: 100, junk_labels: 100, keep_labels: 100 }
  renderPage()
  const card = await screen.findByRole('region', { name: 'Junk' })
  expect(card).toHaveTextContent('42 files Sift suggests deleting')
  await userEvent.click(screen.getByRole('button', { name: 'Calibration' }))
  expect(await screen.findByText('The calibration screen')).toBeInTheDocument()
})
