import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { initToken } from '../lib/token'
import { CrashDialog } from './CrashDialog'

let calls: { method: string; path: string; body: unknown }[]
let crash: { report: string; summary: string } | null

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  calls = []
  crash = { report: 'crash-1.txt', summary: 'ValueError: boom' }
  useEventStore.setState({ last: undefined })
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ method: init.method ?? 'GET', path: url, body: init.body ? JSON.parse(init.body as string) : null })
      if (url === '/api/crash') return Promise.resolve(new Response(JSON.stringify({ crash }), { status: 200 }))
      return Promise.resolve(new Response(null, { status: 204 }))
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

test('shows the dialog on start with the summary, and Close is the focused button', async () => {
  render(<CrashDialog />)
  expect(await screen.findByRole('dialog', { name: 'Sift hit a problem' })).toBeInTheDocument()
  expect(screen.getByText('Sift wrote a report about it. Your backups are safe.')).toBeInTheDocument()
  expect(screen.getByText('ValueError: boom')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()
})

test('shows nothing when there is no crash', async () => {
  crash = null
  render(<CrashDialog />)
  await waitFor(() => expect(calls.some((c) => c.path === '/api/crash')).toBe(true))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('Close marks the report seen and hides the dialog', async () => {
  render(<CrashDialog />)
  await userEvent.click(await screen.findByRole('button', { name: 'Close' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(calls).toContainEqual({ method: 'POST', path: '/api/crash/seen', body: { report: 'crash-1.txt' } })
})

test('Open logs posts to the logs endpoint and keeps the dialog', async () => {
  render(<CrashDialog />)
  await userEvent.click(await screen.findByRole('button', { name: 'Open logs' }))
  expect(calls.some((c) => c.method === 'POST' && c.path === '/api/logs/open')).toBe(true)
  expect(screen.getByRole('dialog')).toBeInTheDocument()
})

test('a crash event on the stream shows the dialog', async () => {
  crash = null
  render(<CrashDialog />)
  await waitFor(() => expect(calls.length).toBe(1))
  crash = { report: 'crash-2.txt', summary: 'late' }
  act(() => useEventStore.setState({ last: { name: 'crash', data: { report: 'crash-2.txt' } } }))
  expect(await screen.findByText('late')).toBeInTheDocument()
})
