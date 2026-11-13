import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { apiFetch, withToken } from '../lib/api'
import { baseName, formatCount, formatWhen } from '../lib/format'
import type { CalibrationItem, CalibrationMark, CalibrationRecord, CalibrationState } from '../lib/types'
import { plain, post } from './Wizard/request'

const KEY = ['cleanup', 'calibration']
const BTN = 'rounded-md border px-4 py-2 text-sm font-medium'
const PLAIN = 'border-neutral-300 hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800'

/** The one-line outcome of a finished check. */
function outcome(r: { errors: number; sample_size: number; passed: boolean; threshold: number }): string {
  if (r.passed) return `${r.errors} wrong in ${formatCount(r.sample_size)}: suggestions will now be ticked`
  if (r.errors === 0) return `0 wrong in ${formatCount(r.sample_size)}, but the picture model changed; check a fresh sample`
  return `${formatCount(r.errors)} wrong: the threshold rose to ${r.threshold.toFixed(2)}; check a fresh sample`
}

function Last({ last }: { last: CalibrationRecord | null }) {
  if (!last) return <p className="text-neutral-600 dark:text-neutral-400">You have not checked a sample yet.</p>
  return (
    <p className="text-neutral-600 dark:text-neutral-400">
      Last check: {formatWhen(last.created_at)}, {formatCount(last.sample_size)} pictures, {formatCount(last.errors)} wrong,{' '}
      {last.passed ? 'passed' : 'not passed'}.
    </p>
  )
}

function Sample({
  item,
  marked,
  total,
  busy,
  onMark,
}: {
  item: CalibrationItem
  marked: number
  total: number
  busy: boolean
  onMark: (item: CalibrationItem, correct: boolean) => void
}) {
  const handler = useRef<(e: KeyboardEvent) => void>(() => undefined)
  useEffect(() => {
    handler.current = (e) => {
      const t = e.target as HTMLElement
      if (['SELECT', 'INPUT', 'TEXTAREA'].includes(t.tagName) || e.ctrlKey || e.metaKey || e.altKey || busy) return
      const key = e.key.toLowerCase()
      if (key === 'j') onMark(item, true)
      else if (key === 'k') onMark(item, false)
      else return
      e.preventDefault()
    }
  })
  useEffect(() => {
    const on = (e: KeyboardEvent) => handler.current(e)
    document.addEventListener('keydown', on)
    return () => document.removeEventListener('keydown', on)
  }, [])

  return (
    <section aria-label="Sample picture" className="flex flex-col gap-3">
      <p role="status" className="font-medium">
        {formatCount(marked + 1)} of {formatCount(total)}
      </p>
      <p className="text-sm text-neutral-600 dark:text-neutral-400">
        Sift suggests deleting this picture. Is that right? Press J for Correct or K for Wrong.
      </p>
      <img
        key={item.id}
        src={withToken(`/api/media/${item.id}/preview?max=1600`)}
        alt={baseName(item.path)}
        className="max-h-[60vh] max-w-full self-start rounded-md bg-neutral-100 object-contain dark:bg-neutral-900"
      />
      <p className="truncate text-xs text-neutral-600 dark:text-neutral-400" title={item.path}>
        {item.path}
      </p>
      <div className="flex gap-3">
        <button type="button" disabled={busy} onClick={() => onMark(item, true)} className={`${BTN} border-green-700 bg-green-700 text-white`}>
          Correct (J)
        </button>
        <button type="button" disabled={busy} onClick={() => onMark(item, false)} className={`${BTN} border-red-700 bg-red-700 text-white`}>
          Wrong (K)
        </button>
      </div>
    </section>
  )
}

export default function CalibrationPage() {
  const qc = useQueryClient()
  const calibration = useQuery({
    queryKey: KEY,
    queryFn: () => apiFetch<CalibrationState>('/api/cleanup/calibration'),
    retry: false,
  })
  const [message, setMessage] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)

  const start = useMutation({
    mutationFn: () => post<CalibrationState>('/api/cleanup/calibration/start'),
    onMutate: () => {
      setMessage(null)
      setResult(null)
    },
    onSuccess: (s) => qc.setQueryData(KEY, s),
    onError: (e) => setMessage(plain(e)),
  })
  const mark = useMutation({
    mutationFn: (v: { item: CalibrationItem; correct: boolean }) =>
      post<CalibrationMark>('/api/cleanup/calibration/mark', { content_hash: v.item.content_hash, correct: v.correct }),
    onMutate: () => setMessage(null),
    onSuccess: (r, v) => {
      if (r.finished) {
        if (r.sample_size != null && r.errors != null) {
          setResult(outcome({ errors: r.errors, sample_size: r.sample_size, passed: r.passed === true, threshold: r.threshold ?? 0 }))
        }
        void qc.invalidateQueries({ queryKey: ['cleanup'] })
        return
      }
      qc.setQueryData<CalibrationState>(KEY, (old) =>
        old && {
          ...old,
          marked: old.marked + 1,
          items: old.items.map((i) => (i.content_hash === v.item.content_hash ? { ...i, verdict: v.correct } : i)),
        },
      )
    },
    onError: (e) => setMessage(plain(e)),
  })

  const data = calibration.data
  const open = data?.items.find((i) => i.verdict === null) ?? null
  const sampling = open != null && result == null

  return (
    <div className="flex max-w-3xl flex-col gap-4 p-6">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold">Calibration</h1>
        <Link to="/cleanup/review?tab=junk" className="text-sm underline">
          Back to Junk
        </Link>
        <Link to="/cleanup" className="text-sm underline">
          Back to Cleanup
        </Link>
      </div>
      {!data && <p className="text-neutral-600 dark:text-neutral-400">{calibration.error ? plain(calibration.error) : 'Loading…'}</p>}
      {message && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {message}
        </p>
      )}
      {result && (
        <p role="status" className="rounded-md bg-neutral-100 px-3 py-2 font-medium dark:bg-neutral-900">
          {result}
        </p>
      )}
      {data && sampling && <Sample item={open} marked={data.marked} total={data.total} busy={mark.isPending} onMark={(item, correct) => mark.mutate({ item, correct })} />}
      {data && !sampling && (
        <>
          <p>
            Sift checks a sample of its suggestions with you. If every one is right, it starts ticking suggestions for you. You look at one
            picture at a time.
          </p>
          <Last last={data.last} />
          <div>
            <button type="button" disabled={start.isPending} onClick={() => start.mutate()} className={`${BTN} ${PLAIN}`}>
              {result ? 'Check a fresh sample' : 'Start'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
