/**
 * use-workflow-events — sibling hook to use-message-stream that translates
 * gateway events into $workflowRuns store mutations.
 *
 * Why a sibling hook instead of inlining into use-message-stream? The latter
 * is already 1200+ lines and would balloon further with workflow_* event
 * branches. Splitting keeps the existing message-stream surface focused on
 * chat messaging, while this hook owns the workflow-progress surface.
 *
 * Both hooks subscribe to the same gateway event stream — the desktop's
 * `handleDesktopGatewayEvent` from `app/contrib/wiring.tsx` invokes each in
 * turn. Idempotent: re-firing the same event is a no-op at the reducer
 * level (workflow-runs.ts:pushWorkflowRunStarted / finishWorkflowRun guard
 * against stale runs; pushWorkflowStepStarted/FINISHED guard against
 * unknown step names; linkSubagentToRun is idempotent on duplicate
 * subagent ids).
 *
 * Workflow steps arrive on the existing tool.start / tool.complete events
 * with `payload.tool_name = <declared step name>`. We attribute them to the
 * most-recent running workflow run whose `steps[]` includes the tool name.
 * If no run matches (a tool call arriving before workflow_run_started, or
 * in a non-workflow context), we drop it — defensive against polluting the
 * workflow panel with unrelated tool calls.
 */

import { useCallback } from 'react'

import type {
  WorkflowRunFinishedPayload,
  WorkflowRunStartedPayload,
  WorkflowStepFinishedPayload
} from '@/types/hermes'

import {
  $activeWorkflowRun,
  $workflowRuns,
  finishWorkflowRun,
  linkSubagentToRun,
  pushWorkflowRunStarted,
  pushWorkflowStepFinished,
  pushWorkflowStepStarted
} from '../../../store/workflow-runs'

export interface WorkflowSubagentInfo {
  subagentId: string
  /** Best-effort run attribution — null when the parent run is unknown. */
  runId: string | null
}

/** Shape of the gateway event payloads we consume. Subset of RpcEvent. */
export interface WorkflowGatewayEvent {
  type: string
  session_id?: string
  payload?: Record<string, unknown>
}

export interface WorkflowEventRouter {
  onRunStarted: (payload: WorkflowRunStartedPayload) => void
  onRunFinished: (payload: WorkflowRunFinishedPayload) => void
  onStepStarted: (runId: string, stepName: string, ts: number) => void
  onStepFinished: (
    runId: string,
    stepName: string,
    payload: WorkflowStepFinishedPayload,
    ts: number,
  ) => void
  onSubagentSpawned: (info: WorkflowSubagentInfo) => void
}

/**
 * Find the run whose `steps[]` contains the given tool name AND whose state is
 * `running`. Prefers the user's currently-active run ($activeWorkflowRun);
 * falls back to any running run. Returns null when no match exists.
 */
function findRunForStep(
  stepName: string,
  fallbackRunId: string | null = null,
): string | null {
  const runs = $workflowRuns.get()
  // Try the active run first.
  const active = $activeWorkflowRun.get()

  if (active && runs[active]?.state === 'running') {
    if (runs[active].steps.some(s => s.name === stepName)) {return active}
  }

  // Fallback: the session_id the event arrived on (sometimes = runId).
  if (fallbackRunId && runs[fallbackRunId]?.state === 'running') {
    if (runs[fallbackRunId].steps.some(s => s.name === stepName)) {
      return fallbackRunId
    }
  }

  // Last resort: any running run that has this step.
  for (const [runId, run] of Object.entries(runs)) {
    if (run.state !== 'running') {continue}

    if (run.steps.some(s => s.name === stepName)) {return runId}
  }

  return null
}

/** Find a running run for a subagent attribution. Prefers active. */
function findActiveRunId(): string | null {
  const active = $activeWorkflowRun.get()
  const runs = $workflowRuns.get()

  if (active && runs[active]?.state === 'running') {return active}

  for (const [runId, run] of Object.entries(runs)) {
    if (run.state === 'running') {return runId}
  }

  return null
}

