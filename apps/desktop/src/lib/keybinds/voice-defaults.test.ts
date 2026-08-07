// Tests for the keybinds store integration with the voice record-key atom.
// Verifies that `composer.voice` defaults flow through `$voiceRecordKey` so
// the same YAML `voice.record_key` the user sets in Settings drives the
// Desktop hotkey the same way it drives the classic CLI and the TUI.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

// `IS_MAC` is resolved once at module load from `navigator`; for these tests
// we run Linux so the platform-specific fallback (unbound default) is
// observable, then assert the function-form `defaults` reacts to the atom.
async function loadFresh(platform: string) {
  Object.defineProperty(window.navigator, 'platform', { value: platform, configurable: true })
  const { vi } = await import('vitest')
  vi.resetModules()

  return {
    keybinds: await import('@/store/keybinds'),
    voicePrefs: await import('@/store/voice-prefs'),
    actions: await import('@/lib/keybinds/actions')
  }
}

beforeEach(async () => {
  const { vi } = await import('vitest')
  vi.resetModules()
})

afterEach(async () => {
  const { vi } = await import('vitest')
  vi.resetModules()
})

describe('composer.voice defaults — Linux/Windows without voice.record_key', () => {
  it('ships unbound by default on Linux to avoid stealing the sidebar binding', async () => {
    const { actions, voicePrefs } = await loadFresh('Linux x86_64')

    // Atom is at the documented default until config seeds it.
    expect(voicePrefs.$voiceRecordKey.get().mod).toBe('ctrl')
    expect(voicePrefs.$voiceRecordKey.get().ch).toBe('b')

    // Linux: the function-form `defaults` returns `[]` so the user has to
    // either configure a non-`ctrl+b` key OR rebind via the panel. Shipping
    // `mod+b` (= Ctrl+B off macOS) would steal `view.toggleSidebar`.
    const defaults = actions.resolveDefaults(actions.keybindAction('composer.voice') as NonNullable<ReturnType<typeof actions.keybindAction>>)
    expect(defaults).toEqual([])
  })

  it('uses the configured voice.record_key on Linux when it differs from the documented default', async () => {
    const { actions, voicePrefs } = await loadFresh('Linux x86_64')

    // Simulate `applyVoiceRecordKeyFromConfig({ voice: { record_key: 'ctrl+o' } })`.
    voicePrefs.applyVoiceRecordKeyFromConfig({ voice: { record_key: 'ctrl+o' } })

    expect(voicePrefs.$voiceRecordKey.get().ch).toBe('o')

    const defaults = actions.resolveDefaults(actions.keybindAction('composer.voice') as NonNullable<ReturnType<typeof actions.keybindAction>>)
    expect(defaults).toEqual(['ctrl+o'])
  })

  it('falls back to ctrl+b on macOS even when the configured key is the default', async () => {
    const { actions, voicePrefs } = await loadFresh('MacIntel')

    // Atom still at the documented default.
    expect(voicePrefs.$voiceRecordKey.get().mod).toBe('ctrl')
    expect(voicePrefs.$voiceRecordKey.get().ch).toBe('b')

    const defaults = actions.resolveDefaults(actions.keybindAction('composer.voice') as NonNullable<ReturnType<typeof actions.keybindAction>>)
    expect(defaults).toEqual(['ctrl+b'])
  })

  it('uses the configured voice.record_key on macOS when it differs from ctrl+b', async () => {
    const { actions, voicePrefs } = await loadFresh('MacIntel')

    voicePrefs.applyVoiceRecordKeyFromConfig({ voice: { record_key: 'ctrl+o' } })

    const defaults = actions.resolveDefaults(actions.keybindAction('composer.voice') as NonNullable<ReturnType<typeof actions.keybindAction>>)
    expect(defaults).toEqual(['ctrl+o'])
  })
})

describe('composer.voice defaults — cross-runtime submit-collision parity', () => {
  it('falls back to the documented default on Linux when voice.record_key=ctrl+m (c-m would shadow Enter/submit on the CLI)', async () => {
    const { actions, voicePrefs } = await loadFresh('Linux x86_64')

    voicePrefs.applyVoiceRecordKeyFromConfig({ voice: { record_key: 'ctrl+m' } })

    // Normalizer rejects c-m → atom at the documented default.
    expect(voicePrefs.$voiceRecordKey.get().ch).toBe('b')
    expect(voicePrefs.$voiceRecordKey.get().mod).toBe('ctrl')

    // On Linux that means unbound (avoid the sidebar collision).
    const defaults = actions.resolveDefaults(actions.keybindAction('composer.voice') as NonNullable<ReturnType<typeof actions.keybindAction>>)
    expect(defaults).toEqual([])
  })
})