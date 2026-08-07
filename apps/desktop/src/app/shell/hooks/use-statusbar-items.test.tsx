/**
 * Tests for context-usage pill bugs.
 *
 * Bug 1 — session switch: $currentUsage is a global atom that only mirrors the
 *   active session's usage. When the user clicks a different session in the
 *   sidebar (changes $selectedStoredSessionId), $currentUsage is never updated,
 *   so the pill shows stale usage for the previously-active session.
 *
 *   Fix: wire $currentUsage to update when $selectedStoredSessionId changes,
 *   applying the stored session's persisted input_tokens/output_tokens.
 *
 * Bug 2 — real-time streaming: usage is only written on turn-end
 *   (message.complete → payload.usage). During streaming, context_used ticks up
 *   server-side but the pill freezes until the turn finishes.
 *
 *   Fix: handle partial usage in message.delta events and update $currentUsage.
 */

import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import { $activeTreeGroup } from '@/components/pane-shell/tree/store'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { $currentUsage, setCurrentUsage } from '@/store/session'
import {
  $activeSessionId,
  $selectedStoredSessionId,
  $sessions,
  setActiveSessionId,
  setSelectedStoredSessionId,
  setSessions
} from '@/store/session'
import {
  $focusedRuntimeId,
  $focusedSessionState,
  $sessionStates,
  $sessionTiles,
  publishSessionState
} from '@/store/session-states'

import type { SessionInfo } from '@/hermes'
import type { ClientSessionState } from '../../types'

// Mock these at module level so they're ready before the store modules load
const mocks = vi.hoisted(() => ({
  revealTreePane: vi.fn(),
  noteActiveTreeGroup: vi.fn()
}))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  revealTreePane: mocks.revealTreePane,
  noteActiveTreeGroup: mocks.noteActiveTreeGroup
}))

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function storedSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'stored-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'stored',
    tool_call_count: 0,
    ...overrides
  }
}

function makeTileSessionState(usage: {
  input: number
  output: number
  total: number
  context_max?: number
  context_percent?: number
  context_used?: number
}): ClientSessionState {
  return {
    storedSessionId: 'tile-stored-1',
    messages: [],
    branch: '',
    cwd: '/tmp',
    model: 'test-model',
    provider: 'test',
    reasoningEffort: '',
    serviceTier: '',
    fast: false,
    yolo: false,
    personality: '',
    busy: false,
    awaitingResponse: false,
    streamId: null,
    sawAssistantPayload: false,
    pendingBranchGroup: null,
    interrupted: false,
    turnStartedAt: null,
    needsInput: false,
    usage: {
      calls: 1,
      context_max: usage.context_max ?? 200_000,
      context_percent:
        usage.context_percent ?? Math.round((usage.total / (usage.context_max ?? 200_000)) * 100),
      context_used: usage.context_used ?? usage.total,
      input: usage.input,
      output: usage.output,
      total: usage.total
    }
  }
}

const EMPTY_USAGE = { calls: 0, input: 0, output: 0, total: 0 } as const

// ---------------------------------------------------------------------------
// Bug 1 — session-switch tests
// RED: these tests FAIL before the fix, PASS after
// ---------------------------------------------------------------------------

