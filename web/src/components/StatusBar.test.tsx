import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import type { StatusResponse } from '../lib/types'
import { StatusBar } from './StatusBar'

const status: StatusResponse = {
  app: { version: '0.1.0', schema_version: 1, config_warnings: [] },
  device: null,
  ssd: null,
  mac_folder: { path: '/Users/a/PhoneBackup', exists: false, free_bytes: 212_000_000_000 },
  jobs: [],
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
