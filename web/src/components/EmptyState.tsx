type Props = { title: string; children: string }

export function EmptyState({ title, children }: Props) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="max-w-md text-neutral-600 dark:text-neutral-400">{children}</p>
    </div>
  )
}
