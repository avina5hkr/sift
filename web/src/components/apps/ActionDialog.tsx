import { useEffect, useRef, useState } from 'react'
import { isWhatsApp } from '../../lib/apps'
import type { AppAction, AppRow, AppsExport, AppsPreflight, AppsResult, AppUnlock } from '../../lib/types'
import { plain, post } from '../../pages/Wizard/request'
import { Modal } from '../delete/Modal'
import { BTN, DANGER, FIELD, MUTED } from './ui'

type Props = { action: AppAction; rows: AppRow[]; onClose: () => void; onFinished: () => void }

const nameOf = (rows: AppRow[], pkg: string) => rows.find((r) => r.package === pkg)?.name ?? pkg

/** The last question before apps are uninstalled or their data cleared. */
export function ActionDialog({ action, rows, onClose, onFinished }: Props) {
  const uninstall = action === 'uninstall'
  const whatsapps = rows.filter((r) => isWhatsApp(r.package))
  const sideloaded = rows.filter((r) => !r.from_play_store)

  const [backupDone, setBackupDone] = useState<Record<string, boolean>>({})
  const [typed, setTyped] = useState<Record<string, string>>({})
  const [saveApks, setSaveApks] = useState(false)
  const [check, setCheck] = useState<AppsPreflight | null>(null)
  const [checkError, setCheckError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<{ run: AppsResult; apks: AppsExport['apks'] | null } | null>(null)

  const unlock: AppUnlock = {}
  for (const r of whatsapps) unlock[r.package] = { backup_done: backupDone[r.package] ?? false, typed_name: typed[r.package] ?? '' }
  const unlockKey = JSON.stringify(unlock)
  const packagesKey = rows.map((r) => r.package).join('\n')

  // The checks run when the dialog opens and again (after a short pause) whenever the unlock answers change.
  const first = useRef(true)
  useEffect(() => {
    let stale = false
    const t = setTimeout(
      () => {
        post<AppsPreflight>('/api/apps/preflight', { action, packages: packagesKey.split('\n'), unlock: JSON.parse(unlockKey) as AppUnlock })
          .then((c) => {
            if (stale) return
            setCheck(c)
            setCheckError(null)
          })
          .catch((e) => {
            if (!stale) setCheckError(plain(e))
          })
      },
      first.current ? 0 : 250,
    )
    first.current = false
    return () => {
      stale = true
      clearTimeout(t)
    }
  }, [action, unlockKey, packagesKey])

  const refused = check ? Object.entries(check.refused) : []
  const ready = check != null && refused.length === 0 && !busy

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      let apks: AppsExport['apks'] | null = null
      if (uninstall && saveApks && sideloaded.length > 0) {
        const out = await post<AppsExport>('/api/apps/export', { apks: sideloaded.map((r) => r.package) })
        apks = out.apks ?? null
      }
      const body = { action, packages: rows.map((r) => r.package), unlock, confirm: true }
      const out = await post<AppsResult>(uninstall ? '/api/apps/uninstall' : '/api/apps/clear-data', body)
      setResult({ run: out, apks })
    } catch (e) {
      setError(plain(e))
    } finally {
      setBusy(false)
    }
  }

  const label = uninstall ? 'Uninstall apps' : 'Clear app data'

  if (result) {
    const { run: r, apks } = result
    const failed = Object.entries(r.failed)
    return (
      <Modal label={label} onClose={onFinished}>
        <h2 className="text-lg font-semibold">{uninstall ? 'Uninstall finished' : 'Clear data finished'}</h2>
        <p className="text-sm">
          {r.done.length === 0
            ? 'Nothing was changed.'
            : `${uninstall ? 'Uninstalled' : 'Cleared data for'}: ${r.done.map((p) => nameOf(rows, p)).join(', ')}.`}
        </p>
        {failed.length > 0 && (
          <ul role="alert" className="list-disc pl-5 text-sm text-amber-800 dark:text-amber-300">
            {failed.map(([p, why]) => (
              <li key={p}>
                {nameOf(rows, p)}: {why}
              </li>
            ))}
          </ul>
        )}
        {r.app_list.length > 0 && <p className={`text-sm ${MUTED}`}>App list saved to: {r.app_list.join(', ')}</p>}
        {apks && (
          <p className={`text-sm ${MUTED}`}>
            APK files saved for {apks.saved} {apks.saved === 1 ? 'app' : 'apps'}
            {apks.failed > 0 ? `; ${apks.failed} could not be saved` : ''}.
          </p>
        )}
        <div className="flex justify-end">
          <button type="button" data-autofocus onClick={onFinished} className={BTN}>
            Close
          </button>
        </div>
      </Modal>
    )
  }

  const count = `${rows.length} ${rows.length === 1 ? 'app' : 'apps'}`
  return (
    <Modal label={label} onClose={busy ? undefined : onClose}>
      <h2 className="text-lg font-semibold">
        {uninstall ? 'Uninstall' : 'Clear data for'} {count}?
      </h2>
      <p className="text-sm">{rows.map((r) => r.name).join(', ')}</p>
      <p className="text-sm">
        {uninstall
          ? "Uninstalling deletes this app's data. This app cannot back it up."
          : "Clearing data deletes this app's data. The app stays installed."}{' '}
        <strong>This cannot be undone.</strong>
      </p>
      {uninstall && <p className={`text-sm ${MUTED}`}>The app list is saved to your backup folder first.</p>}

      {uninstall && sideloaded.length > 0 && (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={saveApks} onChange={(e) => setSaveApks(e.target.checked)} />
          Also save the APK files of apps not from the Play Store
        </label>
      )}

      {whatsapps.map((r) => (
        <fieldset key={r.package} className="flex flex-col gap-2 rounded-md border border-neutral-200 p-3 text-sm dark:border-neutral-700">
          <legend className="px-1 font-medium">{r.name}</legend>
          <p className="text-neutral-600 dark:text-neutral-400">
            This removes {r.name}'s chat backup from the phone too. Sift goes ahead only after the
            newest chat backup is copied to your backup folders (the WhatsApp chat backups set),
            and keeps every copy there until you delete it.
          </p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={backupDone[r.package] ?? false}
              onChange={(e) => setBackupDone((s) => ({ ...s, [r.package]: e.target.checked }))}
            />
            My {r.name} chat backup is complete
          </label>
          <label className="flex flex-col gap-1">
            Type {r.name} to confirm
            <input
              autoComplete="off"
              value={typed[r.package] ?? ''}
              onChange={(e) => setTyped((s) => ({ ...s, [r.package]: e.target.value }))}
              className={`${FIELD} w-56`}
            />
          </label>
        </fieldset>
      ))}

      {check == null && !checkError && <p className={`text-sm ${MUTED}`}>Checking…</p>}
      {checkError && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {checkError}
        </p>
      )}
      {refused.length > 0 && (
        <div role="alert" className="rounded-md bg-amber-100 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          <p className="font-medium">Sift cannot do this yet:</p>
          <ul className="list-disc pl-5">
            {refused.map(([p, why]) => (
              <li key={p}>
                {nameOf(rows, p)}: {why}
              </li>
            ))}
          </ul>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
          {error}
        </p>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" data-autofocus onClick={onClose} disabled={busy} className={BTN}>
          Cancel
        </button>
        <button type="button" disabled={!ready} onClick={() => void run()} className={DANGER}>
          {busy ? 'Working…' : uninstall ? `Uninstall ${count}` : `Clear data for ${count}`}
        </button>
      </div>
    </Modal>
  )
}
