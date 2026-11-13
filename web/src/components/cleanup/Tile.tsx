import { memo } from 'react'
import { withToken } from '../../lib/api'
import { baseName, formatBytes } from '../../lib/format'
import type { ReviewItem } from '../../lib/types'
import { TILE } from './layout'

type Props = {
  item: ReviewItem
  checked: boolean
  focused: boolean
  /** The keeper is shown larger, labelled KEEP, and has no checkbox. */
  keeper?: boolean
  onToggle?: (item: ReviewItem, shift: boolean) => void
  onOpen: (item: ReviewItem) => void
}

function Picture({ item, size }: { item: ReviewItem; size: number }) {
  if (item.kind === 'video') {
    return (
      <div
        aria-hidden="true"
        className="flex items-center justify-center bg-neutral-800 text-3xl text-neutral-300"
        style={{ width: size, height: size }}
      >
        ▶
      </div>
    )
  }
  return (
    <img
      src={withToken(`/api/media/${item.id}/thumb`)}
      alt=""
      loading="lazy"
      decoding="async"
      draggable={false}
      className="object-cover"
      style={{ width: size, height: size }}
    />
  )
}

/** One file: its picture (a plain tile for a video), a size badge and, for a copy, a checkbox. */
export const Tile = memo(function Tile({ item, checked, focused, keeper = false, onToggle, onOpen }: Props) {
  const size = keeper ? 240 : TILE
  const name = baseName(item.path)
  return (
    <div
      id={`tile-${item.id}`}
      data-testid={keeper ? 'keeper' : 'copy'}
      className={`relative shrink-0 overflow-hidden rounded-md border-2 bg-neutral-100 dark:bg-neutral-900 ${
        checked ? 'border-blue-600 dark:border-blue-400' : 'border-transparent'
      } ${focused ? 'outline-2 outline-offset-2 outline-[var(--ring)]' : ''}`}
      style={{ width: size, height: size }}
    >
      <button
        type="button"
        tabIndex={-1}
        aria-label={`Open ${name}`}
        onClick={() => onOpen(item)}
        className="block cursor-zoom-in p-0"
      >
        <Picture item={item} size={size - 4} />
      </button>
      {keeper ? (
        <span className="absolute top-2 left-2 rounded bg-green-700 px-2 py-0.5 text-xs font-bold tracking-wide text-white">KEEP</span>
      ) : (
        <button
          type="button"
          role="checkbox"
          aria-checked={checked}
          aria-label={`Select ${name}`}
          tabIndex={-1}
          onClick={(e) => onToggle?.(item, e.shiftKey)}
          className={`absolute top-1.5 left-1.5 flex h-6 w-6 items-center justify-center rounded border-2 text-sm leading-none font-bold ${
            checked ? 'border-blue-600 bg-blue-600 text-white' : 'border-white bg-black/40 text-transparent'
          }`}
        >
          ✓
        </button>
      )}
      <span className="pointer-events-none absolute right-1.5 bottom-1.5 flex gap-1">
        {item.kind === 'video' && <span className="rounded bg-black/70 px-1.5 py-0.5 text-xs font-semibold text-white">VIDEO</span>}
        <span className="rounded bg-black/70 px-1.5 py-0.5 text-xs text-white">{formatBytes(item.size)}</span>
      </span>
    </div>
  )
})
