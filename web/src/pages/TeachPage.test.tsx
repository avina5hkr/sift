import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import type { Cluster, ClustersResponse } from '../lib/types'
import SearchPage from './SearchPage'
import TeachPage from './TeachPage'

let pages: Record<string, ClustersResponse>
let calls: { method: string; path: string; body: unknown }[]
let noModel = false
let found: unknown[]
let importFails = false
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))
const card = (id: number, extra: Partial<Cluster> = {}): Cluster => ({
  id,
  size: 10,
  unique: 4,
  sample_ids: [id * 10 + 1],
  sample_text: null,
  label: null,
  marked: 0,
  suggestion: null,
  ...extra,
})
const page = (offset: number, clusters: Cluster[], counts = { to_mark: 50, marked: 3 }): ClustersResponse => ({
  images: 200,
  covered: 50,
  ...counts,
  offset,
  clusters,
})

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  noModel = false
  importFails = false
  found = [{ id: 7, path: '/sdcard/a/IMG-7.jpg', content_hash: 'h', score: 0.4, label: null }]
  pages = {
    'to_mark:0': page(0, [
      card(1, {
        size: 30,
        unique: 12,
        sample_ids: [11, 12, 13],
        sample_text: 'Good morning!',
        suggestion: { label: 'junk', reason: 'Looks like a greeting card', source: 'built-in' },
      }),
      card(2, { size: 20, unique: 5, label: 'junk', marked: 20, suggestion: { label: 'keep', reason: 'Looks like family', source: 'your teaching' } }),
      card(3, { suggestion: { label: null, reason: 'Nothing to go on yet', source: 'built-in' } }),
    ]),
    'to_mark:24': page(24, [card(25)]),
    'marked:0': page(0, [card(40, { label: 'keep', marked: 10 })]),
  }
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url.startsWith('/api/cleanup/clusters?')) {
        const q = new URL(url, 'http://x').searchParams
        return reply(pages[`${q.get('section')}:${q.get('offset')}`])
      }
      if (url === '/api/cleanup/clusters/1') return reply([{ id: 11, path: '/x/A.jpg', content_hash: 'h', score: 1, label: null }])
      if (url.startsWith('/api/cleanup/search')) {
        return noModel ? reply({ code: 'no_model', message: 'Download the model first.', detail: null }, 409) : reply(found)
      }
      if (url === '/api/labels') return reply({ labelled: 30 })
      if (url === '/api/labels/export') return reply({ format: 'sift-teaching', labels: [] })
      if (url === '/api/labels/import') {
        return importFails ? reply({ code: 'not_teaching', message: 'This is not a Sift teaching file.', detail: null }, 422) : reply({ read: 5, applied: 4, waiting: 1 })
      }
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
const listCalls = () => calls.filter((c) => c.path.startsWith('/api/cleanup/clusters?'))

test('shows the header, tabs with counts, only this page, the cards largest first and a page line', async () => {
  renderAt('/cleanup/teach')
  expect(await screen.findByText('53 groups, covering 25% of images')).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'To mark (50)' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('tab', { name: 'Marked (3)' })).toBeInTheDocument()
  const cards = screen.getAllByRole('region')
  expect(cards).toHaveLength(3)
  expect(cards[0]).toHaveTextContent('30 pictures, 12 different')
  expect(cards[0]).toHaveTextContent('Good morning!')
  expect(cards[0].querySelectorAll('img')).toHaveLength(3)
  expect(cards[0].querySelector('img')).toHaveAttribute('loading', 'lazy')
  expect(cards[1]).toHaveTextContent('Junk')
  expect(screen.getAllByText('Showing 1–3 of 50')).toHaveLength(2)
  expect(listCalls()).toHaveLength(1)
  expect(listCalls()[0].path).toBe('/api/cleanup/clusters?section=to_mark&offset=0&limit=24')
  expect(calls.some((c) => c.path.startsWith('/api/cleanup/clusters/'))).toBe(false)
})

test('Next page and Previous page fetch that page; the Marked tab fetches its own list', async () => {
  renderAt('/cleanup/teach')
  await screen.findAllByRole('region')
  await userEvent.click(screen.getAllByRole('button', { name: 'Next page' })[0])
  await waitFor(() => expect(screen.getAllByRole('region')).toHaveLength(1))
  expect(listCalls()[1].path).toBe('/api/cleanup/clusters?section=to_mark&offset=24&limit=24')
  expect(screen.getAllByText('Showing 25–25 of 50').length).toBeGreaterThan(0)
  await userEvent.click(screen.getAllByRole('button', { name: 'Previous page' })[0])
  await waitFor(() => expect(screen.getAllByRole('region')).toHaveLength(3))
  await userEvent.click(screen.getByRole('tab', { name: 'Marked (3)' }))
  await waitFor(() => expect(screen.getByRole('tab', { name: 'Marked (3)' })).toHaveAttribute('aria-selected', 'true'))
  await waitFor(() => expect(screen.getAllByRole('region')[0]).toHaveTextContent('Keep'))
  expect(listCalls().at(-1)?.path).toBe('/api/cleanup/clusters?section=marked&offset=0&limit=24')
})

