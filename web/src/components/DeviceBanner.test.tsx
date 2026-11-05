import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { ConnectionState, DeviceStatus } from '../lib/types'
import { DeviceBanner } from './DeviceBanner'

function device(state: ConnectionState): DeviceStatus {
  return {
    state,
    message: `Words for ${state}.`,
    serial: null,
    model: null,
    android_version: null,
    battery_pct: null,
    free_bytes: null,
    total_bytes: null,
    link_mbps: null,
    usb2_hint: false,
    device_uuid: null,
  }
}

test.each(['connected_usb', 'connected_wifi'] as const)('%s shows no banner', (state) => {
  const { container } = render(<DeviceBanner device={device(state)} onRetry={vi.fn()} />)
  expect(container).toBeEmptyDOMElement()
})

test('nothing without a device status', () => {
  const { container } = render(<DeviceBanner device={null} onRetry={vi.fn()} />)
  expect(container).toBeEmptyDOMElement()
})

test.each(['no_adb', 'no_device', 'unauthorized', 'offline', 'wrong_device', 'held_by_other'] as const)(
  '%s shows the tracker message',
  (state) => {
    render(<DeviceBanner device={device(state)} onRetry={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent(`Words for ${state}.`)
  },
)

test.each(['held_by_other', 'offline', 'no_adb'] as const)('%s offers Retry', async (state) => {
  const onRetry = vi.fn()
  render(<DeviceBanner device={device(state)} onRetry={onRetry} />)
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(onRetry).toHaveBeenCalledTimes(1)
})

test.each(['no_device', 'unauthorized', 'wrong_device'] as const)('%s has no action', (state) => {
  render(<DeviceBanner device={device(state)} onRetry={vi.fn()} />)
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})

test('Retry is disabled while a request is pending', () => {
  render(<DeviceBanner device={device('held_by_other')} onRetry={vi.fn()} retrying />)
  expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
})

test('shows a plain-words reason when Retry failed', () => {
  render(<DeviceBanner device={device('offline')} onRetry={vi.fn()} error="Sift is not looking for a phone right now." />)
  expect(screen.getByRole('alert')).toHaveTextContent('Sift is not looking for a phone right now.')
  expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
})
