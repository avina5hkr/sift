import { readdir } from 'node:fs/promises'
import { join } from 'node:path'
import type { Page } from '@playwright/test'
import { expect, test } from './fixtures'

// The Screenshots set is small (20 files). A slow phone makes a run long enough to act on while
// it is going; full speed lets it finish.
const SLOW_BPS = 400
const SCREENSHOTS = '/sdcard/DCIM/Screenshots'

async function filesUnder(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { withFileTypes: true, recursive: true }).catch(() => [])
  return entries.filter((e) => e.isFile()).map((e) => join(e.parentPath, e.name))
}

const screenState = (page: Page) => page.getByTestId('screen-state')

async function open(page: Page) {
  await page.goto('/?t=e2e')
  await expect(page).toHaveURL(/\/backup$/)
  await expect(page.getByText(/^TestPhone, USB/)).toBeVisible()
}

const backUpNow = (page: Page) => page.getByRole('button', { name: 'Back up now', exact: true }).click()

test.beforeEach(async ({ sift }) => {
  await sift.prepare()
})

test('a backup runs to Finished and the files are on the Mac', async ({ page, sift }) => {
  await open(page)
  await expect(screenState(page)).toHaveText('Ready')
  await backUpNow(page)
  await expect(screenState(page)).toHaveText('Finished', { timeout: 30_000 })
  await expect(page.getByText(/^Last backup:/)).toBeVisible()
  await expect(page.getByText('No files copied yet.')).toHaveCount(0)
  expect(await filesUnder(sift.backupDir)).toHaveLength(20)
})

test('a backup can be paused and resumed', async ({ page, sift }) => {
  await sift.phone.speed(SLOW_BPS)
  await open(page)
  await backUpNow(page)
  await expect(screenState(page)).toHaveText('Backing up…', { timeout: 15_000 })

  await page.getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(screenState(page)).toHaveText('Paused', { timeout: 15_000 })
  // Paused means paused: no new file reaches the Mac while it lasts.
  const copied = (await filesUnder(sift.backupDir)).length
  await page.waitForTimeout(1500)
  expect((await filesUnder(sift.backupDir)).length).toBe(copied)
  expect(copied).toBeLessThan(20)

  await sift.phone.speed(null)
  await page.getByRole('button', { name: 'Resume', exact: true }).click()
  await expect(screenState(page)).toHaveText('Finished', { timeout: 30_000 })
  expect(await filesUnder(sift.backupDir)).toHaveLength(20)
})

test('unplugging the phone shows Waiting for your phone, and plugging it back in carries on', async ({
  page,
  sift,
}) => {
  await sift.phone.speed(SLOW_BPS)
  await open(page)
  await backUpNow(page)
  await expect(screenState(page)).toHaveText('Backing up…', { timeout: 15_000 })

  await sift.phone.unplug()
  await expect(screenState(page)).toHaveText('Waiting for your phone', { timeout: 15_000 })
  await expect(page.getByText('Plug your phone back in. The backup carries on by itself.')).toBeVisible()
  await expect(page.getByText('No phone connected')).toBeVisible()

  await sift.phone.speed(null)
  await sift.phone.replug()
  await expect(screenState(page)).toHaveText('Finished', { timeout: 30_000 })
  expect(await filesUnder(sift.backupDir)).toHaveLength(20)
})

test('when many files vanish from the phone, Sift asks before it changes anything', async ({ page, sift }) => {
  await open(page)
  await backUpNow(page)
  await expect(screenState(page)).toHaveText('Finished', { timeout: 30_000 })
  const before = await filesUnder(sift.backupDir)
  expect(before).toHaveLength(20)

  // Every file is gone from the folder: far more than a fifth of the set.
  await sift.phone.remove(SCREENSHOTS, true)
  await backUpNow(page)
  await expect(screenState(page)).toHaveText('Waiting for your confirmation', { timeout: 30_000 })
  const card = page.getByRole('alertdialog', { name: 'Lots of files have gone from your phone' })
  await expect(card).toBeVisible()
  await expect(card.getByRole('button', { name: 'Scan again' })).toBeFocused()
  // Waiting for the owner changes nothing, on the phone's record or on the Mac.
  expect(await filesUnder(sift.backupDir)).toEqual(before)

  await card.getByRole('button', { name: 'Confirm' }).click()
  await expect(screenState(page)).toHaveText('Finished', { timeout: 30_000 })
  await expect(card).toHaveCount(0)
  // The copies already on the Mac are safe either way.
  expect(await filesUnder(sift.backupDir)).toEqual(before)
})