test('Junk and Keep post the label with the chosen kind and show it at once, without reloading the list', async () => {
  renderAt('/cleanup/teach')
  const first = (await screen.findAllByRole('region'))[0]
  await userEvent.selectOptions(first.querySelector('select') as HTMLSelectElement, 'greeting')
  await userEvent.click(first.querySelector('button[aria-pressed]') as HTMLButtonElement)
  await waitFor(() => expect(labelCalls()).toHaveLength(1))
  expect(labelCalls()[0]).toMatchObject({ method: 'POST', body: { cluster_id: 1, label: 'junk', tag: 'greeting' } })
  expect(screen.getAllByRole('region')[0]).toHaveTextContent('Junk')
  expect(screen.getAllByRole('region')).toHaveLength(3)
  expect(listCalls()).toHaveLength(1)
})

test('J labels the focused card at once, moves focus on, and never refetches the list', async () => {
  renderAt('/cleanup/teach')
  const cards = await screen.findAllByRole('region')
  cards[0].focus()
  await userEvent.keyboard('j')
  expect(cards[0].querySelector('span.rounded')).toHaveTextContent('Junk')
  expect(cards[1]).toHaveFocus()
  await userEvent.keyboard('k')
  await waitFor(() => expect(labelCalls()).toHaveLength(2))
  expect(labelCalls()[0].body).toEqual({ cluster_id: 1, label: 'junk' })
  expect(labelCalls()[1].body).toEqual({ cluster_id: 2, label: 'keep' })
  expect(cards[2]).toHaveFocus()
  expect(screen.getAllByRole('region')).toHaveLength(3)
  expect(listCalls()).toHaveLength(1)
})

test('arrow keys move between cards', async () => {
  renderAt('/cleanup/teach')
  const cards = await screen.findAllByRole('region')
  cards[0].focus()
  await userEvent.keyboard('{ArrowRight}')
  expect(cards[1]).toHaveFocus()
})

test('a suggestion shows as a chip with its source, and Accept posts that label', async () => {
  renderAt('/cleanup/teach')
  const cards = await screen.findAllByRole('region')
  expect(cards[0]).toHaveTextContent('Sift suggests: Junk — Looks like a greeting card')
  expect(cards[0]).toHaveTextContent('built in')
  expect(cards[1]).toHaveTextContent('Sift suggests: Keep — Looks like family')
  expect(cards[1]).toHaveTextContent('from your teaching')
  expect(cards[2]).toHaveTextContent('Sift suggests: Not sure')
  expect(within(cards[2]).queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()
  await userEvent.click(within(cards[1]).getByRole('button', { name: 'Accept' }))
  await waitFor(() => expect(labelCalls()).toHaveLength(1))
  expect(labelCalls()[0].body).toEqual({ cluster_id: 2, label: 'keep' })
  expect(cards[1].querySelector('span.rounded')).toHaveTextContent('Keep')
})

test('Accept all asks first, then labels every card with a junk or keep suggestion', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
  renderAt('/cleanup/teach')
  await screen.findAllByRole('region')
  await userEvent.click(screen.getByRole('button', { name: 'Accept all suggestions on this page' }))
  expect(labelCalls()).toHaveLength(0)
  await userEvent.click(screen.getByRole('button', { name: 'Accept all suggestions on this page' }))
  await waitFor(() => expect(labelCalls()).toHaveLength(2))
  expect(labelCalls().map((c) => c.body)).toEqual([
    { cluster_id: 1, label: 'junk' },
    { cluster_id: 2, label: 'keep' },
  ])
  expect(confirm).toHaveBeenCalledTimes(2)
  confirm.mockRestore()
})

test('Refresh reloads the page', async () => {
  renderAt('/cleanup/teach')
  await screen.findAllByRole('region')
  await userEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(listCalls()).toHaveLength(2))
})

test('Show all lists the members, and only for the opened card', async () => {
  renderAt('/cleanup/teach')
  await userEvent.click((await screen.findAllByRole('button', { name: /Show all/ }))[0])
  expect(await screen.findByRole('button', { name: 'Open A.jpg' })).toBeInTheDocument()
  expect(calls.filter((c) => c.path.startsWith('/api/cleanup/clusters/'))).toHaveLength(1)
})

test('Save my teaching to a file downloads the export', async () => {
  const create = vi.fn(() => 'blob:x')
  Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  renderAt('/cleanup/teach')
  await userEvent.click(await screen.findByRole('button', { name: 'Save my teaching to a file' }))
  await waitFor(() => expect(click).toHaveBeenCalled())
  expect(calls.some((c) => c.path === '/api/labels/export' && c.method === 'GET')).toBe(true)
  expect(create).toHaveBeenCalled()
  click.mockRestore()
})

test('Load teaching from a file posts the parsed contents and says what happened', async () => {
  renderAt('/cleanup/teach')
  const input = (await screen.findByLabelText('Teaching file')) as HTMLInputElement
  const body = { format: 'sift-teaching', labels: [{ h: 'a' }] }
  await userEvent.upload(input, new File([JSON.stringify(body)], 'sift-teaching.json', { type: 'application/json' }))
  expect(await screen.findByText('Loaded 4 labels; 1 waits for their pictures to be backed up.')).toBeInTheDocument()
  const imports = calls.filter((c) => c.path === '/api/labels/import')
  expect(imports).toHaveLength(1)
  expect(imports[0]).toMatchObject({ method: 'POST', body })
})

test('Load shows the message when the file is not a teaching file', async () => {
  importFails = true
  renderAt('/cleanup/teach')
  const input = (await screen.findByLabelText('Teaching file')) as HTMLInputElement
  await userEvent.upload(input, new File(['{}'], 'x.json', { type: 'application/json' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('This is not a Sift teaching file.')
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
