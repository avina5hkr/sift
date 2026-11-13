import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type RefObject } from 'react'
import { Chip } from '../components/Chip'
import { FailedDrawer } from '../components/backup/FailedDrawer'
import { AnalysisCard } from '../components/backup/AnalysisCard'
import { ConfirmStatus } from '../components/backup/ConfirmStatus'
import { GuardCard } from '../components/backup/GuardCard'
import { ProblemsCard } from '../components/backup/ProblemsCard'
import { ProgressBar } from '../components/backup/ProgressBar'
import { ReasonButton } from '../components/backup/ReasonButton'
import { SummaryCard } from '../components/backup/SummaryCard'
import { TaskLine } from '../components/backup/TaskLine'
import { ApiError, apiFetch } from '../lib/api'
import { disabledReason, isConnected, isRunning, screenState, type ScreenState } from '../lib/backupState'
import { useEventStore } from '../lib/events'
import { formatBytes, formatCount, formatEta, formatSpeed, formatWhen } from '../lib/format'
import type {
  Analysis,
  BackupOverview,
  BackupSummary,
  DeviceStatus,
  FailuresResponse,
  JobEvent,
  Problem,
  SetStatus,
  TaskEvent,
} from '../lib/types'
import { useStatus } from '../lib/useStatus'

const post = <T,>(path: string, body?: unknown) =>
  apiFetch<T>(path, {
    method: 'POST',
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })

const plain = (e: unknown) =>
  e instanceof ApiError ? e.message : 'Sift could not do that just now. Try again in a moment.'

function phoneSummary(device: DeviceStatus | null | undefined): { text: string; tone: 'ok' | 'warn' | 'neutral' } {
  if (device && isConnected(device)) {
    const free =
      device.free_bytes != null && device.total_bytes
        ? `${Math.round((device.free_bytes / device.total_bytes) * 100)}% free`
        : null
    const parts = [device.model ?? 'Phone', device.state === 'connected_usb' ? 'USB' : 'Wi-Fi', ...(free ? [free] : [])]
    return { text: parts.join(', '), tone: 'ok' }
  }
  if (device?.state === 'wrong_device') return { text: 'A different phone is connected', tone: 'warn' }
  return { text: 'No phone connected', tone: 'neutral' }
}

const STATE_LABEL: Record<ScreenState, string> = {
  not_connected: 'Not connected',
  ready: 'Ready',
  scanning: 'Looking through your phone…',
  confirm: 'Waiting for your confirmation',
  backing_up: 'Backing up…',
  paused: 'Paused',
  waiting_for_phone: 'Waiting for your phone',
  finished: 'Finished',
  failed: 'Something went wrong',
}

function percent(job: JobEvent): number | null {
  if (job.bytes_total) return (job.bytes_done / job.bytes_total) * 100
  if (job.files_total) return (job.files_done / job.files_total) * 100
  return null
}

