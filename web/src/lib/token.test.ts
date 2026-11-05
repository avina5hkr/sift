import { afterEach, expect, test, vi } from 'vitest'
import { getToken, initToken } from './token'

function memoryStorage(initial: Record<string, string> = {}) {
  const storage = new Map<string, string>(Object.entries(initial))
  const st = {
    getItem: (k: string) => storage.get(k) ?? null,
    setItem: (k: string, v: string) => storage.set(k, v),
  } as unknown as Storage
  return { storage, st }
}

afterEach(() => {
  // reset the in-memory token between tests
  initToken(
    new URL('http://127.0.0.1:1/') as unknown as Location,
    memoryStorage().st,
    { replaceState: vi.fn() } as unknown as History,
  )
})

test('initToken moves t from the URL into session storage', () => {
  const { storage, st } = memoryStorage()
  const hist = { replaceState: vi.fn() } as unknown as History
  const loc = new URL('http://127.0.0.1:1/backup?t=abc') as unknown as Location
  expect(initToken(loc, st, hist)).toBe('abc')
  expect(storage.get('sift.token')).toBe('abc')
  expect(hist.replaceState).toHaveBeenCalledWith(null, '', '/backup')
  expect(getToken()).toBe('abc')
})

test('initToken keeps other query params and the hash', () => {
  const { st } = memoryStorage()
  const hist = { replaceState: vi.fn() } as unknown as History
  const loc = new URL('http://127.0.0.1:1/logs?t=abc&x=1#top') as unknown as Location
  initToken(loc, st, hist)
  expect(hist.replaceState).toHaveBeenCalledWith(null, '', '/logs?x=1#top')
})

test('initToken falls back to the stored token on reload', () => {
  const { st } = memoryStorage({ 'sift.token': 'stored' })
  const hist = { replaceState: vi.fn() } as unknown as History
  const loc = new URL('http://127.0.0.1:1/backup') as unknown as Location
  expect(initToken(loc, st, hist)).toBe('stored')
  expect(hist.replaceState).not.toHaveBeenCalled()
})

test('initToken returns null when there is no token anywhere', () => {
  const { st } = memoryStorage()
  const hist = { replaceState: vi.fn() } as unknown as History
  const loc = new URL('http://127.0.0.1:1/backup') as unknown as Location
  expect(initToken(loc, st, hist)).toBeNull()
  expect(getToken()).toBeNull()
})
