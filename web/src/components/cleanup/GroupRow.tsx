import { memo } from 'react'
import { baseName, formatBytes } from '../../lib/format'
import type { ReviewGroup, ReviewItem } from '../../lib/types'
import { COLUMN_GAP, GAP, HEADER_H, KEEPER_W, PAD, TILE } from './layout'
import { Tile } from './Tile'

type Props = {
  group: ReviewGroup
  cols: number
  top: number
  height: number
  /** Which copies are ticked; the row redraws when this changes. */
  checked: (item: ReviewItem) => boolean
  focusId: number | null
  onToggle: (item: ReviewItem, shift: boolean) => void
  onOpen: (item: ReviewItem) => void
  onSelectCopies: (group: ReviewGroup, checked: boolean) => void
}

/** One group of identical files: the keeper on the left, the copies as a grid, a header. */
export const GroupRow = memo(function GroupRow({ group, cols, top, height, checked, focusId, onToggle, onOpen, onSelectCopies }: Props) {
  const keeper = group.keeper
  if (!keeper) return null // the Junk tabs have their own list
  const n = group.copies.length
  const allChecked = n > 0 && group.copies.every((c) => checked(c))
  return (
    <section
      aria-label={`Group: ${baseName(keeper.path)}`}
      data-testid="group"
      className="absolute right-0 left-0 border-b border-neutral-200 dark:border-neutral-800"
      style={{ top, height }}
    >
      <header className="flex items-center gap-3 px-4" style={{ height: HEADER_H }}>
        <h3 className="min-w-0 flex-1 truncate text-sm font-medium">
          {n} cop{n === 1 ? 'y' : 'ies'}
          <span className="font-normal text-neutral-600 dark:text-neutral-400"> · {formatBytes(group.reclaimable)} to free</span>
        </h3>
        <button
          type="button"
          tabIndex={-1}
          onClick={() => onSelectCopies(group, !allChecked)}
          className="rounded-md border border-neutral-300 px-2.5 py-1 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          {allChecked ? 'Unselect copies' : 'Select all copies'}
        </button>
      </header>
      <div className="flex" style={{ paddingLeft: PAD, paddingRight: PAD, gap: COLUMN_GAP }}>
        <div className="shrink-0" style={{ width: KEEPER_W }}>
          <Tile item={keeper} checked={false} focused={focusId === keeper.id} keeper onOpen={onOpen} />
          <p className="truncate pt-1 text-xs text-neutral-600 dark:text-neutral-400" style={{ height: 28 }} title={keeper.path}>
            {baseName(keeper.path)}
          </p>
        </div>
        <div className="grid content-start" style={{ gridTemplateColumns: `repeat(${cols}, ${TILE}px)`, gap: GAP }}>
          {group.copies.map((c) => (
            <Tile key={c.id} item={c} checked={checked(c)} focused={focusId === c.id} onToggle={onToggle} onOpen={onOpen} />
          ))}
        </div>
      </div>
    </section>
  )
})
