import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ApiError, apiFetch, withToken } from './api'
import { initToken } from './token'

beforeEach(() => {
  const store = new Map<string, string>()
  const st = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => store.set(k, v),
  } as unknown as Storage
  initToken(
    new URL('http://127.0.0.1:1/backup?t=abc') as unknown as Location,
    st,
    { replaceState: vi.fn() } as unknown as History,
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

test('apiFetch sends the token header', async () => {
  mockFetch(200, {})
  await apiFetch('/api/status')
  expect(fetch).toHaveBeenCalledWith(
    '/api/status',
    expect.objectContaining({ headers: expect.objectContaining({ 'X-Sift-Token': 'abc' }) }),
  )
})

test('apiFetch throws ApiError with the server error shape', async () => {
  mockFetch(401, {
    code: 'bad_token',
    message: 'This request is not from the Sift window.',
    detail: null,
  })
  await expect(apiFetch('/api/status')).rejects.toMatchObject({
    code: 'bad_token',
    message: 'This request is not from the Sift window.',
  })
  await expect(apiFetch('/api/status')).rejects.toBeInstanceOf(ApiError)
})

test('apiFetch gives a plain message when the error body is not JSON', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>oops</html>', { status: 502 })))
  await expect(apiFetch('/api/status')).rejects.toMatchObject({
    code: 'http_502',
    message: 'Sift could not reach its engine. Try again in a moment.',
  })
})

test('apiFetch turns a network failure into a plain ApiError', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
  await expect(apiFetch('/api/status')).rejects.toMatchObject({
    code: 'network',
    message: 'Sift could not reach its engine. Try again in a moment.',
  })
})

test('withToken', () => {
  expect(withToken('/api/events')).toBe('/api/events?token=abc')
  expect(withToken('/api/media/1/thumb?size=256')).toBe('/api/media/1/thumb?size=256&token=abc')
})
