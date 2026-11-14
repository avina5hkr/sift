import { LABELS } from './settingsLabels'

export type Schema = {
  type?: string
  enum?: string[]
  minimum?: number
  maximum?: number
  items?: { type?: string; enum?: string[] }
}
export type SettingField = { section: string; key: string; value: unknown; schema: Schema; default: unknown }
export type Form = boolean | string | string[]

export const idOf = (f: { section: string; key: string }) => `${f.section}.${f.key}`

export type Kind = 'bool' | 'number' | 'select' | 'text' | 'lines' | 'multi'

export function kindOf(f: SettingField): Kind {
  const s = f.schema
  if (s.type === 'boolean' || typeof f.value === 'boolean') return 'bool'
  if (s.type === 'integer' || s.type === 'number' || typeof f.value === 'number') return 'number'
  if (s.type === 'array' || Array.isArray(f.value)) return s.items?.enum ? 'multi' : 'lines'
  return s.enum ? 'select' : 'text'
}

export function toForm(f: SettingField, value: unknown): Form {
  switch (kindOf(f)) {
    case 'bool':
      return value === true
    case 'number':
      return String(value ?? '')
    case 'lines':
      return (Array.isArray(value) ? value : []).join('\n')
    case 'multi':
      return Array.isArray(value) ? (value as string[]) : []
    default:
      return String(value ?? '')
  }
}

/** The JSON value to send for a form value, or an error sentence the owner can act on. */
export function fromForm(f: SettingField, form: Form): { value: unknown } | { error: string } {
  switch (kindOf(f)) {
    case 'number': {
      const text = String(form).trim()
      const n = Number(text)
      if (text === '' || !Number.isFinite(n)) return { error: `${LABELS[idOf(f)]?.label ?? f.key}: enter a number.` }
      return { value: n }
    }
    case 'lines':
      return { value: String(form).split('\n').map((l) => l.trim()).filter(Boolean) }
    default:
      return { value: form }
  }
}

export const sameForm = (a: Form, b: Form) => JSON.stringify(a) === JSON.stringify(b)

