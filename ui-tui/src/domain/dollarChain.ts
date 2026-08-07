/**
 * Position-0 `$` chain detection and rewrite for the TUI.
 *
 * Mirrors `agent/skill_commands.py:rewrite_dollar_chain_to_slash` on the
 * Python side — the TUI runs no Python helper, so the rewrite is duplicated
 * here. Both implementations MUST stay in sync.
 *
 * The TUI does NOT need to know the canonical `/slug` of every skill — it
 * just rewrites `$name` to `/name` and lets the gateway's `slash.exec` →
 * `command.dispatch` path resolve the rest. If a `$name` doesn't resolve
 * to a real skill, the dispatch falls through to a plain slash command
 * lookup; if THAT fails, the user gets a graceful "not a known command"
 * error instead of an arbitrary crash.
 *
 * Token boundary: letter-start, word-end at whitespace / EOL, leading `$`
 * must sit at start-of-text or after whitespace. `$PATH` (uppercase, no
 * letter-boundary fitting) does NOT match — same rule as
 * `inlineDollarTrigger`.
 */

const POSITION_ZERO_DOLLAR_CHAIN_RE = /^\$([a-zA-Z][\w-]*)/

/**
 * Rewrite a position-0 `$a $b do XYZ` to `/a /b do XYZ`. Returns the
 * original text unchanged when:
 *   - the input is empty or doesn't start with `$`
 *   - the input starts with `$` but the first token isn't a `$skill`-shape
 *     (e.g. `$PATH` is excluded by the letter-start anchor)
 *
 * Unknown `$tokens` are silently dropped from the rewrite. Plain words
 * pass through unchanged.
 */
export function rewritePositionZeroDollarChain(text: string): string {
  if (!text || !text.startsWith('$')) {
    return text
  }
  const tokens = text.split(/\s+/)
  const rewritten: string[] = []
  let foundAny = false
  for (const tok of tokens) {
    if (tok.startsWith('$') && tok.length > 1) {
      const inner = tok.slice(1)
      if (/^[a-zA-Z][\w-]*$/.test(inner)) {
        rewritten.push(`/${inner}`)
        foundAny = true
        continue
      }
      if (tok === '$') {
        rewritten.push(tok)
        continue
      }
      // `$PATH`-shaped or otherwise malformed — drop it.
      continue
    }
    rewritten.push(tok)
  }
  if (!foundAny) {
    return text
  }
  return rewritten.join(' ')
}
