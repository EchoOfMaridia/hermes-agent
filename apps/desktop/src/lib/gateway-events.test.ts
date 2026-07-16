import { describe, expect, it } from 'vitest'

import { gatewayEventRequiresSessionId, isCompactingStatusKind } from './gateway-events'

describe('gateway event routing', () => {
  it('drops only unscoped subagent events (genuinely background work)', () => {
    expect(gatewayEventRequiresSessionId('subagent.progress')).toBe(true)
    expect(gatewayEventRequiresSessionId('subagent.start')).toBe(true)
  })

  it('attributes unscoped foreground turn events to the active chat', () => {
    // These must NOT be dropped when unscoped — they are the focused turn's own
    // output, and dropping them loses the live response until a refetch (#42178).
    expect(gatewayEventRequiresSessionId('message.delta')).toBe(false)
    expect(gatewayEventRequiresSessionId('message.complete')).toBe(false)
    expect(gatewayEventRequiresSessionId('reasoning.delta')).toBe(false)
    expect(gatewayEventRequiresSessionId('tool.start')).toBe(false)
    expect(gatewayEventRequiresSessionId('approval.request')).toBe(false)
  })

  it('allows global events to remain unscoped', () => {
    expect(gatewayEventRequiresSessionId('gateway.ready')).toBe(false)
    expect(gatewayEventRequiresSessionId('preview.restart.progress')).toBe(false)
    expect(gatewayEventRequiresSessionId('session.info')).toBe(false)
    expect(gatewayEventRequiresSessionId(undefined)).toBe(false)
  })
})

describe('isCompactingStatusKind', () => {
  it('accepts the auto-compaction kind (mid-turn re-tagged by the gateway)', () => {
    // The gateway's _status_update re-tags generic `lifecycle` events to
    // `compacting` when the body contains the compaction marker. This is
    // the long-standing path the chrome `CompactionHint` already supports.
    expect(isCompactingStatusKind('compacting')).toBe(true)
  })

  it('accepts the manual /compress kind emitted by session.compress', () => {
    // Regression: tui_gateway/server.py:5918 emits kind="compressing" directly
    // (no re-tag), which the desktop handler previously ignored, so manual
    // /compress never showed the chrome spinner.
    expect(isCompactingStatusKind('compressing')).toBe(true)
  })

  it('rejects unrelated status kinds', () => {
    expect(isCompactingStatusKind('process')).toBe(false)
    expect(isCompactingStatusKind('lifecycle')).toBe(false)
    expect(isCompactingStatusKind('ready')).toBe(false)
    expect(isCompactingStatusKind(undefined)).toBe(false)
    expect(isCompactingStatusKind(null)).toBe(false)
    expect(isCompactingStatusKind(42)).toBe(false)
  })
})
