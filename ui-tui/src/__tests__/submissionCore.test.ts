import { beforeEach, describe, expect, it, vi } from 'vitest'

import { isSessionBusyError, markSubmitting, submitPrompt, type SubmitPromptDeps } from '../app/submissionCore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type { GatewayClient } from '../gatewayClient.js'

// A gateway double whose `input.detect_drop` resolution we control, so we can
// observe UI state DURING the async gap — the exact window the queue-mode race
// lived in.
function makeDeferredGateway() {
  let resolveDrop: (v: unknown) => void = () => {}

  const dropPromise = new Promise(res => {
    resolveDrop = res
  })

  const calls: string[] = []

  const gw = {
    request: vi.fn((method: string) => {
      calls.push(method)

      if (method === 'input.detect_drop') {
        return dropPromise
      }

      // prompt.submit et al: resolve immediately with a success shape.
      return Promise.resolve({ status: 'streaming' })
    })
  } as unknown as GatewayClient

  return { calls, gw, resolveDrop: (v: unknown = { matched: false }) => resolveDrop(v) }
}

function makeDeps(gw: GatewayClient, over: Partial<SubmitPromptDeps> = {}): SubmitPromptDeps {
  return {
    appendMessage: vi.fn(),
    enqueue: vi.fn(),
    expand: (t: string) => t,
    gw,
    setLastUserMsg: vi.fn(),
    sys: vi.fn(),
    ...over
  }
}

describe('submissionCore.submitPrompt — synchronous busy (queue-race fix)', () => {
  beforeEach(() => {
    resetUiState()
    patchUiState({ sid: 'sess-1' })
  })

  it('flips busy=true SYNCHRONOUSLY, before input.detect_drop resolves', () => {
    const { gw, resolveDrop } = makeDeferredGateway()

    expect(getUiState().busy).toBe(false)

    submitPrompt('hello', makeDeps(gw))

    // The critical invariant: busy is already true even though the
    // detect_drop RPC has NOT resolved yet. This is what makes a second,
    // rapid submit take the local-enqueue branch instead of racing a second
    // prompt.submit onto the backend.
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('running…')

    resolveDrop()
  })

  it('regression: two back-to-back sends — the SECOND sees busy=true in the gap', async () => {
    const { gw, resolveDrop } = makeDeferredGateway()

    // Emulate dispatchSubmission's routing decision: it sends only when
    // busy===false, otherwise it would enqueue. We assert the state the
    // router reads, which is the real regression.
    submitPrompt('first message', makeDeps(gw))

    // Before the fix, busy was still false here (set only inside detect_drop's
    // .then), so a second Enter would wrongly route into send() again.
    const busyWhenSecondArrives = getUiState().busy
    expect(busyWhenSecondArrives).toBe(true)

    resolveDrop()
    await Promise.resolve()
  })

  it('does not submit when there is no session, and does not mark busy', () => {
    resetUiState() // sid: null
    const { gw, calls } = makeDeferredGateway()
    const sys = vi.fn()

    submitPrompt('hello', makeDeps(gw, { sys }))

    expect(getUiState().busy).toBe(false)
    expect(sys).toHaveBeenCalledWith('session not ready yet')
    expect(calls).not.toContain('input.detect_drop')
  })

  it('after detect_drop resolves (no file), it issues prompt.submit', async () => {
    const { calls, gw, resolveDrop } = makeDeferredGateway()

    submitPrompt('hi there', makeDeps(gw))
    expect(calls).toEqual(['input.detect_drop'])

    resolveDrop({ matched: false })
    await Promise.resolve()
    await Promise.resolve()

    expect(calls).toContain('prompt.submit')
  })
})

describe('submissionCore.markSubmitting', () => {
  beforeEach(() => resetUiState())

  it('sets busy + running status', () => {
    markSubmitting()
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('running…')
  })
})

describe('submissionCore.isSessionBusyError', () => {
  it('matches the legacy busy rejections but not arbitrary errors', () => {
    expect(isSessionBusyError(new Error('session busy'))).toBe(true)
    expect(isSessionBusyError(new Error('waiting for model response'))).toBe(true)
    expect(isSessionBusyError(new Error('some other failure'))).toBe(false)
    expect(isSessionBusyError('not an error')).toBe(false)
  })
})

