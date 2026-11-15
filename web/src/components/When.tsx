import { formatFull, formatWhen } from '../lib/format'

/** A time in the one shared format ("today at 18:47"), with the full date and time on hover. */
export function When({ at }: { at: number }) {
  return (
    <time dateTime={new Date(at * 1000).toISOString()} title={formatFull(at)}>
      {formatWhen(at)}
    </time>
  )
}
