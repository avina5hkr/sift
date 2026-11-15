import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { ReasonButton } from '../components/backup/ReasonButton'
import { apiFetch } from '../lib/api'
import { baseName, formatBytes, formatCount, formatDay, formatWhen } from '../lib/format'
import type { QuarantineEntry } from '../lib/types'
import { plain, post } from './Wizard/request'

export default function QuarantinePage() {
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['quarantine'],
    queryFn: () => apiFetch<QuarantineEntry[]>('/api/quarantine'),
  })
  const [picked, setPicked] = useState<Set<number>>(() => new Set())
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null)

  const restore = useMutation({
    mutationFn: (to: 'phone' | 'backup') => post<{ restored: number; pushed: number }>('/api/quarantine/restore', { ids: [...picked], to }),
    onMutate: () => setMessage(null),
    onSuccess: (r, to) => {
      setPicked(new Set())
      const files = `${formatCount(r.restored)} ${r.restored === 1 ? 'file' : 'files'}`
      setMessage({ text: to === 'phone' ? `Restored ${files}. ${formatCount(r.pushed)} put back on the phone.` : `Restored ${files} to the backup.`, bad: false })
    },
    onError: (e) => setMessage({ text: plain(e), bad: true }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['quarantine'] }),
  })

  // How long files wait here: the Cleanup setting, editable here too. A change moves the date
  // of the files already here as well.
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiFetch<{ pages: Record<string, { section: string; key: string; value: unknown }[]> }>('/api/settings'),
  })
  const savedDays = Object.values(settings.data?.pages ?? {})
    .flat()
    .find((f) => f.section === 'cleanup' && f.key === 'quarantine_days')?.value as number | undefined
  const [days, setDays] = useState<string | null>(null)
  const saveDays = useMutation({
    mutationFn: (value: number) =>
      apiFetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ changes: [{ section: 'cleanup', key: 'quarantine_days', value }] }),
      }),
    onMutate: () => setMessage(null),
    onSuccess: (_r, value) => {
      setDays(null)
      setMessage({ text: `Files now wait ${formatCount(value)} ${value === 1 ? 'day' : 'days'}, including the ones already here.`, bad: false })
    },
    onError: (e) => setMessage({ text: plain(e), bad: true }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['settings'] })
      void qc.invalidateQueries({ queryKey: ['quarantine'] })
    },
  })
  const dayValue = days ?? (savedDays != null ? String(savedDays) : '')
  const dayNumber = Number(dayValue)
  const daysOk = Number.isInteger(dayNumber) && dayNumber >= 1 && dayNumber !== savedDays

  const clearExpired = useMutation({
    mutationFn: () => post<{ purged: number }>('/api/quarantine/purge'),
    onMutate: () => setMessage(null),
    onSuccess: (r) =>
      setMessage({
        text: r.purged ? `Removed ${formatCount(r.purged)} ${r.purged === 1 ? 'file' : 'files'} whose time was up.` : 'Nothing has reached its date yet.',
        bad: false,
      }),
    onError: (e) => setMessage({ text: plain(e), bad: true }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['quarantine'] }),
  })
  const [confirmAll, setConfirmAll] = useState(false)
  const [typed, setTyped] = useState('')
  const deleteAll = useMutation({
    mutationFn: (count: number) => post<{ purged: number; waiting: number }>('/api/quarantine/purge-all', { count }),
    onMutate: () => setMessage(null),
    onSuccess: (r) => {
      setConfirmAll(false)
      setTyped('')
      setPicked(new Set())
      setMessage({
        text:
          `Deleted ${formatCount(r.purged)} ${r.purged === 1 ? 'file' : 'files'} for good.` +
          (r.waiting ? ` ${formatCount(r.waiting)} are on a drive that is not connected, or back on the phone; they stay until then.` : ''),
        bad: false,
      })
    },
    onError: (e) => setMessage({ text: plain(e), bad: true }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['quarantine'] }),
  })

  const entries = list.data ?? []
  const toggle = (id: number) =>
    setPicked((p) => {
      const n = new Set(p)
      if (!n.delete(id)) n.add(id)
      return n
    })
  const reason = restore.isPending ? 'Restoring…' : picked.size === 0 ? 'Tick the files to restore first.' : null

  return (
    <div className="flex max-w-4xl flex-col gap-4 p-6">
      <h1 className="text-xl font-semibold">Quarantine</h1>
      <p className="text-sm text-neutral-600 dark:text-neutral-400">
        Files you deleted from a review wait here for a while. Restore them if you change your mind. After the date shown they are removed for good. Files you chose to keep are in the{' '}
        <Link to="/archive" className="underline">
          Archive
        </Link>
        .
      </p>
      <section aria-labelledby="clear-title" className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
        <h2 id="clear-title" className="font-semibold">
          Clearing the quarantine
        </h2>
        <form
          className="flex flex-wrap items-center gap-2 text-sm"
          onSubmit={(e) => {
            e.preventDefault()
            if (daysOk) saveDays.mutate(dayNumber)
          }}
        >
          <label htmlFor="quarantine-days">Remove files for good</label>
          <input
            id="quarantine-days"
            type="number"
            min={1}
            step={1}
            value={dayValue}
            onChange={(e) => setDays(e.target.value)}
            className="w-20 rounded-md border border-neutral-300 px-2 py-1 dark:border-neutral-700 dark:bg-neutral-900"
          />
          <span>days after they were deleted.</span>
          <button type="submit" disabled={!daysOk || saveDays.isPending} className={BTN}>
            Save
          </button>
        </form>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Sift removes them when it opens and when the SSD is plugged in. A drive that is not connected keeps its files
          until it is.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={() => clearExpired.mutate()} disabled={clearExpired.isPending || entries.length === 0} className={BTN}>
            Clear expired now
          </button>
          <button
            type="button"
            onClick={() => setConfirmAll(true)}
            disabled={entries.length === 0 || confirmAll}
            className={`${BTN} border-red-300 text-red-800 dark:border-red-800 dark:text-red-300`}
          >
            Delete everything in quarantine now…
          </button>
        </div>
        {confirmAll && (
          <div role="group" aria-labelledby="all-title" className="flex flex-col gap-2 rounded-md border border-red-300 p-3 text-sm dark:border-red-800">
            <p id="all-title" className="font-medium">
              Delete all {formatCount(entries.length)} {entries.length === 1 ? 'file' : 'files'} in quarantine for good?
            </p>
            <p>
              Their backup copies on the Mac and the SSD are removed and cannot be restored. Files you deleted from the
              phone are then gone everywhere.
            </p>
            <label className="flex flex-wrap items-center gap-2">
              Type {formatCount(entries.length)} to confirm:
              <input
                inputMode="numeric"
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                className="w-28 rounded-md border border-neutral-300 px-2 py-1 dark:border-neutral-700 dark:bg-neutral-900"
              />
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={typed.replace(/[\s,.]/g, '') !== String(entries.length) || deleteAll.isPending}
                onClick={() => deleteAll.mutate(entries.length)}
                className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-800 disabled:opacity-50"
              >
                Delete {formatCount(entries.length)} for good
              </button>
              <button
                type="button"
                onClick={() => {
                  setConfirmAll(false)
                  setTyped('')
                }}
                className={BTN}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </section>
      {list.error && <p role="alert" className="text-sm">{plain(list.error)}</p>}
      {message && (
        <p role={message.bad ? 'alert' : 'status'} className={`text-sm ${message.bad ? 'text-amber-800 dark:text-amber-300' : ''}`}>
          {message.text}
        </p>
      )}
      {list.data && entries.length === 0 && <p className="text-neutral-600 dark:text-neutral-400">Nothing is in quarantine.</p>}
      {entries.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <ReasonButton reason={reason} onClick={() => restore.mutate('phone')}>
              Restore to phone
            </ReasonButton>
            <ReasonButton reason={reason} onClick={() => restore.mutate('backup')}>
              Restore to backup only
            </ReasonButton>
            <span className="text-sm text-neutral-600 dark:text-neutral-400">{formatCount(picked.size)} selected</span>
          </div>
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 dark:border-neutral-800">
                <th className="w-8 py-2">
                  <input
                    type="checkbox"
                    aria-label="Select all"
                    checked={picked.size === entries.length}
                    onChange={(e) => setPicked(e.target.checked ? new Set(entries.map((x) => x.id)) : new Set())}
                  />
                </th>
                <th className="py-2 font-medium">File</th>
                <th className="py-2 font-medium">Size</th>
                <th className="py-2 font-medium">Deleted on</th>
                <th className="py-2 font-medium">Removed for good on</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((x) => (
                <tr key={x.id} className="border-b border-neutral-100 dark:border-neutral-900">
                  <td className="py-2">
                    <input type="checkbox" aria-label={`Select ${x.phone_path ? baseName(x.phone_path) : 'file'}`} checked={picked.has(x.id)} onChange={() => toggle(x.id)} />
                  </td>
                  <td className="break-all py-2">{x.phone_path ?? 'Path not known'}</td>
                  <td className="py-2">{formatBytes(x.size)}</td>
                  <td className="py-2">{formatWhen(x.moved_at)}</td>
                  <td className="py-2">{formatDay(x.purge_after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

const BTN =
  'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800'
