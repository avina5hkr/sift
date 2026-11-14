import type { AppRow } from './types'

export const WHATSAPP_PACKAGES = ['com.whatsapp', 'com.whatsapp.w4b']
export const isWhatsApp = (pkg: string) => WHATSAPP_PACKAGES.includes(pkg)
export const LARGE_BYTES = 500_000_000
export const UNAVAILABLE = 'unavailable'

/** The row's reading, or "unavailable" when the phone did not tell. */
export function sizeOrUnavailable(n: number | null, format: (n: number) => string): string {
  return n == null ? UNAVAILABLE : format(n)
}

/** Everything the phone told about the app's footprint, for sorting (unknown last). */
export const totalBytes = (a: AppRow) => a.app_bytes ?? -1

export function installerLabel(a: AppRow): string {
  if (a.from_play_store) return 'Play Store'
  return a.installer || 'unknown'
}

export function lastUsedLabel(a: AppRow, ago: (epoch: number) => string): string {
  return a.last_used == null ? UNAVAILABLE : ago(a.last_used)
}

/** What lets a protected app be uninstalled. */
export function unlockHint(a: AppRow): string {
  return isWhatsApp(a.package)
    ? `Unlocks with a complete ${a.name} chat backup.`
    : 'Unprotect it first to allow this.'
}
