import { expect, test } from 'vitest'
import { buildOffsets, rowAt, visibleRange } from './windowing'

test('offsets are running totals', () => {
  expect(buildOffsets([10, 20, 30])).toEqual([0, 10, 30, 60])
  expect(buildOffsets([])).toEqual([0])
})

test('rowAt finds the row under a position', () => {
  const o = buildOffsets([10, 20, 30])
  expect(rowAt(o, 0)).toBe(0)
  expect(rowAt(o, 9)).toBe(0)
  expect(rowAt(o, 10)).toBe(1)
  expect(rowAt(o, 59)).toBe(2)
  expect(rowAt(o, 60)).toBe(3)
})

test('a window over 10,000 rows draws only the rows in view', () => {
  const o = buildOffsets(new Array<number>(10_000).fill(300))
  const [start, end] = visibleRange(o, 150_000, 800, 600)
  expect(start).toBe(Math.floor(149_400 / 300))
  expect(end - start).toBeLessThan(10)
  expect(visibleRange(o, 0, 800, 0)[0]).toBe(0)
  expect(visibleRange([0], 0, 800)).toEqual([0, 0])
})
