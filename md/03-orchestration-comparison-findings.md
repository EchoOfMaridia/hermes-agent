# Findings: Orchestration Comparison (Thread 03)

**Aspect:** ultracode / Dynamic Workflows vs `/goal`, subagents, Skills, Plan mode, Agent Teams, and external orchestrators (LangGraph, CrewAI, AutoGen, OpenAI Codex, OpenCode, Hermes Kanban).
**Topic:** Claude Code ultracode & Dynamic Workflows
**Date:** 2026-06-26

---

## Web Sources

### A. ultracode / Dynamic Workflows (Claude-native)

1. **MindStudio — "What Is the Ultra Code Mode in Claude Code? X-High Effort Plus Dynamic Workflows"**
   https://www.mindstudio.ai/blog/what-is-ultra-code-mode-claude-code
   *Key findings:* Ultra Code combines xhigh reasoning with automatic dynamic-workflow orchestration. It is the highest effort mode in Claude Code's spectrum (low → medium → high → xhigh → ultracode). Ultracode is a session-wide setting; Claude decides per task whether a workflow is warranted. Recommends "use it for the planning phase separately" and "combine with version control checkpoints" because it makes broad cross-file changes.
   *Relevance:* Primary definitional source. Confirms ultracode ≠ a separate model and matches the "effort setting that activates dynamic workflows" framing.

2. **claudefa.st — "Ultracode in Claude Code: Effort Setting Explained"**
   https://claudefa.st/blog/guide/development/ultracode
   *Key findings:* "Ultracode is a session-wide setting that pins effort to xhigh and auto-orchestrates Dynamic Workflows for every substantive task until you turn it off." Explicitly reframes the earlier "ultracode = separate model" misconception.
   *Relevance:* High — clean operational definition for the report.

3. **Anthropic Platform Docs — "Effort"**
   https://platform.claude.com/docs/en/build-with-claude/effort
   *Key findings:* "Ultracode pairs the xhigh effort level with standing permission for Claude Code to launch multi-agent workflows, granted through mid-conversation system [reminders]." Authoritative — directly from Anthropic.
   *Relevance:* Highest. Resolves conflicting claim from medium.com/no-time and Mark Kashef with the official answer.

4. **Mark Kashef YouTube — "Master All 6 Claude Code Dynamic Workflows"**
   https://www.youtube.com/watch?v=g9b9G8dcS8Y
   *Key findings:* Kashef enumerates six workflow templates: **deep-research, codebase-audit, large-migration, test-generation, documentation, and cross-check** (sources: trilogyai.substack.com and oc-dw NPM templates confirm the same six names). Confirms the claim in CONTEXT.
   *Relevance:* Core — answers Requirement #1 directly.

5. **Mark Kashef — "The Claude Update Everyone Missed (Dynamic Workflows)"**
   https://www.youtube.com/watch?v=-tLlZqrXpo8
   *Key findings:* Companion overview of the 6 workflows; same taxonomy.
   *Relevance:* Corroborating.

