import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import type { CandidatesPage, ReviewGroup, ReviewItem } from '../lib/types'
import ReviewPage from './ReviewPage'

let nextId = 1
function item(over: Partial<ReviewItem> = {}): ReviewItem {
  const id = nextId++
  return {
    id,
    path: `/sdcard/DCIM/Camera/IMG_${id}.jpg`,
    size: 1_000_000,
    mtime: 1_700_000_000,
    origin: 'camera',
    kind: 'image',
    width: 4000,
    height: 3000,
    checked: true,
    role: 'copy',
    ...over,
  }
}
function group(copies: number, over: Partial<ReviewItem> = {}): ReviewGroup {
  const keeper = item({ role: 'keeper', checked: false })
  const list = Array.from({ length: copies }, () => item(over))
  return {
    group_id: keeper.id,
    content_hash: 'abcdef0123456789'.repeat(4),
    keeper,
    copies: list,
    reclaimable: list.reduce((s, c) => s + c.size, 0),
  }
}

let groups: ReviewGroup[]
let pageSize: number
let calls: { method: string; path: string; body: unknown }[]
let failSelection = false
/** What the server holds as ticked, by id; changes with every accepted POST. */
let server: Map<number, boolean>

const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  nextId = 1
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  groups = []
  pageSize = 200
  calls = []
  failSelection = false
  server = new Map()
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      const body = init.body ? JSON.parse(init.body as string) : undefined
      calls.push({ method, path: url, body })
      if (url.startsWith('/api/cleanup/candidates')) {
        const cursor = Number(new URL(url, 'http://x').searchParams.get('cursor'))
        const page = groups.slice(cursor, cursor + pageSize)
        const next = cursor + pageSize < groups.length ? cursor + pageSize : null
        const out: CandidatesPage = {
          tab: 'exact',
          groups: page.map((g) => ({ ...g, copies: g.copies.map((c) => ({ ...c, checked: server.get(c.id) ?? c.checked })) })),
          total_groups: groups.length,
          next_cursor: next,
        }
        return reply(out)
      }
      if (url === '/api/selection') {
        if (failSelection) return reply({ code: 'boom', message: 'Sift could not save that.', detail: null }, 500)
        const b = body as { ids?: number[]; checked: boolean; all_in_tab?: string }
        const ids = b.all_in_tab ? groups.flatMap((g) => g.copies.map((c) => c.id)) : (b.ids ?? [])
        for (const id of ids) server.set(id, b.checked)
        return reply({ changed: ids.length })
      }
      if (url === '/api/delete/preflight') {
        return reply({ run_id: 5, task: { kind: 'preflight', state: 'running', done: 0, total: 2, message: '', result: null } })
      }
      if (url === '/api/tasks/current') {
        return reply({ kind: 'preflight', state: 'running', done: 0, total: 2, message: '', result: null })
      }
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

async function open() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ReviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
}
const selections = () => calls.filter((c) => c.path === '/api/selection')
const box = (id: number) => screen.getByRole('checkbox', { name: `Select IMG_${id}.jpg` })
const footer = () => screen.getByRole('status')

test('a group shows the keeper labelled KEEP with no checkbox, and the copies with checkboxes', async () => {
  groups = [group(3)]
  await open()
  const keeper = screen.getByTestId('keeper')
  expect(within(keeper).getByText('KEEP')).toBeInTheDocument()
  expect(within(keeper).queryByRole('checkbox')).not.toBeInTheDocument()
  const copies = screen.getAllByTestId('copy')
  expect(copies).toHaveLength(3)
  for (const c of copies) expect(within(c).getByRole('checkbox')).toBeChecked()
  const imgs = copies.map((c) => c.querySelector('img')?.getAttribute('src'))
  expect(imgs[0]).toBe(`/api/media/${groups[0]?.copies[0]?.id}/thumb?token=abc`)
  expect(copies[0]?.querySelector('img')).toHaveAttribute('loading', 'lazy')
  expect(screen.getByText(/3 copies/)).toHaveTextContent('3.0 MB to free')
  expect(screen.getAllByText('1.0 MB').length).toBeGreaterThan(0)
})

test('a video is a tile with a VIDEO badge and no thumbnail fetch', async () => {
  groups = [group(1, { kind: 'video' })]
  await open()
  const tile = screen.getByTestId('copy')
  expect(within(tile).getByText('VIDEO')).toBeInTheDocument()
  expect(tile.querySelector('img')).toBeNull()
})

