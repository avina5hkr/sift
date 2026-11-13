import { formatCount } from '../../lib/format'
import type { TaskEvent, TaskKind } from '../../lib/types'

const LABEL: Record<TaskKind, string> = {
  verify: 'Checking your backups',
  adopt: 'Adopting your existing backup',
  rebuild: 'Rebuilding the backup list',
  confirm: 'Confirming a set',
  analyse: 'Analysing the phone',
}
const ENDED = { done: 'finished', stopped: 'stopped', failed: 'could not finish' }

type Props = { task: TaskEvent; stopping: boolean; onStop: () => void }

/** One line for the long task: what it is doing, how far, and Stop. After it ends, how it ended. */
export function TaskLine({ task, stopping, onStop }: Props) {
  if (task.state !== 'running') {
    return (
      <p role="status" className="text-sm text-neutral-700 dark:text-neutral-300">
        {LABEL[task.kind]} {ENDED[task.state]}.{task.message ? ` ${task.message}` : ''}
      </p>
    )
  }
  return (
    <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-neutral-200 px-4 py-2 text-sm dark:border-neutral-800">
      <span className="min-w-0 flex-1">
        {LABEL[task.kind]}
        {task.total > 0 && ` · ${formatCount(task.done)} of ${formatCount(task.total)}`}
        {task.message && <span className="text-neutral-600 dark:text-neutral-400">{` · ${task.message}`}</span>}
      </span>
      <button
        type="button"
        onClick={onStop}
        disabled={stopping}
        className="rounded-md border border-neutral-300 px-3 py-1 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800"
      >
        Stop
      </button>
    </div>
  )
}
