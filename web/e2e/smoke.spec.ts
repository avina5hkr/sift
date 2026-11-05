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
  await expect(page.getByText('No phone', { exact: true })).toBeVisible()
  await expect(page.getByText('SSD not connected')).toBeVisible()
  await expect(page.getByTestId('events-status')).toHaveAttribute('data-connected', 'true')
})

test('reload works after the token left the URL', async ({ page }) => {
  await page.goto('/?t=e2e'); await expect(page).toHaveURL(/\/backup$/)
  const ok = statusOk(page)
  await page.reload()
  await ok
  await expect(page.getByText('No phone', { exact: true })).toBeVisible()
  await expect(page.getByTestId('events-status')).toHaveAttribute('data-connected', 'true')
})

test('API refuses requests without the token', async ({ request }) => {
  const r = await request.get('/api/status')
  expect(r.status()).toBe(401)
  expect((await r.json()).code).toBe('bad_token')
})

// Keep this test last: the server is shared, and once the fake phone has appeared the earlier
// tests' "No phone" no longer holds. The server holds the phone back (SIFT_FAKE_PHONE_DELAY_S,
// see playwright.config.ts) until Retry, so "No phone" is there to see first.
test('the phone chip changes from No phone to the fake phone without a reload', async ({ page, request }) => {
  const ok = statusOk(page)
  await page.goto('/?t=e2e')
  await ok
  await expect(page.getByText('No phone', { exact: true })).toBeVisible()
  await expect(page.getByTestId('device-banner')).toContainText('No phone found')
  let reloaded = false
  page.on('framenavigated', () => (reloaded = true))
  const retry = await request.post('/api/devices/retry', { headers: { 'X-Sift-Token': 'e2e' } })
  expect(retry.status()).toBe(204)
  await expect(page.getByText(/^TestPhone · USB · \d+% · .* GB free$/)).toBeVisible({ timeout: 3000 })
  await expect(page.getByText('No phone', { exact: true })).toHaveCount(0)
  await expect(page.getByTestId('device-banner')).toHaveCount(0)
  expect(reloaded).toBe(false)
  await expect(page.getByText('SSD not connected')).toBeVisible()
})
