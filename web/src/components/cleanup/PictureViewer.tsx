import { useEffect, useRef } from 'react'
import { withToken } from '../../lib/api'

export type ViewItem = { id: number; name: string }

type Props = { items: ViewItem[]; index: number; onIndex: (index: number) => void; onClose: () => void }

/** A large view of one picture. Left and right go through the list, Esc closes. */
export function PictureViewer({ items, index, onIndex, onClose }: Props) {
  const item = items[index]
  const closeRef = useRef<HTMLButtonElement>(null)
  const last = items.length - 1

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowLeft') onIndex(Math.max(0, index - 1))
      else if (e.key === 'ArrowRight') onIndex(Math.min(last, index + 1))
      else return
      e.preventDefault()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [index, last, onIndex, onClose])

  useEffect(() => closeRef.current?.focus(), [])

  if (!item) return null
  return (
    <div role="dialog" aria-modal="true" aria-label={item.name} className="fixed inset-0 z-40 flex flex-col bg-black/85 text-white">
      <div className="flex items-center gap-3 p-3 text-sm">
        <span className="min-w-0 flex-1 truncate">{item.name}</span>
        <span className="text-neutral-400">
          {index + 1} of {items.length}
        </span>
        <button ref={closeRef} type="button" onClick={onClose} className="rounded-md border border-neutral-600 px-2 py-1 hover:bg-neutral-800">
          Close
        </button>
      </div>
      <div className="relative flex min-h-0 flex-1 items-center justify-center p-4">
        <img key={item.id} src={withToken(`/api/media/${item.id}/preview?max=1600`)} alt={item.name} className="max-h-full max-w-full object-contain" />
        <button
          type="button"
          aria-label="Previous"
          disabled={index === 0}
          onClick={() => onIndex(index - 1)}
          className="absolute top-1/2 left-3 h-10 w-10 -translate-y-1/2 rounded-full bg-black/60 text-xl disabled:opacity-30"
        >
          ‹
        </button>
        <button
          type="button"
          aria-label="Next"
          disabled={index === last}
          onClick={() => onIndex(index + 1)}
          className="absolute top-1/2 right-3 h-10 w-10 -translate-y-1/2 rounded-full bg-black/60 text-xl disabled:opacity-30"
        >
          ›
        </button>
      </div>
    </div>
  )
}
