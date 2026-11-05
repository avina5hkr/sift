import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { startEventStream, useEventStore } from './events'
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
  useEventStore.setState({ connected: false, last: undefined })
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

test('hello invalidates the status query, so a reconnect heals at once', () => {
  const qc = new QueryClient()
  const spy = vi.spyOn(qc, 'invalidateQueries')
  startEventStream(qc)
  FakeEventSource.instances[0].emit('hello', { version: '0.1.0' })
  expect(spy).toHaveBeenCalledTimes(1)
  expect(spy).toHaveBeenCalledWith({ queryKey: ['status'] })
})

test('an error marks the stream disconnected', () => {
  const qc = new QueryClient()
  startEventStream(qc)
  const es = FakeEventSource.instances[0]
  es.emit('hello', { version: '0.1.0' })
  es.onerror?.()
  expect(useEventStore.getState().connected).toBe(false)
})
