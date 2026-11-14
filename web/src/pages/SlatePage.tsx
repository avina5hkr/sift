import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { ReasonButton } from '../components/backup/ReasonButton'
import { DeleteFlow } from '../components/delete/DeleteFlow'
import { useDeleteRun } from '../components/delete/useDeleteRun'
import { AdjustStep } from '../components/slate/AdjustStep'
import { DispositionStep, defaultDispositions, type Dispositions } from '../components/slate/DispositionStep'
import { GapsStep } from '../components/slate/GapsStep'
import { PlanSummary } from '../components/slate/PlanSummary'
import { RulesStep } from '../components/slate/RulesStep'
import { formatBytes, formatCount } from '../lib/format'
import { MUTED, SLATE_BTN, SLATE_PRIMARY } from '../lib/slate'
import type { Disposition, SlatePlan } from '../lib/types'

const STEPS = ['Plan', 'Back up gaps', 'Adjust', 'What happens to the backup', 'Check and delete']

export default function SlatePage() {
  const qc = useQueryClient()
  const [step, setStep] = useState(0)
  const [plan, setPlan] = useState<SlatePlan | null>(null)
  const [dispositions, setDispositions] = useState<Dispositions>(defaultDispositions)
  const flow = useDeleteRun({ onFinished: () => void qc.invalidateQueries({ queryKey: ['slate'] }) })

  const lastStep = STEPS.length - 1
  const gaps = plan?.gaps.files ?? 0
  const nothing = plan != null && plan.remove.files === 0
  const nextReason =
    plan == null
      ? 'Make the plan first.'
      : step === 1 && gaps > 0
        ? 'Back up or keep the files that are not backed up yet first.'
        : step >= 2 && nothing
          ? 'There is nothing to remove.'
          : null

  const startChecks = () => {
    if (!plan) return
    const body: Record<string, Disposition> = {}
    for (const [k, v] of Object.entries(dispositions)) if (plan.categories[k as keyof typeof plan.categories]?.files > 0) body[k] = v
    void flow.start({ source: 'slate', plan: plan.id, dispositions: body })
  }

  return (
    <div className="flex max-w-4xl flex-col gap-5 p-6">
      <div>
        <h1 className="text-xl font-semibold">Clean slate</h1>
        <p className={`text-sm ${MUTED}`}>
          Keep what you list and remove the rest from the phone, after every file is safely backed up.{' '}
          <Link to="/cleanup" className="underline">Back to Cleanup</Link>
        </p>
      </div>

      <ol aria-label="Steps" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? 'step' : undefined} className={i === step ? 'font-semibold' : MUTED}>
            {i + 1}. {s}
          </li>
        ))}
      </ol>

      {plan && step > 0 && (
        <section aria-label="Totals" className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
          <PlanSummary plan={plan} />
        </section>
      )}

      <section aria-label={STEPS[step]} className="flex flex-col gap-4">
        <h2 className="font-medium">{STEPS[step]}</h2>
        {step === 0 && (
          <>
            <RulesStep plan={plan} onPlan={setPlan} />
            {plan && (
              <div className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
                <PlanSummary plan={plan} />
              </div>
            )}
          </>
        )}
        {step === 1 && plan && <GapsStep plan={plan} onPlan={setPlan} />}
        {step === 2 && plan && <AdjustStep plan={plan} onPlan={setPlan} />}
        {step === 3 && plan && <DispositionStep plan={plan} value={dispositions} onChange={setDispositions} />}
        {step === 4 && plan && (
          <div className="flex flex-col gap-3">
            <p>
              Sift will check {formatCount(plan.remove.files)} {plan.remove.files === 1 ? 'file' : 'files'} ({formatBytes(plan.remove.bytes)}) against
              your backup and the phone. Nothing is deleted until you confirm.
            </p>
            <div>
              <button type="button" onClick={startChecks} disabled={flow.phase !== 'idle' || nothing} className={SLATE_PRIMARY}>
                Check the files
              </button>
            </div>
          </div>
        )}
      </section>

      <div className="flex items-center gap-2">
        {step > 0 && (
          <button type="button" onClick={() => setStep(step - 1)} className={SLATE_BTN}>
            Back
          </button>
        )}
        {step < lastStep && (
          <ReasonButton primary reason={nextReason} onClick={() => setStep(step + 1)}>
            Next
          </ReasonButton>
        )}
      </div>

      <DeleteFlow flow={flow} />
    </div>
  )
}
