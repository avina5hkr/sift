import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import type { AttachedPhone, SetupPhone } from '../../lib/types'
import { PhonePicker } from './Connect'
import { Button, ErrorNote } from './ui'
import { plain, post } from './request'

const WORD = 'SWITCH'

/**
 * Settings' "Switch phone": back up a different phone from now on. The old phone's backups stay
 * where they are. It needs the word SWITCH typed; the server checks it again.
 */
export function SwitchPhone() {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const phones = useQuery({
    queryKey: ['setup', 'devices'],
    queryFn: () => apiFetch<{ phones: AttachedPhone[] }>('/api/setup/devices'),
    enabled: open,
    refetchInterval: open ? 2000 : false,
    retry: false,
  })
  const state = useQuery({ queryKey: ['setup', 'state'], queryFn: () => apiFetch<{ phone: SetupPhone | null }>('/api/setup/state') })
  const switchTo = useMutation({
    mutationFn: (ref: string) => post<SetupPhone>('/api/setup/switch-phone', { ref, confirmation: typed }),
    onMutate: () => {
      setError(null)
      setDone(null)
    },
    onSuccess: (phone) => {
      setDone(
        `Sift now backs up ${phone.model ?? 'the new phone'} ending ${phone.last4 ?? ''}.` +
          (phone.label ? ` Its files go into the folder ${phone.label}.` : ''),
      )
      setTyped('')
      void qc.invalidateQueries({ queryKey: ['setup'] })
      void qc.invalidateQueries({ queryKey: ['status'] })
    },
    onError: (e) => setError(plain(e)),
  })

  const current = state.data?.phone
  return (
    <div className="flex flex-col gap-3">
      <p>
        {current
          ? `Sift backs up ${current.model ?? 'your phone'} ending ${current.last4 ?? ''}.`
          : 'Sift has not chosen a phone yet.'}
      </p>
      <div>
        <Button aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          Switch phone…
        </Button>
      </div>
      {open && (
        <div className="flex flex-col gap-3 rounded-md border border-neutral-200 p-3 dark:border-neutral-800">
          <p className="text-sm">
            Plug in the new phone (any other phone can stay plugged in), choose it below, and type {WORD}. The new
            phone gets its own folder inside your backup folder, named after its model. The old phone&apos;s backups
            and its folder stay exactly where they are. You cannot switch while a backup is running.
          </p>
          {phones.isError && <ErrorNote message={plain(phones.error)} />}
          {phones.data && phones.data.phones.length === 0 && <p role="status">No phone is connected.</p>}
          {phones.data && phones.data.phones.length > 0 && (
            <>
              <div className="flex flex-col gap-1">
                <label htmlFor="switch-word" className="text-sm font-medium">
                  Type {WORD} to confirm
                </label>
                <input
                  id="switch-word"
                  value={typed}
                  autoComplete="off"
                  onChange={(e) => setTyped(e.target.value)}
                  className="w-48 rounded-md border border-neutral-300 bg-transparent px-3 py-1.5 text-sm dark:border-neutral-700"
                />
              </div>
              <PhonePicker
                phones={phones.data.phones}
                busy={switchTo.isPending}
                blocked={typed.trim().toUpperCase() !== WORD}
                action="Switch to this phone"
                onChoose={(ref) => switchTo.mutate(ref)}
              />
            </>
          )}
          <ErrorNote message={error} />
          {done && <p role="status">{done}</p>}
        </div>
      )}
    </div>
  )
}
