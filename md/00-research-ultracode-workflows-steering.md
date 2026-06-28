# Research Steering File — Claude Code Ultracode & Dynamic Workflows

## Topic
Claude Code's `ultracode` effort setting and Dynamic Workflows feature: exactly how it works, what it does, when to use it, and how it differs from `/goal` and other orchestration mechanisms. Launched alongside Claude Opus 4.8 on 2026-05-28.

## Target Sources
100 unique sources (web + local), distributed ~25 per thread.

## Research Threads

| Thread ID | Aspect | Status | File |
|-----------|--------|--------|------|
| 01 | Feature mechanics — what ultracode is, how workflows are generated, the script-subagent model, parallelism, 1000-subagent cap | PENDING | 01-feature-mechanics-findings.md |
| 02 | Effort / model axis — `/effort ultracode`, x-high reasoning, Opus 4.8 / Sonnet 4.6 / Haiku 4.7 pairing, fast-mode workflow cap, pricing impact | PENDING | 02-effort-model-axis-findings.md |
| 03 | vs `/goal`, vs subagents, vs Skills, vs Plan mode, vs Agent Teams — when to pick which orchestration mechanism | PENDING | 03-orchestration-comparison-findings.md |
| 04 | Production use, failure modes, security, anti-patterns, community sentiment, Reddit/GitHub reports, real-world examples | PENDING | 04-production-use-patterns-findings.md |

## Synthesis Status
PENDING

## Output Destinations
- Findings files: `/home/cage/Desktop/Workspaces/HermesDesktop/md/0[1-4]-*-findings.md`
- Final report: `/home/cage/Desktop/Workspaces/HermesDesktop/md/FINAL-report-claude-code-ultracode-workflows.md`
- Hindsight retain: structured findings clusters (mechanics, model axis, comparison, production patterns)

## Pre-flight Notes
- Anthropic official docs at `code.claude.com/docs/en/workflows` are the primary source.
- Confirmed real feature, not hallucinated — web search returned 50+ matches.
- Public messaging vs technical reality gap is the central analytical tension.
