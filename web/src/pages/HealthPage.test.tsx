import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import HealthPage from './HealthPage'

const REPORT = {
  id: 1,
  created_at: 1_760_000_000,
  unavailable: ['battery'],
  areas: [
    { key: 'system', title: 'System', level: 'ok', verdicts: ['Android 15.'], facts: {} },
    { key: 'storage', title: 'Storage', level: 'warn', verdicts: ['Storage is 91% full.'], facts: {} },
    { key: 'memory', title: 'Memory and CPU', level: 'bad', verdicts: ['Memory is very low.'], facts: {} },
    { key: 'battery', title: 'Battery', level: 'unavailable', verdicts: ['Unavailable on this phone.'], facts: {} },
  ],
}

let calls: { method: string; path: string }[]
let report: unknown
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))
const task = (kind: string, over: object = {}) => ({ kind, state: 'done', done: 1, total: 1, message: null, result: {}, ...over })

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  report = REPORT
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url })
      if (url === '/api/health/report') return reply({ report })
      if (url === '/api/health/run') {
        report = REPORT
        return reply(task('health', { state: 'running' }))
      }
      if (url === '/api/health/bugreport') return reply(task('bugreport', { state: 'running' }))
      if (url === '/api/tasks/current') {
        const last = calls.filter((c) => c.method === 'POST').at(-1)?.path
        return reply(last === '/api/health/bugreport' ? task('bugreport', { result: { paths: ['/Backups/reports/bug.zip'] } }) : task('health'))
      }
      if (url === '/api/health/export') return reply({ paths: ['/Backups/reports/health.md'], markdown: '# x' })
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
        <HealthPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('shows one card per area with its chip and verdicts', async () => {
  renderPage()
  const storage = await screen.findByRole('region', { name: 'Storage' })
  expect(within(storage).getByText('Check')).toBeInTheDocument()
  expect(within(storage).getByText('Storage is 91% full.')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: 'System' })).getByText('OK')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: 'Memory and CPU' })).getByText('Problem')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: 'Battery' })).getByText('Unavailable')).toBeInTheDocument()
  expect(screen.getByText(/does not diagnose hardware faults/)).toBeInTheDocument()
})

test('with no report yet, Run health report starts the task and reloads the report', async () => {
  const user = userEvent.setup()
  report = null
  renderPage()
  expect(await screen.findByText(/No health report yet/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Run health report' }))
  expect(await screen.findByRole('region', { name: 'Storage' })).toBeInTheDocument()
  expect(calls.filter((c) => c.method === 'POST' && c.path === '/api/health/run')).toHaveLength(1)
})

test('bug report and export show where the files went', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByRole('region', { name: 'Storage' })
  await user.click(screen.getByRole('button', { name: 'Capture bug report' }))
  expect(await screen.findByText('/Backups/reports/bug.zip')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Export as Markdown' }))
  expect(await screen.findByText('/Backups/reports/health.md')).toBeInTheDocument()
  await waitFor(() => expect(calls.some((c) => c.path === '/api/health/export' && c.method === 'POST')).toBe(true))
})
