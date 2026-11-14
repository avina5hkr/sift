import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { initToken } from '../lib/token'
import type { BrowseFile, BrowsePage as Page, DeleteRun, TaskEvent } from '../lib/types'
import BrowsePage from './BrowsePage'

const file = (id: number, name: string, status: BrowseFile['status'], over: Partial<BrowseFile> = {}): BrowseFile => ({
  id,
  name,
  path: `/sdcard/${name}`,
  size: 1_000_000 * id,
  mtime: 1_700_000_000,
  kind: 'image',
  origin: 'camera',
  status,
  ...over,
})
const ROOT: Page = {
  path: '/sdcard',
  crumbs: ['/sdcard'],
  folders: [{ name: 'DCIM', path: '/sdcard/DCIM', size: 9_000_000, files: 124, mtime: 1_700_000_000, status: { mac_only: 120, not_backed_up: 3, changed: 1 } }],
  files: [file(1, 'a.jpg', 'mac_ssd'), file(2, 'b.jpg', 'not_backed_up'), file(3, 'c.mp4', 'changed', { kind: 'video' }), file(4, 'd.jpg', 'ssd_only'), file(5, 'e.jpg', 'mac_only')],
  total_files: 5,
  total_size: 24_000_000,
}
const DCIM: Page = {
  path: '/sdcard/DCIM',
  crumbs: ['/sdcard', '/sdcard/DCIM'],
  folders: [],
  files: [file(9, 'inner.jpg', 'mac_only', { path: '/sdcard/DCIM/inner.jpg' })],
  total_files: 1,
  total_size: 9_000_000,
}
const RUN: DeleteRun = {
  id: 7,
  source: 'browse',
  state: 'ready',
  blocked_reason: null,
  disposition: 'quarantine',
  typed_required: false,
  passed: 2,
  passed_bytes: 3_000_000,
  deleted: 0,
  failed: 0,
  bytes_freed: 0,
  manifests: [],
  backups: ['/Backups/Mac'],
  items: [
    { id: 1, path: '/sdcard/a.jpg', size: 1, check: 'ok', reason: null, state: 'planned', error: null },
    { id: 5, path: '/sdcard/e.jpg', size: 2, check: 'ok', reason: null, state: 'planned', error: null },
  ],
}
const task = (kind: 'preflight' | 'delete', over: Partial<TaskEvent> = {}): TaskEvent => ({ kind, state: 'done', done: 2, total: 2, message: '', result: null, ...over })

let calls: { method: string; path: string; body: unknown }[]
let current: TaskEvent
let run: DeleteRun
let skipped: string[]
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  localStorage.clear()
  useEventStore.setState({ task: null })
  calls = []
  current = task('preflight')
  run = structuredClone(RUN)
  skipped = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/browse/lists') return reply([{ key: 'large_videos', title: 'Large videos (over 500 MB)', files: 1, bytes: 900_000_000 }])
      if (url.startsWith('/api/browse/lists/large_videos')) {
        return reply({ key: 'large_videos', title: 'Large videos (over 500 MB)', total_files: 1, files: [file(20, 'big.mp4', 'mac_ssd', { kind: 'video', path: '/sdcard/Movies/big.mp4' })] })
      }
      if (url.startsWith('/api/browse?')) return reply(new URL(url, 'http://x').searchParams.get('path') === '/sdcard/DCIM' ? DCIM : ROOT)
      if (url === '/api/delete/preflight') return reply({ run_id: 7, task: task('preflight', { state: 'running', done: 0 }), skipped })
      if (url === '/api/tasks/current') return reply(current)
      if (url === '/api/delete/runs/7') return reply(run)
      if (url === '/api/delete/execute') {
        current = task('delete')
        run = { ...run, state: 'done', deleted: 2, bytes_freed: 3_000_000 }
        return reply(task('delete', { state: 'running', done: 0 }))
      }
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage(at = '/browse') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[at]}>
      <QueryClientProvider client={qc}>
        <BrowsePage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}
const posted = (path: string) => calls.filter((c) => c.method === 'POST' && c.path === path)

