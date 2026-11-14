import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { SimpleConfirm } from '../components/apps/SimpleConfirm'
import { BTN, DANGER } from '../components/apps/ui'
import { apiFetch } from '../lib/api'
import { formatBytes, formatCount } from '../lib/format'
import type { ChatBackupCopy } from '../lib/types'
import { plain, post } from './Wizard/request'

const MUTED = 'text-neutral-600 dark:text-neutral-400'

function when(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getDate()} ${d.toLocaleString('en-GB', { month: 'short' })} ${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`
}
const where = (drives: ChatBackupCopy['drives']) => (drives.length > 1 ? 'Mac and SSD' : drives[0] === 'ssd' ? 'SSD' : 'Mac')

/** Every WhatsApp chat backup kept in the backup folders, by date; remove the old ones. */
export default function ChatBackupsPage() {
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['archive', 'chat-backups'],
    queryFn: () => apiFetch<{ copies: ChatBackupCopy[] }>('/api/archive/chat-backups'),
  })
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [asking, setAsking] = useState(false)
  const [askError, setAskError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const del = useMutation({
    mutationFn: () => post<{ deleted: number; failed: number; freed_bytes: number }>('/api/archive/chat-backups/delete', { hashes: [...picked], confirm: true }),
    onMutate: () => setAskError(null),
    onSuccess: (r) => {
      setAsking(false)
      setPicked(new Set())
      setMessage(`Deleted ${formatCount(r.deleted)}, freed ${formatBytes(r.freed_bytes)}${r.failed > 0 ? `. ${formatCount(r.failed)} could not be deleted; see Logs.` : ''}`)
      void qc.invalidateQueries({ queryKey: ['archive', 'chat-backups'] })
    },
    onError: (e) => setAskError(plain(e)),
  })

  const copies = list.data?.copies ?? []
  const deletable = copies.filter((c) => c.deletable)
  const selectedBytes = copies.filter((c) => picked.has(c.content_hash)).reduce((n, c) => n + c.size, 0)
  const toggle = (h: string) =>
    setPicked((p) => {
      const n = new Set(p)
      if (!n.delete(h)) n.add(h)
      return n
    })
  const groups: { key: string; app: string; folder: string; rows: ChatBackupCopy[] }[] = []
  for (const c of copies) {
    const key = `${c.app}/${c.folder}`
    let g = groups.find((x) => x.key === key)
    if (!g) groups.push((g = { key, app: c.app, folder: c.folder, rows: [] }))
    g.rows.push(c)
  }

  return (
    <div className="flex max-w-4xl flex-col gap-4 p-6">
      <h1 className="text-xl font-semibold">WhatsApp chat backups</h1>
      <p className={`text-sm ${MUTED}`}>
        Every chat backup WhatsApp made is kept in your backup folders. Remove old ones you do not need. The newest is always kept. Back to the{' '}
        <Link to="/archive" className="underline">
          Archive
        </Link>
        .
      </p>
      {list.error && <p role="alert" className="text-sm">{plain(list.error)}</p>}
      {message && <p role="status" className="text-sm">{message}</p>}
      {list.data && copies.length === 0 && <p className={MUTED}>No WhatsApp chat backups in your backup folders yet.</p>}
      {copies.length > 0 && (
        <div>
          <button type="button" className={BTN} disabled={deletable.length === 0} onClick={() => setPicked(new Set(deletable.map((c) => c.content_hash)))}>
            Select all older
          </button>
        </div>
      )}
      {groups.map((g) => (
        <section key={g.key} className="flex flex-col gap-1">
          <h2 className="font-medium">
            {g.app}, {g.folder}
          </h2>
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 dark:border-neutral-800">
                <th className="w-8 py-2" />
                <th className="py-2 font-medium">Date</th>
                <th className="py-2 font-medium">Name</th>
                <th className="py-2 font-medium">Size</th>
                <th className="py-2 font-medium">Where</th>
                <th className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {g.rows.map((c) => (
                <tr key={`${c.content_hash}/${c.name}`} className="border-b border-neutral-100 dark:border-neutral-900">
                  <td className="py-2">
                    {c.deletable && <input type="checkbox" aria-label={`Select ${c.name}`} checked={picked.has(c.content_hash)} onChange={() => toggle(c.content_hash)} />}
                  </td>
                  <td className="py-2">{when(c.date)}</td>
                  <td className="break-all py-2">{c.name}</td>
                  <td className="py-2">{formatBytes(c.size)}</td>
                  <td className="py-2">{where(c.drives)}</td>
                  <td className={`py-2 ${MUTED}`}>{c.why_kept}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
      {copies.length > 0 && (
        <div className="sticky bottom-0 flex items-center gap-3 border-t border-neutral-200 bg-white py-3 dark:border-neutral-800 dark:bg-neutral-950">
          <span className="text-sm">
            {formatCount(picked.size)} selected, {formatBytes(selectedBytes)}
          </span>
          <button type="button" className={DANGER} disabled={picked.size === 0} onClick={() => { setAskError(null); setAsking(true) }}>
            Delete selected
          </button>
        </div>
      )}
      {asking && (
        <SimpleConfirm
          label="Delete chat backups"
          title="Delete these chat backups?"
          confirmLabel="Delete"
          busy={del.isPending}
          error={askError}
          onConfirm={() => del.mutate()}
          onCancel={() => setAsking(false)}
        >
          This deletes these chat backups from your Mac and SSD backup folders. It cannot be undone.
        </SimpleConfirm>
      )}
    </div>
  )
}
