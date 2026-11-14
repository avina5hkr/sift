import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../../lib/events'
import { initToken } from '../../lib/token'
import type { DeleteItem, DeleteRun, TaskEvent } from '../../lib/types'
import { DeleteFlow } from './DeleteFlow'
import { useDeleteRun } from './useDeleteRun'

const item = (id: number, over: Partial<DeleteItem> = {}): DeleteItem => ({
  id,
  path: `/sdcard/DCIM/Camera/IMG_${id}.jpg`,
  size: 1000,
  check: 'ok',
  reason: null,
  state: 'planned',
  error: null,
  ...over,
})
const RUN: DeleteRun = {
  id: 7,
  source: 'cleanup',
  state: 'ready',
  blocked_reason: null,
  disposition: 'quarantine',
  typed_required: false,
  passed: 2,
  passed_bytes: 2000,
  deleted: 0,
  failed: 0,
  bytes_freed: 0,
  manifests: [],
  backups: ['/Backups/Mac', '/Volumes/SSD/Backups'],
  items: [item(1), item(2)],
}
const task = (kind: 'preflight' | 'delete', over: Partial<TaskEvent> = {}): TaskEvent => ({
  kind,
  state: 'done',
  done: 2,
  total: 2,
  message: '',
  result: null,
  ...over,
})

let run: DeleteRun
let calls: { method: string; path: string; body: unknown }[]
let executeError: { code: string; message: string } | null
let current: TaskEvent
const reply = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  localStorage.clear()
  useEventStore.setState({ task: null })
  run = structuredClone(RUN)
  executeError = null
  current = task('preflight')
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      calls.push({ method, path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/delete/preflight') return reply({ run_id: 7, task: task('preflight', { state: 'running', done: 0 }) })
      if (url === '/api/tasks/current') return reply(current)
      if (url === '/api/tasks/stop') {
        current = task('preflight')
        return reply({})
      }
      if (url === '/api/delete/runs/7') return reply(run)
      if (url === '/api/delete/execute') {
        if (executeError) return reply({ ...executeError, detail: null }, 409)
        current = task('delete')
        run = { ...run, state: 'done', deleted: 2, bytes_freed: 2000, manifests: ['/Backups/Mac/deleted-7.txt'] }
        return reply(task('delete', { state: 'running', done: 0 }))
      }
      return reply({ code: 'nope', message: 'No.', detail: null }, 404)
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function Harness() {
  const flow = useDeleteRun()
  return (
    <>
      <button type="button" onClick={() => void flow.start({ source: 'cleanup', ids: [1, 2] })}>
        Go
      </button>
      <DeleteFlow flow={flow} />
    </>
  )
}
function renderFlow() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Harness />
    </QueryClientProvider>,
  )
}
const posted = (path: string) => calls.filter((c) => c.method === 'POST' && c.path === path)

