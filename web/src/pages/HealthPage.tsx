import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { Chip } from '../components/Chip'
import { BTN, MUTED } from '../components/apps/ui'
import { ReasonButton } from '../components/backup/ReasonButton'
import { TaskLine } from '../components/backup/TaskLine'
import { useTaskRun } from '../components/health/useTaskRun'
import { apiFetch } from '../lib/api'
import type { HealthLevel, HealthReport } from '../lib/types'
import { plain, post } from './Wizard/request'

const CHIP: Record<HealthLevel, { label: string; tone: 'ok' | 'warn' | 'bad' | 'neutral' }> = {
  ok: { label: 'OK', tone: 'ok' },
  warn: { label: 'Check', tone: 'warn' },
  bad: { label: 'Problem', tone: 'bad' },
  unavailable: { label: 'Unavailable', tone: 'neutral' },
}

function SavedPaths({ label, paths }: { label: string; paths: string[] }) {
  return (
    <div role="status" className="text-sm">
      <p>{label}</p>
      <ul className="list-disc pl-5">
        {paths.map((p) => (
          <li key={p} className="break-all">
            {p}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The phone's own status in plain words (spec 10D). It reads the phone and never changes it. */
export default function HealthPage() {
  const qc = useQueryClient()
  const report = useQuery({ queryKey: ['health', 'report'], queryFn: () => apiFetch<{ report: HealthReport | null }>('/api/health/report') })
  const run = useTaskRun('/api/health/run', () => void qc.invalidateQueries({ queryKey: ['health', 'report'] }))
  const [bugPaths, setBugPaths] = useState<string[] | null>(null)
  const bug = useTaskRun('/api/health/bugreport', (t) => setBugPaths((t.result?.paths as string[] | undefined) ?? []))
  const exportMd = useMutation({ mutationFn: () => post<{ paths: string[] }>('/api/health/export') })

  const data = report.data?.report ?? null
  const busyReason = run.busy || bug.busy ? 'Another task is running. Wait for it to finish.' : null

  return (
    <div className="flex max-w-3xl flex-col gap-4 p-6">
      <h1 className="text-xl font-semibold">Health</h1>
      <p className={`text-sm ${MUTED}`}>
        A plain-words check of the phone. Before a factory reset, use the{' '}
        <Link to="/health/checklist" className="underline">
          pre-reset checklist
        </Link>
        .
      </p>

      <div className="flex flex-wrap items-center gap-3">
        <ReasonButton primary reason={busyReason} onClick={() => void run.start()}>
          {data ? 'Run health report again' : 'Run health report'}
        </ReasonButton>
        {data && (
          <span className={`text-sm ${MUTED}`}>Report made {new Date(data.created_at * 1000).toLocaleString()}</span>
        )}
      </div>
      {run.task && run.task.state !== 'done' && <TaskLine task={run.task} stopping={run.stopping} onStop={() => void run.stop()} />}
      {run.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {run.error}
        </p>
      )}
      {report.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {plain(report.error)}
        </p>
      )}
      {report.data && !data && <p className={MUTED}>No health report yet. Run one to see how the phone is doing.</p>}

      {data && (
        <div className="flex flex-col gap-3">
          {data.areas.map((a) => (
            <section key={a.key} aria-label={a.title} className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="mb-2 flex flex-wrap items-center gap-3">
                <h2 className="min-w-0 flex-1 font-medium">{a.title}</h2>
                <Chip tone={CHIP[a.level].tone}>{CHIP[a.level].label}</Chip>
              </div>
              <ul className="flex flex-col gap-1 text-sm">
                {a.verdicts.map((v) => (
                  <li key={v}>{v}</li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <ReasonButton reason={busyReason} onClick={() => void bug.start()}>
          Capture bug report
        </ReasonButton>
        <ReasonButton reason={!data ? 'Run the health report first.' : exportMd.isPending ? 'Saving…' : null} onClick={() => exportMd.mutate()}>
          Export as Markdown
        </ReasonButton>
        <Link to="/health/checklist" className={BTN}>
          Pre-reset checklist
        </Link>
      </div>
      <p className={`text-sm ${MUTED}`}>A bug report takes a few minutes. It is saved to your backup folder.</p>
      {bug.task && bug.task.state !== 'done' && <TaskLine task={bug.task} stopping={bug.stopping} onStop={() => void bug.stop()} />}
      {bug.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {bug.error}
        </p>
      )}
      {bugPaths && bug.task?.state === 'done' && <SavedPaths label="Bug report saved to:" paths={bugPaths} />}
      {exportMd.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {plain(exportMd.error)}
        </p>
      )}
      {exportMd.data && <SavedPaths label="Report saved to:" paths={exportMd.data.paths} />}

      <p className={`text-sm ${MUTED}`}>This report reads the phone&apos;s own status; it does not diagnose hardware faults.</p>
    </div>
  )
}
