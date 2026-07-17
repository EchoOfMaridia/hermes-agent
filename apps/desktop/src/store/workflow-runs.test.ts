import { beforeEach, describe, expect, it } from 'vitest'

import type {
  WorkflowRunFinishedPayload,
  WorkflowRunStartedPayload,
} from '@/types/hermes'

import {
  $workflowRuns,
  $hasRunningWorkflow,
  $activeWorkflowRun,
  $subagentsByRun,
  ORPHAN_RUN_KEY,
  setActiveWorkflowRun,
  pushWorkflowRunStarted,
  finishWorkflowRun,
  pushWorkflowStepStarted,
  pushWorkflowStepFinished,
  linkSubagentToRun,
  unlinkSubagentFromRun,
  subagentsForRun,
} from './workflow-runs'

const ts = (n: number) => 1_700_000_000 + n

describe('workflow-runs store', () => {
  beforeEach(() => {
    $workflowRuns.set({})
    setActiveWorkflowRun(null)
  })

  it('starts empty', () => {
    expect($workflowRuns.get()).toEqual({})
    expect($hasRunningWorkflow.get()).toBe(false)
    expect($activeWorkflowRun.get()).toBe(null)
  })

  it('single run started — atoms populate', () => {
    const payload: WorkflowRunStartedPayload = {
      kind: 'workflow_run_started',
      run_id: 'r_001',
      workflow: 'demo_wf',
      max_concurrent: 4,
      max_total: 16,
      started_at: ts(0),
      steps: ['plan', 'execute', 'verify'],
    }
    pushWorkflowRunStarted(payload)

    const runs = $workflowRuns.get()
    expect(runs['r_001']).toBeDefined()
    expect(runs['r_001'].state).toBe('running')
    expect(runs['r_001'].workflowName).toBe('demo_wf')
    expect(runs['r_001'].steps.map(s => s.name)).toEqual(['plan', 'execute', 'verify'])
    expect(runs['r_001'].startedAt).toBe(ts(0))
    expect($hasRunningWorkflow.get()).toBe(true)
  })

  it('multi-run coexistence — both runs tracked independently', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_a',
      workflow: 'a',
      started_at: ts(0),
      steps: ['s1'],
    })
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_b',
      workflow: 'b',
      started_at: ts(5),
      steps: ['s1', 's2'],
    })
    expect(Object.keys($workflowRuns.get()).sort()).toEqual(['r_a', 'r_b'])
    finishWorkflowRun({
      kind: 'workflow_run_completed',
      run_id: 'r_a',
      ended_at: ts(10),
    } satisfies WorkflowRunFinishedPayload)

    const runs = $workflowRuns.get()
    expect(runs['r_a'].state).toBe('done')
    expect(runs['r_b'].state).toBe('running')
  })

  it('step_started upgrades pending → running', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_002',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    pushWorkflowStepStarted('r_002', 'plan', ts(1))

    const step = $workflowRuns.get()['r_002'].steps[0]
    expect(step).toBeDefined()
    expect(step?.state).toBe('running')
    expect(step?.startedAt).toBe(ts(1))
  })

  it('step_completed sets verified + duration', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_003',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    pushWorkflowStepStarted('r_003', 'plan', ts(1))
    pushWorkflowStepFinished('r_003', 'plan', {
      step: 'plan',
      duration: 1.23,
      ok: true,
      index: 0,
    }, ts(5))

    const step = $workflowRuns.get()['r_003'].steps[0]
    expect(step?.state).toBe('verified')
    expect(step?.endedAt).toBe(ts(5))
    expect(step?.durationSeconds).toBe(1.23)
  })

  it('verifier_returned sets verdict + attempts', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_004',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    pushWorkflowStepStarted('r_004', 'plan', ts(1))
    pushWorkflowStepFinished('r_004', 'plan', {
      step: 'plan',
      duration: 0.5,
      ok: false,
      index: 0,
      valid: false,
      reason: 'schema mismatch',
      attempt: 2,
    }, ts(5))

    const step = $workflowRuns.get()['r_004'].steps[0]
    expect(step?.state).toBe('failed')
    expect(step?.verifierVerdict).toBe('fail')
    expect(step?.attempts).toBe(2)
    expect(step?.verifierReason).toBe('schema mismatch')
  })

  it('run_completed → done', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_005',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    finishWorkflowRun({
      kind: 'workflow_run_completed',
      run_id: 'r_005',
      ended_at: ts(10),
    })
    const run = $workflowRuns.get()['r_005']
    expect(run.state).toBe('done')
    expect(run.endedAt).toBe(ts(10))
    expect($hasRunningWorkflow.get()).toBe(false)
  })

  it('run_failed → failed with error', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_006',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    finishWorkflowRun({
      kind: 'workflow_run_failed',
      run_id: 'r_006',
      error: 'agent crashed',
      error_type: 'RuntimeError',
      ended_at: ts(8),
    })
    const run = $workflowRuns.get()['r_006']
    expect(run.state).toBe('failed')
    expect(run.errorMessage).toBe('agent crashed')
    expect(run.errorType).toBe('RuntimeError')
  })

  it('run_halted → halted with reason', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_007',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    finishWorkflowRun({
      kind: 'workflow_run_halted',
      run_id: 'r_007',
      reason: 'max_total reached',
      ended_at: ts(12),
    })
    const run = $workflowRuns.get()['r_007']
    expect(run.state).toBe('halted')
    expect(run.haltReason).toBe('max_total reached')
  })

  it('run_cancelled → cancelled', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_008',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    finishWorkflowRun({
      kind: 'workflow_run_cancelled',
      run_id: 'r_008',
      reason: 'user_cancelled',
      ended_at: ts(3),
    })
    expect($workflowRuns.get()['r_008'].state).toBe('cancelled')
  })

  it('no-op on unknown run_id (defensive against stale events)', () => {
    // Should not throw, should not create phantom runs.
    expect(() =>
      pushWorkflowStepStarted('r_does_not_exist', 'plan', ts(0))
    ).not.toThrow()
    expect(() =>
      finishWorkflowRun({
        kind: 'workflow_run_completed',
        run_id: 'r_does_not_exist',
        ended_at: ts(0),
      })
    ).not.toThrow()
    expect($workflowRuns.get()).toEqual({})
  })

  it('setActiveWorkflowRun only changes on user action', () => {
    pushWorkflowRunStarted({
      kind: 'workflow_run_started',
      run_id: 'r_009',
      workflow: 'demo_wf',
      started_at: ts(0),
      steps: ['plan'],
    })
    // Even after a run starts, $activeWorkflowRun is NOT auto-set — the
    // user has to click the pill. (Background-work does not steal foreground.)
    expect($activeWorkflowRun.get()).toBe(null)

    setActiveWorkflowRun('r_009')
    expect($activeWorkflowRun.get()).toBe('r_009')

    setActiveWorkflowRun(null)
    expect($activeWorkflowRun.get()).toBe(null)
  })
})

