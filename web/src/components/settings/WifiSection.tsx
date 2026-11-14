import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiFetch } from '../../lib/api'
import { formatWhen } from '../../lib/format'
import { plain, post } from '../../pages/Wizard/request'
import { BTN, FIELD, MUTED } from '../apps/ui'

type Wifi = {
  remembered: { name: string; address: string; paired_at: number; last_connected: number | null }[]
  found: { name: string; kind: string; address: string }[]
  allow_delete: boolean
}

const PAIRING = '_adb-tls-pairing._tcp'

/** Wi-Fi pairing for the phone: pair with a code, connect to a phone Sift knows, forget one. */
export function WifiSection() {
  const qc = useQueryClient()
  const [address, setAddress] = useState('')
  const [code, setCode] = useState('')
  const [note, setNote] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wifi = useQuery({
    queryKey: ['phone', 'wifi'],
    queryFn: () => apiFetch<Wifi>('/api/phone/wifi'),
    refetchInterval: 5000,
    retry: false,
  })
  const done = () => {
    void qc.invalidateQueries({ queryKey: ['phone', 'wifi'] })
    void qc.invalidateQueries({ queryKey: ['status'] })
  }
  const fail = (e: unknown) => {
    setNote(null)
    setError(plain(e))
  }
  const pair = useMutation({
    mutationFn: () => post<{ paired: boolean; connected: string | null }>('/api/phone/wifi/pair', { address: address.trim(), code }),
    onMutate: () => {
      setError(null)
      setNote(null)
    },
    onSuccess: (r) => {
      setNote(r.connected ? `Paired and connected to ${r.connected}.` : 'Paired. Connect it from the list below.')
      setCode('')
      done()
    },
    onError: fail,
  })
  const connect = useMutation({
    mutationFn: (addr: string) => post<unknown>('/api/phone/wifi/connect', { address: addr }),
    onMutate: () => {
      setError(null)
      setNote(null)
    },
    onSuccess: () => {
      setNote('Connected.')
      done()
    },
    onError: fail,
  })
  const forget = useMutation({
    mutationFn: (p: { name: string; address: string }) => post<void>('/api/phone/wifi/forget', p),
    onMutate: () => setError(null),
    onSuccess: done,
    onError: fail,
  })

  const found = (wifi.data?.found ?? []).filter((s) => s.kind !== PAIRING)
  const pairingOffers = (wifi.data?.found ?? []).filter((s) => s.kind === PAIRING)
  const valid = address.trim() !== '' && /^\d{6}$/.test(code)

  return (
    <section aria-labelledby="wifi-title" className="mt-6 flex flex-col gap-3">
      <h2 id="wifi-title" className="text-lg font-semibold">
        Wi-Fi
      </h2>
      <p className={MUTED}>
        On the phone: Developer options, Wireless debugging, Pair device with pairing code. Then type the address and the
        6-digit code it shows here. The phone and this Mac must be on the same network.
      </p>
      {pairingOffers.length > 0 && (
        <p className={`text-sm ${MUTED}`}>
          Pairing offered by{' '}
          {pairingOffers.map((s) => (
            <button key={s.address} type="button" className="mr-2 underline" onClick={() => setAddress(s.address)}>
              {s.name} ({s.address})
            </button>
          ))}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm font-medium">
          Pairing address
          <input className={`${FIELD} w-52`} placeholder="192.168.1.20:37099" value={address} onChange={(e) => setAddress(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Pairing code
          <input
            className={`${FIELD} w-28`}
            inputMode="numeric"
            maxLength={6}
            placeholder="123456"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
          />
        </label>
        <button type="button" className={BTN} disabled={!valid || pair.isPending} onClick={() => pair.mutate()}>
          {pair.isPending ? 'Pairing…' : 'Pair'}
        </button>
      </div>
      {note && <p role="status" className="text-sm text-green-700 dark:text-green-400">{note}</p>}
      {error && <p role="alert" className="text-sm text-red-700 dark:text-red-400">{error}</p>}

      <h3 className="font-medium">Phones Sift remembers</h3>
      {wifi.data && wifi.data.remembered.length === 0 && <p className={`text-sm ${MUTED}`}>None yet.</p>}
      <ul className="flex flex-col gap-2">
        {(wifi.data?.remembered ?? []).map((r) => (
          <li key={`${r.name}|${r.address}`} className="flex flex-wrap items-center gap-3">
            <span>
              {r.name || 'Phone'} <span className={MUTED}>{r.address}</span>
              {r.last_connected != null && <span className={`text-sm ${MUTED}`}> · last connected {formatWhen(r.last_connected)}</span>}
            </span>
            <button type="button" className={BTN} disabled={forget.isPending} onClick={() => forget.mutate({ name: r.name, address: r.address })}>
              Forget
            </button>
          </li>
        ))}
      </ul>

      <h3 className="font-medium">Phones found on this network</h3>
      {wifi.data && found.length === 0 && <p className={`text-sm ${MUTED}`}>None found. Turn on Wireless debugging on the phone.</p>}
      <ul className="flex flex-col gap-2">
        {found.map((s) => (
          <li key={s.address} className="flex flex-wrap items-center gap-3">
            <span>
              {s.name} <span className={MUTED}>{s.address}</span>
            </span>
            <button type="button" className={BTN} disabled={connect.isPending} onClick={() => connect.mutate(s.address)}>
              Connect
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
