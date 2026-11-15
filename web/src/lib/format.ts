const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

// 1000-based, one decimal (bytes are shown whole): 41_200_000_000 -> "41.2 GB".
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  let value = n
  let unit = 0
  while (value >= 1000 && unit < UNITS.length - 1) {
    value /= 1000
    unit += 1
  }
  return unit === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${UNITS[unit]}`
}

// 37_800_000 -> "37.8 MB/s"
export function formatSpeed(bps: number): string {
  return `${formatBytes(bps)}/s`
}

// Rounded the way a person says it: "under a minute", "about 41 min", "about 2 h 5 min".
export function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  if (seconds < 60) return 'under a minute'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `about ${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m === 0 ? `about ${h} h` : `about ${h} h ${m} min`
}

// 12430 -> "12,430"
export function formatCount(n: number): string {
  return n.toLocaleString('en-US')
}

// Seconds since the epoch -> "4 Nov 22:10" (local time).
// The one time format (plan A6): "today at 18:47", "yesterday at 09:12", "15 Nov at 18:47",
// "15 Nov 2025 at 18:47" in another year. `now` is injectable for tests.
export function formatWhen(epochSeconds: number, now: Date = new Date()): string {
  const d = new Date(epochSeconds * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const at = `at ${hh}:${mm}`
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((day(now) - day(d)) / 86_400_000)
  if (days === 0) return `today ${at}`
  if (days === 1) return `yesterday ${at}`
  const month = d.toLocaleString('en-GB', { month: 'short' })
  const year = d.getFullYear() === now.getFullYear() ? '' : ` ${d.getFullYear()}`
  return `${d.getDate()} ${month}${year} ${at}`
}

// The full date and time, for hovering over a `formatWhen` text: "Sunday 15 November 2026, 18:47:05".
export function formatFull(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000)
  const date = d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return `${date.replace(',', '')}, ${time}`
}

// 125 -> "2 min 5 s"
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ${s % 60} s`
  return `${Math.floor(m / 60)} h ${m % 60} min`
}

// "just now", "5 minutes ago", "3 days ago" (now is injectable for tests).
export function formatAgo(epochSeconds: number, now: number = Date.now() / 1000): string {
  const s = Math.max(0, now - epochSeconds)
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'} ago`
  if (s < 60) return 'just now'
  if (s < 3600) return unit(Math.floor(s / 60), 'minute')
  if (s < 86_400) return unit(Math.floor(s / 3600), 'hour')
  return unit(Math.floor(s / 86_400), 'day')
}

// "/sdcard/DCIM/Camera/IMG_1.jpg" -> "IMG_1.jpg"
export const baseName = (path: string) => path.slice(path.lastIndexOf('/') + 1)

// Seconds since the epoch -> "7 Nov" (local time).
export function formatDay(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000)
  return `${d.getDate()} ${d.toLocaleString('en-GB', { month: 'short' })}`
}

const PREFIXES: [string, string][] = [
  ['/sdcard/Android/media/com.whatsapp/WhatsApp/Media/', 'WhatsApp/'],
  ['/sdcard/', ''],
  ['/storage/emulated/0/', ''],
]

// Where a file lives, short enough to read under a thumbnail:
// "/sdcard/DCIM/Camera/a.jpg" -> "DCIM/Camera/a.jpg"; the WhatsApp media folder becomes "WhatsApp/".
export function shortPath(path: string): string {
  for (const [from, to] of PREFIXES) if (path.startsWith(from)) return to + path.slice(from.length)
  return path
}
