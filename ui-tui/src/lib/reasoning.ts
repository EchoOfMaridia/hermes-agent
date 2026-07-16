const TAGS = ['think', 'reasoning', 'thinking', 'thought', 'REASONING_SCRATCHPAD'] as const

export interface SplitReasoning {
  reasoning: string
  text: string
}

export function splitReasoning(input: string): SplitReasoning {
  let text = input
  const reasoning: string[] = []

  for (const tag of TAGS) {
    const paired = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>\\s*`, 'gi')
    text = text.replace(paired, (_m, inner: string) => {
      const trimmed = inner.trim()

      if (trimmed) {
        reasoning.push(trimmed)
      }

      return ''
    })

    // Anchor to start-of-input OR after a newline so a literal `<think>`
    // mid-prose (model quoting the word, code blocks containing the tag,
    // etc.) doesn't eat every paragraph after it.  Real unclosed reasoning
    // blocks emitted after prior assistant text (e.g. "Some text\n<think>...")
    // must also be extracted so the thinking doesn't leak into the message
    // body.  The `(?:^|\\n)` anchor mirrors what `_strip_think_blocks` in
    // Python uses, keeping extraction and stripping aligned.
    const unclosed = new RegExp(`(?:^|\\n)(?=[^ \\t])<${tag}>([\\s\\S]*)$`, 'i')
    text = text.replace(unclosed, (_m, inner: string) => {
      const trimmed = inner.trim()

      if (trimmed) {
        // Split on paragraph break: first \n\n chunk = thinking,
        // rest = answer that flows back into text.  Mirrors the Python
        // _unterm_pat partition in build_assistant_message so extraction
        // and stripping stay aligned.
        const nl = '\n\n'
        const nli = trimmed.indexOf(nl)

        if (nli !== -1) {
          const thinking = trimmed.slice(0, nli).trim()
          const answer = trimmed.slice(nli + nl.length).trim()

          if (thinking) {
            reasoning.push(thinking)
          }

          if (answer) {
            return '\n' + answer
          }

          return ''
        }

        // No \n\n — pure thinking, strip the whole post-tag run.
        reasoning.push(trimmed)

        return ''
      }

      return ''
    })
  }

  return {
    reasoning: reasoning.join('\n\n').trim(),
    text: text.trim()
  }
}

export const hasReasoningTag = (input: string) => {
  for (const tag of TAGS) {
    if (input.includes(`<${tag}>`)) {
      return true
    }
  }

  return false
}
