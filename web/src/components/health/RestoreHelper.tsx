import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { apiFetch } from '../../lib/api'
import { formatCount } from '../../lib/format'
import type { RestoreHelper as Helper } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'
import { BTN, MUTED } from '../apps/ui'
import { SimpleConfirm } from '../apps/SimpleConfirm'

/** After a reset: put the backed-up contacts file on the phone and list the apps to reinstall. */
export function RestoreHelper() {
  const helper = useQuery({ queryKey: ['reset-checklist', 'restore'], queryFn: () => apiFetch<Helper>('/api/reset-checklist/restore') })
  const [asking, setAsking] = useState(false)
  const send = useMutation({
    mutationFn: () => post<{ phone_path: string }>('/api/reset-checklist/restore/contacts', { confirm: true }),
    onSuccess: () => setAsking(false),
  })

  const data = helper.data
  const contacts = data?.contacts ?? null
  const apps = data?.reinstall.apps ?? []
  return (
    <section aria-label="After a reset" className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <h2 className="font-medium">After a reset</h2>
      {helper.error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {plain(helper.error)}
        </p>
      )}

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">Contacts</h3>
        {data && !contacts && <p className={`text-sm ${MUTED}`}>No backed-up contacts file was found.</p>}
        {contacts && (
          <div className="flex flex-wrap items-center gap-3">
            <p className="min-w-0 flex-1 text-sm">
              Contacts file with {formatCount(contacts.cards)} {contacts.cards === 1 ? 'contact' : 'contacts'}.
            </p>
            <button type="button" className={BTN} onClick={() => setAsking(true)}>
              Send contacts to the phone
            </button>
          </div>
        )}
        {send.data && (
          <p role="status" className="text-sm">
            Sent to <span className="break-all">{send.data.phone_path}</span>. Open it on the phone from Download to import.
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-medium">Apps to reinstall</h3>
        {data && apps.length === 0 && (
          <p className={`text-sm ${MUTED}`}>No app list saved yet. Save one from the Apps page.</p>
        )}
        {apps.length > 0 && (
          <ul className="flex max-h-72 flex-col gap-1 overflow-auto text-sm">
            {apps.map((a) => (
              <li key={a.package} className="flex flex-wrap items-center gap-2">
                <span>{a.name || a.package}</span>
                {a.link && (
                  <a href={a.link} target="_blank" rel="noreferrer" className="underline">
                    Play Store
                  </a>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <p className={`text-sm ${MUTED}`}>
        To put photos and other files back, use the{' '}
        <Link to="/archive" className="underline">
          Archive
        </Link>
        .
      </p>

      {asking && (
        <SimpleConfirm
          label="Send contacts to the phone"
          title="Send contacts to the phone?"
          confirmLabel="Send contacts"
          danger={false}
          busy={send.isPending}
          error={send.error ? plain(send.error) : null}
          onConfirm={() => send.mutate()}
          onCancel={() => {
            send.reset()
            setAsking(false)
          }}
        >
          Sift copies your newest backed-up contacts file into the phone&apos;s Download folder. It never replaces a file that is
          already there.
        </SimpleConfirm>
      )}
    </section>
  )
}
