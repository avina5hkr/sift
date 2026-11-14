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
