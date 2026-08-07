// Normalize the `voice.record_key` config value into a structured shape the
// Desktop keybind store can dispatch on.
//
// Mirrors the contract in `ui-tui/src/lib/platform.ts::parseVoiceRecordKey` and
// `hermes_cli/voice.py::normalize_voice_record_key_for_prompt_toolkit` so one
// config value produces the same binding across CLI, TUI, and Desktop:
//
//   - non-string / empty / typo'd / bare-char / multi-modifier / reserved chars
//     → documented default (`{ mod: 'ctrl', ch: 'b', raw: 'ctrl+b' }`)
//   - single-char keys: `ctrl+o` → `{ mod: 'ctrl', ch: 'o', raw: 'ctrl+o' }`
//   - named keys: `ctrl+space` → `{ mod: 'ctrl', ch: 'space', named: 'space', raw: 'ctrl+space' }`
//   - super / win / windows → `{ mod: 'super', ... }` (matches TUI; the
//     cross-runtime parity table at ui-tui/src/lib/platform.ts:88-110 spells
//     out the full modifier-alias contract).
//
// The Desktop UI does not have a submit handler that competes with `c-m` /
// `c-j` the way the classic CLI's prompt_toolkit does — but we still reject
// them at parse time so a config value that "works" on Desktop cannot
// silently produce a dead shortcut on the CLI the user later opens. The
// rejection set is the *union* of platform hazards, not just the Desktop
// ones, mirroring the CLI's `_VOICE_RESERVED_PT_KEYS` set
// (`hermes_cli/voice.py:85`).
//
// Acceptance: malformed configs surface as the documented default so the
// keybind panel never advertises a shortcut that won't fire. Mirror of the
// UI-TUI parser so cross-runtime configs round-trip identically.

const IS_MAC = typeof navigator !== 'undefined' && /mac/i.test(navigator.platform || navigator.userAgent || '')

export type VoiceRecordKeyMod = 'alt' | 'ctrl' | 'super'

export type VoiceRecordKeyNamed = 'backspace' | 'delete' | 'enter' | 'escape' | 'space' | 'tab'

export interface ParsedVoiceRecordKey {
  /** Single character (`'b'`, `'o'`) when `named` is undefined, otherwise the
   * named-key token (`'space'`, `'enter'`…). Kept as one field for back-compat
   * with the v1 `{ ch, mod, raw }` shape the UI-TUI parser exposes. */
  ch: string
  mod: VoiceRecordKeyMod
  named?: VoiceRecordKeyNamed
  raw: string
}

export const DEFAULT_VOICE_RECORD_KEY: ParsedVoiceRecordKey = {
  ch: 'b',
  mod: 'ctrl',
  raw: 'ctrl+b'
}

const _MOD_ALIASES: Record<string, VoiceRecordKeyMod> = {
  alt: 'alt',
  control: 'ctrl',
  ctrl: 'ctrl',
  opt: 'alt',
  option: 'alt',
  super: 'super',
  win: 'super',
  windows: 'super'
}

const _NAMED_KEY_ALIASES: Record<string, VoiceRecordKeyNamed> = {
  backspace: 'backspace',
  bs: 'backspace',
  del: 'delete',
  delete: 'delete',
  enter: 'enter',
  esc: 'escape',
  escape: 'escape',
  ret: 'enter',
  return: 'enter',
  space: 'space',
  spc: 'space',
  tab: 'tab'
}

/** `useInputHandlers()` and global hotkeys intercept these unconditionally
 * before the voice check runs, so a binding like `ctrl+c` (interrupt),
 * `ctrl+d` (quit), or `ctrl+l` (clear screen) would be advertised in the
 * keybind panel but never fire push-to-talk. Reject at parse time so the
 * user gets the documented Ctrl+B instead of a dead shortcut. */
const _RESERVED_CTRL_CHARS = new Set(['c', 'd', 'l'])

/** On macOS the action-modifier intercepts these editor chords via
 * `isCopyShortcut` / `isAction` in `useInputHandlers()`:
 *  - super+c → copy
 *  - super+d → exit
 *  - super+l → clear screen
 *  - super+v → paste (also claimed at the TextInput layer)
 * On Linux/Windows those globals key off Ctrl instead of Super, so
 * super+<letter> bindings don't collide. Gate the rejection to darwin
 * at parse time so kitty/CSI-u `super+<key>` configs still work for
 * non-mac users. */
const _RESERVED_SUPER_CHARS = new Set(['c', 'd', 'l', 'v'])

/** On macOS the action modifier accepts `meta` as Cmd, and Alt reports as
 * `meta` on many terminals. So on darwin a configured `alt+c` / `alt+d` /
 * `alt+l` gets swallowed by `isCopyShortcut` / `isAction` before the voice
 * check runs. Block at parse time so /voice status doesn't advertise a
 * shortcut that actually copies / quits / clears. */
const _RESERVED_ALT_CHARS_MAC = new Set(['c', 'd', 'l'])

