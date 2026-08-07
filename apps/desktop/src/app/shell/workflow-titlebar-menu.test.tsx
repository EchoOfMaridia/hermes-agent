import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { afterEach } from 'vitest'

// Desktop preload bridge stub. The titlebar menu now goes through
// `window.hermesDesktop.api` (the IPC seam that resolves the live
// backend's baseUrl) instead of a raw `fetch('/api/...')`, so the
// earlier test pattern of mocking `globalThis.fetch` no longer matches
// the production code path.
const apiMock = vi.fn()

;(globalThis as unknown as { hermesDesktop: { api: typeof apiMock } }).hermesDesktop = {
  api: apiMock
}

// Mock the i18n module so we don't need a real TranslationsProvider.
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      workflows: {
        title: 'Workflows',
        titlebarMenuLabel: 'Workflows',
        libraryEmptyTitle: 'No saved workflows',
        runButton: 'Run',
        running: 'Running',
      }
    }
  })
}))

import { WorkflowsTitlebarMenu } from './workflow-titlebar-menu'

const inDoc = (el: Element | null): boolean =>
  el !== null && el instanceof HTMLElement && document.body.contains(el)

describe('WorkflowsTitlebarMenu', () => {
  beforeEach(() => {
    apiMock.mockReset()
    apiMock.mockImplementation(async ({ path }: { path: string }) => {
      if (path === '/api/workflows/library') {
        return {
          entries: [
            { name: 'demo', description: 'smoke workflow', path: 'demo.py', created_at: '2026-07-19T22:26:05Z' },
            { name: 'fix_bugs', description: 'verify bug fixes', path: 'fix_bugs.py', created_at: '2026-06-30T21:39:25Z' }
          ]
        }
      }

      if (path === '/api/workflows/run') {
        return { run_id: 'r_titlebar_test' }
      }

      throw new Error(`unexpected path: ${path}`)
    })
  })
  afterEach(() => {
    cleanup()
  })

  it('renders a Workflows trigger button', () => {
    render(<WorkflowsTitlebarMenu />)
    expect(inDoc(screen.getByRole('button', { name: /workflows/i }))).toBe(true)
  })

  it('clicking trigger opens a popover that lists library entries', async () => {
    render(<WorkflowsTitlebarMenu />)
    const trigger = screen.getByRole('button', { name: /workflows/i })
    fireEvent.click(trigger)
    await waitFor(() => {
      expect(inDoc(screen.getByText('demo'))).toBe(true)
    })
    expect(inDoc(screen.getByText('fix_bugs'))).toBe(true)
  })

  it('clicking a library entry starts a run via the desktop bridge', async () => {
    render(<WorkflowsTitlebarMenu />)
    fireEvent.click(screen.getByRole('button', { name: /workflows/i }))
    const runButtons = await screen.findAllByRole('button', { name: /run/i })
    expect(runButtons.length).toBeGreaterThanOrEqual(1)
    fireEvent.click(runButtons[0])
    await waitFor(() => {
      const runCalls = apiMock.mock.calls.filter(c => c[0]?.path === '/api/workflows/run')
      expect(runCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('fetches /api/workflows/library when the popover opens', async () => {
    render(<WorkflowsTitlebarMenu />)
    // Library is fetched lazily on first open, not on mount
    expect(apiMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /workflows/i }))
    // Trigger the lazy fetch explicitly so the test isn't sensitive to
    // useEffect timing in jsdom.
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }))
    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        expect.objectContaining({ path: '/api/workflows/library' })
      )
    })
  })
})
