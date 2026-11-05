import { expect, test, type Page } from '@playwright/test'

// The status chips also render when /api/status fails, so only trust them once the
// API has answered 200 (i.e. the token really reached the backend).
const statusOk = (page: Page) =>
  page.waitForResponse((r) => r.url().endsWith('/api/status') && r.status() === 200)

test('shell loads status and connects to live events', async ({ page }) => {
  const ok = statusOk(page)
  await page.goto('/?t=e2e')
  await ok
  await expect(page).toHaveURL(/\/backup$/)
  await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link'))
    .toHaveText(['Backup', 'Cleanup', 'Quarantine', 'Logs', 'Settings'])
  await expect(page.getByText('No phone')).toBeVisible()
  await expect(page.getByText('SSD not connected')).toBeVisible()
  await expect(page.getByTestId('events-status')).toHaveAttribute('data-connected', 'true')
})

test('reload works after the token left the URL', async ({ page }) => {
  await page.goto('/?t=e2e'); await expect(page).toHaveURL(/\/backup$/)
  const ok = statusOk(page)
  await page.reload()
  await ok
  await expect(page.getByText('No phone')).toBeVisible()
  await expect(page.getByTestId('events-status')).toHaveAttribute('data-connected', 'true')
})

test('API refuses requests without the token', async ({ request }) => {
  const r = await request.get('/api/status')
  expect(r.status()).toBe(401)
  expect((await r.json()).code).toBe('bad_token')
})
