// The Sift window opens at /?t=TOKEN. Take the token once, keep it in memory and in
// sessionStorage (so a reload still works), and remove it from the visible URL.
const STORAGE_KEY = 'sift.token'

let token: string | null = null

export function initToken(
  loc: Location = window.location,
  storage: Storage = window.sessionStorage,
  history: History = window.history,
): string | null {
  const params = new URLSearchParams(loc.search)
  const fromUrl = params.get('t')
  if (fromUrl) {
    token = fromUrl
    try {
      storage.setItem(STORAGE_KEY, fromUrl)
    } catch {
      // storage can be blocked; the in-memory token still works for this load
    }
    params.delete('t')
    const query = params.toString()
    history.replaceState(null, '', loc.pathname + (query ? `?${query}` : '') + loc.hash)
    return token
  }
  try {
    token = storage.getItem(STORAGE_KEY)
  } catch {
    token = null
  }
  return token
}

export function getToken(): string | null {
  return token
}
