import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../../lib/token'
import type { JunkStatus, ReviewGroup } from '../../lib/types'
import ReviewPage from '../../pages/ReviewPage'

const STATUS: JunkStatus = {
  junk_labels: 120,
  keep_labels: 150,
  protected_labels: 0,
  needed_each: 100,
  cold_start: false,
  model_version: 1,
  t_suggest: 0.9,
  precheck_allowed: true,
  never_precheck: false,
  suggested: 2,
  maybe: 1,
  last_calibration: null,
  calibration_open: 0,
}

function junkGroup(id: number, checked: boolean, reasons: { code: string; text: string }[]): ReviewGroup {
  return {
    group_id: id,
    content_hash: `hash${id}`,
    keeper: null,
    reclaimable: 500_000,
    score: 0.95,
    reasons,
    copies: [
      {
        id,
        path: `/sdcard/WhatsApp/Media/IMG-${id}.jpg`,
        size: 500_000,
        mtime: 1_700_000_000,
        origin: 'whatsapp_received',
        kind: 'image',
        width: 800,
        height: 600,
        checked,
        role: 'item',
      },
    ],
  }
}

let status: JunkStatus
let tabs: Record<string, ReviewGroup[]>
let calls: { method: string; path: string; body: unknown }[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  status = { ...STATUS }
  tabs = {
    junk: [
      junkGroup(1, true, [
        { code: 'forwarded', text: 'Forwarded many times' },
        { code: 'text', text: 'Has a greeting' },
      ]),
      junkGroup(2, true, [{ code: 'lookalike', text: 'Looks like pictures you marked Junk' }]),
    ],
    maybe: [junkGroup(3, false, [{ code: 'lookalike', text: 'Looks a bit like junk' }])],
  }
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      const body = init.body ? JSON.parse(init.body as string) : undefined
      calls.push({ method, path: url, body })
      if (url === '/api/cleanup/junk/status') return reply(status)
      if (url.startsWith('/api/cleanup/candidates')) {
        const q = new URL(url, 'http://x').searchParams
        const groups = tabs[q.get('tab') ?? ''] ?? []
        return reply({ tab: q.get('tab'), groups, total_groups: groups.length, next_cursor: null })
      }
      if (url === '/api/labels') {
        const hash = (body as { hashes: string[] }).hashes[0]
        for (const t of Object.keys(tabs)) tabs[t] = (tabs[t] as ReviewGroup[]).filter((g) => g.content_hash !== hash)
        return reply({ labelled: 1 })
      }
      if (url === '/api/selection') return reply({ changed: 1 })
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderJunk() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/cleanup/review?tab=junk']}>
        <Routes>
          <Route path="/cleanup/review" element={<ReviewPage />} />
          <Route path="/cleanup/teach" element={<p>The teach screen</p>} />
          <Route path="/cleanup/calibration" element={<p>The calibration screen</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('the Junk tab shows each file with its reasons, ticked, and asks for score order', async () => {
  renderJunk()
  const items = await screen.findAllByTestId('junk-item')
  expect(items).toHaveLength(2)
  expect(within(items[0] as HTMLElement).getByText('Forwarded many times')).toBeInTheDocument()
  expect(within(items[0] as HTMLElement).getByText('Has a greeting')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Select IMG-1.jpg' })).toBeChecked()
  expect(screen.getByRole('tab', { name: 'Junk' })).toHaveAttribute('aria-selected', 'true')
  const asked = calls.find((c) => c.path.startsWith('/api/cleanup/candidates'))
  expect(asked?.path).toContain('tab=junk')
  expect(asked?.path).toContain('sort=score')
})

test('Not junk posts feedback and the file leaves the tab', async () => {
  renderJunk()
  const first = (await screen.findAllByTestId('junk-item'))[0] as HTMLElement
  await userEvent.click(within(first).getByRole('button', { name: 'Not junk' }))
  await waitFor(() => expect(screen.getAllByTestId('junk-item')).toHaveLength(1))
  const post = calls.find((c) => c.path === '/api/labels')
  expect(post?.body).toEqual({ hashes: ['hash1'], label: 'keep', source: 'feedback' })
  expect(screen.queryByText('Forwarded many times')).not.toBeInTheDocument()
})

test('lower confidence items appear on request and are not ticked', async () => {
  renderJunk()
  await screen.findAllByTestId('junk-item')
  expect(screen.queryByText('Looks a bit like junk')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('checkbox', { name: /Show lower confidence/ }))
  expect(await screen.findByText('Looks a bit like junk')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Select IMG-3.jpg' })).not.toBeChecked()
})

test('before enough labels, the tab says to teach the app first', async () => {
  status = { ...STATUS, cold_start: true, junk_labels: 12, keep_labels: 40 }
  renderJunk()
  expect(await screen.findByText('Teach the app first')).toBeInTheDocument()
  expect(screen.getByText(/12 of 100 Junk, 40 of 100 Keep/)).toBeInTheDocument()
  expect(screen.queryByTestId('junk-item')).not.toBeInTheDocument()
  expect(calls.some((c) => c.path.startsWith('/api/cleanup/candidates'))).toBe(false)
  await userEvent.click(screen.getByRole('link', { name: 'Teach the app' }))
  expect(await screen.findByText('The teach screen')).toBeInTheDocument()
})

test('when pre-checking is not allowed, a line links to Calibration', async () => {
  status = { ...STATUS, precheck_allowed: false }
  renderJunk()
  await screen.findAllByTestId('junk-item')
  expect(screen.getByText(/Suggestions are not ticked until you check a sample/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('link', { name: 'Calibration' }))
  expect(await screen.findByText('The calibration screen')).toBeInTheDocument()
})
