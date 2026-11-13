import { readdir } from 'node:fs/promises'
import { join } from 'node:path'
import type { Page } from '@playwright/test'
import { expect, test } from './fixtures'

// Every file under a folder (none when it does not exist yet).
async function filesUnder(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true, recursive: true }).catch(() => [])
  return entries.filter((e) => e.isFile()).map((e) => join(e.parentPath, e.name))
}

const step = (page: Page, name: string) =>
  expect(page.getByRole('heading', { level: 2, name })).toBeVisible()

test('the wizard goes from the welcome page to a finished first backup', async ({ page, sift }) => {
  await page.goto('/?t=e2e')
  await expect(page).toHaveURL(/\/setup$/)

  await step(page, 'Welcome to Sift')
  await page.getByRole('button', { name: 'Get started' }).click()

  await step(page, 'Turn on USB debugging')
  await page.getByRole('button', { name: 'Continue' }).click()

  // The stand-in adb answers `adb version`, so nothing is offered for download.
  await step(page, 'Phone tools (adb)')
  await expect(page.getByText(/adb is ready/)).toBeVisible()
  await page.getByRole('button', { name: 'Continue' }).click()

  await step(page, 'Connect your phone')
  await expect(page.getByTestId('connect-state')).toHaveText('Connected: TestPhone.')
  await page.getByRole('button', { name: 'Continue' }).click()

  await step(page, 'Choose the backup folder')
  await page.getByRole('textbox', { name: 'Backup folder' }).fill(sift.backupDir)
  await expect(page.getByText(/free here\./)).toBeVisible()
  await page.getByRole('button', { name: 'Save and continue' }).click()

  await step(page, 'An SSD for a second copy (optional)')
  await page.getByRole('button', { name: 'Skip for now' }).click()

  // Sift looks through the phone by itself; the sizes appear without copying anything.
  await step(page, 'What to back up')
  const screenshots = page.getByRole('checkbox', { name: /^Screenshots/ })
  await expect(page.getByText(/^Ticked: \d+ files/)).toBeVisible()
  for (const box of await page.getByRole('checkbox').all()) {
    if ((await box.isChecked()) && box !== screenshots) await box.uncheck()
  }
  await screenshots.check()
  await expect(page.getByRole('checkbox', { checked: true })).toHaveCount(1)
  await page.getByRole('button', { name: 'Save and continue' }).click()

  await step(page, 'You are ready')
  await page.getByRole('button', { name: 'Start my first backup' }).click()

  await expect(page).toHaveURL(/\/backup$/)
  await expect(page.getByTestId('screen-state')).toHaveText('Finished', { timeout: 30_000 })
  expect(await filesUnder(sift.backupDir)).not.toHaveLength(0)

  // Setup is remembered: the app opens on the Backup screen from now on.
  await page.goto('/?t=e2e')
  await expect(page).toHaveURL(/\/backup$/)
})

test('an owner who has set Sift up before can skip the wizard', async ({ page }) => {
  await page.goto('/?t=e2e')
  await page.getByRole('button', { name: 'I have already set Sift up' }).click()
  await expect(page).toHaveURL(/\/backup$/)
})
