/* eslint-disable */
// Regression: a visibility-change reconnect (use-gateway-boot.ts:406 →
// reconnectNow) can rotate the runtime id the composer speaks. The Tasks
// panel above the composer must keep rendering its content across that
// rotation, because the store's durable key is the STORED session id, not
// the runtime id. Before the fix, items were keyed by runtime id and the
// panel rendered empty rows after a reconnect — descriptions vanished and
// the checkboxes floated. The fix translates runtime → stored at the read
// boundary so the same chat reads the same items before and after.

import { render, screen, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { setSessionTodos, $todosBySession } from '@/store/todos'
import { publishSessionState, $sessionStates } from '@/store/session-states'
import { setActiveSessionId, $activeSessionId } from '@/store/session'
import type { ClientSessionState } from '@/app/types'

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

function Harness({ sessionId }: { sessionId: null | string }) {
  return (
    <MemoryRouter>
      <ComposerStatusStack queue={null} sessionId={sessionId} />
    </MemoryRouter>
  )
}

const seed = (runtimeId: string, storedId: string): void => {
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

describe('Tasks panel — visibility-reconnect resilience (regression)', () => {
  beforeEach(() => {
    $sessionStates.set({})
    $todosBySession.set({})
    $activeSessionId.set(null)
  })

  afterEach(() => {
    $sessionStates.set({})
    $todosBySession.set({})
    $activeSessionId.set(null)
  })

  it('renders the same todo content before and after the runtime id rotates', () => {
    // Initial session open.
    seed('runtime-1', 'stored-1')
    setSessionTodos('runtime-1', [
      { content: 'Migrate endpoints', id: 'mig', status: 'in_progress' },
      { content: 'Write release notes', id: 'notes', status: 'pending' }
    ])

    const first = render(<Harness sessionId="runtime-1" />)
    expect(screen.getByText('Migrate endpoints')).toBeTruthy()
    expect(screen.getByText('Write release notes')).toBeTruthy()
    first.unmount()

    // Visibility reconnect: the gateway comes back under a new runtime id
    // for the same stored session. The active-session id rotates; the panel
    // must keep its content because the store is keyed by stored id.
    seed('runtime-2', 'stored-1')
    setActiveSessionId('runtime-2')

    const second = render(<Harness sessionId="runtime-2" />)
    expect(screen.getByText('Migrate endpoints')).toBeTruthy()
    expect(screen.getByText('Write release notes')).toBeTruthy()
  })

  it('falls back to the runtime id when the panel mounts before session.init lands', () => {
    // Pre-bind window: the writer has the runtime id but $sessionStates
    // hasn't been populated yet. The first todo event lands under the
    // runtime id; the panel must still read it via the same fallback.
    setSessionTodos('runtime-orphan', [
      { content: 'First task', id: 'first', status: 'in_progress' }
    ])

    render(<Harness sessionId="runtime-orphan" />)

    expect(screen.getByText('First task')).toBeTruthy()
  })

  it('switches the read key from runtime to stored when session.init lands later', () => {
    // Orphans go under the runtime id; after session.init the next event
    // moves the items to the stored id, and the panel must keep showing
    // them across that migration.
    setSessionTodos('runtime-1', [{ content: 'Carry-over', id: 'carry', status: 'pending' }])

    const first = render(<Harness sessionId="runtime-1" />)
    expect(screen.getByText('Carry-over')).toBeTruthy()
    first.unmount()

    // Now session.init lands and a fresh todo event for the SAME chat
    // arrives through the new runtime id. The store should now key under
    // the stored id.
    act(() => {
      seed('runtime-2', 'stored-1')
    })
    setSessionTodos('runtime-2', [
      { content: 'Carry-over', id: 'carry', status: 'completed' },
      { content: 'New follow-up', id: 'follow', status: 'pending' }
    ])

    const second = render(<Harness sessionId="runtime-2" />)
    expect(screen.getByText('Carry-over')).toBeTruthy()
    expect(screen.getByText('New follow-up')).toBeTruthy()
  })
})
