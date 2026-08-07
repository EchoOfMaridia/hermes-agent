/**
 * Session-token resolution for the desktop renderer.
 *
 * Two injection paths exist for the bearer token used against the
 * /api/* REST surface:
 *
 *  1. The browser-dashboard path — `hermes_cli/web_server.py` injects
 *     `window.__HERMES_SESSION_TOKEN__="..."` into the served HTML at
 *     boot. Synchronous, available before any module renders.
 *  2. The Electron desktop path — the renderer is loaded as a static
 *     `index.html` via `mainWindow.loadURL(pathToFileURL(...))`
 *     (see apps/desktop/electron/main.ts:8520 / 8873). That HTML carries
 *     no token injection; the credential lives behind the preload bridge
 *     at `window.hermesDesktop.getConnection()`, the same surface the rest
 *     of the renderer uses for WebSocket and REST auth.
 *
 * `readSessionToken()` is the shared helper for REST calls that need an
 * Authorization header. It checks the synchronous constant first
 * (covers the browser-dashboard case), then falls back to the
 * preload bridge (covers the Electron case).
 *
 * The legacy workflow panels (`apps/desktop/src/app/workflows/index.tsx`
 * and `apps/desktop/src/app/shell/workflow-titlebar-menu.tsx`) used to
 * read only the window constant — which the desktop renderer's static
 * HTML never sets. That path surfaced "no session token — cannot fetch
 * library" against /api/workflows/library. This module is the seam.
 */

interface ConnectionInfo {
  token: string
  baseUrl: string
}

interface DesktopBridge {
  getConnection?: () => Promise<ConnectionInfo | null>
}

const WINDOW_TOKEN_KEY = '__HERMES_SESSION_TOKEN__'

function readInjectedToken(): string {
  if (typeof window === 'undefined') {
    return ''
  }
  const candidate = (window as unknown as Record<string, unknown>)[WINDOW_TOKEN_KEY]
  return typeof candidate === 'string' && candidate.length > 0 ? candidate : ''
}

function getBridge(): DesktopBridge | null {
  if (typeof window === 'undefined') {
    return null
  }
  const candidate = (window as unknown as { hermesDesktop?: DesktopBridge }).hermesDesktop
  return candidate && typeof candidate.getConnection === 'function' ? candidate : null
}

let cachedToken: string | null = null
let inflight: Promise<string> | null = null

async function resolveFromBridge(): Promise<string> {
  if (cachedToken !== null) {
    return cachedToken
  }
  if (inflight) {
    return inflight
  }
  const bridge = getBridge()
  if (!bridge?.getConnection) {
    return ''
  }
  inflight = (async () => {
    try {
      const connection = await bridge.getConnection!()
      const token = typeof connection?.token === 'string' ? connection.token : ''
      cachedToken = token
      return token
    } catch {
      // Bridge rejection (backend not yet up, profile swap mid-flight).
      // The caller will retry on the next refresh; do not poison the cache.
      return ''
    } finally {
      inflight = null
    }
  })()
  return inflight
}

export async function readSessionToken(): Promise<string> {
  const injected = readInjectedToken()
  if (injected) {
    return injected
  }
  return resolveFromBridge()
}

/**
 * Reset the cached token. Called by the renderer when the user changes
 * profile or reapplies the connection — the previous token may still
 * resolve against the *previous* backend, but the new connection is the
 * authoritative one.
 */
export function resetSessionTokenCache(): void {
  cachedToken = null
  inflight = null
}
