import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useEventStore } from '../lib/events'
import { Toasts } from './Toasts'

beforeEach(() => useEventStore.setState({ toasts: [] }))
afterEach(() => vi.useRealTimers())

test('shows a toast with its title and message, and Dismiss removes it', async () => {
  render(<Toasts />)
  act(() =>
    useEventStore.setState({ toasts: [{ id: 1, level: 'info', title: 'Backup finished', message: '12 new files.' }] }),
  )
  expect(screen.getByRole('status', { name: 'Messages' })).toBeInTheDocument()
  expect(screen.getByText('Backup finished')).toBeInTheDocument()
  expect(screen.getByText('12 new files.')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
  expect(screen.queryByText('Backup finished')).not.toBeInTheDocument()
})

test('a toast goes away by itself after a few seconds', () => {
  vi.useFakeTimers()
  render(<Toasts />)
  act(() => useEventStore.setState({ toasts: [{ id: 2, level: 'warning', title: 'Heads up', message: '' }] }))
  expect(screen.getByText('Heads up')).toBeInTheDocument()
  act(() => void vi.advanceTimersByTime(8100))
  expect(screen.queryByText('Heads up')).not.toBeInTheDocument()
})