export default function BackupPage() {
  const qc = useQueryClient()
  const status = useStatus()
  const device = status.data?.device
  const mac = status.data?.mac_folder

  // A dry run (the wizard's size scan) copies nothing: this screen never shows it as a backup.
  const streamJob = useEventStore((s) => s.job)
  const storedJob = streamJob?.dry_run ? null : streamJob
  const storedAt = useEventStore((s) => s.jobAt)
  const overview = useQuery({
    queryKey: ['backup', 'overview'],
    queryFn: () => apiFetch<BackupOverview>('/api/backup/overview'),
    refetchInterval: () => (isRunning(useEventStore.getState().job) ? 5000 : false),
  })
  const sets: SetStatus[] = overview.data?.sets ?? []
  const recent = overview.data?.recent ?? []
  const noPhoneCounts = overview.data?.phone_connected === false
  const fetched = overview.data?.job && !overview.data.job.dry_run ? overview.data.job : null
  // The server's answer wins when it is newer than what the stream last said: a running job
  // the server no longer lists (or lists as another job) ended while the stream was away.
  const stale =
    storedJob != null &&
    isRunning(storedJob) &&
    overview.dataUpdatedAt > storedAt &&
    overview.data?.job !== undefined &&
    (fetched == null || fetched.job_id !== storedJob.job_id)
  const stored = stale ? null : storedJob
  useEffect(() => {
    if (stale) useEventStore.setState({ job: null, jobAt: 0 })
  }, [stale])
  // Otherwise the stream is fresher; the fetched job covers a window opened mid-run.
  const job = stored && (!fetched || stored.job_id >= fetched.job_id) ? stored : fetched
  const running = isRunning(job)
  const state = screenState(device, job)

  const summary = useQuery({
    queryKey: ['backup', 'summary'],
    queryFn: () => apiFetch<BackupSummary | null>('/api/backup/summary'),
  })
  const failures = useQuery({
    queryKey: ['backup', 'failures'],
    queryFn: () => apiFetch<FailuresResponse>('/api/backup/failures'),
  })

  const [ticked, setTicked] = useState<Record<string, boolean>>({})
  const selected = sets.filter((s) => ticked[s.name] ?? s.enabled).map((s) => s.name)

  const [message, setMessage] = useState<string | null>(null)
  const done = () => void qc.invalidateQueries({ queryKey: ['backup'] })
  const start = useMutation({
    mutationFn: (names: string[]) => post('/api/backup/start', { sets: names }),
    onMutate: () => setMessage(null),
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })
  const control = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'stop') => post(`/api/backup/${action}`),
    onMutate: () => setMessage(null),
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })

  // -- the mass-disappearance guard
  const scan = overview.data?.pending_scan ?? null
  const [rescan, setRescan] = useState<string[] | null>(null)
  const answer = useMutation({
    mutationFn: (v: { action: 'confirm' | 'discard'; again?: string[] }) => {
      if (!scan) throw new ApiError('no_scan', 'That scan is no longer waiting. Reloading.')
      return post(`/api/backup/scan/${scan.id}/${v.action}`)
    },
    onMutate: () => setMessage(null),
    onSuccess: (_d, v) => {
      if (v.again) setRescan(v.again)
    },
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })
  // Scan again: the held scan is dropped, which ends the waiting backup; then a fresh one starts.
  useEffect(() => {
    if (rescan && !running && !start.isPending) {
      start.mutate(rescan)
      setRescan(null)
    }
  }, [rescan, running, start])

  // -- failures drawer
  const [showFailed, setShowFailed] = useState(false)
  const failedTrigger = useRef<HTMLButtonElement>(null)
  const retry = useMutation({
    mutationFn: () => post('/api/backup/retry'),
    onMutate: () => setMessage(null),
    onSuccess: () => setShowFailed(false),
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })
  const closeDrawer = () => {
    setShowFailed(false)
    setTimeout(() => failedTrigger.current?.focus(), 0)
  }
  useEffect(() => {
    if (showFailed) void failures.refetch()
    // only when the drawer opens
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showFailed])

  // -- long tasks (verify, adopt, confirm, analyse), one at a time
  const streamTask = useEventStore((s) => s.task)
  const currentTask = useQuery({
    queryKey: ['backup', 'task'],
    queryFn: () => apiFetch<TaskEvent | null>('/api/tasks/current'),
  })
  const task = streamTask ?? currentTask.data ?? null
  const taskRunning = task?.state === 'running'
  const startTask = useMutation({
    mutationFn: (v: { path: string; body?: unknown }) => post<TaskEvent>(v.path, v.body),
    onMutate: () => setMessage(null),
    onSuccess: (t) => useEventStore.setState({ task: t }),
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })
  const stopTask = useMutation({
    mutationFn: () => post('/api/tasks/stop'),
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })
  const taskReason = (needsSets = false): string | null => {
    if (taskRunning) return 'Another task is running. Wait for it to finish or stop it.'
    if (running) return 'A backup is running. This works when it has finished.'
    if (startTask.isPending) return 'Starting…'
    if (needsSets && !isConnected(device)) return 'Connect your phone to do this.'
    if (needsSets && selected.length === 0) return 'Tick at least one set under "This run".'
    return null
  }
  const ssdConnected = status.data?.ssd?.connected === true

  const problems = useQuery({
    queryKey: ['backup', 'problems'],
    queryFn: () => apiFetch<Problem[]>('/api/backup/problems'),
  })
  const repair = useMutation({
    mutationFn: (p: Problem) => post(`/api/backup/problems/${p.id}/repair`),
    onMutate: () => setMessage(null),
    onError: (e) => setMessage(plain(e)),
    onSettled: done,
  })

  const analysisId = useEventStore((s) => s.analysisId)
  const analysis = useQuery({
    queryKey: ['analysis', analysisId],
    queryFn: () => apiFetch<Analysis>(`/api/analyse/phone/${analysisId}`),
    enabled: analysisId != null,
  })

  // -- Open backup folder
  const [openError, setOpenError] = useState<string | null>(null)
  const open = useMutation({
    mutationFn: () => post('/api/backup/open-folder'),
    onMutate: () => setOpenError(null),
    onError: (e) => setOpenError(plain(e)),
  })

  const reasonFor = (count: number, noSetsReason?: string) =>
    disabledReason({
      device,
      job,
      macFreeBytes: mac?.free_bytes,
      setCount: count,
      noSetsReason,
      starting: start.isPending || rescan != null,
    })
  const headerReason = reasonFor(
    selected.length,
    sets.length === 0 ? undefined : 'Tick at least one set under "This run".',
  )

  const failedCount = running && job ? job.failed : (failures.data?.count ?? 0)
  const phone = phoneSummary(device)
  // Only a real summary (an answer that is not one is ignored rather than drawn).
  const savedSummary = summary.data && typeof summary.data.job_id === 'number' ? summary.data : null
  const lastBackup = savedSummary?.finished_at
  const macTotals = sets.reduce(
    (t, s) => ({ onPhone: t.onPhone + s.on_phone, backedUp: t.backedUp + s.backed_up }),
    { onPhone: 0, backedUp: 0 },
  )
  const files = job?.current_files ?? []
  const showRun = job && running && state !== 'confirm'

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold">Backup</h1>
          <p className="mt-1">
            <Chip tone={phone.tone}>{phone.text}</Chip>
          </p>
        </div>
        <ReasonButton primary reason={headerReason} onClick={() => start.mutate(selected)}>
          Back up now
        </ReasonButton>
      </header>

      {message && (
        <p role="alert" className="rounded-md border border-amber-300 bg-amber-100 px-3 py-2 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
          {message}
        </p>
      )}

      <p role="status" data-testid="screen-state" className="text-lg font-medium">
        {job?.state === 'stopping' ? 'Stopping after this file…' : STATE_LABEL[state]}
      </p>

      {state === 'not_connected' && (
        <p className="text-neutral-700 dark:text-neutral-300">
          {device && device.state !== 'no_device' && device.message
            ? device.message
            : 'Connect your phone with a USB cable to start a backup.'}
        </p>
      )}

      {sets.length === 0 && overview.data && (
        <p className="text-neutral-700 dark:text-neutral-300">
          There is nothing to back up yet. Choose the folders to back up in Settings.
        </p>
      )}

      <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm" disabled={running}>
        <legend className="sr-only">Sets in this run</legend>
        <span className="font-medium">This run:</span>
        {running && job ? (
          <span>{job.sets.join(', ')}</span>
        ) : (
          sets.map((s) => (
            <label key={s.name} className="inline-flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={ticked[s.name] ?? s.enabled}
                onChange={(e) => setTicked((t) => ({ ...t, [s.name]: e.target.checked }))}
              />
              {s.name}
            </label>
          ))
        )}
      </fieldset>

      {state === 'confirm' && (
        <GuardCard
          scan={scan}
          busy={answer.isPending}
          error={message}
          onConfirm={() => answer.mutate({ action: 'confirm' })}
          onScanAgain={() => answer.mutate({ action: 'discard', again: job?.sets ?? selected })}
        />
      )}

      {showRun && (
        <section aria-label="Progress" className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
          {state === 'waiting_for_phone' && (
            <p className="text-sm">Plug your phone back in. The backup carries on by itself.</p>
          )}
          <div className="flex flex-wrap gap-x-6 text-sm">
            <span>
              File {formatCount(job.files_done)}
              {job.files_total != null && ` of ${formatCount(job.files_total)}`}
            </span>
            <span>
              {formatBytes(job.bytes_done)}
              {job.bytes_total != null && ` of ${formatBytes(job.bytes_total)}`}
            </span>
          </div>
          <div className="flex items-center gap-3">
            <ProgressBar label="Overall progress" value={state === 'scanning' ? null : percent(job)} />
            {percent(job) != null && state !== 'scanning' && (
              <span className="w-12 text-right text-sm tabular-nums">{Math.round(percent(job) ?? 0)}%</span>
            )}
          </div>
          {state === 'backing_up' && (job.speed_bps != null || job.eta_s != null) && (
            <p className="text-sm text-neutral-700 dark:text-neutral-300">
              {job.speed_bps != null && formatSpeed(job.speed_bps)}
              {job.speed_bps != null && job.eta_s != null && ', '}
              {job.eta_s != null && formatEta(job.eta_s)}
            </p>
          )}
          <div className="flex items-center gap-3 text-sm">
            <span className="w-24 shrink-0">Mac folder</span>
            <ProgressBar
              label="Mac folder"
              value={!noPhoneCounts && macTotals.onPhone ? (macTotals.backedUp / macTotals.onPhone) * 100 : null}
            />
            <span className="shrink-0">{formatCount(macTotals.backedUp)} files</span>
          </div>
          {(files.length > 0 || job.current) && (
            <div>
              <h2 className="text-sm font-medium">Now copying</h2>
              <ul className="mt-1 flex flex-col gap-1 text-sm">
                {files.length > 0
                  ? files.map((f) => (
                      <li key={f.path} className="flex gap-3">
                        <span className="min-w-0 flex-1 font-mono break-all">{f.path}</span>
                        <span className="shrink-0 text-neutral-600 dark:text-neutral-400">
                          {formatBytes(f.size)}
                        </span>
                        <span className="w-12 shrink-0 text-right tabular-nums">
                          {f.size > 0 ? `${Math.min(100, Math.round((f.done / f.size) * 100))}%` : ''}
                        </span>
                      </li>
                    ))
                  : [<li key="one" className="font-mono break-all">{job.current}</li>]}
              </ul>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span>Skipped {formatCount(job.duplicates_skipped)} duplicates</span>
            <span aria-hidden="true">|</span>
            <FailedLink count={failedCount} onShow={() => setShowFailed(true)} triggerRef={failedTrigger} />
            <span className="ml-auto flex gap-2">
              {state === 'paused' || job.pause_requested ? (
                <button
                  type="button"
                  onClick={() => control.mutate('resume')}
                  disabled={control.isPending}
                  className={BTN}
                >
                  Resume
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => control.mutate('pause')}
                  disabled={control.isPending || job.state === 'stopping'}
                  className={BTN}
                >
                  Pause
                </button>
              )}
              <button
                type="button"
                onClick={() => control.mutate('stop')}
                disabled={control.isPending || job.state === 'stopping'}
                className={BTN}
              >
                Stop
              </button>
            </span>
          </div>
        </section>
      )}

      {!running && state !== 'confirm' && (
        <SummaryCard
          job={job}
          summary={savedSummary}
          openError={openError}
          onOpenFolder={() => open.mutate()}
        />
      )}

      <ProblemsCard
        problems={Array.isArray(problems.data) ? problems.data : []}
        busy={repair.isPending || taskRunning}
        onRepair={(p) => repair.mutate(p)}
      />

      {!running && failedCount > 0 && (
        <FailedLink count={failedCount} onShow={() => setShowFailed(true)} triggerRef={failedTrigger} />
      )}

      {task && <TaskLine task={task} stopping={stopTask.isPending} onStop={() => stopTask.mutate()} />}

      <section aria-labelledby="tools-title">
        <h2 id="tools-title" className="mb-2 text-lg font-semibold">
          Tools
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <ReasonButton reason={taskReason()} onClick={() => startTask.mutate({ path: '/api/backup/verify' })}>
            Verify backups
          </ReasonButton>
          <ReasonButton
            reason={taskReason()}
            onClick={() => startTask.mutate({ path: '/api/backup/adopt', body: { destination: 'mac' } })}
          >
            Adopt existing backup (Mac)
          </ReasonButton>
          {ssdConnected && (
            <ReasonButton
              reason={taskReason()}
              onClick={() => startTask.mutate({ path: '/api/backup/adopt', body: { destination: 'ssd' } })}
            >
              Adopt existing backup (SSD)
            </ReasonButton>
          )}
          <ReasonButton
            reason={taskReason(true)}
            onClick={() => startTask.mutate({ path: '/api/analyse/phone', body: { sets: selected } })}
          >
            Analyse phone
          </ReasonButton>
        </div>
        <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">
          Adopting reads a backup folder that is already there so Sift knows about it. Nothing in the folder is
          changed. Analyse phone only looks: it never changes the phone or your backups.
        </p>
      </section>

      {analysis.data && <AnalysisCard analysis={analysis.data} />}

      <section aria-labelledby="sets-title">
        <h2 id="sets-title" className="mb-2 text-lg font-semibold">
          Your sets
        </h2>
        <ul className="flex flex-col gap-2">
          {sets.map((s) => (
            <li
              key={s.name}
              className="flex flex-wrap items-center gap-3 rounded-lg border border-neutral-200 px-4 py-2 dark:border-neutral-800"
            >
              <div className="min-w-0 flex-1">
                <p className="font-medium">{s.name}</p>
                <p className="text-sm text-neutral-600 dark:text-neutral-400">
                  {noPhoneCounts
                    ? 'Connect your phone to see what is on it.'
                    : `${formatCount(s.on_phone)} on the phone, ${formatCount(s.backed_up)} backed up on the Mac, ${formatCount(s.pending)} waiting`}
                </p>
                <ConfirmStatus set={s.name} />
              </div>
              <ReasonButton
                label={`Confirm ${s.name}`}
                reason={isConnected(device) ? taskReason() : 'Connect your phone to confirm.'}
                onClick={() => startTask.mutate({ path: '/api/backup/confirm', body: { set: s.name } })}
              >
                Confirm
              </ReasonButton>
              <ReasonButton
                label={`Back up now, ${s.name}`}
                reason={reasonFor(s.enabled ? 1 : 0, 'This set is switched off in Settings.')}
                onClick={() => start.mutate([s.name])}
              >
                Back up now
              </ReasonButton>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="recent-title" className="flex flex-wrap gap-6">
        <div className="min-w-0 flex-1">
          <h2 id="recent-title" className="mb-1 text-lg font-semibold">
            Recent activity
          </h2>
          {overview.data && recent.length === 0 ? (
            <p className="text-sm text-neutral-600 dark:text-neutral-400">No files copied yet.</p>
          ) : (
            <ul className="text-sm">
              {recent.map((f) => (
                <li key={f.path} className="flex gap-3 py-0.5">
                  <span className="min-w-0 flex-1 font-mono break-all">{f.path}</span>
                  <span className="shrink-0 text-neutral-600 dark:text-neutral-400">{formatBytes(f.size)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <p className="text-sm">{lastBackup ? `Last backup: ${formatWhen(lastBackup)}` : 'No backup yet'}</p>
      </section>

      {showFailed && (
        <FailedDrawer
          files={failures.data?.files}
          loading={failures.isFetching}
          error={retry.error ? plain(retry.error) : null}
          retryBlocked={running ? 'A backup is running. Retry works when it has finished.' : null}
          onClose={closeDrawer}
          onRetry={() => retry.mutate()}
        />
      )}
    </div>
  )
}

const BTN =
  'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800'

function FailedLink({
  count,
  onShow,
  triggerRef,
}: {
  count: number
  onShow: () => void
  triggerRef: RefObject<HTMLButtonElement>
}) {
  if (count === 0) return <span>0 failed</span>
  return (
    <span>
      {formatCount(count)} failed{' '}
      <button ref={triggerRef} type="button" onClick={onShow} className="underline underline-offset-2">
        show
      </button>
    </span>
  )
}
