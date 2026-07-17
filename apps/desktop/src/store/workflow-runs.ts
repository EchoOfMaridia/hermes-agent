import { atom, computed } from 'nanostores'

import type {
  WorkflowRunFinishedPayload,
  WorkflowRunStartedPayload,
  WorkflowStepFinishedPayload,
} from '@/types/hermes'

// ============================================================================
// WORKFLOW RUNS STORE
// ----------------------------------------------------------------------------
// Subscribes to the gateway event stream and tracks every active and recently
// finished workflow run. The reducer surface is intentionally narrow — only
// the events the workflow plugin's visibility.EventTranslator emits:
//   - workflow_run_started  → pushWorkflowRunStarted()
//   - workflow_run_completed / failed / halted / cancelled → finishWorkflowRun()
//   - tool.start whose tool_name matches a declared step → pushWorkflowStepStarted()
//   - tool.complete whose tool_name matches a declared step → pushWorkflowStepFinished()
//
// Per-run keys mirror the desktop's `$sessions` / `$sessionStates` pattern:
// atom keyed by runId, $activeWorkflowRun is a user-controlled pointer (not
// auto-set on run-started, per AGENTS.md "background work does not steal
// the foreground").
// ============================================================================

export type WorkflowRunState = 'running' | 'done' | 'failed' | 'halted' | 'cancelled'
export type StepState = 'pending' | 'running' | 'verified' | 'failed'
export type VerifierVerdict = 'pass' | 'fail' | null

export interface ActiveAgent {
  index: number
  promptPreview: string
  startedAt: number | null
  endedAt: number | null
  ok: boolean
}

export interface WorkflowStep {
  name: string
  /** `pending` until step_started; `running` while active; `verified` after
   *  the verifier passes; `failed` after a verifier rejection. */
  state: StepState
  startedAt: number | null
  endedAt: number | null
  durationSeconds: number | null
  agentCalls: number
  tokensIn: number
  tokensOut: number
  attempts: number
  verifierVerdict: VerifierVerdict
  verifierReason: string | null
  activeAgents: ActiveAgent[]
}

export interface WorkflowRun {
  runId: string
  workflowName: string | null
  state: WorkflowRunState
  startedAt: number | null
  endedAt: number | null
  maxConcurrent: number | null
  maxTotal: number | null
  steps: WorkflowStep[]
  /** Set when the run finished with `failed`. */
  errorMessage: string | null
  errorType: string | null
  /** Set when the run finished with `halted` or `cancelled`. */
  haltReason: string | null
}

// ---------------------------------------------------------------------------
// Atoms
// ---------------------------------------------------------------------------

export const $workflowRuns = atom<Record<string, WorkflowRun>>({})

export const $hasRunningWorkflow = computed([$workflowRuns], runs =>
  Object.values(runs).some(r => r.state === 'running'),
)

/**
 * The run the user is currently inspecting in the Workflows panel. NOT
 * auto-set when a run starts — the user clicks the pill or a row. This is
 * per AGENTS.md "background work does not steal the foreground."
 */
export const $activeWorkflowRun = atom<string | null>(null)

export function setActiveWorkflowRun(runId: string | null): void {
  $activeWorkflowRun.set(runId)
}

// ---------------------------------------------------------------------------
// Reducers
// ---------------------------------------------------------------------------

function freshStep(name: string): WorkflowStep {
  return {
    name,
    state: 'pending',
    startedAt: null,
    endedAt: null,
    durationSeconds: null,
    agentCalls: 0,
    tokensIn: 0,
    tokensOut: 0,
    attempts: 0,
    verifierVerdict: null,
    verifierReason: null,
    activeAgents: [],
  }
}

export function pushWorkflowRunStarted(payload: WorkflowRunStartedPayload): void {
  if (!payload?.run_id) {return}
  const stepNames = payload.steps ?? []
  $workflowRuns.set({
    ...$workflowRuns.get(),
    [payload.run_id]: {
      runId: payload.run_id,
      workflowName: payload.workflow ?? null,
      state: 'running',
      startedAt: payload.started_at ?? null,
      endedAt: null,
      maxConcurrent: payload.max_concurrent ?? null,
      maxTotal: payload.max_total ?? null,
      steps: stepNames.map(freshStep),
      errorMessage: null,
      errorType: null,
      haltReason: null,
    },
  })
}