async function toConfirm() {
  renderFlow()
  await userEvent.click(screen.getByRole('button', { name: 'Go' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Continue to delete' }))
  return screen.findByRole('dialog', { name: 'Delete from the phone' })
}

test('the checks show their progress with Stop, then how many passed', async () => {
  current = task('preflight', { state: 'running', done: 1 })
  renderFlow()
  await userEvent.click(screen.getByRole('button', { name: 'Go' }))
  await screen.findByText('Checking 2 files…')
  await userEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(posted('/api/tasks/stop')).toHaveLength(1)
  expect(await screen.findByText('2 files can be deleted (2.0 KB)', {}, { timeout: 3000 })).toBeInTheDocument()
  expect(posted('/api/delete/preflight')[0]?.body).toEqual({ source: 'cleanup', ids: [1, 2] })
})

test('files that did not pass are grouped by reason, each a collapsed list of paths', async () => {
  run.items = [
    item(1),
    item(2, { check: 'changed', reason: 'Changed on the phone since the backup' }),
    item(3, { check: 'changed', reason: 'Changed on the phone since the backup' }),
    item(4, { check: 'protected', reason: 'Protected' }),
  ]
  run.passed = 1
  renderFlow()
  await userEvent.click(screen.getByRole('button', { name: 'Go' }))
  const changed = await screen.findByText('Changed on the phone since the backup (2)')
  const details = changed.closest('details') as HTMLElement
  expect(details).not.toHaveAttribute('open')
  expect(within(details).getByText('/sdcard/DCIM/Camera/IMG_3.jpg')).toBeInTheDocument()
  expect(screen.getByText('Protected (1)')).toBeInTheDocument()
})

test('when nothing passes, the reason is shown and there is no delete button', async () => {
  run = { ...run, state: 'blocked', passed: 0, passed_bytes: 0, blocked_reason: 'Plug in the SSD first.' }
  renderFlow()
  await userEvent.click(screen.getByRole('button', { name: 'Go' }))
  expect(await screen.findByText('Plug in the SSD first.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue to delete' })).not.toBeInTheDocument()
})

test('the confirm button waits for the checkbox', async () => {
  const dialog = await toConfirm()
  expect(dialog).toHaveTextContent('Backups: /Backups/Mac, /Volumes/SSD/Backups')
  expect(within(dialog).getByText('This cannot be undone on the phone.')).toBeInTheDocument()
  const del = within(dialog).getByRole('button', { name: 'Delete 2 files' })
  expect(del).toBeDisabled()
  expect(within(dialog).queryByLabelText(/Type/)).not.toBeInTheDocument()
  await userEvent.click(within(dialog).getByRole('checkbox', { name: 'I have reviewed these files' }))
  expect(del).toBeEnabled()
  await userEvent.click(del)
  await waitFor(() => expect(posted('/api/delete/execute')[0]?.body).toEqual({ run_id: 7, confirm: true }))
})

test('a large run also needs the count typed', async () => {
  run = { ...run, typed_required: true, passed: 241 }
  const dialog = await toConfirm()
  const del = within(dialog).getByRole('button', { name: 'Delete 241 files' })
  await userEvent.click(within(dialog).getByRole('checkbox'))
  expect(del).toBeDisabled()
  const field = within(dialog).getByLabelText('Type 241 to confirm')
  await userEvent.type(field, '24')
  expect(del).toBeDisabled()
  await userEvent.type(field, '1')
  expect(del).toBeEnabled()
  await userEvent.click(del)
  await waitFor(() => expect(posted('/api/delete/execute')[0]?.body).toEqual({ run_id: 7, confirm: true, typed_count: 241 }))
})

test('Esc closes the confirm dialog and Tab stays inside it', async () => {
  const dialog = await toConfirm()
  await userEvent.tab({ shift: true })
  expect(dialog).toContainElement(document.activeElement as HTMLElement)
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('dialog', { name: 'Delete from the phone' })).not.toBeInTheDocument()
  expect(posted('/api/delete/execute')).toHaveLength(0)
})

test('the WhatsApp note shows once', async () => {
  run.items = [item(1, { path: '/sdcard/Android/media/com.whatsapp/WhatsApp/Media/a.jpg' }), item(2)]
  let dialog = await toConfirm()
  const note = "Chats keep their text, but deleted media shows as missing, and WhatsApp's storage screen may not shrink at once."
  expect(within(dialog).getByText(note)).toBeInTheDocument()
  await userEvent.click(within(dialog).getByRole('checkbox'))
  await userEvent.click(within(dialog).getByRole('button', { name: 'Delete 2 files' }))
  await screen.findByText('2 files deleted. 2.0 KB freed.')
  expect(localStorage.getItem('sift.whatsappDeleteNoteSeen')).toBe('1')
  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  run = { ...structuredClone(RUN), items: run.items }
  current = task('preflight')
  await userEvent.click(screen.getByRole('button', { name: 'Go' }))
  await userEvent.click(await screen.findByRole('button', { name: 'Continue to delete' }))
  dialog = await screen.findByRole('dialog', { name: 'Delete from the phone' })
  expect(within(dialog).queryByText(note)).not.toBeInTheDocument()
})

test('after the delete: the count, the space freed and where the list is saved', async () => {
  const dialog = await toConfirm()
  await userEvent.click(within(dialog).getByRole('checkbox'))
  await userEvent.click(within(dialog).getByRole('button', { name: 'Delete 2 files' }))
  expect(await screen.findByText('2 files deleted. 2.0 KB freed.')).toBeInTheDocument()
  expect(screen.getByText('The list of deleted files is saved at /Backups/Mac/deleted-7.txt')).toBeInTheDocument()
})

test('failed deletes are listed with their errors', async () => {
  const dialog = await toConfirm()
  await userEvent.click(within(dialog).getByRole('checkbox'))
  run = {
    ...run,
    state: 'done',
    deleted: 1,
    failed: 1,
    bytes_freed: 1000,
    items: [item(1, { state: 'deleted' }), item(2, { state: 'failed', error: 'The file is still on the phone.' })],
  }
  await userEvent.click(within(dialog).getByRole('button', { name: 'Delete 2 files' }))
  expect(await screen.findByText('1 file could not be deleted')).toBeInTheDocument()
  expect(screen.getByText('/sdcard/DCIM/Camera/IMG_2.jpg: The file is still on the phone.')).toBeInTheDocument()
})

test('an old check (stale_preflight) offers to run the checks again', async () => {
  executeError = { code: 'stale_preflight', message: 'The checks are out of date.' }
  const dialog = await toConfirm()
  await userEvent.click(within(dialog).getByRole('checkbox'))
  await userEvent.click(within(dialog).getByRole('button', { name: 'Delete 2 files' }))
  expect(await within(dialog).findByText('The checks are out of date.')).toBeInTheDocument()
  expect(within(dialog).getByRole('button', { name: 'Delete 2 files' })).toBeDisabled()
  await userEvent.click(within(dialog).getByRole('button', { name: 'Run the checks again' }))
  await waitFor(() => expect(posted('/api/delete/preflight')).toHaveLength(2))
  expect(posted('/api/delete/preflight')[1]?.body).toEqual({ source: 'cleanup', ids: [1, 2] })
  expect(await screen.findByRole('button', { name: 'Continue to delete' })).toBeInTheDocument()
})