// ============================================================================
// $subagentsByRun — subagent-to-workflow-run attribution
// ============================================================================

describe('workflow-runs $subagentsByRun', () => {
  beforeEach(() => {
    $workflowRuns.set({})
    $subagentsByRun.set({})
    setActiveWorkflowRun(null)
  })

  it('orphan subagent (no run) goes to _orphan bucket', () => {
    linkSubagentToRun(null, 'delegate-tool:abc:0')
    expect($subagentsByRun.get()[ORPHAN_RUN_KEY]).toEqual(['delegate-tool:abc:0'])
  })

  it('linking a subagent to a run moves it out of orphan', () => {
    linkSubagentToRun(null, 'delegate-tool:abc:0')
    linkSubagentToRun('r_a', 'delegate-tool:abc:0')

    const byRun = $subagentsByRun.get()
    expect(byRun['r_a']).toEqual(['delegate-tool:abc:0'])
    expect(byRun[ORPHAN_RUN_KEY]).toBeUndefined()
  })

  it('single run, multiple subagents — append-only', () => {
    linkSubagentToRun('r_a', 'delegate-tool:abc:0')
    linkSubagentToRun('r_a', 'delegate-tool:abc:1')
    linkSubagentToRun('r_a', 'delegate-tool:def:0')

    expect(subagentsForRun($subagentsByRun.get(), 'r_a')).toEqual([
      'delegate-tool:abc:0',
      'delegate-tool:abc:1',
      'delegate-tool:def:0',
    ])
  })

  it('unlink removes from bucket; empty bucket is cleaned up', () => {
    linkSubagentToRun('r_a', 'delegate-tool:abc:0')
    unlinkSubagentFromRun('r_a', 'delegate-tool:abc:0')

    const byRun = $subagentsByRun.get()
    expect(byRun['r_a']).toBeUndefined()
  })

  it('multi-run coexistence', () => {
    linkSubagentToRun('r_a', 'sub-a1')
    linkSubagentToRun('r_b', 'sub-b1')
    linkSubagentToRun('r_b', 'sub-b2')

    const byRun = $subagentsByRun.get()
    expect(byRun['r_a']).toEqual(['sub-a1'])
    expect(byRun['r_b']).toEqual(['sub-b1', 'sub-b2'])
  })
})