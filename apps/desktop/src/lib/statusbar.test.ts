import { describe, expect, it } from 'vitest'

import { contextBarLabel, formatK, usageContextLabel } from './statusbar'

describe('formatK', () => {
  it('renders sub-thousand values as raw integers', () => {
    expect(formatK(0)).toBe('0')
    expect(formatK(1)).toBe('1')
    expect(formatK(999)).toBe('999')
  })

  it('renders thousands with k suffix and one decimal', () => {
    expect(formatK(1_000)).toBe('1.0k')
    expect(formatK(50_000)).toBe('50.0k')
    expect(formatK(200_000)).toBe('200.0k')
  })

  it('renders millions with M suffix and one decimal', () => {
    expect(formatK(1_000_000)).toBe('1.0M')
    expect(formatK(3_100_000)).toBe('3.1M')
  })

  it('clamps non-finite and negative to zero', () => {
    expect(formatK(NaN)).toBe('0')
    expect(formatK(-1)).toBe('0')
    expect(formatK(Infinity)).toBe('0')
  })
})

describe('usageContextLabel', () => {
  it('renders "used / max" when context_max is set', () => {
    expect(
      usageContextLabel({
        calls: 1,
        context_max: 1_000_000,
        context_used: 750_000,
        input: 750_000,
        output: 250,
        total: 750_250
      })
    ).toBe('750.0k/1.0M')
  })

  it('renders "used / max" for the bug-screenshot shape (clamped at max)', () => {
    // After the turn_context.py clamp fix, the wire-level value can never
    // exceed context_length. This test pins the display contract: even if
    // a stale value above context_max somehow slips through, the display
    // still renders both numbers verbatim — the fix is upstream clamping,
    // not display-side masking.
    expect(
      usageContextLabel({
        calls: 1,
        context_max: 1_000_000,
        context_used: 1_000_000,
        input: 1_000_000,
        output: 250,
        total: 1_000_250
      })
    ).toBe('1.0M/1.0M')
  })

  it('falls back to total tokens when context_max is absent', () => {
    expect(
      usageContextLabel({
        calls: 1,
        input: 12_000,
        output: 800,
        total: 12_800
      })
    ).toBe('12.8k tok')
  })

  it('returns empty string when nothing is known', () => {
    expect(
      usageContextLabel({
        calls: 0,
        input: 0,
        output: 0,
        total: 0
      })
    ).toBe('')
  })

  it('treats context_used as zero when undefined', () => {
    expect(
      usageContextLabel({
        calls: 1,
        context_max: 200_000,
        input: 50_000,
        output: 1_000,
        total: 51_000
      })
    ).toBe('0/200.0k')
  })
})

describe('contextBarLabel', () => {
  it('renders a 100% bar with all blocks filled when context_percent caps at 100', () => {
    const label = contextBarLabel({
      calls: 1,
      context_max: 1_000_000,
      context_percent: 100,
      context_used: 1_000_000,
      input: 1_000_000,
      output: 250,
      total: 1_000_250
    })

    expect(label).toBe('[██████████] 100%')
  })

  it('renders a partially-filled bar at 50%', () => {
    const label = contextBarLabel({
      calls: 1,
      context_max: 200_000,
      context_percent: 50,
      context_used: 100_000,
      input: 100_000,
      output: 0,
      total: 100_000
    })

    expect(label).toBe('[█████░░░░░] 50%')
  })

  it('returns empty string when context_max is missing', () => {
    expect(
      contextBarLabel({
        calls: 0,
        input: 0,
        output: 0,
        total: 0
      })
    ).toBe('')
  })

  it('clamps context_percent above 100 to 100 (display-side guard)', () => {
    // Defense in depth: even if a stale >100% slips through, the bar caps.
    const label = contextBarLabel({
      calls: 1,
      context_max: 1_000_000,
      context_percent: 310,
      context_used: 3_100_000,
      input: 3_100_000,
      output: 0,
      total: 3_100_000
    })

    expect(label).toBe('[██████████] 100%')
  })
})
