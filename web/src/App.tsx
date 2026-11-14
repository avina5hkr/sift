import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router'
import { ConfigWarnings } from './components/ConfigWarnings'
import { DeviceBanner } from './components/DeviceBanner'
import { Sidebar } from './components/Sidebar'
import { StatusBar } from './components/StatusBar'
import { Toasts } from './components/Toasts'
import { ApiError, apiFetch } from './lib/api'
import type { SetupState } from './lib/types'
import { useStatus } from './lib/useStatus'
import AppsPage from './pages/AppsPage'
import ArchivePage from './pages/ArchivePage'
import BackupPage from './pages/BackupPage'
import BrowsePage from './pages/BrowsePage'
import CalibrationPage from './pages/CalibrationPage'
import CleanupPage from './pages/CleanupPage'
import ChecklistPage from './pages/ChecklistPage'
import HealthPage from './pages/HealthPage'
import LogsPage from './pages/LogsPage'
import ReviewPage from './pages/ReviewPage'
import SearchPage from './pages/SearchPage'
import SlatePage from './pages/SlatePage'
import TeachPage from './pages/TeachPage'
import QuarantinePage from './pages/QuarantinePage'
import SettingsPage from './pages/SettingsPage'
import WizardPage from './pages/Wizard'

export default function App() {
  const navigate = useNavigate()
  const status = useStatus()

  // First run: until setup is finished the app opens the wizard instead of the Backup screen.
  const setup = useQuery({
    queryKey: ['setup', 'state'],
    queryFn: () => apiFetch<SetupState>('/api/setup/state'),
    retry: false,
  })
  const home = setup.data?.complete === false ? '/setup' : '/backup'

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
      <StatusBar status={status.data} onOpenSettings={() => navigate('/settings')} onRetry={onRetry} retrying={retry.isPending}
        onEjected={() => void qc.invalidateQueries({ queryKey: ['status'] })}
      />
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
            <Route path="/" element={setup.isPending ? null : <Navigate to={home} replace />} />
            <Route path="/setup" element={<WizardPage />} />
            <Route path="/backup" element={<BackupPage />} />
            <Route path="/cleanup" element={<CleanupPage />} />
            <Route path="/cleanup/review" element={<ReviewPage />} />
            <Route path="/cleanup/teach" element={<TeachPage />} />
            <Route path="/cleanup/calibration" element={<CalibrationPage />} />
            <Route path="/cleanup/search" element={<SearchPage />} />
            <Route path="/browse" element={<BrowsePage />} />
            <Route path="/slate" element={<SlatePage />} />
            <Route path="/apps" element={<AppsPage />} />
            <Route path="/health" element={<HealthPage />} />
            <Route path="/health/checklist" element={<ChecklistPage />} />
            <Route path="/archive" element={<ArchivePage />} />
            <Route path="/quarantine" element={<QuarantinePage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to={home} replace />} />
          </Routes>
        </main>
      </div>
      <Toasts />
    </div>
  )
}