export function finishWorkflowRun(payload: WorkflowRunFinishedPayload): void {
  const current = $workflowRuns.get()[payload.run_id]

  if (!current) {return}

  const base: Pick<WorkflowRun, 'errorMessage' | 'errorType' | 'haltReason'> = {
    errorMessage: current.errorMessage,
    errorType: current.errorType,
    haltReason: current.haltReason,
  }

  let next: WorkflowRun

  if (payload.kind === 'workflow_run_completed') {
    next = { ...current, ...base, state: 'done', endedAt: payload.ended_at ?? null }
  } else if (payload.kind === 'workflow_run_failed') {
    next = {
      ...current,
      state: 'failed',
      endedAt: payload.ended_at ?? null,
      errorMessage: payload.error ?? null,
      errorType: payload.error_type ?? null,
    }
  } else if (payload.kind === 'workflow_run_halted') {
    next = {
      ...current,
      state: 'halted',
      endedAt: payload.ended_at ?? null,
      haltReason: payload.reason ?? null,
    }
  } else {
    next = {
      ...current,
      state: 'cancelled',
      endedAt: payload.ended_at ?? null,
      haltReason: payload.reason ?? null,
    }
  }

  $workflowRuns.set({ ...$workflowRuns.get(), [payload.run_id]: next })
}

export function pushWorkflowStepStarted(
  runId: string,
  stepName: string,
  ts: number,
): void {
  const current = $workflowRuns.get()[runId]

  if (!current) {return}
  let found = false

  const steps: WorkflowStep[] = current.steps.map(step => {
    if (step.name !== stepName) {return step}
    found = true

    return { ...step, state: 'running' as StepState, startedAt: ts }
  })

  if (!found) {return}
  $workflowRuns.set({ ...$workflowRuns.get(), [runId]: { ...current, steps } })
}

export function pushWorkflowStepFinished(
  runId: string,
  stepName: string,
  payload: WorkflowStepFinishedPayload,
  ts: number,
): void {
  const current = $workflowRuns.get()[runId]

  if (!current) {return}
  let found = false

  const steps: WorkflowStep[] = current.steps.map(step => {
    if (step.name !== stepName) {return step}
    found = true
    const isVerifierEvent = typeof payload.valid === 'boolean'

    const stepPassed = isVerifierEvent
      ? Boolean(payload.valid)
      : Boolean(payload.ok)

    return {
      ...step,
      state: (stepPassed ? 'verified' : 'failed') as StepState,
      endedAt: ts,
      durationSeconds: payload.duration ?? step.durationSeconds,
      attempts: isVerifierEvent
        ? Math.max(step.attempts, payload.attempt ?? step.attempts + 1)
        : step.attempts,
      verifierVerdict: isVerifierEvent
        ? (payload.valid ? 'pass' : 'fail')
        : step.verifierVerdict,
      verifierReason: isVerifierEvent
        ? payload.reason ?? step.verifierReason
        : step.verifierReason,
    }
  })

  if (!found) {return}
  $workflowRuns.set({ ...$workflowRuns.get(), [runId]: { ...current, steps } })
}

// ============================================================================
// Subagent → workflow run attribution
// ----------------------------------------------------------------------------
// When a subagent spawns during a workflow step (e.g. delegate_task inside a
// step body), the wiring layer calls linkSubagentToRun() to attribute it to
// the active workflow run. The reducer maintains $subagentsByRun; orphan
// subagents (no known run) go to the `_orphan` bucket and are not surfaced
// in the panel.
//
// The wiring layer is responsible for matching subagent events to a run.
// Today the match is by `(runId, stepName, callIndex)` — the same triple
// the workflow plugin's EventTranslator uses for active_agents[].
// ============================================================================

export const ORPHAN_RUN_KEY = '_orphan'

export const $subagentsByRun = atom<Record<string, string[]>>({})

export function linkSubagentToRun(
  runId: string | null,
  subagentId: string,
): void {
  if (!subagentId) {return}
  const targetKey = runId ?? ORPHAN_RUN_KEY
  const current = $subagentsByRun.get()
  const existing = current[targetKey] ?? []

  if (existing.includes(subagentId)) {return}
  // Defensive: remove the orphan entry for this subagent if it was placed
  // there previously and is now being linked to a real run.
  const next: Record<string, string[]> = { ...current }

  if (runId) {
    const orphan = (next[ORPHAN_RUN_KEY] ?? []).filter(id => id !== subagentId)

    if (orphan.length) {
      next[ORPHAN_RUN_KEY] = orphan
    } else {
      delete next[ORPHAN_RUN_KEY]
    }
  }

  next[targetKey] = [...existing, subagentId]
  $subagentsByRun.set(next)
}

export function unlinkSubagentFromRun(
  runId: string | null,
  subagentId: string,
): void {
  if (!subagentId) {return}
  const targetKey = runId ?? ORPHAN_RUN_KEY
  const current = $subagentsByRun.get()
  const existing = current[targetKey] ?? []
  const filtered = existing.filter(id => id !== subagentId)
  const next: Record<string, string[]> = { ...current }

  if (filtered.length) {
    next[targetKey] = filtered
  } else {
    delete next[targetKey]
  }

  $subagentsByRun.set(next)
}

/** Convenience computed — which subagents belong to this run? */
export const subagentsForRun = (
  byRun: Record<string, string[]>,
  runId: string,
): string[] => byRun[runId] ?? []