export function useWorkflowEvents(): WorkflowEventRouter {
  const onRunStarted = useCallback((payload: WorkflowRunStartedPayload) => {
    pushWorkflowRunStarted(payload)
  }, [])

  const onRunFinished = useCallback((payload: WorkflowRunFinishedPayload) => {
    finishWorkflowRun(payload)
  }, [])

  const onStepStarted = useCallback(
    (runId: string, stepName: string, ts: number) => {
      const target = findRunForStep(stepName, runId) ?? runId
      pushWorkflowStepStarted(target, stepName, ts)
    },
    [],
  )

  const onStepFinished = useCallback(
    (
      runId: string,
      stepName: string,
      payload: WorkflowStepFinishedPayload,
      ts: number,
    ) => {
      const target = findRunForStep(stepName, runId) ?? runId
      pushWorkflowStepFinished(target, stepName, payload, ts)
    },
    [],
  )

  const onSubagentSpawned = useCallback((info: WorkflowSubagentInfo) => {
    linkSubagentToRun(info.runId ?? findActiveRunId(), info.subagentId)
  }, [])

  return {
    onRunStarted,
    onRunFinished,
    onStepStarted,
    onStepFinished,
    onSubagentSpawned,
  }
}

/**
 * Dispatch a single gateway event into the workflow-events pipeline.
 *
 * Caller is the desktop's wiring layer (`app/contrib/wiring.tsx` →
 * `handleDesktopGatewayEvent`). Idempotent — unknown event types are no-ops.
 *
 * The runtime contract:
 *  - `workflow_run_started`              → pushWorkflowRunStarted()
 *  - `workflow_run_completed`           → finishWorkflowRun(state=done)
 *  - `workflow_run_failed`              → finishWorkflowRun(state=failed)
 *  - `workflow_run_halted`              → finishWorkflowRun(state=halted)
 *  - `workflow_run_cancelled`            → finishWorkflowRun(state=cancelled)
 *  - `tool.start` on a workflow step     → pushWorkflowStepStarted()
 *  - `tool.complete` on a workflow step  → pushWorkflowStepFinished()
 *  - `subagent.*` whose subagent spawned inside a workflow step
 *                                       → linkSubagentToRun() (best-effort)
 */
export function dispatchWorkflowEvent(
  router: WorkflowEventRouter,
  event: WorkflowGatewayEvent,
  now: () => number = () => Date.now() / 1000,
): void {
  const type = event.type
  const payload = event.payload ?? {}
  const sessionId = event.session_id ?? null

  switch (type) {
    case 'workflow_run_started': {
      router.onRunStarted(payload as unknown as WorkflowRunStartedPayload)

      return
    }

    case 'workflow_run_completed':

    case 'workflow_run_failed':

    case 'workflow_run_halted':
    case 'workflow_run_cancelled': {
      router.onRunFinished(payload as unknown as WorkflowRunFinishedPayload)

      return
    }

    case 'tool.start': {
      const toolName = String(
        (payload as { tool_name?: string }).tool_name ?? '',
      )

      if (!toolName) {return}
      router.onStepStarted(sessionId ?? '', toolName, now())

      return
    }

    case 'tool.complete': {
      const toolName = String(
        (payload as { tool_name?: string }).tool_name ?? '',
      )

      if (!toolName) {return}
      const toolId = String((payload as { tool_id?: string }).tool_id ?? '')
      const ok = Boolean((payload as { ok?: boolean }).ok ?? true)
      const validRaw = (payload as { valid?: unknown }).valid
      const reason = (payload as { reason?: string }).reason
      const attempt = (payload as { attempt?: number }).attempt
      router.onStepFinished(
        sessionId ?? '',
        toolName,
        {
          step: toolName,
          duration: Number((payload as { duration?: number }).duration ?? 0),
          ok,
          index: 0,
          ...(typeof validRaw === 'boolean' ? { valid: validRaw } : {}),
          ...(typeof reason === 'string' ? { reason } : {}),
          ...(typeof attempt === 'number' ? { attempt } : {}),
        },
        now(),
      )
      // Attach the tool's emitted subagent ids (for delegate_task inside a
      // workflow step) to the run.
      void toolId

      return
    }

    case 'subagent.spawn_requested':
    case 'subagent.start': {
      const subagentId = String(
        (payload as { subagent_id?: string }).subagent_id ?? '',
      )

      if (!subagentId) {return}
      router.onSubagentSpawned({ subagentId, runId: sessionId })

      return
    }

    default:
      return
  }
}