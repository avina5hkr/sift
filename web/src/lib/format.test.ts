import { expect, test } from 'vitest'
import { formatBytes, formatCount, formatDuration, formatEta, formatSpeed } from './format'

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
