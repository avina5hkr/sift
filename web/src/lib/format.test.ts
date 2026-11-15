import { describe, expect, test } from 'vitest'
import {
  formatAgo,
  formatBytes,
  formatCount,
  formatDuration,
  formatEta,
  formatFull,
  formatSpeed,
  formatWhen,
  shortPath,
} from './format'

test('formatBytes', () => {
  expect(formatBytes(0)).toBe('0 B')
  expect(formatBytes(1500)).toBe('1.5 KB')
  expect(formatBytes(41_200_000_000)).toBe('41.2 GB')
  expect(formatBytes(212_000_000_000)).toBe('212.0 GB')
})

test('formatSpeed, formatCount', () => {
  expect(formatSpeed(37_800_000)).toBe('37.8 MB/s')
  expect(formatCount(12430)).toBe('12,430')
})

test('formatEta says it the way a person would', () => {
  expect(formatEta(20)).toBe('under a minute')
  expect(formatEta(41 * 60 + 10)).toBe('about 41 min')
  expect(formatEta(3600)).toBe('about 1 h')
  expect(formatEta(2 * 3600 + 5 * 60)).toBe('about 2 h 5 min')
  expect(formatEta(Number.NaN)).toBe('')
})

test('formatDuration', () => {
  expect(formatDuration(42)).toBe('42 s')
  expect(formatDuration(125)).toBe('2 min 5 s')
  expect(formatDuration(3720)).toBe('1 h 2 min')
})

test('formatAgo says how long ago in plain words', () => {
  expect(formatAgo(1000, 1010)).toBe('just now')
  expect(formatAgo(1000, 1000 + 5 * 60)).toBe('5 minutes ago')
  expect(formatAgo(1000, 1000 + 3600)).toBe('1 hour ago')
  expect(formatAgo(1000, 1000 + 3 * 86_400)).toBe('3 days ago')
})

test('shortPath drops the storage root and shortens the WhatsApp media folder', () => {
  expect(shortPath('/sdcard/DCIM/Camera/20240101_1.jpg')).toBe('DCIM/Camera/20240101_1.jpg')
  expect(shortPath('/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-2024-1.jpg')).toBe(
    'WhatsApp/WhatsApp Images/IMG-2024-1.jpg',
  )
  expect(shortPath('/sdcard/WhatsApp/Media/WhatsApp Images/a.jpg')).toBe('WhatsApp/Media/WhatsApp Images/a.jpg')
  expect(shortPath('/sdcard/a.jpg')).toBe('a.jpg')
  expect(shortPath('/mnt/other/a.jpg')).toBe('/mnt/other/a.jpg')
})

describe('formatWhen, the one time format', () => {
  const now = new Date(2026, 10, 15, 20, 0, 0)
  const at = (y: number, m: number, d: number, hh: number, mm: number) => new Date(y, m, d, hh, mm).getTime() / 1000
  test('today, yesterday, a date this year, and another year', () => {
    expect(formatWhen(at(2026, 10, 15, 18, 47), now)).toBe('today at 18:47')
    expect(formatWhen(at(2026, 10, 14, 9, 12), now)).toBe('yesterday at 09:12')
    expect(formatWhen(at(2026, 10, 3, 18, 47), now)).toBe('3 Nov at 18:47')
    expect(formatWhen(at(2025, 10, 15, 18, 47), now)).toBe('15 Nov 2025 at 18:47')
  })
  test('the full form for hovering', () => {
    expect(formatFull(at(2026, 10, 15, 18, 47))).toMatch(/Sunday 15 November 2026, 18:47:00/)
  })
})
