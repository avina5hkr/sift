import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes, formatCount, shortPath } from '../../lib/format'
import { GAP_LABEL, MUTED, SLATE_BTN, SLATE_PRIMARY } from '../../lib/slate'
import type { SlateFilesPage, SlatePlan } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'
import { useSlateFiles } from './useSlateFiles'

type Props = { plan: SlatePlan; onPlan: (p: SlatePlan) => void }

/** Every id of the files with this gap, read page by page. */
async function allIds(planId: number, gap: string): Promise<number[]> {
  const ids: number[] = []
  for (let offset = 0; ; ) {
    const page = await apiFetch<SlateFilesPage>(`/api/slate/plan/${planId}/files?side=gaps&offset=${offset}`)
    for (const f of page.files) if (f.gap === gap) ids.push(f.id)
    offset += page.files.length
    if (page.files.length === 0 || offset >= page.total_files) return ids
  }
}

/** Step 2: files to remove that are not backed up yet. */
export function GapsStep({ plan, onPlan }: Props) {
  const qc = useQueryClient()
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const gaps = plan.gaps.files
  const list = useSlateFiles(plan.id, 'gaps', '', gaps > 0)
  const files = (list.data?.pages ?? []).flatMap((p) => p.files)

  const backup = useMutation({
    mutationFn: () => post<{ job_id: number; sets: string[] }>(`/api/slate/plan/${plan.id}/backup-gaps`),
    onMutate: () => {
      setError(null)
      setMessage(null)
    },
    onSuccess: (r) =>
      setMessage(
        `A backup of ${formatCount(r.sets.length)} ${r.sets.length === 1 ? 'set' : 'sets'} has started. Come back when the backup finishes, then press Check again.`,
      ),
    onError: (e) => setError(plain(e)),
  })
  const check = useMutation({
    mutationFn: () => apiFetch<SlatePlan>(`/api/slate/plan/${plan.id}`),
    onMutate: () => setError(null),
    onSuccess: (p) => {
      onPlan(p)
      void qc.invalidateQueries({ queryKey: ['slate', 'files'] })
    },
    onError: (e) => setError(plain(e)),
  })
  const keep = useMutation({
    mutationFn: async () => post<SlatePlan>(`/api/slate/plan/${plan.id}/keep`, { ids: await allIds(plan.id, 'no_set'), folders: [] }),
    onMutate: () => setError(null),
    onSuccess: (p) => {
      onPlan(p)
      void qc.invalidateQueries({ queryKey: ['slate'] })
    },
    onError: (e) => setError(plain(e)),
  })

  if (gaps === 0) {
    return (
      <div className="flex flex-col gap-3">
        <p>Every file to remove is already backed up.</p>
        <div>
          <button type="button" onClick={() => check.mutate()} disabled={check.isPending} className={SLATE_BTN}>
            Check again
          </button>
        </div>
        {error && (
          <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
            {error}
          </p>
        )}
      </div>
    )
  }

  const noSet = plan.gaps_no_set
  const backable = gaps - noSet.files
  return (
    <div className="flex flex-col gap-3">
      <p className="font-medium">
        {formatCount(gaps)} {gaps === 1 ? 'file' : 'files'} to remove {gaps === 1 ? 'is' : 'are'} not backed up yet ({formatBytes(plan.gaps.bytes)})
      </p>
      <p className={`text-sm ${MUTED}`}>Sift only removes files that are safely backed up. Back them up, or keep them on the phone.</p>
      <div className="flex flex-wrap items-center gap-2">
        {backable > 0 && (
          <button type="button" onClick={() => backup.mutate()} disabled={backup.isPending} className={SLATE_PRIMARY}>
            Back up now
          </button>
        )}
        {noSet.files > 0 && (
          <button type="button" onClick={() => keep.mutate()} disabled={keep.isPending} className={SLATE_BTN}>
            Keep these {formatCount(noSet.files)} {noSet.files === 1 ? 'file' : 'files'} ({formatBytes(noSet.bytes)})
          </button>
        )}
        <button type="button" onClick={() => check.mutate()} disabled={check.isPending} className={SLATE_BTN}>
          Check again
        </button>
      </div>
      {noSet.files > 0 && (
        <p className={`text-sm ${MUTED}`}>
          {formatCount(noSet.files)} {noSet.files === 1 ? 'file is' : 'files are'} not in any backup set, so Sift cannot back {noSet.files === 1 ? 'it' : 'them'} up.
        </p>
      )}
      {message && (
        <p role="status" className="text-sm">
          {message}
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {error}
        </p>
      )}
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-neutral-200 dark:border-neutral-800">
            <th scope="col" className="py-2 pr-3 font-medium">Path</th>
            <th scope="col" className="py-2 pr-3 font-medium">Size</th>
            <th scope="col" className="py-2 font-medium">Why</th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => (
            <tr key={f.id} className="border-b border-neutral-100 dark:border-neutral-900">
              <td className="break-all py-1.5 pr-3" title={f.path}>{shortPath(f.path)}</td>
              <td className="whitespace-nowrap py-1.5 pr-3">{formatBytes(f.size)}</td>
              <td className={`py-1.5 ${MUTED}`}>{GAP_LABEL[f.gap ?? ''] ?? 'Not backed up yet'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {list.hasNextPage && (
        <div className="flex items-center gap-3">
          <button type="button" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()} className={SLATE_BTN}>
            Show more
          </button>
          <span className={`text-sm ${MUTED}`}>
            {formatCount(files.length)} of {formatCount(gaps)} files shown
          </span>
        </div>
      )}
    </div>
  )
}
