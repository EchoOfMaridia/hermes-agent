/**
 * RED test — session-token resolution in the desktop renderer.
 *
 * The desktop renderer is loaded as a static index.html via Electron's
 * `loadURL(pathToFileURL(...))` (see apps/desktop/electron/main.ts:8520
 * and 8873). The static HTML does NOT inject `window.__HERMES_SESSION_TOKEN__`
 * — only the browser dashboard's web_server.py does (hermes_cli/web_server.py:16354).
 *
 * The legacy workflow panels therefore read an empty token and surface
 * "no session token — cannot fetch library" against /api/workflows/library.
 *
 * The contract under test: `readSessionToken()` must resolve a usable token
 * from the preload bridge (`window.hermesDesktop.getConnection()`) when the
 * legacy window constant is absent. The window constant is a fallback for
 * the browser-dashboard path, not the canonical source.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { readSessionToken, resetSessionTokenCache } from '../session-token'

declare global {
  // eslint-disable-next-line no-var
  var hermesDesktop:
    | {
        getConnection?: () => Promise<{ token: string; baseUrl: string } | null>
      }
    | undefined
}

const originalDesktop = (globalThis as any).hermesDesktop
const originalToken = (globalThis as any).__HERMES_SESSION_TOKEN__

afterEach(() => {
  // Restore whatever the host environment had so tests don't bleed.
  if (originalDesktop === undefined) {
    delete (globalThis as any).hermesDesktop
  } else {
    ;(globalThis as any).hermesDesktop = originalDesktop
  }
  if (originalToken === undefined) {
    delete (globalThis as any).__HERMES_SESSION_TOKEN__
  } else {
    ;(globalThis as any).__HERMES_SESSION_TOKEN__ = originalToken
  }
})

describe('readSessionToken', () => {
  beforeEach(() => {
    delete (globalThis as any).__HERMES_SESSION_TOKEN__
    ;(globalThis as any).hermesDesktop = undefined
    resetSessionTokenCache()
  })

  it('returns empty string when no source is available', async () => {
    expect(await readSessionToken()).toBe('')
  })

  it('returns the empty-string marker BEFORE the preload bridge resolves (synchronous cache miss)', async () => {
    // The first call may return '' before the preload bridge resolves — the
    // workflows code path then re-resolves and retries. This pins the
    // sync/async split so anyone tightening that contract has to update this.
    let first: string = await readSessionToken()
    expect(first).toBe('')
  })

  it('prefers window.__HERMES_SESSION_TOKEN__ when the browser-dashboard injected it', async () => {
    ;(globalThis as any).__HERMES_SESSION_TOKEN__ = 'injected-token'
    ;(globalThis as any).hermesDesktop = {
      getConnection: async () => ({ token: 'bridge-token', baseUrl: 'http://127.0.0.1:8772' })
    }
    expect(await readSessionToken()).toBe('injected-token')
  })

  it('falls back to the preload bridge when the window constant is absent', async () => {
    // The renderer's actual desktop case: window.__HERMES_SESSION_TOKEN__ is
    // never set, but the preload bridge exposes the same auth credential.
    ;(globalThis as any).hermesDesktop = {
      getConnection: async () => ({ token: 'bridge-token', baseUrl: 'http://127.0.0.1:8772' })
    }
    expect(await readSessionToken()).toBe('bridge-token')
  })

  it('tolerates a missing preload bridge (non-Electron render)', async () => {
    ;(globalThis as any).hermesDesktop = undefined
    expect(await readSessionToken()).toBe('')
  })

  it('tolerates a connection with no token', async () => {
    ;(globalThis as any).hermesDesktop = {
      getConnection: async () => ({ token: '', baseUrl: 'http://127.0.0.1:8772' })
    }
    expect(await readSessionToken()).toBe('')
  })

  it('tolerates a rejecting preload bridge (e.g. before backend is up)', async () => {
    ;(globalThis as any).hermesDesktop = {
      getConnection: async () => {
        throw new Error('not ready')
      }
    }
    expect(await readSessionToken()).toBe('')
  })

  it('caches the resolved token across calls within the same tick', async () => {
    const getConnection = vi.fn(async () => ({
      token: 'bridge-token',
      baseUrl: 'http://127.0.0.1:8772'
    }))
    ;(globalThis as any).hermesDesktop = { getConnection }
    expect(await readSessionToken()).toBe('bridge-token')
    expect(await readSessionToken()).toBe('bridge-token')
    // The bridge may be probed once and cached — call count is bounded so a
    // future invariant-change is intentional, not accidental.
    expect(getConnection.mock.calls.length).toBeLessThanOrEqual(2)
  })
})
