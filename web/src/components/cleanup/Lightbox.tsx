import { useEffect, useRef } from 'react'
import { withToken } from '../../lib/api'
import { baseName, formatBytes } from '../../lib/format'
import type { ReviewItem } from '../../lib/types'

const ORIGINS: Record<string, string> = {
  camera: 'Camera',
  screenshot: 'Screenshots',
  whatsapp_received: 'WhatsApp, received',
  whatsapp_sent: 'WhatsApp, sent',
  whatsapp_private: 'WhatsApp, private',
  other: 'Other',
}

type Props = {
  items: ReviewItem[]
  index: number
  checked: boolean
  /** The content hash of the group an item belongs to. */
  hashOf: (item: ReviewItem) => string
  onIndex: (index: number) => void
  onClose: () => void
  /** Keep this copy: untick it. */
  onKeep: (item: ReviewItem) => void
}

/** A large view of one file with its details. Left and right go through the list, Esc closes. */
export function Lightbox({ items, index, checked, hashOf, onIndex, onClose, onKeep }: Props) {
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
  const name = baseName(item.path)
  const resolution = item.width && item.height ? `${item.width} × ${item.height}` : 'Not known'
  const when = new Date(item.mtime * 1000).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })
  const isKeeper = item.role === 'keeper'

  return (
    <div role="dialog" aria-modal="true" aria-label={name} className="fixed inset-0 z-40 flex bg-black/85 text-white">
      <div className="relative flex min-w-0 flex-1 items-center justify-center p-4">
        {item.kind === 'video' ? (
          <video key={item.id} src={withToken(`/api/media/${item.id}/file`)} controls autoPlay className="max-h-full max-w-full" />
        ) : (
          <img key={item.id} src={withToken(`/api/media/${item.id}/preview?max=1600`)} alt={name} className="max-h-full max-w-full object-contain" />
        )}
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
      <aside className="flex w-80 shrink-0 flex-col gap-3 overflow-auto bg-neutral-900 p-5 text-sm">
        <div className="flex items-start gap-2">
          <h2 className="min-w-0 flex-1 text-base font-semibold break-words">{name}</h2>
          <button ref={closeRef} type="button" onClick={onClose} className="rounded-md border border-neutral-600 px-2 py-1 hover:bg-neutral-800">
            Close
          </button>
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-2">
          <dt className="text-neutral-400">Phone path</dt>
          <dd className="break-all">{item.path}</dd>
          <dt className="text-neutral-400">Size</dt>
          <dd>{formatBytes(item.size)}</dd>
          <dt className="text-neutral-400">Resolution</dt>
          <dd>{resolution}</dd>
          <dt className="text-neutral-400">Date</dt>
          <dd>{when}</dd>
          <dt className="text-neutral-400">From</dt>
          <dd>{ORIGINS[item.origin] ?? 'Other'}</dd>
          <dt className="text-neutral-400">Fingerprint</dt>
          <dd className="font-mono">{hashOf(item).slice(0, 12)}</dd>
        </dl>
        {isKeeper ? (
          <p className="rounded-md bg-green-900 px-3 py-2">This is the copy Sift keeps.</p>
        ) : (
          <button
            type="button"
            disabled={!checked}
            onClick={() => onKeep(item)}
            className="rounded-md border border-neutral-500 px-3 py-2 font-medium hover:bg-neutral-800 disabled:opacity-60"
          >
            {checked ? 'Keep this' : 'Keeping this'}
          </button>
        )}
        <p className="text-neutral-400">
          {index + 1} of {items.length}
        </p>
      </aside>
    </div>
  )
}
