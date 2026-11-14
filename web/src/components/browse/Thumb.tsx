import { useState } from 'react'
import { withToken } from '../../lib/api'
import type { BrowseFile } from '../../lib/types'

const BOX = 'flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded bg-neutral-100 text-xs text-neutral-600 dark:bg-neutral-900 dark:text-neutral-400'

/** A small picture for a photo (a plain tile for a video or anything else, and for a photo with no backup copy yet). */
export function Thumb({ file }: { file: Pick<BrowseFile, 'id' | 'kind'> }) {
  const [broken, setBroken] = useState(false)
  if (file.kind === 'image' && !broken) {
    return (
      <span className={BOX}>
        <img
          src={withToken(`/api/media/${file.id}/thumb`)}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          onError={() => setBroken(true)}
          className="h-9 w-9 object-cover"
        />
      </span>
    )
  }
  return (
    <span aria-hidden="true" className={BOX}>
      {file.kind === 'video' ? '▶' : ''}
    </span>
  )
}
