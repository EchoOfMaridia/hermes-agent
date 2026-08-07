import { useStore } from '@nanostores/react'
/**
 * WorkflowWireProbe — on-demand + periodic diagnostic strip for the
 * Workflows panel. The strip renders inside the panel header (below
 * the "X runs since boot" subtitle) and shows the live state of
 * every link in the wire:
 *
 *   Plugin → Runtime → Dispatcher → desktop_event_bridge → tui_gateway
 *     → WebSocket → renderer → $workflowRuns
 *
 * The backend exposes GET /api/workflows/diag which returns a JSON
 * snapshot of the runtime + dispatcher + bridge + active-runs state.
 * The strip polls it on a 2s timer AND any time the user clicks
 * "Run" (so the operator can see whether the run is actually reaching
 * the singleton runtime the bridge wraps).
 *
 * The probe never throws — every fetch failure is captured in
 * `errorMessage` and rendered in red so a broken wire is visible at
 * a glance without leaving the panel.
 */
import { useEffect, useState } from 'react'

import { cn } from '@/lib/utils'
import { $workflowRuns } from '@/store/workflow-runs'

interface DiagSnapshot {
  plugin_active_runtime_present?: boolean
  singleton_runtime_present?: boolean
  singleton_is_plugin_runtime?: boolean | null
  runtime_dispatcher_set?: boolean
  runtime_dispatcher_type?: string | null
  bridge_present?: boolean
  bridge_stats?: {
    received?: number
    translated?: number
    filtered_non_workflow?: number
    emit_ok?: number
    emit_failed?: number
  }
  tui_gateway_emit_importable?: boolean
  active_run_ids?: string[]
  [k: string]: unknown
}

interface WorkflowWireProbeProps {
  /** Optional live marker — bumps every time the user clicks Run so
   *  the operator can see "the panel saw the click" without leaving. */
  clickCounter?: number
}

export function WorkflowWireProbe({ clickCounter = 0 }: WorkflowWireProbeProps) {
  const runs = useStore($workflowRuns)
  const [diag, setDiag] = useState<DiagSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false

    async function fetchDiag() {
      try {
        const bridge = (globalThis as unknown as { hermesDesktop?: { api?: <T>(req: { path: string }) => Promise<T> } })
          .hermesDesktop

        if (!bridge?.api) {
          setError('desktop bridge unavailable')

          return
        }

        const data = await bridge.api<DiagSnapshot>({ path: '/api/workflows/diag' })

        if (cancelled) {return}
        setDiag(data)
        setError(null)
        setLastFetchAt(Date.now())
      } catch (exc) {
        if (cancelled) {return}
        setError(exc instanceof Error ? exc.message : String(exc))
      }
    }

    void fetchDiag()
    const timer = setInterval(() => { void fetchDiag() }, 2000)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [clickCounter])

  const ok = (b: boolean | undefined): 'yes' | 'no' | '?' =>
    b === true ? 'yes' : b === false ? 'no' : '?'

  const latestRun = Object.values(runs).sort(
    (a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0)
  )[0]

  return (
    <div
      aria-live="polite"
      className="mx-3 mb-2 rounded border border-(--stroke-faint) bg-(--ui-bg-quaternary) p-2 font-mono text-[10px] leading-tight"
      data-testid="workflow-wire-probe"
    >
      <div className="mb-1 flex items-center justify-between">
        <span className="font-semibold uppercase tracking-wide text-(--ui-text-secondary)">
          wire-probe
        </span>
        <span className="text-(--ui-text-tertiary)">
          {lastFetchAt ? new Date(lastFetchAt).toLocaleTimeString() : '—'}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        <span>plugin runtime:</span>
        <span className={cn(diag?.plugin_active_runtime_present === false && 'text-(--ui-danger,#f87171)')}>
          {ok(diag?.plugin_active_runtime_present)}
        </span>
        <span>singleton runtime:</span>
        <span className={cn(diag?.singleton_runtime_present === false && 'text-(--ui-danger,#f87171)')}>
          {ok(diag?.singleton_runtime_present)}
        </span>
        <span>same instance:</span>
        <span className={cn(diag?.singleton_is_plugin_runtime === false && 'text-(--ui-danger,#f87171)')}>
          {diag?.singleton_is_plugin_runtime === true ? 'yes' : diag?.singleton_is_plugin_runtime === false ? 'NO' : '?'}
        </span>
        <span>dispatcher:</span>
        <span
          className={cn(
            diag?.runtime_dispatcher_set === false && 'text-(--ui-danger,#f87171)',
            diag?.runtime_dispatcher_type === 'FallbackDispatchSink' && 'text-(--ui-warning,#fbbf24)'
          )}
          title={diag?.runtime_dispatcher_type ?? ''}
        >
          {diag?.runtime_dispatcher_type
            ? `${diag.runtime_dispatcher_type.slice(0, 32)}`
            : ok(diag?.runtime_dispatcher_set)}
        </span>
        <span>bridge:</span>
        <span className={cn(diag?.bridge_present === false && 'text-(--ui-danger,#f87171)')}>
          {ok(diag?.bridge_present)}
          {diag?.bridge_stats ? ` rx=${diag.bridge_stats.received ?? 0} tx=${diag.bridge_stats.translated ?? 0} ok=${diag.bridge_stats.emit_ok ?? 0} fail=${diag.bridge_stats.emit_failed ?? 0}` : ''}
        </span>
        <span>tui_gateway emit:</span>
        <span>{ok(diag?.tui_gateway_emit_importable)}</span>
        <span>$workflowRuns:</span>
        <span>
          {Object.keys(runs).length}
          {latestRun ? ` (last: ${latestRun.runId.slice(0, 16)}… ${latestRun.state})` : ''}
        </span>
        <span>active runs in rt:</span>
        <span>{diag?.active_run_ids?.length ?? 0}</span>
        <span>bridge clicks seen:</span>
        <span>{clickCounter}</span>
      </div>
      {error ? (
        <div className="mt-1 break-words text-[10px] text-(--ui-danger,#f87171)">
          diag fetch failed: {error}
        </div>
      ) : null}
    </div>
  )
}
