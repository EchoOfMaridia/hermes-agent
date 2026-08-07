/* eslint-disable */
// REGRESSION GUARD for the sidebar working-animation bug.
//
// Symptom: A background session keeps its sidebar "working" pulse pinned
// indefinitely (until the 8-min watchdog fires or the user manually
// switches to it). Repro: start a turn on session A, switch to session B,
// let A finish. A's row keeps showing the working animation.
//
// Root cause: use-message-stream.ts gated the session.info runningChanged
// branch behind `if (apply)`, which is true only for the active session.
// The state-patch branch above it ran unconditionally; the busy patch did
// not, so a background session's running=false event was dropped.
//
// Fix: pull the runningChanged handler out of the `if (apply) { ... }`
// guard so it fires for any session whose session_id matches. The view-
// store side effects (model/cwd/usage/statusbar pills) stay gated by
// `apply` because they legitimately only matter for the focused session.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

describe('use-message-stream: background session busy clearing (bug #1)', () => {
  it('runningChanged handler is NOT nested inside `if (apply)` in use-message-stream.ts', () => {
    const path = resolve(__dirname, 'use-message-stream.ts')
    const src = readFileSync(path, 'utf8')

    // The fix is structural: the runningChanged handler must NOT be nested
    // inside an enclosing `if (apply) { ... }` block. Find the runningChanged
    // line, then the MOST RECENT preceding `if (apply) {` line, and assert
    // that apply's matching closing brace comes BEFORE runningChanged.
    const lines = src.split('\n')

    let runningLineIdx = -1

    for (let i = 0; i < lines.length; i += 1) {
      if (/^\s+if \(runningChanged && sessionId\) \{$/.test(lines[i])) {
        runningLineIdx = i
        break
      }
    }

    expect(runningLineIdx).toBeGreaterThan(0)

    // Find the MOST RECENT `if (apply) {` line BEFORE runningChanged.
    let applyLineIdx = -1

    for (let i = runningLineIdx - 1; i >= 0; i -= 1) {
      if (/^\s+if \(apply\) \{$/.test(lines[i])) {
        applyLineIdx = i
        break
      }
    }

    expect(applyLineIdx).toBeGreaterThanOrEqual(0)

    const applyIndent = lines[applyLineIdx].match(/^(\s*)/)![1].length

    // The matching closing `}` for the apply block is at applyIndent
    // whitespace.
    const closingPattern = new RegExp(`^${' '.repeat(applyIndent)}\}\s*$`)

    // Search forward from the apply line for the FIRST matching close.
    let applyCloseLineIdx = -1

    for (let i = applyLineIdx + 1; i < runningLineIdx; i += 1) {
      if (closingPattern.test(lines[i])) {
        applyCloseLineIdx = i
        break
      }
    }

    // If we found a matching close BEFORE runningChanged, runningChanged
    // is OUTSIDE apply. If we never found one, runningChanged is still
    // nested inside apply — the bug.
    expect(applyCloseLineIdx).toBeGreaterThan(applyLineIdx)
    expect(applyCloseLineIdx).toBeLessThan(runningLineIdx)
  })
})
