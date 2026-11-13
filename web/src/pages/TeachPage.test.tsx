import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import type { ClustersResponse } from '../lib/types'
import SearchPage from './SearchPage'
import TeachPage from './TeachPage'

let clusters: ClustersResponse
let calls: { method: string; path: string; body: unknown }[]
let noModel = false
let found: unknown[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  noModel = false
  found = [{ id: 7, path: '/sdcard/a/IMG-7.jpg', content_hash: 'h', score: 0.4, label: null }]
  clusters = {
    images: 200,
    covered: 50,
    clusters: [
      { id: 1, size: 30, unique: 12, sample_ids: [11, 12, 13], sample_text: 'Good morning!', label: null },
      { id: 2, size: 20, unique: 5, sample_ids: [21], sample_text: null, label: 'junk' },
    ],
  }
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/cleanup/clusters') return reply(clusters)
      if (url === '/api/cleanup/clusters/1') return reply([{ id: 11, path: '/x/A.jpg', content_hash: 'h', score: 1, label: null }])
      if (url.startsWith('/api/cleanup/search')) {
        return noModel ? reply({ code: 'no_model', message: 'Download the model first.', detail: null }, 409) : reply(found)
      }
      if (url === '/api/labels') return reply({ labelled: 30 })
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/cleanup/teach" element={<TeachPage />} />
          <Route path="/cleanup/search" element={<SearchPage />} />
          <Route path="/cleanup" element={<p>Cleanup home</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const labelCalls = () => calls.filter((c) => c.path === '/api/labels')

test('shows the header, the cards largest first, counts and sample text', async () => {
  renderAt('/cleanup/teach')
  expect(await screen.findByText('2 groups, covering 25% of images')).toBeInTheDocument()
  const cards = screen.getAllByRole('region')
  expect(cards).toHaveLength(2)
  expect(cards[0]).toHaveTextContent('30 pictures, 12 different')
  expect(cards[0]).toHaveTextContent('Good morning!')
  expect(cards[0].querySelectorAll('img')).toHaveLength(3)
  expect(cards[1]).toHaveTextContent('Junk')
})

test('Junk and Keep post the label with the chosen kind', async () => {
  renderAt('/cleanup/teach')
  const card = (await screen.findAllByRole('region'))[0]
  await userEvent.selectOptions(card.querySelector('select') as HTMLSelectElement, 'greeting')
  await userEvent.click(card.querySelector('button[aria-pressed]') as HTMLButtonElement)
  await waitFor(() => expect(labelCalls()).toHaveLength(1))
  expect(labelCalls()[0]).toMatchObject({ method: 'POST', body: { cluster_id: 1, label: 'junk', tag: 'greeting' } })
  await waitFor(() => expect(screen.getAllByRole('region')[0]).toHaveTextContent('Junk'))
})

test('J and K label the focused card and arrows move between cards', async () => {
  renderAt('/cleanup/teach')
  const cards = await screen.findAllByRole('region')
  cards[0].focus()
  await userEvent.keyboard('j')
  await waitFor(() => expect(labelCalls()).toHaveLength(1))
  expect(labelCalls()[0].body).toEqual({ cluster_id: 1, label: 'junk' })
  await userEvent.keyboard('{ArrowRight}')
  expect(cards[1]).toHaveFocus()
  await userEvent.keyboard('k')
  await waitFor(() => expect(labelCalls()).toHaveLength(2))
  expect(labelCalls()[1].body).toEqual({ cluster_id: 2, label: 'keep' })
})

test('Show all lists the members', async () => {
  renderAt('/cleanup/teach')
  await userEvent.click((await screen.findAllByRole('button', { name: /Show all/ }))[0])
  expect(await screen.findByRole('button', { name: 'Open A.jpg' })).toBeInTheDocument()
})

test('search shows result pictures', async () => {
  renderAt('/cleanup/search?q=morning')
  expect(await screen.findByRole('button', { name: 'Open IMG-7.jpg' })).toBeInTheDocument()
  expect(calls.some((c) => c.path === '/api/cleanup/search?q=morning')).toBe(true)
  expect(screen.queryByText(/0\.4/)).not.toBeInTheDocument()
})

test('search with no matches says what to try', async () => {
  found = []
  renderAt('/cleanup/search?q=zebra')
  expect(await screen.findByText(/Nothing matched “zebra”/)).toBeInTheDocument()
})

test('search before the model is downloaded points to Cleanup', async () => {
  noModel = true
  renderAt('/cleanup/search?q=morning')
  expect(await screen.findByText(/needs the picture model/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Cleanup' })).toHaveAttribute('href', '/cleanup')
})
