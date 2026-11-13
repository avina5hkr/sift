import type { Problem } from '../../lib/types'

type Props = {
  problems: Problem[]
  /** Disable Repair while one is running or a task holds the destinations. */
  busy: boolean
  onRepair: (p: Problem) => void
}

const WHAT: Record<Problem['kind'], string> = {
  hash_mismatch: 'A copy does not match the original',
  missing_file: 'A file is missing',
  older_copy_differs: 'An older copy is different',
  unreadable: 'A file could not be read',
}
const WHERE = { mac: 'on the Mac', ssd: 'on the SSD' }

/** One plain line per open problem: what, where, which file. Hidden when there are none. */
export function ProblemsCard({ problems, busy, onRepair }: Props) {
  if (problems.length === 0) return null
  return (
    <section aria-labelledby="problems-title" className="rounded-lg border border-amber-300 p-4 dark:border-amber-800">
      <h2 id="problems-title" className="text-lg font-semibold">
        Problems
      </h2>
      <ul className="mt-2 flex flex-col gap-2">
        {problems.map((p) => (
          <li key={p.id} className="flex flex-wrap items-center gap-3 text-sm">
            <span className="min-w-0 flex-1" title={p.detail}>
              {WHAT[p.kind]} {p.destination ? WHERE[p.destination] : 'in your backup'}:{' '}
              <span className="font-mono break-all">{p.rel_path}</span>
            </span>
            {p.repairable && (
              <button
                type="button"
                onClick={() => onRepair(p)}
                disabled={busy}
                aria-label={`Repair ${p.rel_path}`}
                className="rounded-md border border-neutral-300 px-3 py-1 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800"
              >
                Repair
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
