import { test as base, expect } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import { chmod, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

export { expect }

const TOKEN = 'e2e'
const REPO = resolve(import.meta.dirname, '..', '..')
const START_TIMEOUT_MS = 60_000

/** A running Sift (headless, fake phone) with its own data folder, backup folder and port. */
export type Sift = {
  url: string
  /** The backup folder: always inside this test's own temporary folder. */
  backupDir: string
  get: <T = unknown>(path: string) => Promise<T>
  post: <T = unknown>(path: string, body?: unknown) => Promise<T>
  /** Drive the fake phone (see sift/server/routes/testphone.py). */
  phone: {
    unplug: () => Promise<void>
    replug: () => Promise<void>
    /** Bytes per second the phone serves; null is full speed. */
    speed: (bytesPerSecond: number | null) => Promise<void>
    /** Delete a file or folder; with `keepFolder` the folder stays, empty. */
    remove: (path: string, keepFolder?: boolean) => Promise<void>
  }
  /** Mark first-run setup as finished (the Backup screen opens instead of the wizard). */
  complete: () => Promise<void>
  /** Setup done through the API: phone connected, backup folder, only the Screenshots set. */
  prepare: () => Promise<void>
}

type Options = {
  /** Seconds before the fake phone shows up (a very high number: not until Retry). */
  phoneDelayS: number
}

async function call(base: string, method: string, path: string, body?: unknown): Promise<unknown> {
  const res = await fetch(base + path, {
    method,
    headers: { 'X-Sift-Token': TOKEN, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const text = await res.text()
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${text}`)
  return text ? JSON.parse(text) : null
}

function start(env: NodeJS.ProcessEnv): Promise<{ child: ChildProcess; url: string }> {
  return new Promise((resolveStart, reject) => {
    // Its own process group, so stopping it also stops what `uv run` started.
    const child = spawn('uv', ['run', 'sift', '--no-window', '--fake-phone', '--port', '0'], {
      cwd: REPO,
      env,
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    const timer = setTimeout(() => reject(new Error(`Sift did not start:\n${out}`)), START_TIMEOUT_MS)
    const onData = (chunk: Buffer) => {
      out += chunk.toString()
      const m = /Sift running at (http:\/\/127\.0\.0\.1:\d+)\//.exec(out)
      if (m?.[1]) {
        clearTimeout(timer)
        resolveStart({ child, url: m[1] })
      }
    }
    child.stdout?.on('data', onData)
    child.stderr?.on('data', onData)
    child.on('exit', (code) => {
      clearTimeout(timer)
      reject(new Error(`Sift exited (${code}) before it was up:\n${out}`))
    })
  })
}

function stop(child: ChildProcess): Promise<void> {
  return new Promise((done) => {
    if (child.exitCode !== null || child.pid === undefined) return done()
    const pid = child.pid
    const kill = setTimeout(() => process.kill(-pid, 'SIGKILL'), 20_000)
    child.once('exit', () => {
      clearTimeout(kill)
      done()
    })
    process.kill(-pid, 'SIGTERM')
  })
}

export const test = base.extend<{ sift: Sift } & Options>({
  phoneDelayS: [0.5, { option: true }],

  sift: async ({ phoneDelayS }, provide) => {
    const root = await mkdtemp(join(tmpdir(), 'sift-e2e-'))
    const home = join(root, 'home')
    const backupDir = join(root, 'backup')
    await mkdir(join(home, 'support'), { recursive: true })
    // A stand-in for adb that only answers `adb version`, so the wizard never downloads anything
    // and never needs a real adb. The fake phone does not use it.
    const adb = join(root, 'adb')
    await writeFile(adb, '#!/bin/sh\necho "Android Debug Bridge version 1.0.41"\necho "Version 35.0.0-e2e"\n')
    await chmod(adb, 0o755)
    await writeFile(join(home, 'support', 'config.toml'), `[phone]\nadb_path = "${adb}"\n`)

    const { child, url } = await start({
      ...process.env,
      SIFT_HOME: home,
      SIFT_DEV_TOKEN: TOKEN,
      SIFT_WEB_DIST: join(REPO, 'web', 'dist'),
      SIFT_TRACKER: 'fake',
      SIFT_FAKE_PHONE_DELAY_S: String(phoneDelayS),
      SIFT_TEST_PHONE_CONTROL: '1',
    })
    const get = <T,>(path: string) => call(url, 'GET', path) as Promise<T>
    const post = <T,>(path: string, body?: unknown) => call(url, 'POST', path, body) as Promise<T>
    const phone = (action: string, body?: unknown) => post(`/api/test/phone/${action}`, body ?? undefined).then(() => {})
    const sift: Sift = {
      url,
      backupDir,
      get,
      post,
      phone: {
        unplug: () => phone('unplug'),
        replug: () => phone('replug'),
        speed: (bytesPerSecond) => phone('speed', { bytes_per_s: bytesPerSecond }),
        remove: (path, keepFolder = false) => phone('remove', { path, keep_folder: keepFolder }),
      },
      complete: () => post('/api/setup/complete').then(() => {}),
      prepare: async () => {
        await expect
          .poll(async () => (await get<{ device: { state: string } | null }>('/api/status')).device?.state, {
            timeout: 15_000,
          })
          .toBe('connected_usb')
        await post('/api/setup/mac-folder', { path: backupDir, label: 'Phone' })
        await post('/api/setup/sets', { enabled: ['Screenshots'] })
        await post('/api/setup/complete')
      },
    }
    try {
      await provide(sift)
    } finally {
      await stop(child)
      await rm(root, { recursive: true, force: true })
    }
  },

  baseURL: async ({ sift }, provide) => {
    await provide(sift.url)
  },
})
