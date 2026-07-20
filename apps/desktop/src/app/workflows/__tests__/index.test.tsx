import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach } from 'vitest'

import { $workflowRuns } from '@/store/workflow-runs'
import { WorkflowsView } from '../index'

// Mock the session token so the Library tab's fetch has something to use.
;(globalThis as unknown as { __HERMES_SESSION_TOKEN__: string }).__HERMES_SESSION_TOKEN__ = 'test-token'

// Helper: assert an element is mounted in the document. We avoid
// @testing-library/jest-dom (not installed) and use the built-in
// `instanceof HTMLElement` check.
const inDoc = (el: Element | null): boolean =>
  el !== null && el instanceof HTMLElement && document.body.contains(el)

// Mock the network: the Library tab calls /api/workflows/library on mount.
const fetchMock = vi.fn(async (url: string) => {
  if (url === '/api/workflows/library') {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        entries: [
          { name: 'demo', description: 'smoke workflow', path: 'demo.py', created_at: '2026-07-19T22:26:05Z' },
          { name: 'bug_fix_verification', description: 'verify bug fixes', path: 'bug_fix_verification.py', created_at: '2026-06-30T21:39:25Z' }
        ]
      })
    } as Response
  }
  if (url === '/api/workflows/run') {
    return {
      ok: true, status: 200,
      json: async () => ({ run_id: 'r_test123' })
    } as Response
  }
  return { ok: false, status: 404, json: async () => ({}) } as Response
})
;(globalThis as unknown as { fetch: typeof fetch }).fetch = fetchMock as unknown as typeof fetch

describe('WorkflowsView tabbed panel', () => {
  beforeEach(() => {
    $workflowRuns.set({})
    fetchMock.mockClear()
  })
  afterEach(() => {
    cleanup()
  })

  it('renders both tabs', () => {
    render(<WorkflowsView onClose={() => {}} />)
    // Both tabs are in the role=tablist with their i18n labels.
    expect(inDoc(screen.getByRole('tab', { name: /runs/i }))).toBe(true)
    expect(inDoc(screen.getByRole('tab', { name: /library/i }))).toBe(true)
  })

  it('default tab is Runs (preserves existing behavior)', () => {
    render(<WorkflowsView onClose={() => {}} />)
    const runsTab = screen.getByRole('tab', { name: /runs/i })
    expect(runsTab.getAttribute('aria-selected')).toBe('true')
  })

  it('clicking Library tab fetches and lists entries', async () => {
    render(<WorkflowsView onClose={() => {}} />)
    const libraryTab = screen.getByRole('tab', { name: /library/i })
    fireEvent.click(libraryTab)
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/workflows/library',
        expect.objectContaining({ headers: expect.any(Object) })
      )
    })
    // Library entries rendered as rows
    expect(inDoc(await screen.findByText('demo'))).toBe(true)
    expect(inDoc(await screen.findByText('bug_fix_verification'))).toBe(true)
  })

  it('Run button on a library entry starts a run via REST', async () => {
    render(<WorkflowsView onClose={() => {}} />)
    const libraryTab = screen.getByRole('tab', { name: /library/i })
    fireEvent.click(libraryTab)
    const runButtons = await screen.findAllByRole('button', { name: /run/i })
    expect(runButtons.length).toBeGreaterThanOrEqual(1)
    fireEvent.click(runButtons[0])
    await waitFor(() => {
      const runCalls = fetchMock.mock.calls.filter(
        c => c[0] === '/api/workflows/run'
      )
      expect(runCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('Runs tab still shows empty state when no runs', () => {
    render(<WorkflowsView onClose={() => {}} />)
    // The empty state for runs is shown by default
    expect(inDoc(screen.getByText(/no workflow runs yet/i))).toBe(true)
  })
})