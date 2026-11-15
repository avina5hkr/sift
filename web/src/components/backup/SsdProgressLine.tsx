import { formatBytes, formatCount, formatSpeed } from '../../lib/format'
import type { SsdProgress } from '../../lib/types'
import { ProgressBar } from './ProgressBar'

/** What the SSD copy is doing, in words; colour is never the only signal. */
export function ssdStateText(ssd: SsdProgress): string {
  switch (ssd.state) {
    case 'waiting_drive':
      return 'Waiting for another copy to the SSD to finish'
    case 'starting':
      return 'Starting'
    case 'waiting_access':
      return 'Waiting for SSD access: check for a macOS permission prompt'
    case 'copying':
      return 'Copying'
    case 'caught_up':
      return 'Up to date with the Mac copy; waiting for the next file'
    case 'paused':
      return 'Paused'
    case 'not_answering':
      return 'The SSD is not answering'
    case 'stopped':
      return 'Stopped; the rest is copied next time'
    case 'done':
      return 'Done'
    case 'incomplete':
      return ssd.reason ?? 'Not everything was copied; the rest is copied next time'
    case 'unplugged':
      return 'The SSD was unplugged; the rest is copied next time'
    case 'full':
      return 'The SSD is full'
    case 'other_drive':
      return 'A different drive is connected under this name; nothing was written to it'
    case 'busy':
      return 'The SSD is busy: an earlier copy is not answering. Quit and reopen Sift to clear it.'
    case 'failed':
      return ssd.reason ?? 'The copy to the SSD stopped because of an error'
  }
}

/** The second bar, "Mac → SSD": its totals grow while the pull adds files ("so far"). */
export function SsdProgressLine({ ssd, pullDone }: { ssd: SsdProgress; pullDone: boolean }) {
  const known = Math.max(ssd.known_files, ssd.files_done)
  const value = known > 0 ? (ssd.files_done / known) * 100 : ssd.state === 'starting' ? null : 0
  const earlier = ssd.backlog_files > 0 ? ` (including ${formatCount(ssd.backlog_files)} from earlier runs)` : ''
  return (
    <div className="flex flex-col gap-1" data-testid="ssd-progress">
      <div className="flex items-center gap-3 text-sm">
        <span className="w-24 shrink-0">Mac → SSD</span>
        <ProgressBar label="Mac → SSD" value={value} />
        <span className="shrink-0 tabular-nums">
          {formatCount(ssd.files_done)} of {formatCount(known)}
          {!pullDone && ' so far'}
        </span>
      </div>
      <p className="text-sm text-neutral-700 dark:text-neutral-300">
        {ssdStateText(ssd)}
        {earlier}
        {ssd.speed_bps != null && ssd.state === 'copying' && `, ${formatSpeed(ssd.speed_bps)}`}
        {ssd.deferred > 0 && `. ${formatCount(ssd.deferred)} could not be copied this time.`}
      </p>
      {ssd.current && ssd.state === 'copying' && (
        <p className="flex gap-3 text-sm">
          <span className="min-w-0 flex-1 font-mono break-all">{ssd.current}</span>
          <span className="shrink-0 text-neutral-600 dark:text-neutral-400">{formatBytes(ssd.current_size)}</span>
        </p>
      )}
    </div>
  )
}
