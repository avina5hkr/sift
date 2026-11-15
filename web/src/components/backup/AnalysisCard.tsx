import { useState } from 'react'
import { Link } from 'react-router'
import { formatBytes, formatCount, formatEta } from '../../lib/format'
import { useStatus } from '../../lib/useStatus'
import type { AnalyseGroup, AnalyseSet, Analysis, AnalysisResult, LatestAnalyses } from '../../lib/types'
import { DeleteFlow } from '../delete/DeleteFlow'
import { useDeleteRun } from '../delete/useDeleteRun'
import { When } from '../When'

type Key = 'verified' | 'mac_only' | 'ssd_only' | 'not_verified' | 'unchecked' | 'not_backed_up' | 'problems'

// In the order the stacked bar draws them. Colour is never the only signal: each has words.
const GROUPS: { key: Key; label: string; bar: string }[] = [
  { key: 'verified', label: 'Backed up and verified', bar: 'bg-green-600' },
  { key: 'mac_only', label: 'On the Mac, not on the SSD yet', bar: 'bg-teal-500' },
  { key: 'ssd_only', label: 'On the SSD, not on the Mac yet', bar: 'bg-teal-300' },
  { key: 'not_verified', label: 'Not verified yet', bar: 'bg-sky-500' },
  { key: 'unchecked', label: 'Not checked yet', bar: 'bg-neutral-400 dark:bg-neutral-500' },
  { key: 'not_backed_up', label: 'Not backed up', bar: 'bg-amber-500' },
  { key: 'problems', label: 'Problems', bar: 'bg-red-600' },
]

const EMPTY: AnalyseGroup = { files: 0, bytes: 0 }
const group = (set: AnalyseSet, key: Key): AnalyseGroup => set[key] ?? EMPTY
const count = (g: AnalyseGroup) => `${formatCount(g.files)} ${g.files === 1 ? 'file' : 'files'}, ${formatBytes(g.bytes)}`
const OPEN_KEY = 'sift.analysis.details'

function sum(sets: AnalyseSet[]): AnalyseSet {
  const total = (k: Key) =>
    sets.reduce((g, s) => ({ files: g.files + group(s, k).files, bytes: g.bytes + group(s, k).bytes }), EMPTY)
  return {
    verified: total('verified'),
    mac_only: total('mac_only'),
    ssd_only: total('ssd_only'),
    not_verified: total('not_verified'),
    unchecked: total('unchecked'),
    not_backed_up: total('not_backed_up'),
    problems: total('problems'),
  }
}

type Badge = { rank: number; icon: string; text: string; cls: string }

/** What needs attention first, in words with an icon. */
function badge(set: AnalyseSet): Badge {
  const n = (k: Key) => group(set, k).files
  if (n('problems'))
    return { rank: 0, icon: '✕', text: `${formatCount(n('problems'))} with problems`, cls: 'bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200' }
  if (n('not_backed_up'))
    return { rank: 1, icon: '!', text: `${formatCount(n('not_backed_up'))} not backed up`, cls: 'bg-amber-100 text-amber-950 dark:bg-amber-950 dark:text-amber-100' }
  if (n('mac_only') || n('ssd_only')) {
    const where = n('mac_only') ? 'not on the SSD yet' : 'not on the Mac yet'
    return { rank: 2, icon: '◑', text: `${formatCount(n('mac_only') + n('ssd_only'))} ${where}`, cls: 'bg-teal-100 text-teal-950 dark:bg-teal-950 dark:text-teal-100' }
  }
  if (n('unchecked'))
    return { rank: 3, icon: '…', text: `${formatCount(n('unchecked'))} not checked yet`, cls: 'bg-neutral-200 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100' }
  if (n('not_verified'))
    return { rank: 4, icon: '◐', text: `${formatCount(n('not_verified'))} not verified yet`, cls: 'bg-sky-100 text-sky-950 dark:bg-sky-950 dark:text-sky-100' }
  if (n('verified')) return { rank: 5, icon: '✓', text: 'All backed up', cls: 'bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-200' }
  return { rank: 6, icon: '–', text: 'No files', cls: 'bg-neutral-100 text-neutral-700 dark:bg-neutral-900 dark:text-neutral-300' }
}

