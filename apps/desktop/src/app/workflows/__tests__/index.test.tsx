import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { afterEach } from 'vitest'

import { $workflowRuns } from '@/store/workflow-runs'

import { WorkflowsView } from '../index'

// Mock the desktop preload bridge. The Library tab now goes through
// `window.hermesDesktop.api` (the IPC seam that resolves the live
// backend's baseUrl) instead of a raw `fetch('/api/...')`, so the
// earlier test pattern of mocking `globalThis.fetch` no longer matches
// the production code path. Stub the bridge the same way the real
// renderer sees it.

const apiMock = vi.fn()

;(globalThis as unknown as { hermesDesktop: { api: typeof apiMock } }).hermesDesktop = {
  api: apiMock
}

// Helper: assert an element is mounted in the document. We avoid
// @testing-library/jest-dom (not installed) and use the built-in
// `instanceof HTMLElement` check.
const inDoc = (el: Element | null): boolean =>
  el !== null && el instanceof HTMLElement && document.body.contains(el)

describe('WorkflowsView tabbed panel', () => {
  beforeEach(() => {
    $workflowRuns.set({})
    apiMock.mockReset()
    apiMock.mockImplementation(async ({ path }: { path: string }) => {
      if (path === '/api/workflows/library') {
        return {
          entries: [
            { name: 'demo', description: 'smoke workflow', path: 'demo.py', created_at: '2026-07-19T22:26:05Z' },
            { name: 'bug_fix_verification', description: 'verify bug fixes', path: 'bug_fix_verification.py', created_at: '2026-06-30T21:39:25Z' }
          ]
        }
      }

      if (path === '/api/workflows/run') {
        return { run_id: 'r_test123' }
      }

      throw new Error(`unexpected path: ${path}`)
    })
  })
  afterEach(() => {
    cleanup()
  })

  it('renders both tabs', () => {
    render(<WorkflowsView onClose={() => {}} />)
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
      expect(apiMock).toHaveBeenCalledWith(
        expect.objectContaining({ path: '/api/workflows/library' })
      )
    })
    expect(inDoc(await screen.findByText('demo'))).toBe(true)
    expect(inDoc(await screen.findByText('bug_fix_verification'))).toBe(true)
  })

  it('Run button on a library entry starts a run via the desktop bridge', async () => {
    render(<WorkflowsView onClose={() => {}} />)
    const libraryTab = screen.getByRole('tab', { name: /library/i })
    fireEvent.click(libraryTab)
    const runButtons = await screen.findAllByRole('button', { name: /run/i })
    expect(runButtons.length).toBeGreaterThanOrEqual(1)
    fireEvent.click(runButtons[0])
    await waitFor(() => {
      const runCalls = apiMock.mock.calls.filter(
        c => c[0]?.path === '/api/workflows/run'
      )

      expect(runCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('Runs tab still shows empty state when no runs', () => {
    render(<WorkflowsView onClose={() => {}} />)
    expect(inDoc(screen.getByText(/no workflow runs yet/i))).toBe(true)
  })

  // Regression: the Library tab must use the desktop bridge even when
  // the window constant that the browser-dashboard web_server injects
  // is absent (the desktop renderer's static index.html never sets it).
  // The earlier failure mode — raw `fetch('/api/workflows/library')`
  // against a `file://` page — surfaced as "Failed to fetch" because
  // the page has no origin serving that path.
  it('Library tab fetches via the desktop bridge (no window-constant dependency)', async () => {
    render(<WorkflowsView onClose={() => {}} />)
    const libraryTab = screen.getByRole('tab', { name: /library/i })
    fireEvent.click(libraryTab)
    expect(inDoc(await screen.findByText('demo'))).toBe(true)

    const libraryCalls = apiMock.mock.calls.filter(
      c => c[0]?.path === '/api/workflows/library'
    )

    expect(libraryCalls.length).toBeGreaterThanOrEqual(1)
  })

  // Regression: a Run that fails must surface its error inline next to
  // the failing entry, even when the Library tab has rows. The earlier
  // shape put run-errors into the library-level $libraryError, which
  // was only rendered when entries.length === 0 — so a failed Run
  // against a populated library silently flickered the button and
  // the user had no way to see why the run did nothing.
  it('Run failure surfaces an inline error next to the failing entry', async () => {
    render(<WorkflowsView onClose={() => {}} />)
    const libraryTab = screen.getByRole('tab', { name: /library/i })
    fireEvent.click(libraryTab)
    const runButtons = await screen.findAllByRole('button', { name: /run/i })
    expect(runButtons.length).toBeGreaterThanOrEqual(1)

    // Override the mock AFTER the library has loaded so the next call
    // (the /api/workflows/run POST) hits our failure path.
    apiMock.mockImplementationOnce(async ({ path }: { path: string }) => {
      if (path === '/api/workflows/run') {
        throw new Error('submit failed: workflow script missing on disk: demo.py')
      }

      throw new Error(`unexpected path: ${path}`)
    })

    fireEvent.click(runButtons[0])
    const errorEl = await screen.findByTestId('workflow-run-error')
    expect(inDoc(errorEl)).toBe(true)
    expect(errorEl.textContent).toMatch(/submit failed.*workflow script missing/)
  })

  // Regression: the Run button must stay disabled while a run is in
  // flight, not just while the submit is pending. The earlier shape
  // set disabled={startingRun === entry.name}, which flipped back to
  // false the moment startRun() resolved — so the user only saw a
  // one-frame spinner before the button re-enabled, even though the
  // run was still going. Now disabled follows $workflowRuns too:
  // any run whose workflowName matches and whose state === 'running'
  // keeps the button busy.
  it('Run button stays disabled while a matching run is running in $workflowRuns', async () => {
    // Seed $workflowRuns with a running row that matches the first
    // library entry. The click on Run should still hit the bridge
    // (proving the button is *not* silently disabled for the wrong
    // reason) — but the button's disabled state must reflect the
    // live run, not just the local submit promise.
    $workflowRuns.set({
      r_already_running: {
        runId: 'r_already_running',
        workflowName: 'demo',
        state: 'running',
        startedAt: Date.now() / 1000,
        endedAt: null,
        maxConcurrent: null,
        maxTotal: null,
        steps: [],
        errorMessage: null,
        errorType: null,
        haltReason: null
      }
    })

    render(<WorkflowsView onClose={() => {}} />)
    const libraryTab = screen.getByRole('tab', { name: /library/i })
    fireEvent.click(libraryTab)
    const runButtons = await screen.findAllByRole('button', { name: /run/i })
    // The button on the running row must be disabled; the button on
    // the OTHER row (bug_fix_verification) must remain enabled.
    const demoRow = runButtons.find(b => b.closest('[data-testid="workflow-run-error"]') === null) ?? runButtons[0]
    expect(runButtons[0].hasAttribute('disabled')).toBe(true)
    expect(runButtons[1].hasAttribute('disabled')).toBe(false)
    void demoRow
  })

  // The WireProbe is a self-diagnostic strip the operator uses to
  // tell at a glance whether the run-click → bridge → runtime →
  // dispatcher → tui_gateway chain is wired correctly. It calls
  // /api/workflows/diag on mount and every 2s, and renders the
  // live state inside the panel so a "0 runs since boot" mystery
  // is debuggable without leaving the panel.
  it('renders the wire-probe diagnostic strip with live bridge stats', async () => {
    apiMock.mockImplementation(async ({ path }: { path: string }) => {
      if (path === '/api/workflows/diag') {
        return {
          plugin_active_runtime_present: true,
          singleton_runtime_present: true,
          singleton_is_plugin_runtime: true,
          runtime_dispatcher_set: true,
          runtime_dispatcher_type: '_chained',
          bridge_present: true,
          bridge_stats: { received: 7, translated: 4, emit_ok: 4, emit_failed: 0, filtered_non_workflow: 3 },
          tui_gateway_emit_importable: true,
          active_run_ids: ['r_one', 'r_two']
        }
      }

      if (path === '/api/workflows/library') {
        return {
          entries: [
            { name: 'demo', description: 'smoke workflow', path: 'demo.py', created_at: '2026-07-19T22:26:05Z' }
          ]
        }
      }

      throw new Error(`unexpected path: ${path}`)
    })

    render(<WorkflowsView onClose={() => {}} />)
    const probe = await screen.findByTestId('workflow-wire-probe')
    expect(inDoc(probe)).toBe(true)
    // Wait for the diag fetch + render to settle.
    await waitFor(() => {
      expect(probe.textContent).toMatch(/rx=7/)
      expect(probe.textContent).toMatch(/tx=4/)
    })
  })

  it('wire-probe surfaces a fetch failure instead of going silent', async () => {
    apiMock.mockImplementation(async ({ path }: { path: string }) => {
      if (path === '/api/workflows/diag') {
        throw new Error('401: unauthorized')
      }

      if (path === '/api/workflows/library') {
        return { entries: [] }
      }

      throw new Error(`unexpected path: ${path}`)
    })

    render(<WorkflowsView onClose={() => {}} />)
    const probe = await screen.findByTestId('workflow-wire-probe')
    await waitFor(() => {
      expect(probe.textContent).toMatch(/diag fetch failed.*401/)
    })
  })
})
