import type { StatusbarMenuItem } from '@/app/shell/statusbar-controls'

const LOG_TAIL = 5

interface RpcEventLike {
  payload?: unknown
  type?: string
}

function asRecord(payload: unknown): Record<string, unknown> {
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
}

/**
 * Whether an unscoped event (no `session_id`) must be dropped rather than
 * attributed to the focused chat.
 *
 * Only `subagent.*` qualifies: it describes background/async work that must
 * never attach to whichever chat happens to be focused. Every other scoped
 * event — message/reasoning/thinking/tool/status/prompt — is, when unscoped,
 * the active turn's own output. The gateway always stamps a *background*
 * session's events with that session's id, so a missing id can only mean "the
 * focused turn". #42178 dropped those too, which silently swallowed the live
 * answer; it then reappeared only after a transcript refetch (manual refresh).
 */
export function gatewayEventRequiresSessionId(eventType: string | undefined): boolean {
  return eventType?.startsWith('subagent.') ?? false
}

export function gatewayEventCompletedFileDiff(event: RpcEventLike): boolean {
  if (event.type !== 'tool.complete') {
    return false
  }

  const diff = asRecord(event.payload).inline_diff

  return typeof diff === 'string' && diff.trim().length > 0
}

/**
 * Whether a `status.update` event's `kind` field signals that a compaction
 * (manual /compress OR mid-turn auto-compaction) is in flight for the session.
 *
 * The gateway emits two shapes for the same intent:
 *   - `"compacting"` — auto-compaction mid-turn. The gateway's `_status_update`
 *     re-tags the generic `lifecycle` kind to `"compacting"` when it detects
 *     the compaction marker in the body.
 *   - `"compressing"` — manual `/compress` invoked from the desktop composer.
 *     `tui_gateway/server.py:5918` calls `_status_update(sid, "compressing", …)`
 *     directly, without going through the re-tag branch, so the kind string
 *     never normalizes to `"compacting"`.
 *
 * Without accepting both, manual `/compress` on a large context emits a
 * `status.update` event that the desktop drops on the floor — the chrome
 * spinner never shows and the user can't tell the command fired.
 */
export function isCompactingStatusKind(kind: unknown): boolean {
  return kind === 'compacting' || kind === 'compressing'
}

export function buildGatewayLogItems(lines: readonly string[]): readonly StatusbarMenuItem[] {
  if (lines.length === 0) {
    return [
      {
        className: 'text-muted-foreground',
        disabled: true,
        id: 'gateway-log-empty',
        label: 'No recent gateway log lines'
      }
    ]
  }

  return lines.slice(-LOG_TAIL).map((line, index) => ({
    className: 'font-mono text-[0.68rem] text-muted-foreground',
    disabled: true,
    id: `gateway-log:${index}`,
    label: line.trim().slice(0, 120) || '(blank log line)'
  }))
}
