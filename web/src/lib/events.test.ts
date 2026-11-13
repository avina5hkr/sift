import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { dismissToast, startEventStream, useEventStore } from './events'
import { initToken } from './token'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  onerror: (() => void) | null = null
  closed = false
  listeners = new Map<string, ((e: MessageEvent) => void)[]>()
  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn])
  }
  emit(name: string, data: unknown) {
    for (const fn of this.listeners.get(name) ?? []) {
      fn({ data: JSON.stringify(data) } as MessageEvent)
    }
  }
  close() {
    this.closed = true
  }
}

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  useEventStore.setState({ connected: false, last: undefined, job: null, jobAt: 0, toasts: [] })
  initToken(
    new URL('http://127.0.0.1:1/backup?t=abc') as unknown as Location,
    { getItem: () => null, setItem: () => undefined } as unknown as Storage,
    { replaceState: vi.fn() } as unknown as History,
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

test('connects to /api/events with the token and marks connected on hello', () => {
  const qc = new QueryClient()
  const close = startEventStream(qc)
  const es = FakeEventSource.instances[0]
  expect(es.url).toBe('/api/events?token=abc')
  expect(useEventStore.getState().connected).toBe(false)
  es.emit('hello', { version: '0.1.0' })
  expect(useEventStore.getState().connected).toBe(true)
  expect(useEventStore.getState().last).toEqual({ name: 'hello', data: { version: '0.1.0' } })
  close()
  expect(es.closed).toBe(true)
})

test('device and job events invalidate the status query', () => {
  const qc = new QueryClient()
  const spy = vi.spyOn(qc, 'invalidateQueries')
  startEventStream(qc)
  const es = FakeEventSource.instances[0]
  es.emit('device', { state: 'connected' })
  es.emit('job', { id: 'x' })
  expect(spy).toHaveBeenCalledTimes(2)
  expect(spy).toHaveBeenCalledWith({ queryKey: ['status'] })
  es.emit('log', { line: 'hi' })
  expect(spy).toHaveBeenCalledTimes(2)
})

test('hello invalidates the status and backup queries, so a reconnect heals at once', () => {
  const qc = new QueryClient()
  const spy = vi.spyOn(qc, 'invalidateQueries')
  startEventStream(qc)
  FakeEventSource.instances[0].emit('hello', { version: '0.1.0' })
  expect(spy).toHaveBeenCalledTimes(2)
  expect(spy).toHaveBeenCalledWith({ queryKey: ['status'] })
  expect(spy).toHaveBeenCalledWith({ queryKey: ['backup'] })
})

const JOB = { job_id: 4, type: 'backup', state: 'pulling', files_done: 1, bytes_done: 10 }

test('progress events keep the newest backup job and refetch only when the state changes', () => {
  const qc = new QueryClient()
  const spy = vi.spyOn(qc, 'invalidateQueries')
  startEventStream(qc)
  const es = FakeEventSource.instances[0]
  es.emit('progress', JOB)
  expect(useEventStore.getState().job).toMatchObject({ job_id: 4, state: 'pulling' })
  expect(spy).toHaveBeenCalledWith({ queryKey: ['backup'] })
  spy.mockClear()
  es.emit('progress', { ...JOB, files_done: 2 })
  expect(spy).not.toHaveBeenCalled()
  es.emit('progress', { ...JOB, state: 'done' })
  expect(spy).toHaveBeenCalledWith({ queryKey: ['backup'] })
})

test('a dry run (the wizard size scan) is not a backup: the stored job stays as it was', () => {
  startEventStream(new QueryClient())
  const es = FakeEventSource.instances[0]
  es.emit('progress', { ...JOB, state: 'done' })
  es.emit('progress', { ...JOB, job_id: 5, state: 'scanning', dry_run: true })
  es.emit('progress', { ...JOB, job_id: 5, state: 'done', dry_run: true })
  expect(useEventStore.getState().job).toMatchObject({ job_id: 4, state: 'done' })
})

test('progress events of other kinds of job or without a job id are ignored', () => {
  startEventStream(new QueryClient())
  const es = FakeEventSource.instances[0]
  es.emit('progress', { ...JOB, type: 'cleanup' })
  es.emit('progress', { state: 'pulling', type: 'backup' })
  expect(useEventStore.getState().job).toBeNull()
})

test('toast events queue up, at most four, and can be dismissed', () => {
  startEventStream(new QueryClient())
  const es = FakeEventSource.instances[0]
  for (let i = 0; i < 5; i += 1) es.emit('toast', { level: 'warning', title: `T${i}`, message: 'm' })
  es.emit('toast', { level: 'nonsense', title: 'Odd', message: 'm' })
  es.emit('toast', { title: 3 })
  const toasts = useEventStore.getState().toasts
  expect(toasts).toHaveLength(4)
  expect(toasts.at(-1)).toMatchObject({ title: 'Odd', level: 'info' })
  dismissToast(toasts[0].id)
  expect(useEventStore.getState().toasts).toHaveLength(3)
})

test('an error marks the stream disconnected', () => {
  const qc = new QueryClient()
  startEventStream(qc)
  const es = FakeEventSource.instances[0]
  es.emit('hello', { version: '0.1.0' })
  es.onerror?.()
  expect(useEventStore.getState().connected).toBe(false)
})

test('hello forgets the job the stream last reported, so the server answer replaces it', () => {
  startEventStream(new QueryClient())
  const es = FakeEventSource.instances[0]
  es.emit('progress', JOB)
  expect(useEventStore.getState().job).not.toBeNull()
  expect(useEventStore.getState().jobAt).toBeGreaterThan(0)
  es.emit('hello', { version: '0.1.0' })
  expect(useEventStore.getState().job).toBeNull()
})

test('task events are stored; a finished analyse run remembers its id; the SSD refetches status', () => {
  const qc = new QueryClient()
  const spy = vi.spyOn(qc, 'invalidateQueries')
  startEventStream(qc)
  const es = FakeEventSource.instances[0]
  es.emit('task', { kind: 'verify', state: 'running', done: 1, total: 4, message: '', result: null })
  expect(useEventStore.getState().task?.done).toBe(1)
  es.emit('task', { kind: 'analyse', state: 'done', done: 4, total: 4, message: '', result: { analysis_id: 9 } })
  expect(useEventStore.getState().analysisId).toBe(9)
  expect(spy).toHaveBeenCalledWith({ queryKey: ['backup'] })
  spy.mockClear()
  es.emit('ssd', { configured: true, connected: false })
  expect(spy).toHaveBeenCalledWith({ queryKey: ['status'] })
})
