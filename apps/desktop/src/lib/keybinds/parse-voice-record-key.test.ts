// Tests for the Desktop `voice.record_key` normalizer — the bridge between the
// YAML config knob and the rebindable `composer.voice` hotkey.
//
// The normalizer mirrors the contract in `ui-tui/src/lib/platform.ts::parseVoiceRecordKey`
// and `hermes_cli/voice.py::normalize_voice_record_key_for_prompt_toolkit` so one
// config value produces the same binding shape across CLI, TUI, and Desktop.
//
// The rejection blocklist (`_RESERVED_SUBMIT_KEYS`) closes the bug class where
// `voice.record_key: ctrl+m` or `voice.record_key: ctrl+j` shadowed the prompt's
// Enter/submit handler on the classic CLI. On Desktop those key sequences have
// no submit binding, but rejecting them at parse time keeps cross-runtime parity
// so a config value that survives one surface can't surprise the user on another.

import { afterEach, describe, expect, it, vi } from 'vitest'

// `IS_MAC` is resolved once at module load from `navigator`, so each platform
// case overrides the platform and re-imports the module fresh — same pattern
// as combo.test.ts.
async function loadParser(platform: string) {
  Object.defineProperty(window.navigator, 'platform', { value: platform, configurable: true })
  vi.resetModules()

  return import('./parse-voice-record-key')
}

afterEach(() => {
  vi.resetModules()
})

describe('parseVoiceRecordKey — documented default', () => {
  it('returns the canonical ctrl+b when no config is supplied', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    const parsed = parseVoiceRecordKey(undefined)

    expect(parsed).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parsed).toEqual({ mod: 'ctrl', ch: 'b', raw: 'ctrl+b' })
  })

  it('returns the canonical ctrl+b for empty / whitespace / non-string input', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    for (const input of [null, undefined, '', '   ', 1, true, {}, []]) {
      expect(parseVoiceRecordKey(input)).toEqual({ mod: 'ctrl', ch: 'b', raw: 'ctrl+b' })
    }
  })
})

describe('parseVoiceRecordKey — modifier aliases', () => {
  it('collapses ctrl / control / alt / option / opt to their canonical form', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('ctrl+o').mod).toBe('ctrl')
    expect(parseVoiceRecordKey('control+o').mod).toBe('ctrl')
    expect(parseVoiceRecordKey('alt+r').mod).toBe('alt')
    expect(parseVoiceRecordKey('option+r').mod).toBe('alt')
    expect(parseVoiceRecordKey('opt+r').mod).toBe('alt')
  })

  it('accepts super / win / windows on Linux/Windows (matches TUI parser parity)', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('super+b').mod).toBe('super')
    expect(parseVoiceRecordKey('win+o').mod).toBe('super')
    expect(parseVoiceRecordKey('windows+o').mod).toBe('super')
  })

  it('is case-insensitive across the whole config value', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('Ctrl+B')).toEqual({ mod: 'ctrl', ch: 'b', raw: 'ctrl+b' })
    expect(parseVoiceRecordKey('CONTROL+O').mod).toBe('ctrl')
  })

  it('trims whitespace within and around the modifier and key tokens', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    // Compare on the binding shape, not the `raw` field — `raw` preserves
    // the user's literal input spelling (incl. surrounding whitespace).
    const spec = (input: string) => {
      const parsed = parseVoiceRecordKey(input)
      return { mod: parsed.mod, ch: parsed.ch, named: parsed.named }
    }

    expect(spec('ctrl + b')).toEqual({ mod: 'ctrl', ch: 'b' })
    expect(spec('  option + space  ')).toEqual({ mod: 'alt', ch: 'space', named: 'space' })
  })
})

describe('parseVoiceRecordKey — named key aliases', () => {
  it('collapses named keys to a canonical token', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('ctrl+return')).toEqual({
      mod: 'ctrl',
      ch: 'enter',
      named: 'enter',
      raw: 'ctrl+return'
    })
    expect(parseVoiceRecordKey('ctrl+esc')).toEqual({
      mod: 'ctrl',
      ch: 'escape',
      named: 'escape',
      raw: 'ctrl+esc'
    })
    expect(parseVoiceRecordKey('ctrl+bs')).toEqual({
      mod: 'ctrl',
      ch: 'backspace',
      named: 'backspace',
      raw: 'ctrl+bs'
    })
    expect(parseVoiceRecordKey('alt+del')).toEqual({
      mod: 'alt',
      ch: 'delete',
      named: 'delete',
      raw: 'alt+del'
    })
    expect(parseVoiceRecordKey('ctrl+spc')).toEqual({
      mod: 'ctrl',
      ch: 'space',
      named: 'space',
      raw: 'ctrl+spc'
    })
    expect(parseVoiceRecordKey('ctrl+ret')).toEqual({
      mod: 'ctrl',
      ch: 'enter',
      named: 'enter',
      raw: 'ctrl+ret'
    })
  })
})

