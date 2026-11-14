import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { expect, test } from 'vitest'
import { Sidebar } from './Sidebar'

test('lists the areas in order', () => {
  render(
    <MemoryRouter>
      <Sidebar />
    </MemoryRouter>,
  )
  expect(
    within(screen.getByRole('navigation', { name: 'Main' }))
      .getAllByRole('link')
      .map((l) => l.textContent),
  ).toEqual(['Backup', 'Cleanup', 'Browse', 'Clean slate', 'Archive', 'Quarantine', 'Logs', 'Settings'])
})

test('marks the current area with aria-current', () => {
  render(
    <MemoryRouter initialEntries={['/logs']}>
      <Sidebar />
    </MemoryRouter>,
  )
  expect(screen.getByRole('link', { name: 'Logs' })).toHaveAttribute('aria-current', 'page')
  expect(screen.getByRole('link', { name: 'Backup' })).not.toHaveAttribute('aria-current')
})
