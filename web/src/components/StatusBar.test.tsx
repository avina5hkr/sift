import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { ConnectionState, DeviceStatus, StatusResponse } from '../lib/types'
import { StatusBar } from './StatusBar'

const status: StatusResponse = {
  app: { version: '0.1.0', schema_version: 1, config_warnings: [] },
  device: null,
  ssd: null,
  mac_folder: { path: '/Users/a/PhoneBackup', exists: false, free_bytes: 212_000_000_000 },
  jobs: [],
}

function device(state: ConnectionState, extra: Partial<DeviceStatus> = {}): StatusResponse {
  return {
    ...status,
    device: {
      state,
      message: 'A message from the tracker.',
      serial: null,
      model: null,
      android_version: null,
      battery_pct: null,
      free_bytes: null,
      total_bytes: null,
      link_mbps: null,
      usb2_hint: false,
      device_uuid: null,
      reason: null,
      ...extra,
    },
  }
}

const connected = {
  serial: 'FAKE-0001',
  model: 'TestPhone',
  android_version: '14',
  battery_pct: 87,
  free_bytes: 12_000_000_000,
  total_bytes: 128_000_000_000,
}

test('shows placeholders and Mac folder free space', () => {
  render(<StatusBar status={status} />)
  expect(screen.getByText('No phone')).toBeInTheDocument()
  expect(screen.getByText('SSD not connected')).toBeInTheDocument()
  expect(screen.getByText('PhoneBackup · 212.0 GB free')).toBeInTheDocument()
})

test('shows neutral chips before status has loaded', () => {
  render(<StatusBar status={undefined} />)
  expect(screen.getByText('No phone')).toBeInTheDocument()
  expect(screen.getByText('SSD not connected')).toBeInTheDocument()
  expect(screen.getByText('Mac folder: checking')).toBeInTheDocument()
})

test('omits free space when the folder size is unknown', () => {
  render(<StatusBar status={{ ...status, mac_folder: { ...status.mac_folder, free_bytes: null } }} />)
  expect(screen.getByText('PhoneBackup')).toBeInTheDocument()
})

test('a phone over USB shows model, link, battery and free space', () => {
  render(<StatusBar status={device('connected_usb', connected)} />)
  expect(screen.getByText('TestPhone · USB · 87% · 12.0 GB free')).toBeInTheDocument()
  expect(screen.queryByText('USB 2')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
})

test('a phone over Wi-Fi uses the same layout', () => {
  render(<StatusBar status={device('connected_wifi', connected)} />)
  expect(screen.getByText('TestPhone · Wi-Fi · 87% · 12.0 GB free')).toBeInTheDocument()
})

test('a connected phone with unknown details leaves those parts out', () => {
  render(<StatusBar status={device('connected_usb', { model: 'TestPhone' })} />)
  expect(screen.getByText('TestPhone · USB')).toBeInTheDocument()
  render(<StatusBar status={device('connected_usb')} />)
  expect(screen.getByText('Phone · USB')).toBeInTheDocument()
})

test('the USB 2 tag has a tooltip and an accessible description', () => {
  render(<StatusBar status={device('connected_usb', { ...connected, link_mbps: 480, usb2_hint: true })} />)
  const tip = 'Use a USB 3 cable in a direct port for faster backups.'
  const tag = screen.getByText('USB 2')
  expect(tag).toHaveAttribute('title', tip)
  expect(tag).toHaveAccessibleDescription(tip)
})

test.each([
  ['unauthorized', 'Tap Allow on the phone'],
  ['offline', 'Phone not responding'],
  ['wrong_device', 'A different phone is connected'],
  ['no_adb', 'adb not installed'],
  ['no_device', 'No phone'],
] as const)('%s shows "%s"', (state, text) => {
  render(<StatusBar status={device(state)} />)
  expect(screen.getByText(text)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
})

test.each([
  ['adb_missing', 'adb not installed'],
  ['adb_unavailable', 'adb not available'],
] as const)('no_adb because of %s shows "%s"', (reason, text) => {
  render(<StatusBar status={device('no_adb', { reason })} />)
  expect(screen.getByText(text)).toBeInTheDocument()
})

test('a phone held by another tool has a Retry button', async () => {
  const onRetry = vi.fn()
  render(<StatusBar status={device('held_by_other')} onRetry={onRetry} />)
  expect(screen.getByText('Phone busy in another tool')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Retry connecting to the phone' }))
  expect(onRetry).toHaveBeenCalledTimes(1)
})

test('the SSD chip stays "SSD not connected"', () => {
  render(<StatusBar status={device('connected_usb', connected)} />)
  expect(screen.getByText('SSD not connected')).toBeInTheDocument()
})

test('the chip Retry is disabled while a request is pending', () => {
  render(<StatusBar status={device('held_by_other')} onRetry={vi.fn()} retrying />)
  expect(screen.getByRole('button', { name: 'Retry connecting to the phone' })).toBeDisabled()
})