describe('parseVoiceRecordKey — rejection blocklist', () => {
  it('rejects bare tokens and multi-modifier chords to the documented default', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    for (const input of ['b', 'o', 'space', 'ctrl+alt+r', 'ctrl+shift+b', 'meta+b']) {
      expect(parseVoiceRecordKey(input)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    }
  })

  it('rejects ctrl+c / ctrl+d / ctrl+l (claimed by the global input handlers)', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('ctrl+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  // The bug-class closure: ctrl+m and ctrl+j would shadow the prompt's
  // submit handler on the classic CLI. The Desktop UI does not bind them
  // globally, but we reject them anyway so a config value that survives
  // one surface can't surprise the user on another — cross-runtime parity.
  it('rejects ctrl+m / ctrl+j so a config binding does not shadow Enter/submit on the CLI', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('ctrl+m')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('control+m')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+j')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('control+j')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('rejects unknown named keys and unknown modifiers to the documented default', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    for (const input of ['ctrl+spcae', 'ctrl+f5', 'ctrl+meta', 'shift+b']) {
      expect(parseVoiceRecordKey(input)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    }
  })

  it('rejects alt+c / alt+d / alt+l on macOS (collide with copy/exit/clear)', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('MacIntel')

    expect(parseVoiceRecordKey('alt+c')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('option+d')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('opt+l')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('permits alt+c / alt+d / alt+l on Linux/Windows (no global collision)', async () => {
    const { parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(parseVoiceRecordKey('alt+c').ch).toBe('c')
    expect(parseVoiceRecordKey('alt+d').ch).toBe('d')
    expect(parseVoiceRecordKey('alt+l').ch).toBe('l')
  })

  it('rejects super+c / super+d / super+l / super+v on macOS', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('MacIntel')

    for (const input of ['super+c', 'super+d', 'super+l', 'super+v']) {
      expect(parseVoiceRecordKey(input)).toEqual(DEFAULT_VOICE_RECORD_KEY)
    }
  })
})

describe('parseVoiceRecordKey — exact equality with the documented default', () => {
  it('treats ctrl+b and control+b as the documented default shape', async () => {
    const { parseVoiceRecordKey, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    // Compare on the parsed spec, not on the `raw` field — `raw` preserves
    // the user's literal input spelling, but the binding shape (mod + ch +
    // named) collapses to the documented default for every alias.
    const expectedSpec = { mod: DEFAULT_VOICE_RECORD_KEY.mod, ch: DEFAULT_VOICE_RECORD_KEY.ch, named: DEFAULT_VOICE_RECORD_KEY.named }

    const spec = (input: string) => {
      const parsed = parseVoiceRecordKey(input)
      return { mod: parsed.mod, ch: parsed.ch, named: parsed.named }
    }

    expect(spec('ctrl+b')).toEqual(expectedSpec)
    expect(spec('control+b')).toEqual(expectedSpec)
    expect(spec('ctrl + b')).toEqual(expectedSpec)
  })
})

describe('voiceRecordKeyToCombo — render into a Desktop keybind combo', () => {
  it('formats ctrl+b as the canonical combo', async () => {
    const { voiceRecordKeyToCombo, DEFAULT_VOICE_RECORD_KEY } = await loadParser('Linux x86_64')

    expect(voiceRecordKeyToCombo(DEFAULT_VOICE_RECORD_KEY)).toBe('ctrl+b')
  })

  it('formats a named key like ctrl+space', async () => {
    const { voiceRecordKeyToCombo, parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(voiceRecordKeyToCombo(parseVoiceRecordKey('ctrl+space'))).toBe('ctrl+space')
  })

  it('formats a super modifier as the literal super combo', async () => {
    const { voiceRecordKeyToCombo, parseVoiceRecordKey } = await loadParser('Linux x86_64')

    expect(voiceRecordKeyToCombo(parseVoiceRecordKey('super+b'))).toBe('super+b')
  })
})