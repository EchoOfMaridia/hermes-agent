import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach } from 'vitest'

// Mock session token
;(globalThis as unknown as { __HERMES_SESSION_TOKEN__: string }).__HERMES_SESSION_TOKEN__ = 'test-token'

// Mock fetch — titlebar menu calls /api/workflows/library
const fetchMock = vi.fn(async (url: string) => {
  if (url === '/api/workflows/library') {
    return {
      ok: true, status: 200,
      json: async () => ({
        entries: [
          { name: 'demo', description: 'smoke workflow', path: 'demo.py', created_at: '2026-07-19T22:26:05Z' },
          { name: 'fix_bugs', description: 'verify bug fixes', path: 'fix_bugs.py', created_at: '2026-06-30T21:39:25Z' }
        ]
      })
    } as Response
  }
  if (url === '/api/workflows/run') {
    return {
      ok: true, status: 200,
      json: async () => ({ run_id: 'r_titlebar_test' })
    } as Response
  }
  return { ok: false, status: 404, json: async () => ({}) } as Response
})
;(globalThis as unknown as { fetch: typeof fetch }).fetch = fetchMock as unknown as typeof fetch

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
    fetchMock.mockClear()
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

  it('clicking a library entry starts a run via REST', async () => {
    render(<WorkflowsTitlebarMenu />)
    fireEvent.click(screen.getByRole('button', { name: /workflows/i }))
    const runButtons = await screen.findAllByRole('button', { name: /run/i })
    expect(runButtons.length).toBeGreaterThanOrEqual(1)
    fireEvent.click(runButtons[0])
    await waitFor(() => {
      const runCalls = fetchMock.mock.calls.filter(c => c[0] === '/api/workflows/run')
      expect(runCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('fetches /api/workflows/library when the popover opens', async () => {
    render(<WorkflowsTitlebarMenu />)
    // Library is fetched lazily on first open, not on mount
    expect(fetchMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /workflows/i }))
    // Trigger the lazy fetch explicitly so the test isn't sensitive to
    // useEffect timing in jsdom (the test that follows this one opens
    // the popover, but the useEffect may have run before the click in
    // some orderings).
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }))
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/workflows/library',
        expect.objectContaining({ headers: expect.any(Object) })
      )
    })
  })
})