import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { initToken } from '../lib/token'
import type { DeleteRun, SlatePlan, TaskEvent } from '../lib/types'
import SlatePage from './SlatePage'

const zero = { files: 0, bytes: 0 }
const PLAN: SlatePlan = {
  id: 3,
  rules: { keep_folders: ['/sdcard/Documents'], keep_newer_than_days: 30, keep_labelled: true, keep_camera: false, patterns: [] },
  kept_by_hand: { files: 0, folders: [] },
  remove: { files: 41_230, bytes: 96_000_000_000 },
  keep: { files: 3_102, bytes: 12_000_000_000 },
  protected: { files: 50, bytes: 1_000_000 },
  never: { files: 7, bytes: 2_000 },
  gaps: zero,
  gaps_no_set: zero,
  categories: {
    camera: { files: 40_000, bytes: 90_000_000_000 },
    screenshots: zero,
    whatsapp: zero,
    documents: zero,
    junk: { files: 1_230, bytes: 6_000_000_000 },
    other: zero,
  },
  gap_sets: [],
}
const RUN: DeleteRun = {
  id: 9,
  source: 'slate',
  state: 'ready',
  blocked_reason: null,
  disposition: 'quarantine',
  typed_required: true,
  passed: 41_230,
  passed_bytes: 96_000_000_000,
  deleted: 0,
  failed: 0,
  bytes_freed: 0,
  manifests: [],
  backups: ['/Backups/Mac'],
  items: [],
}
const task = (kind: 'preflight' | 'delete', over: Partial<TaskEvent> = {}): TaskEvent => ({ kind, state: 'done', done: 1, total: 1, message: '', result: null, ...over })

let calls: { method: string; path: string; body: unknown }[]
let plan: SlatePlan
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  useEventStore.setState({ task: null })
  calls = []
  plan = structuredClone(PLAN)
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/slate/plan') return reply(plan)
      if (url === '/api/slate/plan/3') return reply(plan)
      if (url === '/api/slate/plan/3/backup-gaps') return reply({ job_id: 5, sets: ['dcim', 'pictures'] })
      if (url === '/api/slate/plan/3/keep') {
        const b = JSON.parse(init.body as string) as { folders: string[] }
        plan = { ...plan, remove: { files: 1_000, bytes: 5_000_000_000 }, keep: { files: 43_332, bytes: 103_000_000_000 }, kept_by_hand: { files: 0, folders: b.folders } }
        return reply(plan)
      }
      if (url.startsWith('/api/slate/plan/3/tree')) {
        return reply([{ folder: '/sdcard/DCIM/Camera', files: 40_000, bytes: 90_000_000_000, sample_ids: [1, 2], reasons: { 'Not kept by any rule': 40_000 } }])
      }
      if (url.startsWith('/api/slate/plan/3/files')) {
        return reply({
          total_files: 2,
          files: [
            { id: 11, path: '/sdcard/Pictures/a.jpg', size: 1_000_000, mtime: 1, kind: 'image', status: 'mac_only', reason: 'x', category: 'camera', gap: 'no_set' },
            { id: 12, path: '/sdcard/Pictures/b.jpg', size: 2_000_000, mtime: 1, kind: 'image', status: 'not_backed_up', reason: 'x', category: 'camera', gap: 'not_backed_up' },
          ],
        })
      }
      if (url === '/api/slate/plan/3/preflight') return reply({ run_id: 9, task: task('preflight', { state: 'running', done: 0 }) })
      if (url === '/api/tasks/current') return reply(task('preflight'))
      if (url === '/api/delete/runs/9') return reply(RUN)
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SlatePage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}
const posted = (path: string) => calls.filter((c) => c.method === 'POST' && c.path === path)
const makePlan = async () => {
  await userEvent.click(screen.getByRole('button', { name: 'Make the plan' }))
  await screen.findByText('Remove 41,230 files (96.0 GB). Keep 3,102 files (12.0 GB).')
}
const next = () => userEvent.click(screen.getByRole('button', { name: 'Next' }))

test('the rules form posts its body and the summary appears', async () => {
  renderPage()
  expect(screen.getByRole('checkbox', { name: /Protected files are always kept/ })).toBeDisabled()
  await userEvent.type(screen.getByRole('textbox', { name: 'Folder to keep' }), '/sdcard/Taxes')
  await userEvent.click(screen.getByRole('button', { name: 'Add folder' }))
  await userEvent.click(screen.getByRole('button', { name: 'Remove /sdcard/Documents from the folders to keep' }))
  const days = screen.getByRole('spinbutton', { name: 'Keep files newer than (days)' })
  await userEvent.clear(days)
  await userEvent.type(days, '0')
  await userEvent.click(screen.getByRole('checkbox', { name: 'Keep camera originals' }))
  await userEvent.type(screen.getByRole('textbox', { name: /Keep paths matching/ }), '/sdcard/Download/*.pdf')
  await makePlan()
  expect(posted('/api/slate/plan')[0]?.body).toEqual({
    keep_folders: ['/sdcard/Taxes'],
    keep_newer_than_days: 0,
    keep_labelled: true,
    keep_camera: true,
    patterns: ['/sdcard/Download/*.pdf'],
  })
  expect(screen.getByText(/50 files \(1\.0 MB\) protected/)).toBeInTheDocument()
})

