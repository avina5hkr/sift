import { useInfiniteQuery } from '@tanstack/react-query'
import { apiFetch } from '../../lib/api'
import type { SlateFilesPage } from '../../lib/types'

/** One side of the plan (or its backup gaps), a page of 500 at a time. */
export function useSlateFiles(planId: number, side: string, folder = '', enabled = true) {
  return useInfiniteQuery({
    queryKey: ['slate', 'files', planId, side, folder],
    enabled,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      apiFetch<SlateFilesPage>(
        `/api/slate/plan/${planId}/files?${new URLSearchParams({ side, folder, offset: String(pageParam) })}`,
      ),
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, p) => n + p.files.length, 0)
      return loaded < last.total_files ? loaded : undefined
    },
  })
}
