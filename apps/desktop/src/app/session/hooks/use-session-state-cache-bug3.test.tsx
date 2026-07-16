/* eslint-disable */
// Bug #3: "Click session A → shows session B's content; can only be viewed
// with 'open in new window'."
//
// This test reproduces the EXACT user scenario through `useSessionStateCache`:
//
//   1. User is viewing session B (B_runtimeId is the active session, B's
//      transcript is in $messages, viewSessionIdRef === B_runtimeId).
//   2. User clicks session A in the sidebar.
//   3. `resumeSession('A')` runs the cached fast-path in
//      use-session-actions.ts:618-639:
//        setActiveSessionId(A_runtimeId)
//        activeSessionIdRef.current = A_runtimeId
//        syncSessionStateToView(A_runtimeId, A_state)
//   4. syncSessionStateToView must flush A's transcript synchronously,
//      so $messages contains A's messages — not B's.
//
// The previous bug #2 fix added an `isSessionSwitch` check based on
// `pendingViewStateRef.current.sessionId !== viewSessionIdRef.current`.
// That alone is NOT sufficient: when the foreground session is mid-turn
// (`state.busy === true`), `!state.busy` is false, `state.needsInput` is
// false, so the only critical signal is `isSessionSwitch`. If that check
// has any path where it returns false on a real session switch, the flush
// falls into the RAF branch — and the chat view shows the OLD session
// until the RAF fires. This test pins the contract: a click on a cached
// session MUST repaint the view synchronously, period.
import { act, cleanup, render } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { $messages, setActiveSessionId, setMessages, setSelectedStoredSessionId } from '@/store/session'

import { useSessionStateCache } from './use-session-state-cache'

type Cache = ReturnType<typeof useSessionStateCache>

function Harness({
  activeSessionId,
  onReady,
  selectedStoredSessionId
}: {
  activeSessionId: string | null
  onReady: (cache: Cache) => void
  selectedStoredSessionId: string | null
}) {
  const busyRef: MutableRefObject<boolean> = { current: false }
  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse: () => undefined,
    setBusy: () => undefined,
    setMessages: (next: ChatMessage[]) => setMessages(next)
  })

  onReady(cache)

  return null
}

function makeMessages(ids: string[]): ChatMessage[] {
  return ids.map((id, index) => ({
    id,
    parts: [{ type: 'text', text: `message ${id}` }],
    role: index % 2 === 0 ? 'user' : 'assistant'
  }))
}

describe('useSessionStateCache — cached fast-path repaints on click (bug #3)', () => {
  // Make RAF a no-op so we can prove the synchronous flush path runs
  // WITHOUT relying on RAF.
  beforeEach(() => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 0)
    setMessages([])
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    setMessages([])
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
  })

  it('clicking a cached session paints the new session, not the previously-rendered one', () => {
    let cache!: Cache

    // User is currently viewing session B. B's transcript is in $messages.
    const bMessages = makeMessages(['b-1', 'b-2'])
    setMessages(bMessages)

    const { rerender } = render(
      <Harness activeSessionId="b-runtime" onReady={c => (cache = c)} selectedStoredSessionId="b-stored" />
    )

    // Cache B's state into the hook's sessionStateByRuntimeIdRef so the
    // cached fast-path has something to read.
    act(() => {
      cache.updateSessionState(
        'b-runtime',
        state => ({
          ...state,
          busy: true,
          messages: bMessages
        }),
        'b-stored'
      )
    })

    // User clicks session A. Re-render with A as the active session.
    // Pre-seed A's cached state.
    const aMessages = makeMessages(['a-1', 'a-2'])
    act(() => {
      cache.updateSessionState(
        'a-runtime',
        state => ({
          ...state,
          busy: true,
          messages: aMessages
        }),
        'a-stored'
      )
    })

    rerender(<Harness activeSessionId="a-runtime" onReady={c => (cache = c)} selectedStoredSessionId="a-stored" />)

    // Mirror what `resumeSession` does in the cached fast-path:
    // setActiveSessionId(A_runtimeId), activeSessionIdRef.current = A_runtimeId,
    // then syncSessionStateToView(A_runtimeId, A_state).
    act(() => {
      setActiveSessionId('a-runtime')
      cache.activeSessionIdRef.current = 'a-runtime'
      const aState = cache.sessionStateByRuntimeIdRef.current.get('a-runtime')
      expect(aState).toBeTruthy()
      cache.syncSessionStateToView('a-runtime', aState!)
    })

    const messagesAfterClick = $messages.get() as unknown as Array<{ id: string }>
    // The fix: $messages now holds A's transcript.
    // The bug: $messages still holds B's transcript.
    expect(messagesAfterClick.map(m => m.id)).toEqual(['a-1', 'a-2'])
  })
})
