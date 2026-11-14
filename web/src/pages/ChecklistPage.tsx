import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link } from 'react-router'
import { BTN, FIELD, MUTED } from '../components/apps/ui'
import { RestoreHelper } from '../components/health/RestoreHelper'
import { apiFetch } from '../lib/api'
import type { Checklist, ChecklistItem } from '../lib/types'
import { plain, post } from './Wizard/request'

type Tick = { ticked: boolean; done_on: string | null; note: string | null }

function Row({ item, phone, onSave }: { item: ChecklistItem; phone: boolean; onSave: (id: number, t: Tick) => void }) {
  // The local values are the source of truth: every save sends all three, so one edit never undoes another.
  const [ticked, setTicked] = useState(item.ticked)
  const [doneOn, setDoneOn] = useState(item.done_on ?? '')
  const [note, setNote] = useState(item.note ?? '')
  const latest = useRef({ ticked: item.ticked, doneOn: item.done_on ?? '', note: item.note ?? '' })
  const manual = item.how !== 'auto'
  const edit = (over: Partial<typeof latest.current>, saveNow = true) => {
    latest.current = { ...latest.current, ...over }
    const l = latest.current
    setTicked(l.ticked)
    setDoneOn(l.doneOn)
    setNote(l.note)
    if (saveNow) onSave(item.id, { ticked: l.ticked, done_on: l.doneOn || null, note: l.note.trim() || null })
  }
  const unchanged = (v: string, saved: string | null) => v === (saved ?? '')

  return (
    <li className="flex gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <span
        aria-label={item.green ? 'Done' : 'Not done'}
        role="img"
        className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${
          item.green ? 'bg-green-600 text-white' : 'border border-neutral-400 text-transparent'
        }`}
      >
        ✓
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <h2 className="font-medium">{item.title}</h2>
        <p className={`text-sm ${MUTED}`}>{item.help}</p>
        {item.how !== 'tick' && (
          <p className="text-sm">
            {item.auto_detail ?? (phone ? 'Not checked yet.' : null)}
            {!phone && <span className={MUTED}> Connect the phone to check this.</span>}
          </p>
        )}
        {manual && (
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={ticked} onChange={(e) => edit({ ticked: e.target.checked })} />I did this
            </label>
            <label className="flex items-center gap-2 text-sm">
              Done on
              <input
                type="date"
                className={FIELD}
                value={doneOn}
                onChange={(e) => edit({ doneOn: e.target.value })}
              />
            </label>
            <label className="flex min-w-48 flex-1 items-center gap-2 text-sm">
              Note
              <input
                type="text"
                maxLength={500}
                className={`${FIELD} min-w-0 flex-1`}
                value={note}
                onChange={(e) => edit({ note: e.target.value }, false)}
                onBlur={() => {
                  if (!unchanged(note, item.note) || ticked !== item.ticked || doneOn !== (item.done_on ?? '')) edit({})
                }}
              />
            </label>
          </div>
        )}
      </div>
    </li>
  )
}

/** The list to work through before a factory reset you do yourself (spec 10D). Sift never resets the phone. */
export default function ChecklistPage() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['reset-checklist'], queryFn: () => apiFetch<Checklist>('/api/reset-checklist') })
  const [message, setMessage] = useState<string | null>(null)
  // The newest request per item: an answer to an older one is ignored.
  const newest = useRef(new Map<number, number>())
  const counter = useRef(0)
  const put = useMutation({
    mutationFn: ({ id, ...body }: Tick & { id: number; n: number }) =>
      apiFetch<Checklist>(`/api/reset-checklist/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticked: body.ticked, done_on: body.done_on, note: body.note }),
      }),
    onMutate: () => setMessage(null),
    onSuccess: (data, v) => {
      if (newest.current.get(v.id) === v.n) qc.setQueryData(['reset-checklist'], data)
    },
    onError: (e, v) => {
      if (newest.current.get(v.id) === v.n) setMessage(plain(e))
    },
  })
  const save = (id: number, t: Tick) => {
    counter.current += 1
    newest.current.set(id, counter.current)
    put.mutate({ id, n: counter.current, ...t })
  }
  const exportMd = useMutation({
    mutationFn: () => post<{ paths: string[] }>('/api/reset-checklist/export'),
    onError: (e) => setMessage(plain(e)),
  })

  const data = list.data
  const done = data?.items.filter((i) => i.green).length ?? 0

  return (
    <div className="flex max-w-3xl flex-col gap-4 p-6">
      <h1 className="text-xl font-semibold">Pre-reset checklist</h1>
      <p className={`text-sm ${MUTED}`}>
        Sift never resets the phone. Use this list before a factory reset you do yourself.{' '}
        <Link to="/health" className="underline">
          Back to Health
        </Link>
        .
      </p>
      {list.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {plain(list.error)}
        </p>
      )}
      {data && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <p className="min-w-0 flex-1 font-medium">
              {done} of {data.items.length} done
            </p>
            <button type="button" className={BTN} disabled={exportMd.isPending} onClick={() => exportMd.mutate()}>
              Export checklist
            </button>
          </div>
          {message && (
            <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
              {message}
            </p>
          )}
          {exportMd.data && (
            <div role="status" className="text-sm">
              <p>Checklist saved to:</p>
              <ul className="list-disc pl-5">
                {exportMd.data.paths.map((p) => (
                  <li key={p} className="break-all">
                    {p}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <ol className="flex flex-col gap-3">
            {data.items.map((i) => (
              <Row key={i.id} item={i} phone={data.phone} onSave={save} />
            ))}
          </ol>
        </>
      )}
      <RestoreHelper />
    </div>
  )
}
