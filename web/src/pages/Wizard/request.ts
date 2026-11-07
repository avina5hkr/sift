import { ApiError, apiFetch } from '../../lib/api'

export const post = <T,>(path: string, body?: unknown) =>
  apiFetch<T>(path, {
    method: 'POST',
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })

export const plain = (e: unknown) =>
  e instanceof ApiError ? e.message : 'Sift could not do that just now. Try again in a moment.'
