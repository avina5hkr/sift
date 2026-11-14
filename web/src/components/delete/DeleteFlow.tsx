import { formatBytes, formatCount } from '../../lib/format'
import type { DeleteItem, DeleteRun } from '../../lib/types'
import { ConfirmDeleteDialog } from './ConfirmDeleteDialog'
import { Modal } from './Modal'
import type { DeleteRunApi } from './useDeleteRun'

const BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800'
const PRIMARY =
  'rounded-md border border-neutral-900 bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300'
const MUTED = 'text-neutral-600 dark:text-neutral-400'
const SHOWN = 200
const OUTDATED = ['stale_preflight', 'selection_changed']

const plural = (n: number, one: string, many: string) => `${formatCount(n)} ${n === 1 ? one : many}`

function Progress({ flow, title, label, counts }: { flow: DeleteRunApi; title: string; label: string; counts?: boolean }) {
  const { task } = flow
  return (
    <Modal label={label}>
      <h2 className="text-lg font-semibold" role="status">
        {title}
      </h2>
      {counts && task && task.total > 0 && (
        <p className={`text-sm ${MUTED}`}>
          {formatCount(task.done)} of {formatCount(task.total)}
        </p>
      )}
      {flow.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {flow.error}
        </p>
      )}
      <div className="flex justify-end">
        <button type="button" data-autofocus onClick={() => void flow.stop()} disabled={flow.stopping} className={BTN}>
          Stop
        </button>
      </div>
    </Modal>
  )
}

/** The files that did not pass, grouped by why, each group a collapsed list of paths. */
function Failed({ items }: { items: DeleteItem[] }) {
  const groups = new Map<string, { text: string; paths: string[] }>()
  for (const i of items) {
    if (i.check === 'ok') continue
    const g = groups.get(i.check) ?? { text: i.reason ?? i.check, paths: [] }
    g.paths.push(i.path)
    groups.set(i.check, g)
  }
  if (groups.size === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <h3 className="text-sm font-medium">Left out</h3>
      {[...groups.entries()].map(([code, g]) => (
        <details key={code} className="text-sm">
          <summary className="cursor-pointer">
            {g.text} ({formatCount(g.paths.length)})
          </summary>
          <ul className={`mt-1 max-h-48 list-none overflow-auto break-all pl-4 text-xs ${MUTED}`}>
            {g.paths.slice(0, SHOWN).map((p) => (
              <li key={p}>{p}</li>
            ))}
            {g.paths.length > SHOWN && <li>and {formatCount(g.paths.length - SHOWN)} more</li>}
          </ul>
        </details>
      ))}
    </div>
  )
}

function Result({ flow, run }: { flow: DeleteRunApi; run: DeleteRun }) {
  const canDelete = run.state === 'ready' && run.passed > 0
  let title: string
  if (canDelete) title = `${plural(run.passed, 'file', 'files')} can be deleted (${formatBytes(run.passed_bytes)})`
  else if (flow.checksFailed) title = 'The checks could not finish.'
  else if (run.state === 'stopped') title = 'The checks were stopped.'
  else title = run.blocked_reason ?? 'None of these files can be deleted right now.'
  return (
    <Modal label="Result of the checks" onClose={flow.close}>
      <h2 className="text-lg font-semibold">{title}</h2>
      {canDelete && run.items.some((i) => i.check !== 'ok') && (
        <p className={`text-sm ${MUTED}`}>The other files will be left alone.</p>
      )}
      <Failed items={run.items} />
      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={flow.close} className={BTN}>
          {canDelete ? 'Cancel' : 'Close'}
        </button>
        {canDelete && (
          <button type="button" data-autofocus onClick={flow.openConfirm} className={PRIMARY}>
            Continue to delete
          </button>
        )}
      </div>
    </Modal>
  )
}

function Finished({ flow, run }: { flow: DeleteRunApi; run: DeleteRun }) {
  const failures = run.items.filter((i) => i.error)
  const left = run.items.filter((i) => i.check === 'ok' && (i.state === 'gone' || i.state === 'changed')).length
  let title = 'Done'
  if (run.state === 'stopped') title = 'Stopped'
  else if (run.state === 'blocked') title = run.blocked_reason ?? 'Nothing was deleted.'
  else if (run.state !== 'done') title = flow.task?.message || 'The delete could not finish.'
  return (
    <Modal label="Result of the delete" onClose={flow.close}>
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="text-sm">
        {plural(run.deleted, 'file', 'files')} deleted. {formatBytes(run.bytes_freed)} freed.
      </p>
      {left > 0 && (
        <p className={`text-sm ${MUTED}`}>
          {plural(left, 'file was', 'files were')} left alone because {left === 1 ? 'it' : 'they'} changed or {left === 1 ? 'was' : 'were'} already gone.
        </p>
      )}
      {run.failed > 0 && (
        <div className="flex flex-col gap-1">
          <h3 className="text-sm font-medium">{plural(run.failed, 'file', 'files')} could not be deleted</h3>
          <ul className={`max-h-48 list-none overflow-auto break-all text-xs ${MUTED}`}>
            {failures.slice(0, SHOWN).map((i) => (
              <li key={i.id}>
                {i.path}: {i.error}
              </li>
            ))}
          </ul>
        </div>
      )}
      {run.manifests.map((m) => (
        <p key={m} className="break-all text-sm">
          The list of deleted files is saved at {m}
        </p>
      ))}
      <div className="flex justify-end">
        <button type="button" data-autofocus onClick={flow.close} className={PRIMARY}>
          Close
        </button>
      </div>
    </Modal>
  )
}

/** The screens of a delete flow, one at a time over the page. Shows nothing until a flow starts. */
export function DeleteFlow({ flow }: { flow: DeleteRunApi }) {
  const { phase, run } = flow
  if (phase === 'idle') return null
  if (phase === 'error') {
    return (
      <Modal label="Delete from the phone" onClose={flow.close}>
        <p role="alert" className="text-sm">
          {flow.error}
        </p>
        <div className="flex justify-end">
          <button type="button" data-autofocus onClick={flow.close} className={PRIMARY}>
            Close
          </button>
        </div>
      </Modal>
    )
  }
  if (phase === 'checking') {
    const title = flow.total > 0 ? `Checking ${plural(flow.total, 'file', 'files')}…` : 'Checking the files…'
    return <Progress flow={flow} title={title} label="Checking the files" counts />
  }
  if (phase === 'deleting') {
    return <Progress flow={flow} title={`Deleting ${formatCount(flow.task?.done ?? 0)} of ${formatCount(flow.total)}`} label="Deleting" />
  }
  if (!run) return null
  if (phase === 'finished') return <Finished flow={flow} run={run} />
  if (flow.confirming && run.state === 'ready') {
    return (
      <ConfirmDeleteDialog
        run={run}
        busy={flow.executing}
        error={flow.error}
        outdated={flow.errorCode != null && OUTDATED.includes(flow.errorCode)}
        onConfirm={(typed) => void flow.execute(typed)}
        onCancel={flow.closeConfirm}
        onRecheck={flow.recheck}
      />
    )
  }
  return <Result flow={flow} run={run} />
}
