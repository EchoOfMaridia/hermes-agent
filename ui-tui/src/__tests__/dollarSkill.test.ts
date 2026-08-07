import { describe, expect, it } from 'vitest'

import { inlineDollarTrigger, splitDollarRefs } from '../domain/slash.js'
import { completionRequestForInput } from '../hooks/useCompletion.js'

// `$` is the codex-style skill reference. Parallels the existing `/` trigger:
//   - At position 0 it is a forced multi-skill chain; the TUI completion
//     surfaces skill and bundle completions.
//   - Mid-prose it is an inline reference; same boundary semantics as `/`.
// Token boundary: letter-start, word-end at whitespace / EOL, leading `$`
// must sit at start-of-text or after whitespace so `$PATH` (env-var shape)
// does not match.

describe('inlineDollarTrigger', () => {
  it('detects a dollar token typed mid-message', () => {
    expect(inlineDollarTrigger('please run $cle')).toEqual({ query: 'cle', start: 11 })
  })

  it('detects a bare dollar after whitespace, before any name is typed', () => {
    expect(inlineDollarTrigger('please run $')).toEqual({ query: '', start: 11 })
  })

  it('fires at position 0 — `$` is a forced multi-skill chain, not a path', () => {
    // Unlike `/`, `$` is NOT path-shaped at position 0. The whole point
    // is to give the user a way to force-load multiple skills without
    // remembering the `/a /b` stacked-skill syntax.
    expect(inlineDollarTrigger('$skill-a')).toEqual({ query: 'skill-a', start: 0 })
    expect(inlineDollarTrigger('$')).toEqual({ query: '', start: 0 })
  })

  it('fires after a newline, not just a space', () => {
    expect(inlineDollarTrigger('text\n$skill')).toEqual({ query: 'skill', start: 5 })
  })

  it('does not match env-var-shaped tokens', () => {
    // `$PATH` is uppercase with no letter-lowercase boundary; the regex
    // requires a letter-start + word boundary. This is the
    // `$PATH`-collision guard.
    expect(inlineDollarTrigger('echo $PATH')).toBeNull()
    expect(inlineDollarTrigger('cd $HOME/foo')).toBeNull()
  })

  it('reports a start index that replaces only the typed token', () => {
    const text = 'please run $cle'
    const trigger = inlineDollarTrigger(text)!

    expect(text.slice(0, trigger.start)).toBe('please run ')
    expect(text.slice(trigger.start)).toBe('$cle')
  })
})

describe('completionRequestForInput — dollar references', () => {
  it('asks for skills+bundles when `$` is mid-message', () => {
    const request = completionRequestForInput('please run $cle')

    expect(request).toMatchObject({
      method: 'complete.slash',
      params: { text: '/cle', dollarOnly: true },
      replaceFrom: 12
    })
  })

  it('asks for skills+bundles when `$` is at position 0', () => {
    const request = completionRequestForInput('$cle')

    expect(request).toMatchObject({
      method: 'complete.slash',
      params: { text: '/cle', dollarOnly: true },
      replaceFrom: 1
    })
  })
})

describe('splitDollarRefs', () => {
  it('marks a dollar-ref mid-prose', () => {
    expect(splitDollarRefs('clean this up with $clean')).toEqual([
      { ref: false, text: 'clean this up with ' },
      { ref: true, text: '$clean' }
    ])
  })

  it('keeps the prose on both sides of the reference', () => {
    expect(splitDollarRefs('run $clean then $work ok')).toEqual([
      { ref: false, text: 'run ' },
      { ref: true, text: '$clean' },
      { ref: false, text: ' then ' },
      { ref: true, text: '$work' },
      { ref: false, text: ' ok' }
    ])
  })

  it('does not mark env-var-shaped tokens', () => {
    expect(splitDollarRefs('echo $PATH')).toEqual([
      { ref: false, text: 'echo $PATH' }
    ])
  })

  it('marks a leading `$ref` — it is a forced multi-skill chain', () => {
    // Unlike the `/` rule, `$` IS allowed at position 0 (forced multi-skill).
    expect(splitDollarRefs('$clean')).toEqual([
      { ref: true, text: '$clean' }
    ])
  })

  it('round-trips the input exactly', () => {
    for (const text of ['run $clean then $work ok', 'plain text', '', 'echo $PATH']) {
      expect(
        splitDollarRefs(text)
          .map(s => s.text)
          .join('')
      ).toBe(text)
    }
  })

  it('always returns at least one segment', () => {
    expect(splitDollarRefs('')).toEqual([{ ref: false, text: '' }])
  })
})
