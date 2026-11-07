import { formatBytes, formatCount, formatDuration } from '../../lib/format'
import type { BackupSummary, JobEvent } from '../../lib/types'

type Props = {
  /** The run that just ended in this window, if any; otherwise the saved summary alone. */
  job: JobEvent | null
  summary: BackupSummary | null
  onOpenFolder: () => void
  openError: string | null
}

function heading(job: JobEvent | null, summary: BackupSummary | null): string {
  if (job?.state === 'failed' || summary?.outcome === 'failed') return 'The backup could not finish'
  if (summary?.outcome === 'discarded') return 'Nothing was copied'
  if (job?.stopped || summary?.outcome === 'stopped') return 'Backup stopped'
  return 'Backup finished'
}

/**
 * What a run did. With the run that just ended in this window, numbers come from its saved
 * summary, else from the job itself. Without one (after a reload) the saved summary alone.
 */
export function SummaryCard({ job, summary, onOpenFolder, openError }: Props) {
  const mine = summary && (!job || summary.job_id === job.job_id) ? summary : null
  if (!job && !mine) return null
  const stats: [string, string][] = [
    ['New files', formatCount(mine?.new_files ?? job?.files_done ?? 0)],
    ['Duplicates skipped', formatCount(mine?.duplicates_skipped ?? job?.duplicates_skipped ?? 0)],
    ['Failed', formatCount(mine?.failed ?? job?.failed ?? 0)],
    ['Copied', formatBytes(mine?.bytes ?? job?.bytes_done ?? 0)],
    [
      'Time taken',
      formatDuration(mine?.duration_s ?? Math.max(0, (job?.finished_at ?? 0) - (job?.started_at ?? 0))),
    ],
  ]
  const problem = job?.error ?? mine?.error ?? null
  return (
    <section
      aria-labelledby="summary-title"
      className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800"
    >
      <h2 id="summary-title" className="text-lg font-semibold">
        {heading(job, mine)}
      </h2>
      {mine?.outcome === 'discarded' && (
        <p className="mt-1 text-sm">You chose not to go ahead, so nothing changed.</p>
      )}
      {problem && (
        <p role="alert" className="mt-1 text-sm font-medium">
          {problem}
        </p>
      )}
      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-5">
        {stats.map(([k, v]) => (
          <div key={k}>
            <dt className="text-neutral-600 dark:text-neutral-400">{k}</dt>
            <dd className="font-medium">{v}</dd>
          </div>
        ))}
      </dl>
      {mine && mine.warnings.length > 0 && (
        <ul className="mt-3 list-disc pl-5 text-sm">
          {mine.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={onOpenFolder}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          Open backup folder
        </button>
        {openError && (
          <p role="alert" className="text-sm">
            {openError}
          </p>
        )}
      </div>
    </section>
  )
}
