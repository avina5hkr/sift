import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'
import type { StatusResponse } from './types'

/** GET /api/status, shared by the app shell and the pages (same query key, one request). */
export function useStatus() {
  return useQuery({
    queryKey: ['status'],
    queryFn: () => apiFetch<StatusResponse>('/api/status'),
    refetchInterval: 30_000,
  })
}
