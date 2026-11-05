import { getToken } from './token'
import type { ApiErrorBody } from './types'

const UNREACHABLE = 'Sift could not reach its engine. Try again in a moment.'

export class ApiError extends Error {
  code: string
  detail: unknown

  constructor(code: string, message: string, detail: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.detail = detail
  }
}

function isErrorBody(body: unknown): body is ApiErrorBody {
  return (
    typeof body === 'object' &&
    body !== null &&
    typeof (body as ApiErrorBody).code === 'string' &&
    typeof (body as ApiErrorBody).message === 'string'
  )
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = Object.fromEntries(new Headers(init.headers).entries())
  const token = getToken()
  if (token) {
    delete headers['x-sift-token']
    headers['X-Sift-Token'] = token
  }

  let res: Response
  try {
    res = await fetch(path, { ...init, headers })
  } catch {
    throw new ApiError('network', UNREACHABLE)
  }

  if (!res.ok) {
    let body: unknown = null
    try {
      body = await res.json()
    } catch {
      // not JSON: fall through to the plain message
    }
    if (isErrorBody(body)) throw new ApiError(body.code, body.message, body.detail ?? null)
    throw new ApiError(`http_${res.status}`, UNREACHABLE)
  }

  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

// Only GET /api/events and GET /api/media/* accept ?token= (EventSource and <img>
// cannot send headers). Never use this for anything else.
export function withToken(url: string): string {
  const token = getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}
