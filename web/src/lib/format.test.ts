import { expect, test } from 'vitest'
import { formatBytes } from './format'

test('formatBytes', () => {
  expect(formatBytes(0)).toBe('0 B')
  expect(formatBytes(1500)).toBe('1.5 KB')
  expect(formatBytes(41_200_000_000)).toBe('41.2 GB')
  expect(formatBytes(212_000_000_000)).toBe('212.0 GB')
})
