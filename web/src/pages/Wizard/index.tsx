import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { apiFetch } from '../../lib/api'
import type { SetupState } from '../../lib/types'
import { useStatus } from '../../lib/useStatus'
import { Adb } from './Adb'
import { Connect } from './Connect'
import { Done } from './Done'
import { MacFolder } from './MacFolder'
import { Ssd } from './Ssd'
import { UsbDebugging } from './UsbDebugging'
import { Welcome } from './Welcome'
import { WhatToBackUp } from './WhatToBackUp'
import { ErrorNote } from './ui'
import { plain, post } from './request'

const STEPS = ['Welcome', 'USB debugging', 'Phone tools', 'Connect', 'Backup folder', 'SSD', 'What to back up', 'Done']

/** The first-run wizard. It opens by itself until setup is finished, and is always reachable from Settings. */
export default function WizardPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const status = useStatus()
  const setup = useQuery({ queryKey: ['setup', 'state'], queryFn: () => apiFetch<SetupState>('/api/setup/state') })
  const [step, setStep] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const complete = () => post<SetupState>('/api/setup/complete')
  const settle = async () => {
    await qc.invalidateQueries({ queryKey: ['setup', 'state'] })
  }
  const skip = useMutation({
    mutationFn: complete,
    onError: (e) => setError(plain(e)),
    onSuccess: async () => {
      await settle()
      navigate('/backup')
    },
  })
  const finish = async (names: string[] | null) => {
    await complete()
    if (names) await post('/api/backup/start', { sets: names })
    await settle()
    await qc.invalidateQueries({ queryKey: ['backup'] })
    navigate('/backup')
  }

  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1))
  const back = () => setStep((s) => Math.max(s - 1, 0))
  const state = setup.data

  let body
  if (!state) {
    body = setup.isError ? <ErrorNote message={plain(setup.error)} /> : <p role="status">Loading…</p>
  } else {
    switch (step) {
      case 0:
        body = <Welcome next={next} skip={() => skip.mutate()} skipping={skip.isPending} />
        break
      case 1:
        body = <UsbDebugging next={next} model={status.data?.device?.model ?? state.phone?.model} />
        break
      case 2:
        body = <Adb next={next} />
        break
      case 3:
        body = <Connect next={next} />
        break
      case 4:
        body = <MacFolder next={next} setup={state} />
        break
      case 5:
        body = <Ssd next={next} />
        break
      case 6:
        body = <WhatToBackUp next={next} />
        break
      default:
        body = <Done finish={finish} />
    }
  }

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-5 p-6">
      <header>
        <h1 className="text-2xl font-semibold">Set up Sift</h1>
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
          Step {step + 1} of {STEPS.length}: {STEPS[step]}
        </p>
        <ol aria-label="Setup steps" className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
          {STEPS.map((name, i) => (
            <li
              key={name}
              aria-current={i === step ? 'step' : undefined}
              className={i === step ? 'font-semibold underline' : i < step ? '' : 'text-neutral-600 dark:text-neutral-400'}
            >
              {i + 1}. {name}
            </li>
          ))}
        </ol>
      </header>
      <ErrorNote message={error} />
      {body}
      {step > 0 && (
        <div>
          <button
            type="button"
            onClick={back}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            Back
          </button>
        </div>
      )}
    </div>
  )
}
