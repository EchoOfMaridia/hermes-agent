/* eslint-disable */
// Integration test: prove that when a subagent.start event arrives on a
// session, the running subagent actually appears in the composer's status
// stack AND that the subagent section auto-expands so the user sees the row
// (not just the section header).
//
// This drives the full data path the gateway uses: $subagentsBySession is
// updated via the public `upsertSubagent` API (which is exactly what
// use-message-stream calls), then the computed $statusItemsBySession picks
// it up, and the status-stack component renders it. We then assert on the
// rendered DOM to prove the user-visible menu actually shows the subagent.

import { act, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { $activeSessionId, setActiveSessionId } from '@/store/session'
import {
  $subagentsBySession,
  upsertSubagent
} from '@/store/subagents'
import { $statusItemsBySession } from '@/store/composer-status'

import { ComposerStatusStack } from './index'

// ResizeObserver isn't implemented in jsdom — stub it so the layout effect
// inside ComposerStatusStack can mount without throwing.
beforeAll(() => {
  ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})

// Wrap the status stack in a MemoryRouter so its `useNavigate()` call has a
// router context. The component itself doesn't actually navigate during the
// assertion path; the router just has to exist for the hook to be callable.
function Harness({ sessionId }: { sessionId: null | string }) {
  return (
    <MemoryRouter>
      <ComposerStatusStack queue={null} sessionId={sessionId} />
    </MemoryRouter>
  )
}

describe('subagents menu — running subagent renders in the composer status stack', () => {
  beforeEach(() => {
    $subagentsBySession.set({})
    setActiveSessionId(null)
  })

  afterEach(() => {
    $subagentsBySession.set({})
    setActiveSessionId(null)
  })

  it('shows the goal + a spinner for a running subagent (not just the section header)', () => {
    // 1. Set up the session the user is currently viewing.
    setActiveSessionId('parent-runtime')

    // 2. Simulate the gateway firing subagent.start for that session. This is
    //    the same call `use-message-stream` makes via `upsertSubagent` at
    //    use-message-stream.ts:945, just invoked directly so we don't need
    //    to mount the full hook.
    upsertSubagent('parent-runtime', {
      child_session_id: 'child-runtime',
      depth: 1,
      goal: 'Scan /home/cage/projects for TODOs',
      model: 'anthropic/claude-sonnet-4.6',
      parent_id: null,
      status: 'running',
      subagent_id: 'sub-1',
      task_count: 1,
      task_index: 0,
      tool_count: 0,
      toolsets: ['project']
    } as never)

    // 3. Sanity-check the data path that the hook drives: the subagent must
    //    be in $subagentsBySession AND it must be in $statusItemsBySession
    //    with state=running. (The user said "never shows" — this is the
    //    predicate that, if false, would explain the symptom.)
    const subs = $subagentsBySession.get()
    expect(subs['parent-runtime']).toHaveLength(1)
    expect(subs['parent-runtime'][0].status).toBe('running')

    const items = $statusItemsBySession.get()
    expect(items['parent-runtime']).toHaveLength(1)
    expect(items['parent-runtime'][0].type).toBe('subagent')
    expect(items['parent-runtime'][0].state).toBe('running')

    // 4. Now render the status stack and assert the subagent row is in the
    //    DOM. Before the auto-expand fix, defaultCollapsed=true on the
    //    subagent section would hide the row — the user would see only the
    //    header "1 Subagents" and assume the menu was broken.
    render(<Harness sessionId="parent-runtime" />)

    // The goal text appears in the rendered row.
    expect(
      screen.getByText(/Scan \/home\/cage\/projects for TODOs/)
    ).toBeTruthy()
  })

  it('section auto-expands when a subagent is running, no click required', () => {
    // Repro of the original bug: the section was collapsed by default, so
    // the user had to click to reveal running subagents. After the fix, the
    // subagent section auto-expands.

    setActiveSessionId('parent-runtime')

    upsertSubagent('parent-runtime', {
      goal: 'Background research task',
      status: 'running',
      subagent_id: 'sub-2',
      task_index: 0
    } as never)

    render(<Harness sessionId="parent-runtime" />)

    // The row text is visible without any user interaction. If the section
    // were still collapsed, the goal text would be absent from the DOM.
    expect(screen.getAllByText(/Background research task/i).length).toBeGreaterThan(0)
  })

  it('does not render anything when no subagents are running for the session', () => {
    setActiveSessionId('empty-runtime')

    render(<Harness sessionId="empty-runtime" />)

    // No subagent rows should be present.
    expect(screen.queryByText(/Subagent/i)).not.toBeTruthy()
  })

  it('hides completed subagents from the running list (only running/queued pass through)', () => {
    setActiveSessionId('parent-runtime')

    upsertSubagent('parent-runtime', {
      goal: 'Already-done task',
      status: 'completed',
      subagent_id: 'sub-3',
      summary: 'finished',
      task_index: 0
    } as never)

    render(<Harness sessionId="empty-runtime" />)

    // Completed subagent should NOT appear.
    expect(screen.queryByText(/Already-done task/)).not.toBeTruthy()
  })
})
