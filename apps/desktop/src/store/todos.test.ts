import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TodoItem } from '@/lib/todos'
import type { ClientSessionState } from '@/app/types'

import {
  $todosBySession,
  clearActiveSessionTodos,
  clearSessionTodos,
  setSessionTodos,
  todosForHydration
} from './todos'
import { publishSessionState, $sessionStates } from './session-states'
import { setActiveSessionId } from './session'

const todo = (id: string, status: TodoItem['status']): TodoItem => ({ content: `task ${id}`, id, status })

describe('setSessionTodos finished-list auto-clear', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    clearSessionTodos('s1')
    vi.useRealTimers()
  })

  it('keeps an in-flight list indefinitely', () => {
    setSessionTodos('s1', [todo('a', 'completed'), todo('b', 'in_progress')])

    vi.advanceTimersByTime(60_000)

    expect($todosBySession.get().s1).toHaveLength(2)
  })

  it('drops the list shortly after every item completes', () => {
    setSessionTodos('s1', [todo('a', 'completed'), todo('b', 'cancelled')])

    expect($todosBySession.get().s1).toHaveLength(2)

    vi.advanceTimersByTime(5_000)

    expect($todosBySession.get().s1).toBeUndefined()
  })

  it('cancels the pending clear when a new active list arrives', () => {
    setSessionTodos('s1', [todo('a', 'completed')])
    vi.advanceTimersByTime(2_000)

    // The next turn starts a fresh plan before the linger expires.
    setSessionTodos('s1', [todo('a', 'completed'), todo('b', 'pending')])
    vi.advanceTimersByTime(60_000)

    expect($todosBySession.get().s1).toHaveLength(2)
  })
})

describe('clearActiveSessionTodos (turn-end cleanup)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    clearSessionTodos('s1')
    vi.useRealTimers()
  })

  it('drops a still-active list when the turn has ended', () => {
    setSessionTodos('s1', [todo('a', 'completed'), todo('b', 'in_progress')])

    clearActiveSessionTodos('s1')

    expect($todosBySession.get().s1).toBeUndefined()
  })

  it('leaves a finished list to its normal linger instead of clearing immediately', () => {
    setSessionTodos('s1', [todo('a', 'completed')])

    clearActiveSessionTodos('s1')

    expect($todosBySession.get().s1).toHaveLength(1)
    vi.advanceTimersByTime(5_000)
    expect($todosBySession.get().s1).toBeUndefined()
  })

  it('is a no-op when the session has no todos', () => {
    clearActiveSessionTodos('s1')

    expect($todosBySession.get().s1).toBeUndefined()
  })
})

describe('todosForHydration (stale-active guard on restore)', () => {
  it('does not restore an active list (stale after a completed turn)', () => {
    expect(todosForHydration([todo('a', 'completed'), todo('b', 'in_progress')])).toBeNull()
    expect(todosForHydration([todo('a', 'pending')])).toBeNull()
  })

  it('restores a finished list so its linger shows the final checkmarks', () => {
    const finished = [todo('a', 'completed'), todo('b', 'cancelled')]

    expect(todosForHydration(finished)).toEqual(finished)
  })

  it('returns null when there is nothing stored', () => {
    expect(todosForHydration(null)).toBeNull()
  })
})

// The status stack above the composer is panel chrome the user reads at a
// glance. Items are written by live "todo" tool events (use-message-stream) and
// read by the composer-status selector. Both ends speak RUNTIME session id —
// the id the gateway mints for a socket. On a visibility-change reconnect
// (use-gateway-boot.ts:406) the gateway tears down and re-mints, so the same
// stored session can land on a NEW runtime id while the user is still looking
// at the panel. The old runtime's items are orphaned under the previous id and
// the panel renders empty rows under the new id — descriptions vanish and the
// checkboxes float. The fix is to key the store by STORED session id (the
// lineage-stable identity) and translate runtime → stored at the boundary.
describe('todos are keyed by stored session id (visibility-reconnect resilience)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    $sessionStates.set({})
    setActiveSessionId(null)
  })

  afterEach(() => {
    clearSessionTodos('stored-1')
    clearSessionTodos('runtime-orphan')
    $sessionStates.set({})
    setActiveSessionId(null)
    vi.useRealTimers()
  })

  const seedSession = (runtimeId: string, storedId: string): void => {
    setActiveSessionId(runtimeId)
    const state: ClientSessionState = {
      awaitingResponse: false,
      branch: '',
      busy: true,
      cwd: '',
      fast: false,
      interrupted: false,
      messages: [],
      model: '',
      needsInput: false,
      pendingBranchGroup: null,
      personality: '',
      provider: '',
      reasoningEffort: '',
      sawAssistantPayload: false,
      serviceTier: '',
      streamId: null,
      storedSessionId: storedId,
      turnStartedAt: null,
      usage: null,
      yolo: false
    }
    publishSessionState(runtimeId, state)
  }

  it('writes live todo events under the stored id, surviving a runtime id replacement', () => {
    seedSession('runtime-1', 'stored-1')

    setSessionTodos('runtime-1', [todo('a', 'in_progress'), todo('b', 'pending')])

    // The store must hold the items under the stored id, not the runtime id,
    // so they survive when the gateway re-mints.
    expect($todosBySession.get()['stored-1']).toHaveLength(2)
    expect($todosBySession.get()['stored-1']?.[0]?.content).toBe('task a')
    expect($todosBySession.get()['runtime-1']).toBeUndefined()

    // Visibility-triggered reconnect: the gateway comes back under a fresh
    // runtime id for the same stored session. The active-session id rotates,
    // but the items must already be reachable by stored id.
    seedSession('runtime-2', 'stored-1')

    expect($todosBySession.get()['stored-1']).toHaveLength(2)
    expect($todosBySession.get()['stored-1']?.[1]?.content).toBe('task b')
  })

  it('clearSessionTodos under the runtime id still drops the stored-id entry', () => {
    seedSession('runtime-1', 'stored-1')
    setSessionTodos('runtime-1', [todo('a', 'completed')])
    expect($todosBySession.get()['stored-1']).toHaveLength(1)

    // The wiring may pass the runtime id on the clear path; the store must
    // honor the stored id so we don't leak stale items.
    clearSessionTodos('runtime-1')

    expect($todosBySession.get()['stored-1']).toBeUndefined()
  })

  it('falls back to the raw id when no $sessionStates entry exists (pre-bind)', () => {
    // Before the gateway publishes a session-state event, the runtime id is
    // the only key the caller has. The store must accept it and write to it
    // so the very first todo event isn't lost.
    setSessionTodos('runtime-orphan', [todo('a', 'in_progress')])

    expect($todosBySession.get()['runtime-orphan']).toHaveLength(1)
    expect($todosBySession.get()['runtime-orphan']?.[0]?.content).toBe('task a')
  })

  it('clearActiveSessionTodos resolves the stored id before dropping', () => {
    seedSession('runtime-1', 'stored-1')
    setSessionTodos('runtime-1', [todo('a', 'in_progress')])

    clearActiveSessionTodos('runtime-1')

    expect($todosBySession.get()['stored-1']).toBeUndefined()
    expect($todosBySession.get()['runtime-1']).toBeUndefined()
  })
})
