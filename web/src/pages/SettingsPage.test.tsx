import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../lib/token'
import SettingsPage from './SettingsPage'

const field = (section: string, key: string, value: unknown, schema: object, def: unknown = value) => ({ section, key, value, schema, default: def })
const SETTINGS = {
  pages: {
    general: [field('cleanup', 'never_precheck', false, { type: 'boolean' }), field('phone', 'keep_awake', false, { type: 'boolean' })],
    phone: [
      field('phone', 'allow_delete_over_wifi', false, { type: 'boolean' }),
      field('phone', 'parallel_reads_usb', 3, { type: 'integer', minimum: 1, maximum: 6 }),
      field('phone', 'adb_path', '', { type: 'string' }),
    ],
    destinations: [
      field('backup', 'layout', 'mirror', { type: 'string', enum: ['mirror', 'by_date'] }),
      field('backup', 'required_destinations', ['mac', 'ssd'], { type: 'array', items: { type: 'string', enum: ['mac', 'ssd'] } }),
    ],
    backup: [], models: [], advanced: [],
    cleanup: [field('cleanup', 'delete_roots', ['/sdcard/DCIM', '/sdcard/Pictures'], { type: 'array', items: { type: 'string' } }, ['/sdcard/DCIM'])],
  },
  warnings: [],
}
let calls: { method: string; path: string; body: unknown }[]
let putReply: () => Response
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  putReply = () => json(SETTINGS)
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      const method = init.method ?? 'GET'
      calls.push({ method, path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/settings') return Promise.resolve(method === 'PUT' ? putReply() : json(SETTINGS))
      if (url === '/api/phone/wifi') {
        return Promise.resolve(json({
          remembered: [{ name: 'Pixel', address: '192.168.1.5:5555', paired_at: 1, last_connected: null }],
          found: [{ name: 'adb-abc', kind: '_adb-tls-connect._tcp', address: '192.168.1.6:40000' }],
          allow_delete: false,
        }))
      }
      if (url === '/api/phone/wifi/pair') return Promise.resolve(json({ paired: true, connected: 'adb-abc' }))
      if (url === '/api/phone/wifi/forget') return Promise.resolve(new Response(null, { status: 204 }))
      if (url === '/api/phone/wifi/connect') return Promise.resolve(json({}))
      if (url === '/api/setup/reader-test') return Promise.resolve(json({ tested: true, readers: 3, speed_bps: 38_000_000, message: 'Done.' }))
      return Promise.resolve(json({ code: 'nope', message: 'No.', detail: null }, 404))
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
          <Route path="/settings/:page" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('renders the fields from the schema with plain labels', async () => {
  renderAt('/settings/phone')
  expect(await screen.findByLabelText('Allow delete over Wi-Fi')).not.toBeChecked()
  expect(screen.getByText('Off: deleting from the phone needs a USB cable.')).toBeInTheDocument()
  const n = screen.getByLabelText('Files read at once over USB')
  expect(n).toHaveAttribute('type', 'number')
  expect(n).toHaveAttribute('min', '1')
  expect(n).toHaveAttribute('max', '6')
  expect(screen.getByLabelText('adb program path')).toHaveAttribute('type', 'text')
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
})

test('select, enum list and textarea render from the schema', async () => {
  renderAt('/settings/destinations')
  expect(await screen.findByLabelText('Folder layout')).toHaveValue('mirror')
  expect(screen.getByRole('checkbox', { name: 'The SSD' })).toBeChecked()
  renderAt('/settings/cleanup')
  expect(await screen.findByLabelText('Folders Cleanup may delete from')).toHaveValue('/sdcard/DCIM\n/sdcard/Pictures')
})

test('Save sends only the changed fields', async () => {
  renderAt('/settings/phone')
  await userEvent.click(await screen.findByLabelText('Allow delete over Wi-Fi'))
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
    changes: [{ section: 'phone', key: 'allow_delete_over_wifi', value: true }],
  }))
})

test('text areas are saved as lists and Reset to default fills the default', async () => {
  renderAt('/settings/cleanup')
  await screen.findByLabelText('Folders Cleanup may delete from')
  await userEvent.click(screen.getByRole('button', { name: 'Reset Folders Cleanup may delete from to default' }))
  expect(screen.getByLabelText('Folders Cleanup may delete from')).toHaveValue('/sdcard/DCIM')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
    changes: [{ section: 'cleanup', key: 'delete_roots', value: ['/sdcard/DCIM'] }],
  }))
})

test('a 422 message is shown inline', async () => {
  putReply = () => json({ code: 'invalid', message: 'phone.parallel_reads_usb: Input should be at most 6', detail: null }, 422)
  renderAt('/settings/phone')
  const n = await screen.findByLabelText('Files read at once over USB')
  await userEvent.clear(n)
  await userEvent.type(n, '9')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('phone.parallel_reads_usb: Input should be at most 6')
})

test('Wi-Fi: Pair sends the address and the code; remembered phones can be forgotten', async () => {
  renderAt('/settings/phone')
  const pair = await screen.findByRole('button', { name: 'Pair' })
  expect(pair).toBeDisabled()
  await userEvent.type(screen.getByLabelText('Pairing address'), '192.168.1.6:37099')
  await userEvent.type(screen.getByLabelText('Pairing code'), '123456')
  await userEvent.click(pair)
  await waitFor(() => expect(calls.find((c) => c.path === '/api/phone/wifi/pair')?.body).toEqual({ address: '192.168.1.6:37099', code: '123456' }))
  expect(await screen.findByText('Paired and connected to adb-abc.')).toBeInTheDocument()
  await userEvent.click(await screen.findByRole('button', { name: 'Forget' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/phone/wifi/forget')?.body).toEqual({ name: 'Pixel', address: '192.168.1.5:5555' }))
  await userEvent.click(screen.getByRole('button', { name: 'Connect' }))
  await waitFor(() => expect(calls.find((c) => c.path === '/api/phone/wifi/connect')?.body).toEqual({ address: '192.168.1.6:40000' }))
})

test('the connection test shows MB/s', async () => {
  renderAt('/settings/phone')
  await userEvent.click(await screen.findByRole('button', { name: 'Test the connection' }))
  expect(await screen.findByText(/38\.0 MB\/s with 3 files read at once/)).toBeInTheDocument()
})
