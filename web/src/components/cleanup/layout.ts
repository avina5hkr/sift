import type { ReviewGroup } from '../../lib/types'

// Pixel sizes of a group row. Rows are placed by arithmetic (see lib/windowing.ts), so the
// markup in GroupRow.tsx must use exactly these numbers.
export const HEADER_H = 48
export const PAD = 16
export const KEEPER_W = 240
export const KEEPER_BLOCK_H = 240 + 28 // the picture and its label
export const TILE = 160
export const GAP = 8
export const COLUMN_GAP = 16
export const BORDER = 1
/** What a row that is not loaded yet is assumed to be, for the scroll bar. */
export const ESTIMATED_ROW_H = HEADER_H + PAD * 2 + KEEPER_BLOCK_H + BORDER

export function columnsFor(width: number): number {
  const room = width - PAD * 2 - KEEPER_W - COLUMN_GAP
  return Math.max(1, Math.floor((room + GAP) / (TILE + GAP)))
}

export function rowHeight(group: ReviewGroup, cols: number): number {
  const lines = Math.max(1, Math.ceil(group.copies.length / cols))
  const grid = lines * TILE + (lines - 1) * GAP
  return HEADER_H + PAD * 2 + Math.max(KEEPER_BLOCK_H, grid) + BORDER
}

/** Where a copy's tile sits inside its row (top, for scrolling it into view). */
export function copyTop(index: number, cols: number): number {
  return HEADER_H + PAD + Math.floor(index / cols) * (TILE + GAP)
}