test('shows folders then files with backup badges, and a folder opens', async () => {
  const { container } = renderPage()
  expect(await screen.findByText('a.jpg')).toBeInTheDocument()
  expect(screen.getByText('DCIM')).toBeInTheDocument()
  expect(screen.getByText('120 backed up · 4 not')).toBeInTheDocument()
  const table = within(screen.getByRole('table'))
  for (const label of ['Mac and SSD', 'SSD only', 'Mac only', 'Not backed up', 'Changed']) expect(table.getByText(label)).toBeInTheDocument()
  const thumbs = [...container.querySelectorAll('img')]
  expect(thumbs).toHaveLength(4)
  expect(thumbs.every((i) => i.getAttribute('loading') === 'lazy')).toBe(true)

  await userEvent.click(screen.getByRole('button', { name: /DCIM/ }))
  expect(await screen.findByText('inner.jpg')).toBeInTheDocument()
  expect(screen.queryByText('a.jpg')).not.toBeInTheDocument()
  const crumbs = screen.getByRole('navigation', { name: 'Folder' })
  expect(within(crumbs).getByText('DCIM')).toHaveAttribute('aria-current', 'page')
  expect(calls.some((c) => c.path.includes('path=%2Fsdcard%2FDCIM'))).toBe(true)
})

test('files that are not backed up cannot be ticked', async () => {
  renderPage()
  await screen.findByText('a.jpg')
  for (const name of ['b.jpg', 'c.mp4']) {
    const box = screen.getByRole('checkbox', { name: `Select ${name}` })
    expect(box).toBeDisabled()
    expect(box).toHaveAttribute('title', 'Not backed up yet')
  }
  expect(screen.getByRole('checkbox', { name: 'Select a.jpg' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Delete selected' })).toHaveAttribute('aria-disabled', 'true')
})

test('Delete selected posts the ids and folders and lists the skipped files', async () => {
  skipped = ['/sdcard/DCIM/x1.jpg', '/sdcard/DCIM/x2.jpg']
  renderPage()
  await userEvent.click(await screen.findByRole('checkbox', { name: 'Select a.jpg' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Select e.jpg' }))
  await userEvent.click(screen.getByRole('checkbox', { name: 'Select folder DCIM' }))
  expect(screen.getByText('2 files and 1 folder selected')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
  await screen.findByRole('dialog', { name: 'Result of the checks' })
  expect(posted('/api/delete/preflight')[0]?.body).toEqual({ source: 'browse', ids: [1, 5], folders: ['/sdcard/DCIM'] })
  const summary = screen.getByText('2 files in these folders are not backed up and will be skipped')
  expect(summary.closest('details')).not.toHaveAttribute('open')
})

test('the disposition chosen in the dialog is sent on execute', async () => {
  renderPage()
  await userEvent.click(await screen.findByRole('checkbox', { name: 'Select a.jpg' }))
  await userEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Continue to delete' }))
  const dialog = await screen.findByRole('dialog', { name: 'Delete from the phone' })
  expect(within(dialog).getByRole('radio', { name: /Move the backup copy to Quarantine/ })).toBeChecked()
  await userEvent.click(within(dialog).getByRole('radio', { name: 'Keep the backup copy (archive)' }))
  await userEvent.click(within(dialog).getByRole('checkbox', { name: 'I have reviewed these files' }))
  await userEvent.click(within(dialog).getByRole('button', { name: 'Delete 2 files' }))
  await waitFor(() => expect(posted('/api/delete/execute')[0]?.body).toEqual({ run_id: 7, confirm: true, disposition: 'archive' }))
})

test('a smart list shows its files in the same table, without folders', async () => {
  renderPage()
  await userEvent.click(await screen.findByRole('button', { name: /Large videos/ }))
  expect(await screen.findByText('big.mp4')).toBeInTheDocument()
  expect(screen.queryByText('a.jpg')).not.toBeInTheDocument()
  expect(screen.queryByText('DCIM')).not.toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Select big.mp4' })).toBeEnabled()
})

test('opens with the backup status filter chosen from the link', async () => {
  renderPage('/browse?status=mac_only')
  await screen.findByText('a.jpg')
  expect(screen.getByRole('combobox', { name: 'Backup status' })).toHaveValue('mac_only')
  expect(calls.some((c) => c.path.startsWith('/api/browse?') && c.path.includes('status=mac_only'))).toBe(true)
})
