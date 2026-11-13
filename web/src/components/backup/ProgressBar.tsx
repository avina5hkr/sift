type Props = { label: string; value: number | null }

/** A bar filled to `value` percent (0 to 100); null shows it as "working, no number yet". */
export function ProgressBar({ label, value }: Props) {
  const pct = value == null ? null : Math.max(0, Math.min(100, value))
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct == null ? undefined : Math.round(pct)}
      className="h-3 flex-1 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800"
    >
      <div
        className={`h-full rounded-full bg-green-600 ${pct == null ? 'w-1/3 animate-pulse' : ''}`}
        style={pct == null ? undefined : { width: `${pct}%` }}
      />
    </div>
  )
}
