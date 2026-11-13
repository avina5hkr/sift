import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { isConnected } from '../../lib/backupState'
import type { AttachedPhone, SetupPhone } from '../../lib/types'
import { useStatus } from '../../lib/useStatus'
import { Button, ErrorNote, StepHeading, type StepProps } from './ui'
import { plain, post } from './request'

const POLL_MS = 2000

const phoneName = (p: AttachedPhone) => `${p.model ?? 'Phone'} ending ${p.last4}`

/**
 * The picker: shown when more than one phone is plugged in (emulators are never listed). The
 * serial number is never shown, only its last four characters. `endpoint` is the choose action
 * (first run) or the switch action (Settings, with a typed confirmation).
 */
export function PhonePicker({
  phones,
  busy,
  onChoose,
  action = 'Use this phone',
  blocked = false,
}: {
  phones: AttachedPhone[]
  busy: boolean
  onChoose: (ref: string) => void
  action?: string
  /** Something else must happen first (the typed confirmation of Switch phone). */
  blocked?: boolean
}) {
  const [picked, setPicked] = useState<string | null>(null)
  const current = picked ?? phones.find((p) => p.stored)?.ref ?? null
  const chosen = phones.find((p) => p.ref === current)
  return (
    <fieldset className="flex flex-col gap-2 rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
      <legend className="px-1 text-sm font-medium">Which phone should Sift back up?</legend>
      {phones.map((p) => (
        <label key={p.ref} className="flex items-center gap-2 text-sm">
          <input type="radio" name="phone" checked={current === p.ref} onChange={() => setPicked(p.ref)} />
          <span>
            {phoneName(p)}
            {p.stored && ' (chosen)'}
            {p.waiting_for_allow && ' (tap Allow on this phone first)'}
            {!p.ready && !p.waiting_for_allow && ' (not responding)'}
          </span>
        </label>
      ))}
      <div>
        <Button primary disabled={!chosen || !chosen.ready || busy || blocked} onClick={() => chosen && onChoose(chosen.ref)}>
          {action}
        </Button>
      </div>
    </fieldset>
  )
}

export function Connect({ next }: StepProps) {
  const qc = useQueryClient()
  const status = useStatus()
  const device = status.data?.device
  const phones = useQuery({
    queryKey: ['setup', 'devices'],
    queryFn: () => apiFetch<{ phones: AttachedPhone[] }>('/api/setup/devices'),
    refetchInterval: POLL_MS,
    retry: false,
  })
  const [error, setError] = useState<string | null>(null)
  const choose = useMutation({
    mutationFn: (ref: string) => post<SetupPhone>('/api/setup/phone', { ref }),
    onMutate: () => setError(null),
    onError: (e) => setError(plain(e)),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['setup'] })
      void qc.invalidateQueries({ queryKey: ['status'] })
    },
  })

  const list = phones.data?.phones ?? []
  const several = list.length > 1
  const connected = isConnected(device)
  return (
    <section aria-labelledby="step-title" className="flex flex-col gap-4">
      <StepHeading>Connect your phone</StepHeading>
      <p>Plug your phone into this Mac with the USB cable and unlock it. Sift notices it by itself.</p>
      <p role="status" aria-live="polite" className="text-lg font-medium" data-testid="connect-state">
        {connected
          ? `Connected: ${device?.model ?? 'your phone'}.`
          : device?.state === 'unauthorized'
            ? 'Waiting for you to allow this Mac on the phone.'
            : (device?.message ?? 'Looking for your phone…')}
      </p>
      {device?.state === 'unauthorized' && (
        <p>
          Look at the phone&apos;s screen. Tick Always allow from this computer and tap Allow. If no question appears,
          unplug the cable, plug it in again, and make sure the USB mode is File transfer.
        </p>
      )}
      {several && (
        <>
          <p>More than one phone is plugged in. Choose the one to back up.</p>
          <PhonePicker phones={list} busy={choose.isPending} onChoose={(ref) => choose.mutate(ref)} />
        </>
      )}
      <ErrorNote message={error} />
      <div>
        <Button primary disabled={!connected} onClick={next}>
          Continue
        </Button>
        {!connected && (
          <>
            <Button className="ml-3" onClick={next}>
              Skip for now
            </Button>
            <p className="mt-2 text-sm">
              Continue is available once your phone is connected. You can skip this and connect the phone later, but the
              next steps cannot measure your phone until then.
            </p>
          </>
        )}
      </div>
    </section>
  )
}
