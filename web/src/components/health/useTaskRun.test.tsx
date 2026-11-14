import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { initToken } from '../../lib/token'
import { useTaskRun } from './useTaskRun'

let current: unknown
const reply = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
const task = (kind: string, state: string) => ({ kind, state, done: 1, total: 4, message: 'Working', result: null })

beforeEach(() => {
  initToken(
    new URL('http://127.0.0.1:1/?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => (url === '/api/tasks/current' ? reply(current) : reply(null))),
  )
})
afterEach(() => vi.unstubAllGlobals())

test('a running task of its kind is followed again when the page opens', async () => {
  current = task('health', 'running')
  const onDone = vi.fn()
  const { result } = renderHook(() => useTaskRun('/api/health/run', 'health', onDone))
  await waitFor(() => expect(result.current.task?.state).toBe('running'))
  expect(result.current.busy).toBe(true)
  current = { ...task('health', 'done'), result: { ok: true } }
  await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1), { timeout: 2000 })
  await waitFor(() => expect(result.current.busy).toBe(false))
})

test('a running task of another kind is left alone', async () => {
  current = task('bugreport', 'running')
  const { result } = renderHook(() => useTaskRun('/api/health/run', 'health', vi.fn()))
  await act(async () => {
    await new Promise((r) => setTimeout(r, 50))
  })
  expect(result.current.task).toBeNull()
  expect(result.current.busy).toBe(false)
})