/** Cross-runtime submit-collision set. On the classic CLI
 * `_bind_prompt_submit_keys` (cli.py:13779) registers `Keys.Enter`, which is
 * literally `Keys.ControlM` (`c-m`), and on bare POSIX also `c-j` for
 * LF-submitting terminals — prompt_toolkit fires both stacked handlers and
 * the submit handler shadows the voice one. The Desktop UI doesn't have
 * those bindings, but rejecting the values at parse time keeps cross-
 * runtime parity so a config value that survives the Desktop can't
 * silently fail on the CLI. Mirrors `hermes_cli/voice.py::_VOICE_RESERVED_PT_KEYS`. */
const _RESERVED_SUBMIT_KEYS = new Set(['c-m', 'c-j'])

/** Parse a config-string voice record key like `ctrl+b` / `alt+r` /
 * `ctrl+space` into `{ mod, ch, named? }`. Accepts single characters AND
 * the named tokens declared in `_NAMED_KEY_ALIASES` (`space`,
 * `enter`/`return`, `tab`, `escape`/`esc`, `backspace`, `delete`).
 *
 * Non-string / empty / unrecognized values fall back to the documented
 * default so a typo never silently disables the shortcut. */
export const parseVoiceRecordKey = (raw: unknown): ParsedVoiceRecordKey => {
  if (typeof raw !== 'string') {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const lower = raw.trim().toLowerCase()

  if (!lower) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const parts = lower
    .split('+')
    .map(p => p.trim())
    .filter(Boolean)

  if (!parts.length) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const last = parts[parts.length - 1]
  const modCandidates = parts.slice(0, -1)

  // Reject multi-modifier chords (`ctrl+alt+r`, `cmd+ctrl+b`) rather than
  // silently dropping the extra modifier — the previous single-token
  // validator made a typo bind a different shortcut than the user
  // configured. The classic CLI only supports single-modifier bindings via
  // prompt_toolkit's `c-x` / `a-x` rewrite, so this matches CLI parity.
  if (modCandidates.length > 1) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  // Require an explicit modifier. A bare `o` / `space` / `escape` has no
  // sensible mapping; the CLI's prompt_toolkit binds the raw key (no
  // rewrite) so bare-char configs would silently diverge between the two
  // runtimes. Fall back to the documented default.
  if (modCandidates.length === 0) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const norm = _MOD_ALIASES[modCandidates[0]]

  // Unknown modifier token (e.g. bare `meta+b` which is ambiguous on the
  // wire) falls back to the documented default rather than silently
  // coercing to Ctrl and producing a misleading bind.
  if (!norm) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const mod = norm

  // Block bindings the global input handler intercepts before the voice
  // check — `ctrl+c` / `ctrl+d` / `ctrl+l` would never actually fire
  // push-to-talk, so advertising them in the keybind panel is a lie.
  if (mod === 'ctrl' && last.length === 1 && _RESERVED_CTRL_CHARS.has(last)) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  // Cross-runtime submit-collision block. `c-m` / `c-j` shadow the CLI's
  // submit handler; reject at parse time so the same config value behaves
  // identically across CLI, TUI, and Desktop.
  if (mod === 'ctrl' && last.length === 1 && _RESERVED_SUBMIT_KEYS.has(`c-${last}`)) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  // Same for `super+c` / `super+d` / `super+l` / `super+v` on macOS only —
  // those are copy / exit / clear / paste and get claimed by
  // `isCopyShortcut` / `isAction` / the TextInput paste layer before
  // voice has a chance to toggle. On Linux/Windows the globals key off
  // Ctrl (not Super), so kitty/CSI-u `super+<letter>` bindings stay
  // usable for non-mac users.
  if (IS_MAC && mod === 'super' && last.length === 1 && _RESERVED_SUPER_CHARS.has(last)) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  // On macOS hermes-ink reports Alt as `key.meta`, which `isActionMod`
  // accepts as the mac action modifier. So `alt+c` / `alt+d` / `alt+l`
  // collide with copy / exit / clear in `useInputHandlers()` before the
  // voice check. Reject at parse time on darwin only — non-mac
  // `alt+<letter>` bindings are still usable.
  if (IS_MAC && mod === 'alt' && last.length === 1 && _RESERVED_ALT_CHARS_MAC.has(last)) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  if (last.length === 1) {
    return { ch: last, mod, raw: lower }
  }

  const named = _NAMED_KEY_ALIASES[last]

  if (named) {
    return { ch: named, mod, named, raw: lower }
  }

  // Unknown multi-character token (e.g. typo'd `ctrl+spcae`) — fall back
  // to the documented default rather than silently disabling the binding.
  return DEFAULT_VOICE_RECORD_KEY
}

/** Render a parsed key as the Desktop `mod+key` combo string the keybind
 * store expects. Inverse of `comboFromEvent` for the same set of keys. */
export const voiceRecordKeyToCombo = (parsed: ParsedVoiceRecordKey): string => {
  const keyLabel = parsed.named ?? parsed.ch

  return `${parsed.mod}+${keyLabel}`
}