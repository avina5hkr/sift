import { expect, test } from '@playwright/test'

test('shell loads status and connects to live events', async ({ page }) => {
  await page.goto('/?t=e2e')
  await expect(page).toHaveURL(/\/backup$/)
  await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link'))
    .toHaveText(['Backup', 'Cleanup', 'Quarantine', 'Logs', 'Settings'])
  await expect(page.getByText('No phone')).toBeVisible()
  await expect(page.getByText('SSD not connected')).toBeVisible()
  await expect(page.getByTestId('events-status')).toHaveAttribute('data-connected', 'true')
})

test('reload works after the token left the URL', async ({ page }) => {
  await page.goto('/?t=e2e'); await expect(page).toHaveURL(/\/backup$/)
  await page.reload()
  await expect(page.getByText('No phone')).toBeVisible()
})

test('API refuses requests without the token', async ({ request }) => {
  const r = await request.get('/api/status')
  expect(r.status()).toBe(401)
  expect((await r.json()).code).toBe('bad_token')
})
