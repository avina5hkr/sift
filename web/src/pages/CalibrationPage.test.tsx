import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import type { CalibrationState } from '../lib/types'
import CalibrationPage from './CalibrationPage'

const item = (id: number, verdict: boolean | null = null) => ({ content_hash: `h${id}`, id, path: `/sdcard/IMG-${id}.jpg`, score: 0.95, verdict })

let state: CalibrationState
let startFails: boolean
let finish: Record<string, unknown>
let calls: { method: string; path: string; body: unknown }[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  state = { items: [], marked: 0, total: 0, last: { created_at: 1_700_000_000, sample_size: 300, errors: 1, threshold: 0.93, model_version: 1, passed: false } }
  startFails = false
  finish = { finished: true, passed: true, errors: 0, sample_size: 2, threshold: 0.9 }
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      const body = init.body ? JSON.parse(init.body as string) : null
      calls.push({ method, path: url, body })
      if (url === '/api/cleanup/calibration') return reply(state)
      if (url === '/api/cleanup/calibration/start') {
        if (startFails) return reply({ code: 'nothing_to_calibrate', message: 'Nothing is suggested yet.', detail: null }, 409)
        state = { ...state, items: [item(1), item(2)], total: 2, marked: 0 }
        return reply(state)
      }
      if (url === '/api/cleanup/calibration/mark') {
        const first = state.items.find((i) => i.verdict === null)
        if (first?.content_hash === (body as { content_hash: string }).content_hash && state.marked === 0) {
          state = { ...state, marked: 1, items: state.items.map((i) => (i === first ? { ...i, verdict: true } : i)) }
          return reply({ finished: false })
        }
        state = { ...state, items: [], total: 0, marked: 0 }
        return reply(finish)
      }
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CalibrationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
const marks = () => calls.filter((c) => c.path === '/api/cleanup/calibration/mark')

test('shows the last calibration and a Start button', async () => {
  renderPage()
  expect(await screen.findByText(/Last check: .*300 pictures, 1 wrong, not passed/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Start' })).toBeInTheDocument()
})

test('Start with nothing suggested shows the server message', async () => {
  startFails = true
  renderPage()
  await userEvent.click(await screen.findByRole('button', { name: 'Start' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Nothing is suggested yet.')
})

test('J marks correct, K marks wrong, and the result is shown at the end', async () => {
  renderPage()
  await userEvent.click(await screen.findByRole('button', { name: 'Start' }))
  expect(await screen.findByText('1 of 2')).toBeInTheDocument()
  expect(screen.getByRole('img')).toHaveAttribute('alt', 'IMG-1.jpg')
  await userEvent.keyboard('j')
  expect(await screen.findByText('2 of 2')).toBeInTheDocument()
  expect(marks()[0]?.body).toEqual({ content_hash: 'h1', correct: true })
  finish = { finished: true, passed: false, errors: 1, sample_size: 2, threshold: 0.96 }
  await userEvent.keyboard('k')
  await waitFor(() => expect(marks()[1]?.body).toEqual({ content_hash: 'h2', correct: false }))
  expect(await screen.findByText('1 wrong: the threshold rose to 0.96; check a fresh sample')).toBeInTheDocument()
})

test('a clean sample says suggestions will now be ticked', async () => {
  renderPage()
  await userEvent.click(await screen.findByRole('button', { name: 'Start' }))
  await screen.findByText('1 of 2')
  await userEvent.keyboard('j')
  await screen.findByText('2 of 2')
  await userEvent.keyboard('j')
  expect(await screen.findByText('0 wrong in 2: suggestions will now be ticked')).toBeInTheDocument()
})
