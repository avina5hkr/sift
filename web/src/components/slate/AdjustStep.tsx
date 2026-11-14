import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatBytes, formatCount, shortPath } from '../../lib/format'
import { MUTED, SLATE_BTN } from '../../lib/slate'
import type { SlateGroup, SlatePlan, SlateSide } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'
import { Thumb } from '../browse/Thumb'
import { useSlateFiles } from './useSlateFiles'

const TABS: { side: SlateSide; label: string; empty: string }[] = [
  { side: 'remove', label: 'To remove', empty: 'Nothing is planned for removal.' },
  { side: 'keep', label: 'To keep', empty: 'Nothing is kept by your rules.' },
  { side: 'protected', label: 'Protected', empty: 'No protected files.' },
]

type Props = { plan: SlatePlan; onPlan: (p: SlatePlan) => void }

/** "Taken 41 times, 12 of them in a folder you keep": the top reasons of a group. */
function topReasons(reasons: Record<string, number>): string {
  return Object.entries(reasons)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([r, n]) => `${r} (${formatCount(n)})`)
    .join(', ')
}

function Folder({ plan, side, folder, onBack, onKeep, busy }: { plan: SlatePlan; side: SlateSide; folder: string; onBack: () => void; onKeep: (ids: number[]) => void; busy: boolean }) {
  const list = useSlateFiles(plan.id, side, folder)
  const files = (list.data?.pages ?? []).flatMap((p) => p.files)
  const total = list.data?.pages[0]?.total_files ?? 0
  const [ticked, setTicked] = useState<Set<number>>(() => new Set())
  const toggle = (id: number) =>
    setTicked((s) => {
      const n = new Set(s)
      if (!n.delete(id)) n.add(id)
      return n
    })
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={onBack} className={SLATE_BTN}>
          Back to folders
        </button>
        <h3 className="break-all font-medium">{folder}</h3>
        {side === 'remove' && (
          <button
            type="button"
            disabled={ticked.size === 0 || busy}
            onClick={() => {
              onKeep([...ticked])
              setTicked(new Set())
            }}
            className={SLATE_BTN}
          >
            Keep selected{ticked.size > 0 ? ` (${formatCount(ticked.size)})` : ''}
          </button>
        )}
      </div>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-neutral-200 dark:border-neutral-800">
            {side === 'remove' && (
              <th scope="col" className="w-8 py-2">
                <span className="sr-only">Select</span>
              </th>
            )}
            <th scope="col" className="py-2 pr-3 font-medium">File</th>
            <th scope="col" className="py-2 pr-3 font-medium">Size</th>
            <th scope="col" className="py-2 font-medium">Why</th>
          </tr>
        </thead>
        <tbody>
          {files.map((f) => {
            const name = f.path.slice(f.path.lastIndexOf('/') + 1)
            return (
              <tr key={f.id} className="border-b border-neutral-100 dark:border-neutral-900">
                {side === 'remove' && (
                  <td className="py-1.5">
                    <input type="checkbox" aria-label={`Select ${name}`} checked={ticked.has(f.id)} onChange={() => toggle(f.id)} />
                  </td>
                )}
                <td className="py-1.5 pr-3">
                  <span className="flex items-center gap-2">
                    <Thumb file={f} />
                    <span className="min-w-0 truncate" title={f.path}>{shortPath(f.path)}</span>
                  </span>
                </td>
                <td className="whitespace-nowrap py-1.5 pr-3">{formatBytes(f.size)}</td>
                <td className={`py-1.5 ${MUTED}`}>{f.reason}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {list.hasNextPage && (
        <div className="flex items-center gap-3">
          <button type="button" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()} className={SLATE_BTN}>
            Show more
          </button>
          <span className={`text-sm ${MUTED}`}>
            {formatCount(files.length)} of {formatCount(total)} files shown
          </span>
        </div>
      )}
    </div>
  )
}

/** Step 3: look at what is planned, move folders or files to Keep. */
export function AdjustStep({ plan, onPlan }: Props) {
  const qc = useQueryClient()
  const [side, setSide] = useState<SlateSide>('remove')
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const tree = useQuery({
    queryKey: ['slate', 'tree', plan.id, side],
    queryFn: () => apiFetch<SlateGroup[]>(`/api/slate/plan/${plan.id}/tree?side=${side}`),
  })
  const keep = useMutation({
    mutationFn: (body: { ids: number[]; folders: string[] }) => post<SlatePlan>(`/api/slate/plan/${plan.id}/keep`, body),
    onMutate: () => setError(null),
    onSuccess: (p) => {
      onPlan(p)
      void qc.invalidateQueries({ queryKey: ['slate', 'tree'] })
      void qc.invalidateQueries({ queryKey: ['slate', 'files'] })
    },
    onError: (e) => setError(plain(e)),
  })
  const tab = TABS.find((t) => t.side === side) ?? TABS[0]!
  const groups = tree.data ?? []

  return (
    <div className="flex flex-col gap-3">
      <div role="tablist" aria-label="Plan" className="flex gap-1">
        {TABS.map((t) => (
          <button
            key={t.side}
            type="button"
            role="tab"
            aria-selected={t.side === side}
            onClick={() => {
              setSide(t.side)
              setOpen(null)
            }}
            className={`rounded-md px-3 py-1.5 text-sm ${t.side === side ? 'bg-neutral-200 font-medium dark:bg-neutral-800' : 'hover:bg-neutral-100 dark:hover:bg-neutral-900'}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {error}
        </p>
      )}
      {open != null ? (
        <Folder
          plan={plan}
          side={side}
          folder={open}
          busy={keep.isPending}
          onBack={() => setOpen(null)}
          onKeep={(ids) => keep.mutate({ ids, folders: [] })}
        />
      ) : (
        <>
          {tree.error && (
            <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
              {plain(tree.error)}
            </p>
          )}
          {tree.data && groups.length === 0 && <p className={MUTED}>{tab.empty}</p>}
          {groups.length > 0 && (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-200 dark:border-neutral-800">
                  <th scope="col" className="py-2 pr-3 font-medium">Folder</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Files</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Size</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Samples</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Why</th>
                  {side === 'remove' && (
                    <th scope="col" className="py-2 font-medium">
                      <span className="sr-only">Action</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.folder} className="border-b border-neutral-100 dark:border-neutral-900">
                    <td className="py-1.5 pr-3">
                      <button type="button" onClick={() => setOpen(g.folder)} className="break-all text-left font-medium hover:underline">
                        {shortPath(g.folder)}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3">{formatCount(g.files)}</td>
                    <td className="whitespace-nowrap py-1.5 pr-3">{formatBytes(g.bytes)}</td>
                    <td className="py-1.5 pr-3">
                      <span className="flex gap-1">
                        {g.sample_ids.slice(0, 4).map((id) => (
                          <Thumb key={id} file={{ id, kind: 'image' }} />
                        ))}
                      </span>
                    </td>
                    <td className={`py-1.5 pr-3 ${MUTED}`}>{topReasons(g.reasons)}</td>
                    {side === 'remove' && (
                      <td className="py-1.5">
                        <button
                          type="button"
                          aria-label={`Keep this folder: ${shortPath(g.folder)}`}
                          disabled={keep.isPending}
                          onClick={() => keep.mutate({ ids: [], folders: [g.folder] })}
                          className={SLATE_BTN}
                        >
                          Keep this folder
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}
