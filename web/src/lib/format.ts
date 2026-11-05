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
