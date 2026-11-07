import { expect, test } from 'vitest'
import { disabledReason, screenState } from './backupState'
import type { ConnectionState, DeviceStatus, JobEvent, JobStateName } from './types'

function device(state: ConnectionState, message = 'A message from the tracker.'): DeviceStatus {
  return {
    state,
    message,
    serial: null,
    model: 'Test phone',
    android_version: null,
    battery_pct: null,
    free_bytes: null,
    total_bytes: null,
    link_mbps: null,
    usb2_hint: false,
    device_uuid: null,
    reason: null,
  }
}

function job(state: JobStateName): JobEvent {
  return {
    job_id: 1,
    type: 'backup',
    state,
    sets: ['Camera'],
    dry_run: false,
    files_done: 0,
    bytes_done: 0,
    files_total: null,
    bytes_total: null,
    duplicates_skipped: 0,
    failed: 0,
    current: null,
    current_files: [],
    speed_bps: null,
    eta_s: null,
    eta_basis: 'reference',
    pause_requested: false,
    stopped: false,
    error: null,
    started_at: 1,
    finished_at: null,
  }
}

test('the screen state follows the job, else the phone', () => {
  expect(screenState(null, null)).toBe('not_connected')
  expect(screenState(device('no_device'), null)).toBe('not_connected')
  expect(screenState(device('connected_usb'), null)).toBe('ready')
  const cases: [JobStateName, string][] = [
    ['queued', 'scanning'],
    ['scanning', 'scanning'],
    ['awaiting_confirmation', 'confirm'],
    ['pulling', 'backing_up'],
    ['stopping', 'backing_up'],
    ['paused', 'paused'],
    ['waiting_for_phone', 'waiting_for_phone'],
    ['done', 'finished'],
    ['failed', 'failed'],
  ]
  for (const [state, want] of cases) expect(screenState(device('connected_usb'), job(state))).toBe(want)
})

const base = { device: device('connected_usb'), job: null, macFreeBytes: 5_000_000_000, setCount: 2 }

test('no reason when everything is fine, or the last job is over', () => {
  expect(disabledReason(base)).toBeNull()
  expect(disabledReason({ ...base, job: job('done') })).toBeNull()
})

test('each reason is plain words', () => {
  expect(disabledReason({ ...base, device: null })).toBe('Connect your phone to start a backup.')
  expect(disabledReason({ ...base, device: device('no_device') })).toBe('Connect your phone to start a backup.')
  expect(disabledReason({ ...base, device: device('unauthorized', 'Tap Allow on the phone.') })).toBe(
    'Tap Allow on the phone.',
  )
  expect(disabledReason({ ...base, device: device('wrong_device') })).toMatch(/different phone/)
  expect(disabledReason({ ...base, job: job('pulling') })).toBe('A backup is already running.')
  expect(disabledReason({ ...base, starting: true })).toBe('The backup is starting.')
  expect(disabledReason({ ...base, setCount: 0 })).toMatch(/Settings/)
  expect(disabledReason({ ...base, macFreeBytes: 1000 })).toMatch(/almost full/)
  expect(disabledReason({ ...base, macFreeBytes: null })).toBeNull()
})