function BadgeChip({ set }: { set: AnalyseSet }) {
  const b = badge(set)
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs font-semibold ${b.cls}`}>
      <span aria-hidden="true">{b.icon}</span>
      {b.text}
    </span>
  )
}

/** One bar split by group; its text label says the same in words. */
function StackedBar({ set, label }: { set: AnalyseSet; label: string }) {
  const total = GROUPS.reduce((n, g) => n + group(set, g.key).files, 0)
  const words = GROUPS.filter((g) => group(set, g.key).files > 0)
    .map((g) => `${g.label}: ${formatCount(group(set, g.key).files)}`)
    .join(', ')
  return (
    <div role="img" aria-label={`${label}. ${words || 'No files'}`} className="flex h-2.5 min-w-24 flex-1 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
      {total > 0 &&
        GROUPS.map((g) => {
          const files = group(set, g.key).files
          return files > 0 ? <div key={g.key} className={g.bar} style={{ width: `${(files / total) * 100}%` }} /> : null
        })}
    </div>
  )
}

function SetRows({ result }: { result: AnalysisResult }) {
  const rows = Object.entries(result.sets).sort(
    ([a, x], [b, y]) => badge(x).rank - badge(y).rank || a.localeCompare(b),
  )
  return (
    <ul className="mt-3 flex flex-col gap-1">
      {rows.map(([name, set]) => (
        <li key={name}>
          <details className="group rounded-md px-2 py-1 hover:bg-neutral-50 dark:hover:bg-neutral-900">
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-3">
              <span className="w-40 min-w-0 font-medium">{name}</span>
              <StackedBar set={set} label={name} />
              <BadgeChip set={set} />
            </summary>
            <dl className="mt-1 grid grid-cols-1 gap-x-6 gap-y-1 pl-2 text-sm sm:grid-cols-2">
              {GROUPS.map((g) => (
                <div key={g.key}>
                  <dt className="text-neutral-600 dark:text-neutral-400">{g.label}</dt>
                  <dd className="font-medium">{count(group(set, g.key))}</dd>
                </div>
              ))}
            </dl>
          </details>
        </li>
      ))}
    </ul>
  )
}

function Coverage({ result }: { result: AnalysisResult }) {
  const parts: string[] = []
  if (result.left_out_sets?.length) parts.push(`Not in this check: ${result.left_out_sets.join(', ')}.`)
  if (result.skipped_by_rule) parts.push(`${formatCount(result.skipped_by_rule)} files left out by the skip rules.`)
  if (result.unreadable?.length) parts.push(`Could not read: ${result.unreadable.join(', ')}.`)
  if (!parts.length) return null
  return <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">{parts.join(' ')}</p>
}

function readOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) !== 'closed'
  } catch {
    return true
  }
}

function ActiveRun({ run }: { run: Analysis }) {
  const sofar = run.partial ? sum(Object.values(run.partial.sets)) : null
  return (
    <div className="mt-3 rounded-md border border-dashed border-neutral-300 p-3 dark:border-neutral-700" data-testid="analysis-active">
      <p className="text-sm font-medium">
        Checking now
        {run.processed != null && run.total != null && `: ${formatCount(run.processed)} of ${formatCount(run.total)}`}
        {run.started_at != null && (
          <>
            {' · started '}
            <When at={run.started_at} />
          </>
        )}
      </p>
      {sofar && (
        <>
          <div className="mt-2 flex items-center gap-3">
            <StackedBar set={sofar} label="So far" />
          </div>
          <p className="mt-1 text-sm text-neutral-700 dark:text-neutral-300">
            So far: {formatCount(group(sofar, 'verified').files)} backed up and verified,{' '}
            {formatCount(group(sofar, 'not_backed_up').files)} not backed up, {formatCount(group(sofar, 'unchecked').files)} not
            checked yet.
          </p>
        </>
      )}
    </div>
  )
}

function EndedRun({ run }: { run: Analysis }) {
  return (
    <p className="mt-3 text-sm" data-testid="analysis-ended">
      {run.state === 'stopped'
        ? `Stopped${run.processed != null && run.total != null ? ` at ${formatCount(run.processed)} of ${formatCount(run.total)}` : ''}`
        : `The check failed${run.error ? `: ${run.error}` : '.'}`}
      {run.finished_at != null && (
        <>
          {' · '}
          <When at={run.finished_at} />
        </>
      )}
      . The last complete result below is unchanged.
    </p>
  )
}

/**
 * What Analyse phone found. The last complete result keeps its own block ("Checked at …"); a run
 * going on now shows apart from it, with counts "so far" and never a percentage; a stopped or
 * failed run never replaces it. Read-only: nothing here changes the phone or the backup.
 */
export function AnalysisCard({ latest }: { latest: LatestAnalyses }) {
  const { completed, active, ended, stale_reason } = latest
  const result = completed?.result ?? null
  const flow = useDeleteRun()
  const hasSsd = useStatus().data?.ssd != null
  const [later, setLater] = useState(false)
  const [open, setOpen] = useState(readOpen)
  const toggle = () => {
    setOpen((o) => {
      try {
        localStorage.setItem(OPEN_KEY, o ? 'closed' : 'open')
      } catch {
        /* a private window: the choice is not remembered */
      }
      return !o
    })
  }
  const totals = result ? sum(Object.values(result.sets)) : null
  const verifiedBytes = totals ? totals.verified.bytes : 0
  const allFiles = totals ? GROUPS.reduce((n, g) => n + group(totals, g.key).files, 0) : 0
  return (
    <section aria-labelledby="analysis-title" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id="analysis-title" className="min-w-0 flex-1 text-lg font-semibold">
          Files in your selected backup folders
        </h2>
        {result && (
          <button
            type="button"
            aria-expanded={open}
            onClick={toggle}
            className="rounded-md border border-neutral-300 px-3 py-1 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            {open ? 'Hide details' : 'Show details'}
          </button>
        )}
      </div>
      {active && <ActiveRun run={active} />}
      {ended && <EndedRun run={ended} />}
      {!completed && !active && !ended && <p className="mt-1 text-sm">No check has finished yet.</p>}
      {completed && result && totals && (
        <div className="mt-3" data-testid="analysis-completed">
          <p className="text-sm text-neutral-700 dark:text-neutral-300">
            Checked {completed.finished_at != null ? <When at={completed.finished_at} /> : 'earlier'}
          </p>
          {stale_reason && (
            <p role="note" className="mt-1 text-sm font-medium text-amber-800 dark:text-amber-300">
              {stale_reason}
            </p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <StackedBar set={totals} label="All checked files" />
            <BadgeChip set={totals} />
          </div>
          <p className="mt-1 text-sm">
            {formatCount(totals.verified.files)} of {formatCount(allFiles)} files backed up and verified
            {group(totals, 'mac_only').files > 0 && ` · ${formatCount(group(totals, 'mac_only').files)} on the Mac, not on the SSD yet`}
            {group(totals, 'ssd_only').files > 0 && ` · ${formatCount(group(totals, 'ssd_only').files)} on the SSD, not on the Mac yet`}
            {totals.not_backed_up.files > 0 && ` · ${formatCount(totals.not_backed_up.files)} not backed up`}
            {totals.problems.files > 0 && ` · ${formatCount(totals.problems.files)} with problems`}
          </p>
          <Coverage result={result} />
          {open && (
            <>
              <SetRows result={result} />
              <p className="mt-3 text-sm text-neutral-700 dark:text-neutral-300">
                {result.estimate_s > 0
                  ? `Checking what is left on the phone would take ${formatEta(result.estimate_s)}.`
                  : 'Nothing is left to check on the phone.'}
              </p>
              {result.warnings.length > 0 && (
                <ul className="mt-2 list-disc pl-5 text-sm">
                  {result.warnings.map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              )}
            </>
          )}
          {verifiedBytes > 0 && !later && !stale_reason && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={flow.phase !== 'idle'}
                onClick={() => void flow.start({ source: 'analysis', sets: completed.sets })}
                className="rounded-md border border-neutral-900 bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 disabled:opacity-50 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300"
              >
                Delete the backed-up files ({formatBytes(verifiedBytes)})
              </button>
              <button
                type="button"
                onClick={() => setLater(true)}
                className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
              >
                Not now
              </button>
              <Link
                to={`/browse?status=${hasSsd ? 'mac_ssd' : 'mac_only'}`}
                className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
              >
                Choose files
              </Link>
            </div>
          )}
          <DeleteFlow flow={flow} />
        </div>
      )}
    </section>
  )
}
