import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router'
import { ConfigWarnings } from './components/ConfigWarnings'
import { DeviceBanner } from './components/DeviceBanner'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { ApiError, apiFetch } from './lib/api'
import type { StatusResponse } from './lib/types'
import BackupPage from './pages/BackupPage'
import CleanupPage from './pages/CleanupPage'
import LogsPage from './pages/LogsPage'
import QuarantinePage from './pages/QuarantinePage'
import SettingsPage from './pages/SettingsPage'

export default function App() {
  const navigate = useNavigate()
  const status = useQuery({
    queryKey: ['status'],
    queryFn: () => apiFetch<StatusResponse>('/api/status'),
    refetchInterval: 30_000,
  })

  const qc = useQueryClient()
  const retry = useMutation({
    mutationFn: () => apiFetch<void>('/api/devices/retry', { method: 'POST' }),
    onSettled: () => qc.invalidateQueries({ queryKey: ['status'] }),
  })
  const deviceState = status.data?.device?.state
  const resetRetry = retry.reset
  // A failure message belongs to the situation it was shown in; drop it when the phone changes.
  useEffect(() => resetRetry(), [deviceState, resetRetry])
  const onRetry = () => {
    if (!retry.isPending) retry.mutate()
  }
  const retryError = retry.error
    ? retry.error instanceof ApiError
      ? retry.error.message
      : 'Sift could not retry just now. Try again in a moment.'
    : null

  const errorMessage = status.error
    ? status.error instanceof ApiError
      ? status.error.message
      : 'Something went wrong loading the status. Try again in a moment.'
    : null

  return (
    <div className="flex h-screen flex-col">
      <StatusBar status={status.data} onOpenSettings={() => navigate('/settings')} onRetry={onRetry} retrying={retry.isPending} />
      <DeviceBanner device={status.data?.device} onRetry={onRetry} retrying={retry.isPending} error={retryError} />
      <ConfigWarnings warnings={status.data?.app.config_warnings} />
      {errorMessage && (
        <div
          role="alert"
          className="border-b border-amber-300 bg-amber-100 px-4 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200"
        >
          {errorMessage}
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-auto">
          <Routes>
            <Route path="/" element={<Navigate to="/backup" replace />} />
            <Route path="/backup" element={<BackupPage />} />
            <Route path="/cleanup" element={<CleanupPage />} />
            <Route path="/quarantine" element={<QuarantinePage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/backup" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
