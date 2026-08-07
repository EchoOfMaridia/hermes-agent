/* eslint-disable */
import { act, cleanup, render } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { $messages, setMessages } from '@/store/session'

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

// The user reports the symptom "URL changes but the chat stays put".
// That maps to: $messages holds the OLD session's messages after a
// session switch, instead of being repainted with the NEW session's
// transcript. Specifically the cached fast-path (use-session-actions.ts
// line 639) calls syncSessionStateToView with the new session's cached
// state — if that update is RAF-deferred, $messages stays stale until
// the RAF actually fires.
describe('useSessionStateCache — session switch flush (bug #2)', () => {
  // Make RAF a no-op so we can prove the synchronous flush path runs
  // WITHOUT relying on RAF. Without the fix, this test fails because the
  // session-switch update is RAF-deferred and the no-op RAF means the
  // view never updates.
  beforeEach(() => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 0)
    $messages.set([])
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $messages.set([])
  })

  it('flushes session-switch updates synchronously, not on RAF (the chat-stays-put bug)', () => {
    let cache!: Cache

    // Render with session A active and A's transcript already in $messages.
    $messages.set([
      { id: 'a-1', parts: [{ type: 'text', text: 'A message 1' }], role: 'user' },
      { id: 'a-2', parts: [{ type: 'text', text: 'A reply' }], role: 'assistant' }
    ])

    const { rerender } = render(
      <Harness activeSessionId="a-runtime" onReady={c => (cache = c)} selectedStoredSessionId="a-stored" />
    )

    // Cache A's state into the hook's sessionStateByRuntimeIdRef map so
    // the cached fast-path has something to read.
    act(() => {
      cache.updateSessionState(
        'a-runtime',
        state => ({
          ...state,
          busy: true,
          messages: [
            { id: 'a-1', parts: [{ type: 'text', text: 'A message 1' }], role: 'user' },
            { id: 'a-2', parts: [{ type: 'text', text: 'A reply' }], role: 'assistant' }
          ]
        }),
        'a-stored'
      )
    })

    // User clicks session B. The Cached fast-path then: rerender with B's
    // runtime as activeSessionId, then sync B's cached state into the view.
    rerender(
      <Harness activeSessionId="b-runtime" onReady={c => (cache = c)} selectedStoredSessionId="b-stored" />
    )

    // Simulate the cached fast-path: store B's cached state under b-runtime.
    act(() => {
      cache.updateSessionState(
        'b-runtime',
        state => ({
          ...state,
          busy: true,
          messages: [
            { id: 'b-1', parts: [{ type: 'text', text: 'B message 1' }], role: 'user' },
            { id: 'b-2', parts: [{ type: 'text', text: 'B reply' }], role: 'assistant' }
          ]
        }),
        'b-stored'
      )
    })

    const bState = cache.sessionStateByRuntimeIdRef.current.get('b-runtime')
    expect(bState).toBeTruthy()

    // Now the cached fast-path paints B's state into the view. This is
    // what use-session-actions.ts:639 does for a cached session click.
    // Without the fix, this update is RAF-deferred. With RAF mocked to
    // a no-op, the synchronous flush must run instead — otherwise $messages
    // would still hold A's transcript.
    act(() => {
      cache.syncSessionStateToView('b-runtime', bState!)
    })

    const messagesAfterSwitch = $messages.get() as unknown as Array<{ id: string }>

    // The fix: $messages now holds B's transcript (or at least b-2's id).
    // The bug: $messages still holds A's transcript (a-1, a-2).
    expect(messagesAfterSwitch.map(m => m.id)).toContain('b-2')
    expect(messagesAfterSwitch.map(m => m.id)).not.toContain('a-2')
  })
})
