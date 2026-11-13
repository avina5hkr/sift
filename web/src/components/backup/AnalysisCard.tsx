import { formatBytes, formatCount, formatEta } from '../../lib/format'
import type { Analysis, AnalyseGroup } from '../../lib/types'

const GROUPS = [
  ['verified', 'Backed up and verified'],
  ['not_verified', 'Not verified yet'],
  ['not_backed_up', 'Not backed up'],
  ['problems', 'Problems'],
] as const

const count = (g: AnalyseGroup) => `${formatCount(g.files)} ${g.files === 1 ? 'file' : 'files'}, ${formatBytes(g.bytes)}`

/** What Analyse phone found, per set. Read-only: nothing here changes the phone or the backup. */
export function AnalysisCard({ analysis }: { analysis: Analysis }) {
  const result = analysis.result
  return (
    <section aria-labelledby="analysis-title" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <h2 id="analysis-title" className="text-lg font-semibold">
        What is on the phone
      </h2>
      {analysis.error && (
        <p role="alert" className="mt-1 text-sm font-medium">
          {analysis.error}
        </p>
      )}
      {!result && !analysis.error && <p className="mt-1 text-sm">Still looking. The answer will appear here.</p>}
      {result &&
        Object.entries(result.sets).map(([name, set]) => (
          <div key={name} className="mt-3">
            <h3 className="font-medium">{name}</h3>
            <dl className="mt-1 grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
              {GROUPS.map(([key, label]) => (
                <div key={key}>
                  <dt className="text-neutral-600 dark:text-neutral-400">{label}</dt>
                  <dd className="font-medium">{count(set[key])}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      {result && (
        <p className="mt-3 text-sm text-neutral-700 dark:text-neutral-300">
          {result.estimate_s > 0
            ? `Checking what is left on the phone would take ${formatEta(result.estimate_s)}.`
            : 'Nothing is left to check on the phone.'}
        </p>
      )}
      {result && result.warnings.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm">
          {result.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
