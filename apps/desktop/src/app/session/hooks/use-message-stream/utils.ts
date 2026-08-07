// Re-exports from use-message-stream.ts for the useMessageStream hook.
// These are defined here so they can be shared between use-message-stream.ts
// and use-message-stream/index.ts (which re-exports the hook).

export const STREAM_DELTA_FLUSH_MS = 33

export function completionErrorText(finalText: string): string | null {
  const COMPLETION_ERROR_PATTERNS = [
    /^API call failed after \d+ retries:/i,
    /^HTTP\s+\d{3}\b/i,
    /^(Provider|Gateway)\s+error:/i
  ]
  const text = finalText.trim()
  return text && COMPLETION_ERROR_PATTERNS.some(re => re.test(text)) ? text : null
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function parseMaybeRecord(value: any): Record<string, unknown> {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  return {}
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function firstString(...values: any[]): string | undefined {
  for (const v of values) {
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return undefined
}

export function delegateTaskPayloads(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: any,
  phase: 'running' | 'complete',
  sourceEventType?: string
): Record<string, unknown>[] {
  if (payload?.name !== 'delegate_task') {
    return []
  }

  const args = parseMaybeRecord(payload.args ?? payload.input)
  const result = parseMaybeRecord(payload.result)
  const rawTasks = Array.isArray(args.tasks) ? args.tasks : []
  const tasks = rawTasks.length ? rawTasks.map(parseMaybeRecord) : [args]

  const toolId = payload.tool_id || payload.tool_call_id || payload.id || 'delegate_task'

  return tasks.map((task, index) => {
    const goal = firstString(task.goal, args.goal, payload.context) || 'Delegated task'
    const summary = firstString(result.summary, payload.summary, payload.message)
    const status = phase === 'complete' ? (payload.error ? 'failed' : 'completed') : 'running'
    const eventType =
      phase === 'complete'
        ? 'subagent.complete'
        : sourceEventType === 'tool.start'
          ? 'subagent.start'
          : 'subagent.progress'

    return {
      name: 'delegate_task',
      id: `${toolId}-${index}`,
      goal,
      status,
      eventType,
      progressText: firstString(payload.preview, payload.message, payload.context),
      summary,
      error: phase === 'complete' ? payload.error : undefined
    }
  })
}

// session.info state patch — stub returning empty object.
// gateway-event.ts only reads .model and .provider from the result.
export function sessionInfoStatePatch(_payload: unknown): Record<string, unknown> {
  return {}
}

export function hasSessionInfoStatePatch(patch: Record<string, unknown>): boolean {
  return Object.keys(patch).length > 0
}

export const SUBAGENT_EVENT_TYPES = new Set([
  'subagent.spawn_requested',
  'subagent.start',
  'subagent.thinking',
  'subagent.tool',
  'subagent.progress',
  'subagent.complete'
])

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function toTodoPayload(payload: any): unknown {
  if (!payload) return undefined
  const isTodo = payload.name === 'todo' || (!payload.name && Object.hasOwn(payload, 'todos'))
  return isTodo ? { ...payload, name: 'todo', tool_id: payload.tool_id || 'todo-live' } : undefined
}
