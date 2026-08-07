import { atom } from 'nanostores'

import type { TodoItem } from '@/lib/todos'

import { $sessionStates } from './session-states'

/**
 * Live todo list per STORED session, rendered by the composer status stack
 * (the inline transcript panel is gone). Fed from two places:
 *
 * - live `todo` tool events (use-message-stream) — caller passes the runtime
 *   id, store resolves to stored id via `$sessionStates` so a runtime id
 *   rotation (visibility-change reconnect) doesn't orphan the items.
 * - stored-session hydration (desktop-controller) — but only when the list is
 *   still in flight, so reopening an old chat doesn't pin its finished plan
 *   above the composer forever.
 */
export const $todosBySession = atom<Record<string, TodoItem[]>>({})

export const todoListActive = (todos: readonly TodoItem[]) =>
  todos.some(t => t.status === 'pending' || t.status === 'in_progress')

// Decide which todo list to restore when rehydrating a session from stored
// history. Rehydration runs *after* a turn completes, so an active list (last
// item still pending/in_progress) is stale — the turn ended without a final
// `todo` update — and must NOT be re-pinned (that would undo the turn-end
// clear and, because it's read back from history, resurrect on restart). Only
// a finished list is restored, so its short linger shows the last checkmark.
// Returns null when there's nothing to restore (caller should clear).
export function todosForHydration(todos: readonly TodoItem[] | null): TodoItem[] | null {
  return todos && !todoListActive(todos) ? [...todos] : null
}

// Resolve the writer's id (typically a runtime id) to the durable stored id
// the store actually keys by. Falls back to the raw id when no session-state
// entry exists yet — the very first todo event for a brand-new session
// arrives before the gateway has published a `session.init`, and dropping
// it would lose the task list. The stored id, once it appears, takes over
// on the next event; the brief window where items sit under the runtime id
// is harmless because no read uses the runtime id as a key.
function durableIdFor(id: string): string {
  const stored = $sessionStates.get()[id]?.storedSessionId
  return stored ?? id
}

// Once a list finishes (every item completed/cancelled), the final state
// lingers just long enough to see the last checkmark land, then the group
// drops out of the stack on its own.
const FINISHED_LINGER_MS = 4_000
const clearTimers = new Map<string, ReturnType<typeof setTimeout>>()

function cancelScheduledClear(sid: string) {
  const timer = clearTimers.get(sid)

  if (timer !== undefined) {
    clearTimeout(timer)
    clearTimers.delete(sid)
  }
}

export function setSessionTodos(sid: string, todos: TodoItem[]) {
  if (!sid) {
    return
  }

  const key = durableIdFor(sid)
  cancelScheduledClear(key)
  $todosBySession.set({ ...$todosBySession.get(), [key]: todos })

  if (!todoListActive(todos)) {
    clearTimers.set(
      key,
      setTimeout(() => {
        clearTimers.delete(key)
        clearSessionTodos(key)
      }, FINISHED_LINGER_MS)
    )
  }
}

export function clearSessionTodos(sid: string) {
  const key = durableIdFor(sid)
  cancelScheduledClear(key)

  const map = $todosBySession.get()

  if (!(key in map)) {
    return
  }

  const { [key]: _drop, ...rest } = map
  $todosBySession.set(rest)
}

// Drop a still-active todo list (any pending/in_progress item) — used at turn
// end, when an unfinished list means the turn stopped without a final `todo`
// update, so the "Tasks N/M" panel would otherwise stay pinned above the
// composer forever. A finished list is left untouched so its short linger
// still shows the last checkmark landing.
export function clearActiveSessionTodos(sid: string) {
  const key = durableIdFor(sid)
  const todos = $todosBySession.get()[key]

  if (!todos || !todoListActive(todos)) {
    return
  }

  clearSessionTodos(key)
}
