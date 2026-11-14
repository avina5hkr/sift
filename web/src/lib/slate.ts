import type { SlateCategory } from './types'

export const SLATE_CATEGORIES: { key: SlateCategory; label: string }[] = [
  { key: 'camera', label: 'Camera pictures and videos' },
  { key: 'screenshots', label: 'Screenshots' },
  { key: 'whatsapp', label: 'WhatsApp media' },
  { key: 'documents', label: 'Documents' },
  { key: 'junk', label: 'Junk' },
  { key: 'other', label: 'Everything else' },
]

export const GAP_LABEL: Record<string, string> = {
  not_backed_up: 'Not backed up yet',
  no_set: 'Not in any backup set',
}

export const SLATE_BTN = 'rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium hover:bg-neutral-100 disabled:opacity-60 dark:border-neutral-700 dark:hover:bg-neutral-800'
export const SLATE_PRIMARY =
  'rounded-md border border-neutral-900 bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700 disabled:opacity-60 dark:border-neutral-100 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-300'
export const SLATE_FIELD = 'rounded-md border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700'
export const MUTED = 'text-neutral-600 dark:text-neutral-400'
