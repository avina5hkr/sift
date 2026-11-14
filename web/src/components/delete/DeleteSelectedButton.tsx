import { formatBytes, formatCount } from '../../lib/format'
import { ReasonButton } from '../backup/ReasonButton'

/** "Delete 12 selected (1.2 GB)", the button that starts the delete flow from a review tab. */
export function DeleteSelectedButton({ count, bytes, busy, onClick }: { count: number; bytes: number; busy: boolean; onClick: () => void }) {
  const reason = busy ? 'Starting…' : count === 0 ? 'Tick the files you want to delete first.' : null
  return (
    <ReasonButton primary placement="above" reason={reason} onClick={onClick}>
      {count > 0 ? `Delete ${formatCount(count)} selected (${formatBytes(bytes)})` : 'Delete selected'}
    </ReasonButton>
  )
}