test('the footer counts what is selected and the Delete button names the count and size', async () => {
  groups = [group(2), group(1)]
  await open()
  expect(footer()).toHaveTextContent('3 selected, 3.0 MB')
  await userEvent.click(box(groups[0]?.copies[0]?.id as number))
  expect(footer()).toHaveTextContent('2 selected, 2.0 MB')
  expect(screen.getByRole('button', { name: 'Delete 2 selected (2.0 MB)' })).not.toHaveAttribute('aria-disabled')
  await userEvent.click(screen.getByRole('button', { name: 'Select none' }))
  const del = screen.getByRole('button', { name: 'Delete selected' })
  expect(del).toHaveAttribute('aria-disabled', 'true')
  expect(del).toHaveAccessibleDescription('Tick the files you want to delete first.')
})

test('Delete selected starts the checks with the ticked ids', async () => {
  groups = [group(2)]
  const ids = groups[0]?.copies.map((c) => c.id)
  await open()
  await userEvent.click(screen.getByRole('button', { name: 'Delete 2 selected (2.0 MB)' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/delete/preflight')?.body).toEqual({ source: 'cleanup', ids }))
  expect(await screen.findByText('Checking 2 files…')).toBeInTheDocument()
})

test('toggling a checkbox saves it', async () => {
  groups = [group(2)]
  await open()
  const id = groups[0]?.copies[0]?.id as number
  await userEvent.click(box(id))
  expect(box(id)).not.toBeChecked()
  await waitFor(() => expect(selections()).toHaveLength(1))
  expect(selections()[0]?.body).toEqual({ ids: [id], checked: false })
})

test('Shift-click selects a range of copies', async () => {
  groups = [group(5)]
  await open()
  const ids = groups[0]?.copies.map((c) => c.id) as number[]
  // untick all, then tick the first and Shift-click the fourth
  await userEvent.click(screen.getByRole('button', { name: 'Select none' }))
  await waitFor(() => expect(footer()).toHaveTextContent('0 selected'))
  await userEvent.click(box(ids[0] as number))
  const user = userEvent.setup()
  await user.keyboard('{Shift>}')
  await user.click(box(ids[3] as number))
  await user.keyboard('{/Shift}')
  for (const id of ids.slice(0, 4)) expect(box(id)).toBeChecked()
  expect(box(ids[4] as number)).not.toBeChecked()
  const last = selections().at(-1)
  // the first one was ticked already, so only the other three change
  expect(last?.body).toEqual({ ids: ids.slice(1, 4), checked: true })
})

test('a change that cannot be saved is rolled back with a message', async () => {
  groups = [group(2)]
  await open()
  failSelection = true
  const id = groups[0]?.copies[0]?.id as number
  await userEvent.click(box(id))
  expect(await screen.findByRole('alert')).toHaveTextContent('Sift could not save that.')
  expect(box(id)).toBeChecked()
  expect(footer()).toHaveTextContent('2 selected')
})

test('Select all copies in a group, Select none and Select all in tab', async () => {
  groups = [group(2), group(2)]
  await open()
  await userEvent.click(screen.getByRole('button', { name: 'Select none' }))
  await waitFor(() => expect(footer()).toHaveTextContent('0 selected'))
  expect(selections().at(-1)?.body).toEqual({ all_in_tab: 'exact', checked: false })
  await userEvent.click(screen.getAllByRole('button', { name: 'Select all copies' })[0] as HTMLElement)
  expect(footer()).toHaveTextContent('2 selected')
  await userEvent.click(screen.getByRole('button', { name: 'Select all in tab' }))
  await waitFor(() => expect(footer()).toHaveTextContent('4 selected'))
  expect(selections().at(-1)?.body).toEqual({ all_in_tab: 'exact', checked: true })
})

test('tabs: Exact duplicates on, Near-duplicates, Similar shots and Junk usable', async () => {
  groups = [group(1)]
  await open()
  expect(screen.getByRole('tab', { name: 'Exact duplicates' })).toHaveAttribute('aria-selected', 'true')
  for (const name of [/Near-duplicates/, /Similar shots/, /Junk/]) {
    expect(screen.getByRole('tab', { name })).not.toHaveAttribute('aria-disabled')
  }
})

test('the sort control asks the server for that order', async () => {
  groups = [group(1)]
  await open()
  await userEvent.selectOptions(screen.getByLabelText('Sort by'), 'date')
  await waitFor(() => expect(calls.some((c) => c.path.includes('sort=date&cursor=0'))).toBe(true))
})

test('more pages load with next_cursor as the user scrolls, and only the rows in view are drawn', async () => {
  groups = Array.from({ length: 60 }, () => group(2))
  pageSize = 25
  await open()
  expect(calls.filter((c) => c.path.startsWith('/api/cleanup/candidates'))).toHaveLength(1)
  expect(screen.getAllByTestId('group').length).toBeLessThan(20)
  expect(footer()).toHaveTextContent('25 of 60 groups loaded')
  const scroller = screen.getByTestId('review-scroller')
  scroller.scrollTop = 7000
  fireEvent.scroll(scroller)
  await waitFor(() => expect(calls.some((c) => c.path.includes('cursor=25'))).toBe(true))
  await waitFor(() => expect(footer()).toHaveTextContent('50 of 60 groups loaded'))
  expect(screen.getAllByTestId('group').length).toBeLessThan(20)
})

// -- the lightbox ---------------------------------------------------------------------------

test('the lightbox shows the preview and the details, and arrows move through the list', async () => {
  groups = [group(2, { width: 800, height: 600 })]
  await open()
  const [a, b] = (groups[0] as ReviewGroup).copies
  await userEvent.click(screen.getByRole('button', { name: `Open IMG_${a?.id}.jpg` }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByRole('img')).toHaveAttribute('src', `/api/media/${a?.id}/preview?max=1600&token=abc`)
  expect(dialog).toHaveTextContent(`/sdcard/DCIM/Camera/IMG_${a?.id}.jpg`)
  expect(dialog).toHaveTextContent('800 × 600')
  expect(dialog).toHaveTextContent('Camera')
  expect(dialog).toHaveTextContent('abcdef012345')
  expect(dialog).not.toHaveTextContent('abcdef0123456')
  expect(dialog).toHaveTextContent('2 of 3')
  await userEvent.keyboard('{ArrowRight}')
  expect(within(dialog).getByRole('img')).toHaveAttribute('src', `/api/media/${b?.id}/preview?max=1600&token=abc`)
  await userEvent.keyboard('{ArrowLeft}{ArrowLeft}')
  expect(dialog).toHaveTextContent('1 of 3')
  expect(dialog).toHaveTextContent('This is the copy Sift keeps')
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('Keep this unticks the copy', async () => {
  groups = [group(2)]
  await open()
  const a = groups[0]?.copies[0] as ReviewItem
  await userEvent.click(screen.getByRole('button', { name: `Open IMG_${a.id}.jpg` }))
  await userEvent.click(screen.getByRole('button', { name: 'Keep this' }))
  expect(box(a.id)).not.toBeChecked()
  await waitFor(() => expect(selections().at(-1)?.body).toEqual({ ids: [a.id], checked: false }))
})

test('a video plays inline in the lightbox from the file', async () => {
  groups = [group(1, { kind: 'video' })]
  await open()
  const v = groups[0]?.copies[0] as ReviewItem
  await userEvent.click(screen.getByRole('button', { name: `Open IMG_${v.id}.jpg` }))
  const video = screen.getByRole('dialog').querySelector('video')
  expect(video).toHaveAttribute('src', `/api/media/${v.id}/file?token=abc`)
})

// -- the keyboard ---------------------------------------------------------------------------

test('arrows move the focus, Space and X toggle, Return opens, Esc closes', async () => {
  groups = [group(3)]
  await open()
  const [a, b] = (groups[0] as ReviewGroup).copies
  const list = screen.getByTestId('review-list')
  const keeper = groups[0]?.keeper as ReviewItem
  expect(list).toHaveAttribute('aria-activedescendant', `tile-${keeper.id}`)
  await userEvent.keyboard('{ArrowRight}')
  expect(list).toHaveAttribute('aria-activedescendant', `tile-${a?.id}`)
  await userEvent.keyboard(' ')
  expect(box(a?.id as number)).not.toBeChecked()
  await userEvent.keyboard('{ArrowRight}x')
  expect(box(b?.id as number)).not.toBeChecked()
  await userEvent.keyboard('{Enter}')
  expect(screen.getByRole('dialog')).toHaveTextContent(`IMG_${b?.id}.jpg`)
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  // the keeper cannot be ticked
  await userEvent.keyboard('{ArrowLeft}{ArrowLeft}{ArrowLeft} ')
  expect(selections().every((c) => !(c.body as { ids?: number[] }).ids?.includes(keeper.id))).toBe(true)
})

test('Cmd+A selects everything in the tab and Cmd+D clears it', async () => {
  groups = [group(2), group(2)]
  await open()
  await userEvent.keyboard('{Meta>}d{/Meta}')
  await waitFor(() => expect(footer()).toHaveTextContent('0 selected'))
  await userEvent.keyboard('{Meta>}a{/Meta}')
  await waitFor(() => expect(footer()).toHaveTextContent('4 selected'))
  await act(async () => undefined)
})

test('with nothing found it says so', async () => {
  groups = []
  await open()
  expect(screen.getByText('No exact duplicates found.')).toBeInTheDocument()
})
