import { ENUM_TEXT, LABELS } from '../../lib/settingsLabels'
import { idOf, kindOf, sameForm, toForm } from '../../lib/settingsForm'
import type { Form, SettingField } from '../../lib/settingsForm'
import { BTN, FIELD, MUTED } from '../apps/ui'

export function FieldRow({
  field,
  form,
  onChange,
}: {
  field: SettingField
  form: Form
  onChange: (next: Form) => void
}) {
  const id = idOf(field)
  const text = LABELS[id] ?? { label: field.key.replaceAll('_', ' '), help: '' }
  const kind = kindOf(field)
  const atDefault = sameForm(form, toForm(field, field.default))
  const helpId = `${id}-help`
  const common = { id, 'aria-describedby': text.help ? helpId : undefined }

  let control
  if (kind === 'bool') {
    control = <input type="checkbox" {...common} checked={form === true} onChange={(e) => onChange(e.target.checked)} />
  } else if (kind === 'number') {
    control = (
      <input
        type="number"
        {...common}
        className={`${FIELD} w-32`}
        min={field.schema.minimum}
        max={field.schema.maximum}
        step={field.schema.type === 'integer' ? 1 : 'any'}
        value={String(form)}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  } else if (kind === 'select') {
    control = (
      <select {...common} className={FIELD} value={String(form)} onChange={(e) => onChange(e.target.value)}>
        {(field.schema.enum ?? []).map((o) => (
          <option key={o} value={o}>
            {ENUM_TEXT[o] ?? o}
          </option>
        ))}
      </select>
    )
  } else if (kind === 'lines') {
    control = (
      <textarea
        {...common}
        className={`${FIELD} w-full font-mono`}
        rows={Math.min(8, Math.max(2, String(form).split('\n').length + 1))}
        value={String(form)}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  } else {
    control = <input type="text" {...common} className={`${FIELD} w-full`} value={String(form)} onChange={(e) => onChange(e.target.value)} />
  }

  const multi = kind === 'multi'
  const chosen = Array.isArray(form) ? form : []
  return (
    <div className="flex flex-col gap-1 border-b border-neutral-200 py-3 dark:border-neutral-800">
      {kind === 'bool' ? (
        <label htmlFor={id} className="flex items-center gap-2 font-medium">
          {control}
          {text.label}
        </label>
      ) : multi ? (
        <fieldset aria-describedby={text.help ? helpId : undefined}>
          <legend className="font-medium">{text.label}</legend>
          <div className="mt-1 flex gap-4">
            {(field.schema.items?.enum ?? []).map((o) => (
              <label key={o} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={chosen.includes(o)}
                  onChange={(e) => onChange(e.target.checked ? [...chosen, o] : chosen.filter((x) => x !== o))}
                />
                {ENUM_TEXT[o] ?? o}
              </label>
            ))}
          </div>
        </fieldset>
      ) : (
        <>
          <label htmlFor={id} className="font-medium">
            {text.label}
          </label>
          {control}
        </>
      )}
      {text.help && (
        <p id={helpId} className={`text-sm ${MUTED}`}>
          {text.help}
        </p>
      )}
      {!atDefault && (
        <div>
          <button
            type="button"
            className={`${BTN} !px-2 !py-0.5 !text-xs`}
            aria-label={`Reset ${text.label} to default`}
            onClick={() => onChange(toForm(field, field.default))}
          >
            Reset to default
          </button>
        </div>
      )}
    </div>
  )
}
