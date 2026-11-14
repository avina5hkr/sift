import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { ReasonButton } from '../components/backup/ReasonButton'
import { apiFetch } from '../lib/api'
import { formatBytes, formatCount } from '../lib/format'
import type { ArchiveDestination, ArchiveListing } from '../lib/types'
import { plain, post } from './Wizard/request'

const MUTED = 'text-neutral-600 dark:text-neutral-400'
const KIND_LABEL = { mac: 'Mac', ssd: 'SSD' }
const plural = (n: number, one: string, many: string) => `${formatCount(n)} ${n === 1 ? one : many}`

/** The backup copies of files that were deleted from the phone and kept: look through them and put some back. */
type RestoreResult = { restored: number; already_on_phone: number; failed?: number; not_done?: number }

export default function ArchivePage() {
  const qc = useQueryClient()
  const dests = useQuery({ queryKey: ['archive', 'destinations'], queryFn: () => apiFetch<ArchiveDestination[]>('/api/archive/destinations') })
  const [chosen, setChosen] = useState<number | null>(null)
  const [path, setPath] = useState('')
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [message, setMessage] = useState<{ text: string; bad: boolean } | null>(null)

  const dest = chosen ?? dests.data?.[0]?.id ?? null
  const listing = useQuery({
    queryKey: ['archive', 'list', dest, path],
    enabled: dest != null,
    queryFn: () => apiFetch<ArchiveListing>(`/api/archive?dest=${dest}&path=${encodeURIComponent(path)}`),
    retry: false,
  })

  const restore = useMutation({
    mutationFn: async () => {
      // restoring runs as a background task: start it, then wait for it to finish
      await post('/api/archive/restore', { dest, paths: [...picked] })
      for (;;) {
        await new Promise((r) => setTimeout(r, 400))
        const t = await apiFetch<{ state: string; message: string | null; result: RestoreResult }>('/api/tasks/current')
        if (t.state === 'running') continue
        if (t.state === 'failed') throw new Error(t.message ?? 'Sift could not restore these files.')
        return t.result
      }
    },
    onMutate: () => setMessage(null),
    onSuccess: (r) => {
      setPicked(new Set())
      const parts = [`Put ${plural(r.restored, 'file', 'files')} back on the phone.`]
      if (r.already_on_phone > 0) parts.push(`${plural(r.already_on_phone, 'file was', 'files were')} already there.`)
      if (r.failed) parts.push(`${plural(r.failed, 'file', 'files')} could not be restored; see Logs.`)
      if (r.not_done) parts.push(`${plural(r.not_done, 'file was', 'files were')} not restored (stopped or the phone went away).`)
      setMessage({ text: parts.join(' '), bad: false })
    },
    onError: (e) => setMessage({ text: plain(e), bad: true }),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['archive', 'list'] }),
  })

  const go = (p: string) => {
    setPath(p)
    setPicked(new Set())
  }
  const toggle = (rel: string) =>
    setPicked((s) => {
      const n = new Set(s)
      if (!n.delete(rel)) n.add(rel)
      return n
    })
  const parts = path ? path.split('/') : []
  const folders = listing.data?.folders ?? []
  const files = listing.data?.files ?? []
  const reason = restore.isPending ? 'Restoring…' : picked.size === 0 ? 'Tick the files to restore first.' : null

  return (
    <div className="flex max-w-4xl flex-col gap-4 p-6">
      <h1 className="text-xl font-semibold">Archive</h1>
      <p className={`text-sm ${MUTED}`}>
        Files you deleted from the phone and kept in the backup. Put them back on the phone if you want them again. See also{' '}
        <Link to="/quarantine" className="underline">
          Quarantine
        </Link>
        .
      </p>
      <p className={`text-sm ${MUTED}`}>
        <Link to="/archive/chat-backups" className="underline">
          WhatsApp chat backups
        </Link>
        : see them by date and remove old ones.
      </p>
      {dests.error && (
        <p role="alert" className="text-sm">
          {plain(dests.error)}
        </p>
      )}
      {dests.data && dests.data.length === 0 && <p className={MUTED}>No backup drive is available right now.</p>}
      {dests.data && dests.data.length > 1 && (
        <div role="group" aria-label="Backup" className="flex gap-2">
          {dests.data.map((d) => (
            <button
              key={d.id}
              type="button"
              aria-pressed={dest === d.id}
              onClick={() => {
                setChosen(d.id)
                go('')
              }}
              className={`rounded-md border px-3 py-1.5 text-sm font-medium ${dest === d.id ? 'border-neutral-900 bg-neutral-200 dark:border-neutral-100 dark:bg-neutral-800' : 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'}`}
            >
              {KIND_LABEL[d.kind]}
            </button>
          ))}
        </div>
      )}
      {dest != null && (
        <nav aria-label="Folder" className="flex flex-wrap items-center gap-1 text-sm">
          {parts.length === 0 ? (
            <span aria-current="page" className="font-medium">
              Archive
            </span>
          ) : (
            <button type="button" onClick={() => go('')} className="underline">
              Archive
            </button>
          )}
          {parts.map((p, i) => (
            <span key={parts.slice(0, i + 1).join('/')} className="flex items-center gap-1">
              <span aria-hidden="true">/</span>
              {i === parts.length - 1 ? (
                <span aria-current="page" className="font-medium">
                  {p}
                </span>
              ) : (
                <button type="button" onClick={() => go(parts.slice(0, i + 1).join('/'))} className="underline">
                  {p}
                </button>
              )}
            </span>
          ))}
        </nav>
      )}
      {listing.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {plain(listing.error)}
        </p>
      )}
      {message && (
        <p role={message.bad ? 'alert' : 'status'} className={`text-sm ${message.bad ? 'text-amber-800 dark:text-amber-300' : ''}`}>
          {message.text}
        </p>
      )}
      {listing.data && folders.length === 0 && files.length === 0 && <p className={MUTED}>Nothing is kept here.</p>}
      {(folders.length > 0 || files.length > 0) && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <ReasonButton primary reason={reason} onClick={() => restore.mutate()}>
              Restore to phone
            </ReasonButton>
            <span className={`text-sm ${MUTED}`}>{formatCount(picked.size)} selected</span>
          </div>
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 dark:border-neutral-800">
                <th scope="col" className="w-8 py-2">
                  <span className="sr-only">Select</span>
                </th>
                <th scope="col" className="py-2 font-medium">Name</th>
                <th scope="col" className="py-2 font-medium">Size</th>
                <th scope="col" className="py-2 font-medium">On the phone</th>
              </tr>
            </thead>
            <tbody>
              {folders.map((f) => (
                <tr key={f.rel_path} className="border-b border-neutral-100 dark:border-neutral-900">
                  <td />
                  <td className="py-1.5">
                    <button type="button" onClick={() => go(f.rel_path)} className="flex items-center gap-2 font-medium hover:underline">
                      <span aria-hidden="true">📁</span>
                      {f.name}
                    </button>
                  </td>
                  <td className="py-1.5">{formatBytes(f.size)}</td>
                  <td className={`py-1.5 ${MUTED}`}>{plural(f.files, 'file', 'files')}</td>
                </tr>
              ))}
              {files.map((f) => (
                <tr key={f.rel_path} className="border-b border-neutral-100 dark:border-neutral-900">
                  <td className="py-1.5">
                    <input
                      type="checkbox"
                      aria-label={`Select ${f.name}`}
                      title={f.on_phone ? 'Already on the phone' : undefined}
                      disabled={f.on_phone}
                      checked={picked.has(f.rel_path)}
                      onChange={() => toggle(f.rel_path)}
                    />
                  </td>
                  <td className="py-1.5">
                    <span className="break-all">{f.name}</span>
                  </td>
                  <td className="py-1.5">{formatBytes(f.size)}</td>
                  <td className="py-1.5">{f.on_phone ? 'On the phone' : 'Not on the phone'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
