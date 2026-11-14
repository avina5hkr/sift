import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { initToken } from '../lib/token'
import LogsPage from './LogsPage'

let calls: { method: string; path: string; body: unknown }[]
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url.startsWith('/api/logs')) {
        return Promise.resolve(json({ entries: [
          { ts: '2026-11-14T10:00:00', level: 'WARNING', area: 'sift.backup', message: 'Copy was slow' },
          { ts: '2026-11-14T09:59:00', level: 'INFO', area: 'sift.device', message: 'Phone connected' },
        ] }))
      }
      if (url === '/api/diagnostics') return Promise.resolve(json({ path: '/Users/a/Downloads/sift-diagnostics-1.zip', name: 'sift-diagnostics-1.zip' }))
      return Promise.resolve(json({ code: 'nope', message: 'No.', detail: null }, 404))
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LogsPage />
    </QueryClientProvider>,
  )
}

test('shows the log lines with level, area and message', async () => {
  renderPage()
  expect(await screen.findByText('Copy was slow')).toBeInTheDocument()
  expect(screen.getByText('WARNING')).toBeInTheDocument()
  expect(screen.getByText('sift.device')).toBeInTheDocument()
  expect(calls[0].path).toContain('level=INFO')
})

test('level, area and search go to the server', async () => {
  renderPage()
  await screen.findByText('Copy was slow')
  await userEvent.selectOptions(screen.getByLabelText('Level'), 'ERROR')
  await userEvent.type(screen.getByLabelText('Area'), 'backup')
  await userEvent.type(screen.getByLabelText('Search'), 'slow')
  await waitFor(() => {
    const last = calls.filter((c) => c.path.startsWith('/api/logs')).at(-1)!.path
    const p = new URLSearchParams(last.split('?')[1])
    expect(p.get('level')).toBe('ERROR')
    expect(p.get('area')).toBe('backup')
    expect(p.get('q')).toBe('slow')
  })
})

test('a live log event is added at the top', async () => {
  renderPage()
  await screen.findByText('Copy was slow')
  act(() => useEventStore.setState({ last: { name: 'log', data: { level: 'ERROR', area: 'sift.jobs', message: 'Something broke' } } }))
  expect(await screen.findByText('Something broke')).toBeInTheDocument()
})

test('Include file paths is off by default and exporting shows the file name', async () => {
  renderPage()
  const box = screen.getByRole('checkbox', { name: 'Include file paths' })
  expect(box).not.toBeChecked()
  await userEvent.click(screen.getByRole('button', { name: 'Export diagnostics' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/diagnostics')?.body).toEqual({ include_paths: false }))
  expect(await screen.findByText(/sift-diagnostics-1\.zip/)).toBeInTheDocument()
  await userEvent.click(box)
  await userEvent.click(screen.getByRole('button', { name: 'Export diagnostics' }))
  await waitFor(() => expect(calls.filter((c) => c.path === '/api/diagnostics').at(-1)?.body).toEqual({ include_paths: true }))
})
