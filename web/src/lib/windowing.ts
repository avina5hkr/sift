/** Windowing for a long list of rows with different heights: only the rows in view are drawn. */

/** offsets[i] is the top of row i; offsets[n] is the total height of the n known rows. */
export function buildOffsets(heights: readonly number[]): number[] {
  const offsets = new Array<number>(heights.length + 1)
  offsets[0] = 0
  for (let i = 0; i < heights.length; i++) offsets[i + 1] = (offsets[i] as number) + (heights[i] as number)
  return offsets
}

/** The first row whose bottom is below `y` (or the row count when `y` is past the end). */
export function rowAt(offsets: readonly number[], y: number): number {
  const n = offsets.length - 1
  let lo = 0
  let hi = n
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if ((offsets[mid + 1] as number) <= y) lo = mid + 1
    else hi = mid
  }
  return lo
}

/** The rows [start, end) to draw for a viewport, with `overscan` pixels drawn above and below. */
export function visibleRange(
  offsets: readonly number[],
  scrollTop: number,
  viewportHeight: number,
  overscan = 600,
): [number, number] {
  const n = offsets.length - 1
  if (n <= 0) return [0, 0]
  const start = rowAt(offsets, Math.max(0, scrollTop - overscan))
  let end = rowAt(offsets, scrollTop + viewportHeight + overscan)
  if (end < n) end += 1
  return [start, Math.min(n, end)]
}
