/**
 * Bug 2 — real-time streaming usage test
 *
 * Verifies that partial usage arriving in message.delta events updates
 * $currentUsage so the context-usage pill reflects climbing context_used
 * in real time during streaming.
 *
 * Red: message.delta events do NOT update $currentUsage (pre-fix)
 * Green: message.delta events update $currentUsage (post-fix)
 */

import { QueryClient } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { useEffect, useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { $currentUsage, setCurrentUsage } from '@/store/session'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { RpcEvent } from '@/types/hermes'

import { useMessageStream } from './index'

const SID = 'session-streaming'

let handleEvent: ((event: RpcEvent) => void) | null = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let sessionStateByRuntimeIdRef: any = null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let queryClientRef: any = null

function Harness() {
  const activeSessionIdRef = useRef<string | null>(SID)
  sessionStateByRuntimeIdRef = useRef(new Map<string, ClientSessionState>())
  queryClientRef = useRef(new QueryClient())

  const stream = useMessageStream({
    activeSessionIdRef,
    hydrateFromStoredSession: vi.fn(async () => undefined),
    queryClient: queryClientRef.current,
    refreshHermesConfig: vi.fn(async () => undefined),
    refreshSessions: vi.fn(async () => undefined),
    sessionStateByRuntimeIdRef,
    updateSessionState: (sessionId, updater) => {
      const current = sessionStateByRuntimeIdRef.current.get(sessionId) ?? createClientSessionState()
      const next = updater(current)
      sessionStateByRuntimeIdRef.current.set(sessionId, next)
      return next
    }
  })

  useEffect(() => {
    handleEvent = stream.handleGatewayEvent
  }, [stream.handleGatewayEvent])

  return null
}

async function mountStream() {
  render(<Harness />)
  await waitFor(() => expect(handleEvent).not.toBeNull())
}

// ---------------------------------------------------------------------------
// Bug 2 — message.delta partial usage
// ---------------------------------------------------------------------------

describe('BUG 2 — usage updates during streaming (message.delta partial usage)', () => {
  beforeEach(async () => {
    // Prime with initial usage at turn start
    setCurrentUsage({
      calls: 1,
      context_max: 200_000,
      context_percent: 25,
      context_used: 50_000,
      input: 50_000,
      output: 0,
      total: 50_000
    })
    await mountStream()
  })

  afterEach(() => {
    cleanup()
    handleEvent = null
    setCurrentUsage({
      calls: 0,
      input: 0,
      output: 0,
      total: 0,
      context_max: undefined,
      context_percent: undefined,
      context_used: undefined
    })
    vi.restoreAllMocks()
  })

  /**
   * RED: This test FAILS before the fix, PASSES after.
   *
   * Before the fix: message.delta handler ignores usage → $currentUsage stays
   * at 50_000 (turn-start baseline) for the entire streaming duration.
   *
   * After the fix: message.delta handler applies partial usage → $currentUsage
   * climbs from 50_000 → 65_000 → 85_000 in real time.
   */
  it('updates $currentUsage incrementally as partial usage arrives in message.delta', async () => {
    // Turn starts
    act(() => {
      handleEvent!({ payload: {}, session_id: SID, type: 'message.start' })
    })

    // First delta chunk arrives with partial usage
    act(() => {
      handleEvent!({
        payload: {
          text: 'The',
          usage: {
            context_used: 65_000,
            context_max: 200_000,
            context_percent: 32,
            input: 65_000,
            output: 0
          }
        },
        session_id: SID,
        type: 'message.delta'
      })
    })

    // RED: stays at 50_000 (pre-fix — message.delta ignores usage)
    // GREEN: climbs to 65_000 (post-fix — message.delta applies partial usage)
    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(65_000)
      expect(usage.context_percent).toBe(32)
    })

    // Second delta — tokens climbing further
    act(() => {
      handleEvent!({
        payload: {
          text: ' quick brown fox',
          usage: {
            context_used: 85_000,
            context_max: 200_000,
            context_percent: 42,
            input: 85_000,
            output: 0
          }
        },
        session_id: SID,
        type: 'message.delta'
      })
    })

    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(85_000)
      expect(usage.context_percent).toBe(42)
    })

    // Turn ends — message.complete with final authoritative usage
    act(() => {
      handleEvent!({
        payload: {
          text: 'The quick brown fox jumps over the lazy dog.',
          usage: {
            context_used: 102_400,
            context_max: 200_000,
            context_percent: 51,
            input: 102_400,
            output: 15_600,
            total: 118_000
          }
        },
        session_id: SID,
        type: 'message.complete'
      })
    })

    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(102_400)
      expect(usage.output).toBe(15_600)
    })
  })

  /**
   * Delta events with no usage payload should not crash and should not
   * overwrite valid existing usage data.
   */
  it('handles message.delta without usage payload gracefully', async () => {
    act(() => {
      handleEvent!({ payload: {}, session_id: SID, type: 'message.start' })
    })

    // Delta with no usage data
    act(() => {
      handleEvent!({
        payload: { text: 'Hello' },
        session_id: SID,
        type: 'message.delta'
      })
    })

    // Usage should remain at baseline (not clobbered)
    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.total).toBe(50_000)
      expect(usage.context_used).toBe(50_000)
    })
  })

  /**
   * A background session's delta should NOT update $currentUsage
   * (it should only update the per-session state for that session).
   */
  it('does not update $currentUsage for background session deltas', async () => {
    const BACKGROUND_SID = 'session-bg'

    // Background turn starts
    act(() => {
      handleEvent!({
        payload: {},
        session_id: BACKGROUND_SID,
        type: 'message.start'
      })
    })

    // Background delta with different usage
    act(() => {
      handleEvent!({
        payload: {
          text: 'Background',
          usage: {
            context_used: 150_000,
            context_max: 200_000,
            context_percent: 75,
            input: 150_000,
            output: 0
          }
        },
        session_id: BACKGROUND_SID,
        type: 'message.delta'
      })
    })

    // $currentUsage should NOT reflect the background session's usage
    await waitFor(() => {
      const usage = $currentUsage.get()
      expect(usage.context_used).toBe(50_000) // active session baseline, unchanged
    })
  })
})