test('gaps block Next, Back up now posts, and Keep these posts the ids', async () => {
  plan = { ...plan, gaps: { files: 2, bytes: 3_000_000 }, gaps_no_set: { files: 1, bytes: 1_000_000 }, gap_sets: ['dcim'] }
  renderPage()
  await userEvent.click(screen.getByRole('button', { name: 'Make the plan' }))
  await screen.findByText(/Remove 41,230/)
  await next()
  expect(await screen.findByText('2 files to remove are not backed up yet (3.0 MB)')).toBeInTheDocument()
  expect(await screen.findByText('Pictures/a.jpg')).toBeInTheDocument()
  expect(screen.getByText('Not in any backup set')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Next' })).toHaveAttribute('aria-disabled', 'true')

  await userEvent.click(screen.getByRole('button', { name: 'Back up now' }))
  expect(await screen.findByText(/Come back when the backup finishes/)).toBeInTheDocument()
  expect(posted('/api/slate/plan/3/backup-gaps')).toHaveLength(1)

  await userEvent.click(screen.getByRole('button', { name: /Keep these 1 file/ }))
  await waitFor(() => expect(posted('/api/slate/plan/3/keep')[0]?.body).toEqual({ ids: [11], folders: [] }))

  plan = { ...plan, gaps: { files: 0, bytes: 0 }, gaps_no_set: { files: 0, bytes: 0 } }
  await userEvent.click(screen.getByRole('button', { name: 'Check again' }))
  await screen.findByText('Every file to remove is already backed up.')
  expect(screen.getByRole('button', { name: 'Next' })).not.toHaveAttribute('aria-disabled')
})

test('Keep this folder posts the folder and updates the totals', async () => {
  renderPage()
  await makePlan()
  await next()
  await next()
  expect(await screen.findByText('/sdcard/DCIM/Camera'.replace('/sdcard/', ''))).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'To remove' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('tab', { name: 'To keep' })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Protected' })).toBeInTheDocument()
  expect(screen.getByText('Remove 41,230 files (96.0 GB). Keep 3,102 files (12.0 GB).')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /Keep this folder/ }))
  await waitFor(() => expect(posted('/api/slate/plan/3/keep')[0]?.body).toEqual({ ids: [], folders: ['/sdcard/DCIM/Camera'] }))
  expect(await screen.findByText('Remove 1,000 files (5.0 GB). Keep 43,332 files (103.0 GB).')).toBeInTheDocument()
})

test('opening a folder and ticking files posts Keep selected', async () => {
  renderPage()
  await makePlan()
  await next()
  await next()
  await userEvent.click(await screen.findByRole('button', { name: 'DCIM/Camera' }))
  await userEvent.click(await screen.findByRole('checkbox', { name: 'Select a.jpg' }))
  await userEvent.click(screen.getByRole('button', { name: /Keep selected/ }))
  await waitFor(() => expect(posted('/api/slate/plan/3/keep')[0]?.body).toEqual({ ids: [11], folders: [] }))
})

test('the dispositions are sent to the checks, which hand over to the delete flow', async () => {
  renderPage()
  await makePlan()
  await next()
  await next()
  await next()
  const junk = screen.getByRole('group', { name: /Junk/ })
  expect(within(junk).getByRole('radio', { name: /Quarantine for 30 days/ })).toBeChecked()
  const camera = screen.getByRole('group', { name: /Camera pictures/ })
  expect(within(camera).getByRole('radio', { name: /Keep in the backup/ })).toBeChecked()
  expect(screen.queryByRole('group', { name: /Screenshots/ })).not.toBeInTheDocument()
  await userEvent.click(within(camera).getByRole('radio', { name: /Quarantine for 30 days/ }))
  await next()
  await userEvent.click(screen.getByRole('button', { name: 'Check the files' }))
  await screen.findByRole('dialog', { name: 'Result of the checks' })
  expect(posted('/api/slate/plan/3/preflight')[0]?.body).toEqual({ dispositions: { camera: 'quarantine', junk: 'quarantine' } })
  await userEvent.click(screen.getByRole('button', { name: 'Continue to delete' }))
  const dialog = await screen.findByRole('dialog', { name: 'Delete from the phone' })
  expect(within(dialog).getByText('Type 41,230 to confirm')).toBeInTheDocument()
})
