import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  dispatchWorkflowEvent,
  type WorkflowEventRouter,
} from '../app/session/hooks/use-workflow-events'

import {
  $subagentsByRun,
  $workflowRuns,
  finishWorkflowRun,
  linkSubagentToRun,
  pushWorkflowRunStarted,
  pushWorkflowStepFinished,
  pushWorkflowStepStarted,
  setActiveWorkflowRun,
} from './workflow-runs'

/**
 * Build a router that calls the reducers directly. The dispatcher's
 * findRunForStep logic is replicated inline so this test does not need
 * to call useWorkflowEvents (which is a React hook).
 */
function buildRouter(): WorkflowEventRouter {
  return {
    onRunStarted: payload => pushWorkflowRunStarted(payload),
    onRunFinished: payload => finishWorkflowRun(payload),
    onStepStarted: (runId, stepName, ts) =>
      pushWorkflowStepStarted(runId, stepName, ts),
    onStepFinished: (runId, stepName, payload, ts) =>
      pushWorkflowStepFinished(runId, stepName, payload, ts),
    onSubagentSpawned: info => linkSubagentToRun(info.runId, info.subagentId),
  }
}

const ts = (n: number) => 1_700_000_000 + n

describe('dispatchWorkflowEvent — end-to-end integration', () => {
  let router: WorkflowEventRouter
  let nowSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    $workflowRuns.set({})
    $subagentsByRun.set({})
    setActiveWorkflowRun(null)
    router = buildRouter()
    nowSpy = vi.spyOn(Date, 'now').mockImplementation(() => ts(100))
  })

  it('runs the canonical happy-path scenario', () => {
    // 1. workflow_run_started → run exists, state=running, steps=[plan,execute,verify]
    dispatchWorkflowEvent(router, {
      type: 'workflow_run_started',
      session_id: 'r_e2e',
      payload: {
        kind: 'workflow_run_started',
        run_id: 'r_e2e',
        workflow: 'e2e_wf',
        started_at: ts(0),
        steps: ['plan', 'execute', 'verify'],
      },
    })

    let run = $workflowRuns.get()['r_e2e']
    expect(run).toBeDefined()
    expect(run?.state).toBe('running')
    expect(run?.workflowName).toBe('e2e_wf')
    expect(run?.steps.map(s => s.name)).toEqual(['plan', 'execute', 'verify'])

    // 2. tool.start on 'plan' → plan step becomes running
    dispatchWorkflowEvent(router, {
      type: 'tool.start',
      session_id: 'r_e2e',
      payload: { tool_name: 'plan', tool_id: 't1' },
    })
    run = $workflowRuns.get()['r_e2e']
    expect(run?.steps.find(s => s.name === 'plan')?.state).toBe('running')

    // 3. tool.complete on 'plan' with ok=true → plan step becomes verified
    dispatchWorkflowEvent(router, {
      type: 'tool.complete',
      session_id: 'r_e2e',
      payload: {
        tool_name: 'plan',
        tool_id: 't1',
        ok: true,
        duration: 2.5,
      },
    })
    run = $workflowRuns.get()['r_e2e']
    expect(run?.steps.find(s => s.name === 'plan')?.state).toBe('verified')
    expect(run?.steps.find(s => s.name === 'plan')?.durationSeconds).toBe(2.5)

    // 4. workflow_run_completed → run state=done
    dispatchWorkflowEvent(router, {
      type: 'workflow_run_completed',
      session_id: 'r_e2e',
      payload: {
        kind: 'workflow_run_completed',
        run_id: 'r_e2e',
        ended_at: ts(50),
      },
    })
    expect($workflowRuns.get()['r_e2e'].state).toBe('done')
    expect($workflowRuns.get()['r_e2e'].endedAt).toBe(ts(50))

    nowSpy.mockRestore()
  })

  it('failure path: verifier fails → step state=failed + reason captured', () => {
    dispatchWorkflowEvent(router, {
      type: 'workflow_run_started',
      session_id: 'r_fail',
      payload: {
        kind: 'workflow_run_started',
        run_id: 'r_fail',
        workflow: 'e2e_wf',
        started_at: ts(0),
        steps: ['verify'],
      },
    })
    dispatchWorkflowEvent(router, {
      type: 'tool.start',
      session_id: 'r_fail',
      payload: { tool_name: 'verify', tool_id: 't1' },
    })
    dispatchWorkflowEvent(router, {
      type: 'tool.complete',
      session_id: 'r_fail',
      payload: {
        tool_name: 'verify',
        tool_id: 't1',
        ok: false,
        valid: false,
        reason: 'schema mismatch',
        attempt: 1,
      },
    })
    dispatchWorkflowEvent(router, {
      type: 'workflow_run_failed',
      session_id: 'r_fail',
      payload: {
        kind: 'workflow_run_failed',
        run_id: 'r_fail',
        error: 'verifier rejected',
        error_type: 'VerifierMismatch',
      },
    })

    const run = $workflowRuns.get()['r_fail']
    expect(run.state).toBe('failed')
    expect(run.errorMessage).toBe('verifier rejected')
    expect(run.errorType).toBe('VerifierMismatch')
    const step = run.steps.find(s => s.name === 'verify')
    expect(step?.state).toBe('failed')
    expect(step?.verifierVerdict).toBe('fail')
    expect(step?.verifierReason).toBe('schema mismatch')
    expect(step?.attempts).toBe(1)

    nowSpy.mockRestore()
  })

  it('orphan tool.complete (tool_name not in any step) is a no-op', () => {
    dispatchWorkflowEvent(router, {
      type: 'workflow_run_started',
      session_id: 'r_a',
      payload: {
        kind: 'workflow_run_started',
        run_id: 'r_a',
        workflow: 'e2e_wf',
        started_at: ts(0),
        steps: ['plan'],
      },
    })
    // tool_name='terminal' is NOT in steps → must be silently dropped
    dispatchWorkflowEvent(router, {
      type: 'tool.complete',
      session_id: 'r_a',
      payload: { tool_name: 'terminal', tool_id: 't1', ok: true },
    })
    const run = $workflowRuns.get()['r_a']
    expect(run.steps[0]?.state).toBe('pending')
    expect(run.steps[0]?.durationSeconds).toBe(null)

    nowSpy.mockRestore()
  })

  it('unknown event types are no-ops (forward-compatible)', () => {
    expect(() =>
      dispatchWorkflowEvent(router, {
        type: 'workflow_forked',
        session_id: 'r_x',
        payload: { kind: 'workflow_forked', run_id: 'r_x' },
      })
    ).not.toThrow()
    expect($workflowRuns.get()).toEqual({})

    nowSpy.mockRestore()
  })

  it('subagent attribution: delegate_task inside a workflow step is linked', () => {
    dispatchWorkflowEvent(router, {
      type: 'workflow_run_started',
      session_id: 'r_sub',
      payload: {
        kind: 'workflow_run_started',
        run_id: 'r_sub',
        workflow: 'e2e_wf',
        started_at: ts(0),
        steps: ['plan'],
      },
    })
    setActiveWorkflowRun('r_sub')

    dispatchWorkflowEvent(router, {
      type: 'subagent.start',
      session_id: 'r_sub',
      payload: { subagent_id: 'delegate-tool:t1:0', task_index: 0 },
    })

    expect($subagentsByRun.get()['r_sub']).toEqual(['delegate-tool:t1:0'])

    nowSpy.mockRestore()
  })

  it('multi-run coexistence: tool.complete on run A does not affect run B', () => {
    // Two parallel runs.
    for (const id of ['r_one', 'r_two']) {
      dispatchWorkflowEvent(router, {
        type: 'workflow_run_started',
        session_id: id,
        payload: {
          kind: 'workflow_run_started',
          run_id: id,
          workflow: id,
          started_at: ts(0),
          steps: ['only'],
        },
      })
    }

    // tool.start on r_one only
    dispatchWorkflowEvent(router, {
      type: 'tool.start',
      session_id: 'r_one',
      payload: { tool_name: 'only', tool_id: 't1' },
    })
    dispatchWorkflowEvent(router, {
      type: 'tool.complete',
      session_id: 'r_one',
      payload: { tool_name: 'only', tool_id: 't1', ok: true, duration: 1.0 },
    })

    const one = $workflowRuns.get()['r_one']
    const two = $workflowRuns.get()['r_two']
    expect(one.steps[0]?.state).toBe('verified')
    expect(two.steps[0]?.state).toBe('pending')

    nowSpy.mockRestore()
  })
})