6. **Marktechpost — "Anthropic Ships Claude Opus 4.8 Alongside Dynamic Workflows and Cheaper Fast Mode (workflows capped at 1000 subagents)"**
   https://www.marktechpost.com/2026/05/28/anthropic-ships-claude-opus-4-8-alongside-dynamic-workflows-and-cheaper-fast-mode-with-workflows-capped-at-1000-subagents/
   *Key findings:* "Ultracode combines xhigh reasoning effort with automatic workflow orchestration. Claude Code also bundles /deep-research as a built-in workflow." Confirms 1000-subagent cap and Opus 4.8 launch.
   *Relevance:* Confirms built-in `/deep-research` and 1000-subagent ceiling (Requirement #4).

7. **Medium / no-time — "Dynamic Workflows vs /goal in Claude Code: What's the Real Difference?"**
   https://medium.com/no-time/dynamic-workflows-vs-goal-in-claude-code-whats-the-real-difference-24f828b4a4ed
   *Key findings:* "Ultracode is the effort setting that activates dynamic workflows automatically. It's not a separate model or a different API endpoint. It's a [setting]." Source of the conflicting claim cited in CONTEXT.
   *Relevance:* High — anchors the "ultracode is not a model" claim and contrasts with `/goal`.

8. **Medium / Illumination — "Claude Code's Dynamic Workflows: The AI agent architecture that just rewrote 750,000 lines of code"**
   https://medium.com/illumination/claude-codes-dynamic-workflows-the-ai-agent-architecture-that-just-rewrote-750-000-lines-of-code-d605a1d9b6d4
   *Key findings:* "This combines xhigh reasoning effort with automatic workflow orchestration. With ultracode on, Claude decides when a task warrants a workflow." Real-world scale anecdote: 750k LoC migration.
   *Relevance:* Empirical evidence of the workflow ceiling.

9. **alirezarezvani / Medium — "Claude Code Workflows: Build Deterministic Agent Runs"**
   https://alirezarezvani.medium.com/claude-code-workflows-build-deterministic-agent-runs-eaf2c6ac52d5
   *Key findings:* "Dynamic workflows are in research preview and require Claude Code version 2.1.154 or later. They are on by default across the paid plans." Version-gating.
   *Relevance:* Versioning evidence.

10. **levelup.gitconnected — "Claude's Dynamic Workflows: The Hands-On Playbook (and the three jobs where LangGraph still wins)"**
    https://levelup.gitconnected.com/claudes-dynamic-workflows-the-hands-on-playbook-and-the-three-jobs-where-langgraph-still-wins-ab44b85a70ee
    *Key findings:* "Option B — turn on ultracode from the effort menu. From that point on, Claude decides per-task whether a workflow is the right tool." Provides the external comparison to LangGraph.
    *Relevance:* Bridges ultracode and external frameworks (Requirement #7).

11. **Pebblous — "Every Job Is an Algorithm — Claude Code Workflows Deep Analysis"**
    https://blog.pebblous.ai/report/claude-code-workflows-enterprise-ai/en/
    *Key findings:* "For large-scale migration and codebase audit, Claude Code Dynamic Workflows currently has no peer. However, the space is moving fast: Google [ADK] is closing in."
    *Relevance:* Comparative positioning.

12. **trilogyai Substack — "Claude Code's Dynamic Workflows: A Thousand Agents, One Script"**
    https://trilogyai.substack.com/p/claude-codes-dynamic-workflows-a
    *Key findings:* Describes `/deep-research` as a bundled workflow that "fans searches across several angles, cross-checks the sources against each other."
    *Relevance:* Built-in workflow example.

13. **AIPractitioner — "Claude Dynamic Workflows: Scaling Complex Work Through Orchestration"**
    https://aipractitioner.substack.com/p/claude-dynamic-workflows-scaling
    *Key findings:* Architecture-level analysis; where dynamic workflows sit in the Claude Code stack.
    *Relevance:* Architectural framing.

14. **MCP.Directory — "Claude Code Parallel Agents & Workflows (2026)"**
    https://mcp.directory/blog/claude-code-parallel-subagents-workflows-2026
    *Key findings:* "Dynamic workflows require Claude Code v2.1.154+." Use cases enumerated: codebase audit, 500-file mechanical migration, research across many sources.
    *Relevance:* Selection criteria.

15. **findskill.ai — "Claude Dynamic Workflows & Ultracode: How to Use Them"**
    https://findskill.ai/blog/claude-dynamic-workflows-ultracode-claude-code/
    *Key findings:* "Ultracode is a Claude Code setting rather than a model effort level: it sends xhigh to the model and additionally has Claude orchestrate [workflows]."
    *Relevance:* Confirms the "setting not model" claim with a different framing.

16. **oc-dw (OpenCode Dynamic Workflows port) on libraries.io**
    https://libraries.io/npm/oc-dw
    *Key findings:* Built-in templates list: **deep-research, codebase-audit, large-migration, test-generation, documentation, [cross-check]** — confirms the "6 workflows" taxonomy.
    *Relevance:* Cross-validates Kashef's list (Requirement #1).

### B. `/goal` command

17. **Anthropic Claude Code Docs — "Keep Claude working toward a goal"**
    https://code.claude.com/docs/en/goal
    *Key findings:* "/goal requires Claude Code v2.1.139 or later. The /goal command sets a completion condition and Claude keeps working toward it without you prompting each step."
    *Relevance:* Authoritative for `/goal` (Requirement #2).

18. **XDA Developers — "I finally understood Claude Code's /goal command…"**
    https://www.xda-developers.com/finally-understood-claude-code-goal-command/
    *Key findings:* "Anthropic launched the new /goal slash command within Claude Code. The /goal command pushes Claude to keep going." Launch reported as May 13, 2026.
    *Relevance:* Launch date (Requirement #2).

19. **Medium / data-science-collective — "How To Build a Claude Code /goal Better Than 99% of People"**
    https://medium.com/data-science-collective/how-to-build-a-claude-code-goal-better-than-99-of-people-5a4490095cf4
    *Key findings:* "On May 13, 2026, Anthropic's official account @ClaudeDevs announced a new command for Claude Code: 'Claude Code /goal'!"
    *Relevance:* Launch confirmation.

### C. Agent Teams vs Subagents

20. **Anthropic Claude Code Docs — "Orchestrate teams of Claude Code sessions"**
    https://code.claude.com/docs/en/agent-teams
    *Key findings:* "Subagents only report results back to the main agent and never talk to each other. In agent teams, teammates share a task list, claim work, and communicate [with each other]."
    *Relevance:* Authoritative (Requirement #3).

21. **Alex Op — "Claude Code Agent Teams: How Multiple Sessions Coordinate (2026)"**
    https://alexop.dev/posts/from-tasks-to-swarms-agent-teams-in-claude-code/
    *Key findings:* Subagents vs Agent Teams table: communication (report-to-main vs message-each-other), coordination (main-manages vs shared-task-list).
    *Relevance:* Decision criteria (Requirement #3, #8).

22. **Reddit r/Anthropic — "Claude agent teams vs subagents (made this to understand it)"**
    https://www.reddit.com/r/Anthropic/comments/1ryn14c/claude_agent_teams_vs_subagents_made_this_to/
    *Key findings:* "Subagents inherit the full parent context so nothing gets lost, while teams require explicit handoffs or external memory."
    *Relevance:* Decision criteria.

23. **MindStudio — "Claude Code Agent Teams vs Sub-Agents: Which Pattern Should [you use]"**
    https://www.mindstudio.ai/blog/claude-code-agent-teams-vs-sub-agents
    *Key findings:* "A sub-agent operates under the direction of a central orchestrator, which delegates tasks, collects results, and maintains the overall plan."
    *Relevance:* Comparative analysis.

24. **MindStudio — "Claude Code Dynamic Workflows vs Agent Teams vs Sub-Agents"**
    https://www.mindstudio.ai/blog/claude-code-dynamic-workflows-vs-agent-teams-vs-sub-agents
    *Key findings:* Three patterns compared on hierarchy, parallelism, role specialization, setup overhead, debuggability. Dynamic workflows = single adaptive agent; subagents = parent/child; agent teams = peer network.
    *Relevance:* Core comparison source (Requirement #8).

25. **Github — FlorianBruniaux/claude-code-ultimate-guide / agent-teams.md**
    https://github.com/FlorianBruniaux/claude-code-ultimate-guide/blob/main/guide/workflows/agent-teams.md
    *Key findings:* "Agent teams enable multiple Claude instances to work in parallel on different subtasks while coordinating through a git-based system. Unlike [subagents]…"
    *Relevance:* Git-based implementation pattern.

### D. Skills system

26. **Anthropic Engineering — "Equipping agents for the real world with Agent Skills"**
    https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
    *Key findings:* Skills are SKILL.md files; Claude uses bash to read them when triggered. Skills load into the main context, subagents do not.
    *Relevance:* Skills vs subagents distinction (Requirement #5).

27. **Anthropic Claude Code Docs — "Extend Claude with skills"**
    https://code.claude.com/docs/en/skills
    *Key findings:* Skills are orthogonal to workflows: a skill is content loaded into the current context, while a workflow is a script that orchestrates execution.
    *Relevance:* Requirement #5.

28. **levelup.gitconnected — "A Mental Model for Claude Code: Skills, Subagents, and Plugins"**
    https://levelup.gitconnected.com/a-mental-model-for-claude-code-skills-subagents-and-plugins-3dea9924bf05
    *Key findings:* "A skill is a reusable set of instructions you write once and make available to Claude whenever it's relevant. Subagents are independent. They do a job, return a result, and never talk to each other."
    *Relevance:* Mental model for decision-making.

### E. Plan mode / ExitPlanMode

29. **ClaudeLog — "Plan Mode"**
    https://www.claudelog.com/mechanics/plan-mode/
    *Key findings:* "When researching in Plan Mode, Claude may automatically use the Explore Subagent — a Haiku-powered specialist that efficiently searches your [codebase]."
    *Relevance:* Plan mode delegates to subagents (Requirement #6).

30. **Anthropic Claude Code Docs — "Tools reference"**
    https://code.claude.com/docs/en/tools-reference
    *Key findings:* "ExitPlanMode: Presents a plan for approval and exits plan mode." Defined as a tool Claude can call.
    *Relevance:* Requirement #6.

31. **Claude Directory — "Claude Code Plan Mode (2026)"**
    https://claudedirectory.org/blog/claude-code-plan-mode-guide
    *Key findings:* "Exiting Plan Mode is an explicit gate. You exit Plan Mode and ask the main agent to dispatch four subagents, one per component, each with isolation: worktree." Plan mode → subagent fan-out is the recommended exit pattern.
    *Relevance:* Workflow vs Plan mode handoff.

32. **Anthropic Claude Code Docs — "permission modes" / settings**
    https://code.claude.com/docs/en/settings
    *Key findings:* Plan mode settings block bypassPermissions; ultracode does not bypass plan mode but may run inside an auto-accept workflow.
    *Relevance:* Ultracode vs plan mode gating (Requirement #6).

### F. External orchestrators

33. **LetsDataScience — "AI Agent Frameworks 2026: LangGraph vs CrewAI & More"**
    https://letsdatascience.com/blog/ai-agent-frameworks-compared
    *Key findings:* "LangGraph leads on production maturity and persistence. OpenAI Agents SDK leads on simplicity. Claude Agent SDK leads on lifecycle control."
    *Relevance:* Framework comparison (Requirement #7).

34. **Alicelabs — "Best AI Agent Frameworks 2026: 7 Production-Tested Rankings"**
    https://alicelabs.ai/en/insights/best-ai-agent-frameworks-2026
    *Key findings:* "LangGraph #1 for complex stateful workflows, Claude Agent SDK #2 for Anthropic-native production agents (the framework that powers Claude Code)."
    *Relevance:* Claude Agent SDK positioning.

35. **OpenAI Developers — "Subagents – Codex"**
    https://developers.openai.com/codex/subagents
    *Key findings:* "Codex can run subagent workflows by spawning specialized agents in parallel and then collecting their results in one response." Equivalent to Claude Code subagents.
    *Relevance:* Requirement #7.

36. **OpenAI — "An open-source spec for Codex orchestration: Symphony"**
    https://openai.com/index/open-source-codex-orchestration-symphony/
    *Key findings:* OpenAI released Symphony in April 2026 — turns issue trackers into always-on agent systems. External orchestration layer for Codex.
    *Relevance:* Requirement #7.

37. **OpenCode Docs — "Agents"**
    https://opencode.ai/docs/agents/
    *Key findings:* "Subagents are specialized assistants that primary agents can invoke for specific tasks. OpenCode comes with three built-in subagents, General, Explore, and [plan]." OpenCode's primary/subagent model is the model Claude Code's subagent pattern most closely resembles.
    *Relevance:* Requirement #7.

38. **Augment Code — "9 Open-Source Agent Orchestrators for AI Coding (2026)"**
    https://www.augmentcode.com/tools/open-source-agent-orchestrators
    *Key findings:* Compares Hermes Kanban-style multi-agent boards. Orchestrators that "run multiple AI coding agents in parallel across isolated git worktrees."
    *Relevance:* Requirement #7.

39. **Agent Kanban — "Orchestrate AI Coding Agents on a Kanban Board"**
    https://agent-kanban.dev/
    *Key findings:* "Open-source multi-agent orchestration board for Claude Code, Codex, Gemini CLI, GitHub Copilot, and Hermes. A leader agent plans and assigns — worker agents [execute]." Direct competitor for ultracode-style orchestration across CLIs.
    *Relevance:* Requirement #7.

40. **Dev.to — "Porting Claude Code's Agent Teams to OpenCode"**
    https://dev.to/uenyioha/porting-claude-codes-agent-teams-to-opencode-4hol
    *Key findings:* "Claude Code shipped the concept in early February 2026. We built our own implementation in OpenCode — same idea, different architecture."
    *Relevance:* Agent Teams portability.

41. **kenhuangus Substack — "Tool Orchestration and Execution (Claude Code vs. Hermes Agent)"**
    https://kenhuangus.substack.com/p/chapter-5-tool-orchestration-and
    *Key findings:* Tool orchestration = the layer between "the model wants to call 5 tools" and "those 5 tools actually execute safely."
    *Relevance:* Cross-tool positioning.

42. **DevsDigest — "Claude Agent SDK vs LangGraph: Choosing Your Agent Stack in 2026"**
    https://www.developersdigest.tech/blog/claude-agent-sdk-vs-langgraph
    *Key findings:* "LangGraph is a low-level orchestration runtime. Both are production-grade in mid-2026. Use LangGraph when you need explicit control." Claude Agent SDK is the framework under Claude Code.
    *Relevance:* Framework selection (Requirement #7, #8).

---

## Local Sources

None (per task spec — local repo is HermesDesktop's own apps/desktop, unrelated to ultracode docs).

---

## Summary

Claude Code in mid-2026 exposes five first-party orchestration mechanisms plus a non-orchestration planning mode, and ultracode is a meta-control that auto-selects among them. Resolving the conflicting claim in CONTEXT: per Anthropic's own platform docs (`platform.claude.com/docs/en/build-with-claude/effort`), **ultracode is a session-wide setting, not a model and not an API endpoint** — it pins reasoning effort to `xhigh` and grants Claude standing permission to launch multi-agent workflows. The "Mark Kashef 6 dynamic workflows" (deep-research, codebase-audit, large-migration, test-generation, documentation, cross-check) are the bundled templates exposed under v2.1.154+; ultracode auto-picks the appropriate template per task instead of forcing the user to invoke one. The 1000-subagent cap is shared across all dynamic-workflow invocations (Marktechpost).

The decision matrix is best read as a 2×2 plus two outliers. **(1) Subagents** are the workhorse: parent/child hierarchy, single-context, never talk to each other — pick these for any task that fits in a fresh context window and can be decomposed up front (Alex Op, code.claude.com/docs/en/agent-teams). **(2) Agent Teams** (shipped Feb 2026) add a peer-network, shared task list, and direct inter-agent messaging — use them when subtasks need explicit handoffs or external memory, not just one-way report-to-main. **(3) Dynamic Workflows** (with or without ultracode) are a single adaptive agent that reorders, backtracks, and self-directs — the right choice when subtasks are interdependent, scope is unknown upfront, or you need one auditable execution trace (MindStudio). **(4) `/goal`** (launched May 13, 2026, requires v2.1.139+, per code.claude.com/docs/en/goal) is orthogonal: it sets a *completion condition* and lets Claude iterate; it does not parallelize, it just removes babysitting. **(5) Skills** (SKILL.md) are content loaded into the current context — orthogonal to all the above; a workflow *can* invoke a skill, but skills themselves do not orchestrate. **(6) Plan mode** is a read-only pre-execution phase, not an orchestrator; Claude typically delegates Plan-mode research to an Explore subagent (ClaudeLog), and the user exits via the `ExitPlanMode` tool to a normal session that may then dispatch subagents, an agent team, or a workflow. Ultracode does not bypass plan mode — you can still be in plan mode with ultracode set; it just makes the eventual execution more aggressive.

Against the external landscape, the picture is sharper. **LangGraph** is the production standard for stateful, auditable orchestration when you need explicit control over the graph; the "Hands-On Playbook" article concedes three jobs where LangGraph still wins over Claude dynamic workflows (long-running, externally-triggered, or visually-debugged flows). **CrewAI** and **AutoGen/AG2** are role-driven and conversational respectively, but Microsoft killed AutoGen on April 7, 2026 — most "AutoGen vs CrewAI" tutorials are now stale. **OpenAI's Codex subagents** (developers.openai.com/codex/subagents) and **Symphony** (open-source issue-tracker orchestrator, Apr 2026) offer functionally equivalent primitives but with different lifecycles. **OpenCode's primary+subagent model** (opencode.ai/docs/agents) ships three built-in subagents (General, Explore, plan) and is the closest functional mirror of Claude Code's pre-ultracode subagent system. **Hermes Kanban / Agent Kanban** sit *above* Claude Code entirely — they orchestrate a board of agents (Claude Code, Codex, Gemini, Copilot, Hermes) with a leader/worker pattern across git worktrees; this is the right tool when the question is "which CLI?" rather than "which Claude Code feature?".

**Selection criteria for ultracode vs alternatives:** Turn on ultracode when the task is a "senior-engineer-spends-an-hour-planning" problem (MindStudio), the codebase is large enough that one adaptive trace beats a hand-decomposed plan, and you have clean git state to checkpoint against. Skip ultracode for short single-file edits, anything under a few hundred lines, or work that needs a different model in the loop. Use **explicit dynamic workflows** (`/deep-research`, `/codebase-audit`, etc.) when you know the shape upfront and want the bundled template's cross-checks. Use **subagents** when you want predictable, one-shot, context-clean parallelism. Use **agent teams** when the work needs two-way coordination. Use **`/goal`** for "keep going until this condition is met" autonomous loops. Use **plan mode** before any of them when the requirements are unclear. Use **LangGraph/CrewAI/Codex** when the orchestrator itself must be the product, not the helper. The single biggest "ultracode tax" to be aware of is the 1000-subagent cap and the 5-hour session limit — Reddit users have reported burning ~50M tokens in a 30-minute `/deep-research` run and timing out, so ultracode is not a free upgrade; it is a deliberate commitment to a long, expensive session.