describe('submissionCore.submitPrompt — $ prefix routes through slash.exec', () => {
  // These tests pin the contract the user hit:
  // `$shitty-bob $tpipe-trace-parser do XYZ` must reach the gateway's
  // command.dispatch (via slash.exec → command.dispatch fallthrough on
  // 4018 rejection), which loads both skill bodies, NOT prompt.submit
  // with literal slash text as a plain message.

  beforeEach(() => {
    resetUiState()
    patchUiState({ sid: 'sess-1' })
  })

  it('rewrites $skill to /skill, calls slash.exec, falls through to command.dispatch on 4018, loads skills', async () => {
    const calls: string[] = []
    let dispatchResponse: unknown = {
      type: 'send',
      message: '[IMPORTANT] stacked skill scaffold body with both bodies',
      notice: '⚡ Loading 2 stacked skills: shitty-bob, tpipe-trace-parser',
    }
    const gw = {
      request: vi.fn((method: string, params: { command?: string; name?: string; arg?: string; text?: string }) => {
        if (method === 'slash.exec') {
          calls.push(`slash.exec:${params.command}`)
          // slash.exec REJECTS skill commands with JSON-RPC error 4018.
          return Promise.reject(new Error('slash.exec: 4018 skill command: use command.dispatch for /' + (params.command ?? '')))
        }
        if (method === 'command.dispatch') {
          calls.push(`command.dispatch:${params.name} | arg=${params.arg}`)
          return Promise.resolve(dispatchResponse)
        }
        if (method === 'prompt.submit') {
          calls.push(`prompt.submit:${params.text?.slice(0, 60)}`)
          return Promise.resolve({ status: 'streaming' })
        }
        calls.push(`${method}:${JSON.stringify(params).slice(0, 60)}`)
        return Promise.resolve({ status: 'streaming' })
      }),
    } as unknown as GatewayClient
    const sys = vi.fn()

    submitPrompt(
      '$shitty-bob $tpipe-trace-parser show me the trace',
      makeDeps(gw, { sys })
    )

    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    // Order of gateway calls must be:
    //   1. slash.exec with the REWRITTEN command
    //   2. command.dispatch (because slash.exec rejected with 4018)
    //   3. prompt.submit with the dispatch's `message` payload (NOT the
    //      user's original slash text — the gateway would treat that as
    //      plain prose and never load the skills)
    expect(calls[0]).toBe('slash.exec:shitty-bob /tpipe-trace-parser show me the trace')
    expect(calls[1]).toBe('command.dispatch:shitty-bob | arg=/tpipe-trace-parser show me the trace')
    expect(calls[2]).toMatch(/^prompt.submit:\[IMPORTANT\] stacked skill scaffold/)
    // The user-facing notice from the dispatch must surface.
    expect(sys).toHaveBeenCalledWith(
      '⚡ Loading 2 stacked skills: shitty-bob, tpipe-trace-parser'
    )
  })

  it('a single $skill at position 0 routes slash.exec → command.dispatch', async () => {
    const calls: string[] = []
    const gw = {
      request: vi.fn((method: string, params: { name?: string; arg?: string; text?: string }) => {
        if (method === 'slash.exec') {
          calls.push(`slash.exec`)
          return Promise.reject(new Error('slash.exec: 4018 skill command'))
        }
        if (method === 'command.dispatch') {
          calls.push(`command.dispatch:${params.name}`)
          return Promise.resolve({
            type: 'skill',
            message: '...scaffold...',
            name: 'shitty-bob',
          })
        }
        if (method === 'prompt.submit') {
          calls.push(`prompt.submit`)
          return Promise.resolve({ status: 'streaming' })
        }
        return Promise.resolve({})
      }),
    } as unknown as GatewayClient
    const sys = vi.fn()

    submitPrompt('$shitty-bob do the thing', makeDeps(gw, { sys }))

    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(calls[0]).toBe('slash.exec')
    expect(calls[1]).toBe('command.dispatch:shitty-bob')
    expect(calls[2]).toBe('prompt.submit')
    expect(sys).toHaveBeenCalledWith('⚡ loading skill: shitty-bob')
  })

  it('plain text (no $, no /) still uses prompt.submit — no regression', async () => {
    const calls: string[] = []
    const gw = {
      request: vi.fn((method: string, params: { text?: string }) => {
        calls.push(`${method}:${params.text ?? ''}`)
        return Promise.resolve({ status: 'streaming' })
      }),
    } as unknown as GatewayClient

    submitPrompt('just a plain message', makeDeps(gw))

    await Promise.resolve()
    await Promise.resolve()

    expect(calls[0]).toBe('input.detect_drop:just a plain message')
    expect(calls).toContain('prompt.submit:just a plain message')
    expect(calls.find(c => c.startsWith('slash.exec'))).toBeUndefined()
  })

  it('mid-prose $skill (not at position 0) does NOT enter the slash dispatch — prompt.submit fires', async () => {
    // Mid-prose $tokens are inline references, NOT command-style triggers.
    // They pass through as ordinary prompt text.
    const calls: string[] = []
    const gw = {
      request: vi.fn((method: string, params: { text?: string }) => {
        calls.push(`${method}:${params.text ?? ''}`)
        return Promise.resolve({ status: 'streaming' })
      }),
    } as unknown as GatewayClient

    submitPrompt('please run $shitty-bob inline', makeDeps(gw))

    await Promise.resolve()
    await Promise.resolve()

    expect(calls).toContain('input.detect_drop:please run $shitty-bob inline')
    expect(calls).toContain('prompt.submit:please run $shitty-bob inline')
    expect(calls.find(c => c.startsWith('slash.exec'))).toBeUndefined()
  })
})
