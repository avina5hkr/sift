const HEADING = 'Some settings in config.toml were not used'

/** Amber banner listing what the server could not use from config.toml; renders nothing when empty. */
export function ConfigWarnings({ warnings }: { warnings: string[] | undefined }) {
  if (!warnings || warnings.length === 0) return null
  return (
    <div
      role="status"
      aria-labelledby="config-warnings-heading"
      className="border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100"
    >
      <p id="config-warnings-heading" className="font-semibold">
        {HEADING}
      </p>
      <ul className="mt-1 list-disc pl-5">
        {warnings.map((warning, i) => (
          <li key={i}>{warning}</li>
        ))}
      </ul>
    </div>
  )
}
