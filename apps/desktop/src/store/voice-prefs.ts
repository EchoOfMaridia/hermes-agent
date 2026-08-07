import { atom } from 'nanostores'

import { getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import {
  DEFAULT_VOICE_RECORD_KEY,
  parseVoiceRecordKey,
  type ParsedVoiceRecordKey,
  voiceRecordKeyToCombo
} from '@/lib/keybinds/parse-voice-record-key'

// "Read replies aloud" — mirrors the canonical `voice.auto_tts` config key (also
// in Settings → Voice, honored by the messaging gateway) so the composer toggle
// and the Settings switch are one source of truth, not two that can disagree.
export const $autoSpeakReplies = atom<boolean>(false)

/** Seed the atom from a loaded config payload (mount / refresh). */
export function applyAutoSpeakFromConfig(config: { voice?: { auto_tts?: unknown } | null } | null | undefined) {
  $autoSpeakReplies.set(Boolean(config?.voice?.auto_tts))
}

/**
 * Flip the preference and persist it. Optimistic — the atom updates instantly and
 * reverts if the config write fails. Read-modify-writes the whole record (the
 * same path the Settings page uses; there's no partial-update endpoint).
 */
export async function setAutoSpeakReplies(enabled: boolean): Promise<void> {
  const previous = $autoSpeakReplies.get()

  if (previous === enabled) {
    return
  }

  $autoSpeakReplies.set(enabled)

  try {
    const record = await getHermesConfigRecord()
    const voice = record.voice && typeof record.voice === 'object' ? (record.voice as Record<string, unknown>) : {}

    await saveHermesConfig({ ...record, voice: { ...voice, auto_tts: enabled } })
  } catch (error) {
    $autoSpeakReplies.set(previous)
    throw error
  }
}

// Voice record-key plumbing — feeds `composer.voice` defaults from
// `voice.record_key` in config.yaml so the same config value the user sets
// in Settings → Voice Shortcut (or the YAML) drives the Desktop hotkey the
// same way it drives the classic CLI and the TUI.
//
// The cross-runtime parity table at ui-tui/src/lib/platform.ts:88-110 and
// the CLI blocklist at hermes_cli/voice.py:85 cover the full rejection
// contract; the Desktop normalizer (parse-voice-record-key.ts) is a
// faithful port so a config value that survives Desktop cannot silently
// fail on the CLI.
//
// The atom holds the *parsed* shape (mod + ch + named), not the raw string,
// because every consumer (`composer.voice` defaults, conflict detection,
// keybind panel display) needs the structured form. The raw string is
// recoverable via `voiceRecordKeyToCombo(parsed)`.
export const $voiceRecordKey = atom<ParsedVoiceRecordKey>(DEFAULT_VOICE_RECORD_KEY)

/** Seed the atom from a loaded config payload (mount / refresh). */
export function applyVoiceRecordKeyFromConfig(
  config: { voice?: { record_key?: unknown } | null } | null | undefined
) {
  $voiceRecordKey.set(parseVoiceRecordKey(config?.voice?.record_key))
}

/**
 * Update the configured record key and persist it. Optimistic — the atom
 * updates instantly and reverts to the previous parsed value if the config
 * write fails. The Settings page drives this; the persisted value flows
 * back through `applyVoiceRecordKeyFromConfig` on the next config refresh.
 */
export async function setVoiceRecordKey(parsed: ParsedVoiceRecordKey): Promise<void> {
  const previous = $voiceRecordKey.get()
  const combo = voiceRecordKeyToCombo(parsed)

  if (previous.mod === parsed.mod && previous.ch === parsed.ch && previous.named === parsed.named) {
    return
  }

  $voiceRecordKey.set(parsed)

  try {
    const record = await getHermesConfigRecord()
    const voice = record.voice && typeof record.voice === 'object' ? (record.voice as Record<string, unknown>) : {}

    await saveHermesConfig({ ...record, voice: { ...voice, record_key: combo } })
  } catch (error) {
    $voiceRecordKey.set(previous)
    throw error
  }
}