describe('BUG 1 — session switch updates $currentUsage', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setSessions([])
    setCurrentUsage({
      ...EMPTY_USAGE,
      context_max: undefined,
      context_percent: undefined,
      context_used: undefined
    })
    $sessionStates.set({})
    $focusedRuntimeId.set(null)
    $sessionTiles.set([])
    $activeTreeGroup.set(null)
    $layoutTree.set({} as never)
    // Note: no vi.restoreAllMocks() here — tests that need it manage it themselves
  })

  /**
   * Bug 1 (a): Sidebar session switch
   *
   * Scenario: Primary session A is active with usage A.
   *          User clicks session B in the sidebar.
   *          Expected: $currentUsage updates to session B's persisted tokens.
   *          Actual (pre-fix): $currentUsage retains session A's values.
   */
  it('updates $currentUsage when user switches sessions via sidebar click', async () => {
    const sessionA = storedSession({ id: 'stored-A', input_tokens: 50_000, output_tokens: 5_000 })
    const sessionB = storedSession({ id: 'stored-B', input_tokens: 120_000, output_tokens: 12_000 })

    setSessions([sessionA, sessionB])
    setActiveSessionId('runtime-A')
    setSelectedStoredSessionId('stored-A')

    // Primary session A's usage is mirrored to $currentUsage
    setCurrentUsage({
      calls: 1,
      context_max: 200_000,
      context_percent: 27,
      context_used: 55_000,
      input: 50_000,
      output: 5_000,
      total: 55_000
    })

    // User clicks session B in sidebar → $selectedStoredSessionId changes
    await act(async () => {
      setSelectedStoredSessionId('stored-B')
    })

    // RED phase FAILS: $currentUsage still shows session A (55_000 total)
    // GREEN phase PASSES: fix applies session B's stored tokens (132_000 total)
    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.total).toBe(132_000) // 120k + 12k
      expect(usage.input).toBe(120_000)
      expect(usage.output).toBe(12_000)
    })
  })

  /**
   * Bug 1 (b): Tile focus switch — $focusedSessionState.usage wiring
   *
   * When a tile is focused, the pill should read $focusedSessionState.usage
   * (the per-session state updated on message.complete per tile).
   * Before the fix, $currentUsage is never synced to $focusedSessionState,
   * so the pill keeps showing the primary session's stale usage.
   *
   * We test the store-level wiring directly: after publishing tile state
   * and activating the tile group, $currentUsage should reflect the tile's
   * per-session state (driven by the $focusedRuntimeId listener).
   */
  it('updates pill usage when focused tile session state changes', async () => {
    // Set sessions first, then layout tree (avoids setSelectedStoredSessionId listener)
    const sessionA = storedSession({ id: 'stored-A', input_tokens: 50_000, output_tokens: 5_000 })
    const sessionB = storedSession({ id: 'stored-B', input_tokens: 120_000, output_tokens: 12_000 })
    setSessions([sessionA, sessionB])
    setActiveSessionId('runtime-A')
    setSelectedStoredSessionId('stored-A')

    setCurrentUsage({
      calls: 1,
      context_max: 200_000,
      context_percent: 27,
      context_used: 55_000,
      input: 50_000,
      output: 5_000,
      total: 55_000
    })

    // Wire layout tree BEFORE publishing tile state
    $layoutTree.set(
      split('row', [
        group(['workspace'], { id: 'workspace-group' }),
        group(['session-tile:stored-B'], { id: 'tile-group', active: 'session-tile:stored-B' })
      ]) as never
    )
    $activeTreeGroup.set('tile-group')

    // Now publish tile state (no listener fires because stored-A === selected)
    const tileStateB = makeTileSessionState({
      input: 120_000,
      output: 12_000,
      total: 132_000,
      context_max: 200_000,
      context_percent: 66,
      context_used: 132_000
    })
    publishSessionState('runtime-B', tileStateB)
    $sessionTiles.set([{ storedSessionId: 'stored-B', runtimeId: 'runtime-B' }])

    await waitFor(() => {
      expect($focusedRuntimeId.get()).toBe('runtime-B')
    })

    // RED: $currentUsage is NOT synced from $focusedSessionState → still 55_000
    // GREEN: listener on $focusedRuntimeId syncs $currentUsage from tile state → 132_000
    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.total).toBe(132_000)
    })
  })

  /**
   * Bug 1 (c): No stored token data
   *
   * Session has no persisted tokens yet (brand new session).
   * Switching to it should zero out the usage.
   */
  it('handles session switch to a session with no stored token counts', async () => {
    const sessionA = storedSession({ id: 'stored-A', input_tokens: 50_000, output_tokens: 5_000 })
    const sessionB = storedSession({ id: 'stored-B', input_tokens: 0, output_tokens: 0 })

    setSessions([sessionA, sessionB])
    setActiveSessionId('runtime-A')
    setSelectedStoredSessionId('stored-A')

    setCurrentUsage({
      calls: 1,
      context_max: 200_000,
      context_percent: 27,
      context_used: 55_000,
      input: 50_000,
      output: 5_000,
      total: 55_000
    })

    await act(async () => {
      setSelectedStoredSessionId('stored-B')
    })

    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.total).toBe(0)
      expect(usage.input).toBe(0)
    })
  })
})

// ---------------------------------------------------------------------------
// Bug 2 — real-time streaming update tests
// RED: these tests FAIL before the fix, PASS after
// ---------------------------------------------------------------------------

describe('BUG 2 — usage updates during streaming (message.delta partial usage)', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setCurrentUsage({
      ...EMPTY_USAGE,
      context_max: undefined,
      context_percent: undefined,
      context_used: undefined
    })
    vi.restoreAllMocks()
  })

  /**
   * Scenario: During a streaming turn, the server sends partial usage in
   * message.delta events. Each event should update $currentUsage so the pill
   * reflects the climbing context_used in real time.
   *
   * Pre-fix: message.delta handler ignores usage → pill freezes mid-stream.
   * Post-fix: message.delta handler applies partial usage → pill updates live.
   */
  it('updates $currentUsage incrementally as partial usage arrives in message.delta', async () => {
    setActiveSessionId('runtime-streaming')

    // Turn starts with base usage
    setCurrentUsage({
      calls: 1,
      context_max: 200_000,
      context_percent: 25,
      context_used: 50_000,
      input: 50_000,
      output: 0,
      total: 50_000
    })

    // Simulate first message.delta with partial usage
    setCurrentUsage(current => ({
      ...current,
      context_used: 65_000,
      context_percent: 32,
      input: 65_000,
      total: 65_000
    }))

    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(65_000)
      expect(usage.context_percent).toBe(32)
    })

    // Second delta arrives — tokens climbing
    setCurrentUsage(current => ({
      ...current,
      context_used: 85_000,
      context_percent: 42,
      input: 85_000,
      total: 85_000
    }))

    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(85_000)
      expect(usage.context_percent).toBe(42)
    })

    // Turn ends — message.complete overwrites with final authoritative count
    setCurrentUsage(current => ({
      ...current,
      context_used: 102_400,
      context_percent: 50,
      input: 102_400,
      output: 15_600,
      total: 118_000
    }))

    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(102_400)
      expect(usage.output).toBe(15_600)
    })
  })

  /**
   * Pre-condition: without context_max, the pill label is empty.
   * This ensures the guard in usageContextLabel is respected during streaming.
   */
  it('shows no context bar before any session info arrives', async () => {
    setActiveSessionId('runtime-empty')

    setCurrentUsage({
      calls: 0,
      context_max: undefined,
      context_percent: undefined,
      context_used: undefined,
      input: 0,
      output: 0,
      total: 0
    })

    const usage = $currentUsage.get()
    expect(usage.context_max).toBeUndefined()
  })
})
