# Findings: Feature Mechanics

Thread: 01 — Claude Code `ultracode` & Dynamic Workflows
Aspect: Feature mechanics (lifecycle, script generation, subagent fan-out, parallelism caps, cross-checked parallelism, context sharing, deterministic reruns)
Compiled: 2026-06-26

## Web Sources

### 1. Anthropic — *Orchestrate subagents at scale with dynamic workflows* (primary docs)
- URL: https://code.claude.com/docs/en/workflows
- Authoring: Anthropic (official Claude Code documentation)
- Key findings:
  - A **dynamic workflow is a JavaScript script** that orchestrates subagents at scale. Claude writes the script from the task description; a runtime executes it in the background so the session stays responsive.
  - Lifecycle: every run writes its script to a file under the session directory in `~/.claude/projects/`. The path is returned to Claude, so users can `diff`, edit, and relaunch from the edited file (iterative edit-and-rerun is first-class).
  - **Hard runtime constraints** (Behavior and limits table): no mid-run user input (only agent permission prompts can pause a run), no direct filesystem or shell access from the workflow itself (agents do that), **up to 16 concurrent agents** (fewer on low-CPU machines), and **1,000 agents total per run** to prevent runaway loops.
  - Approval flow varies by permission mode: Default/acceptEdits prompts every run; Auto prompts only on first launch (and is skipped entirely when `ultracode` is on); bypassPermissions / `claude -p` / Agent SDK never prompt.
  - Subagents inside a workflow always run in `acceptEdits` mode and inherit the user's tool allowlist regardless of the parent session's permission mode; file edits are auto-approved.
  - Built-in workflow: `/deep-research <question>` — fans out web searches, fetches and cross-checks sources, votes on each claim, and returns a cited report with non-surviving claims already filtered out.
  - The four parallelism primitives are contrasted in a table: subagents (Claude decides, context holds results), skills (Claude follows instructions, context holds results), agent teams (lead agent supervises, shared task list), workflows (script decides, script variables hold results, **resumable in the same session**, **dozens to hundreds of agents per run**).
  - Resumability: if you stop a run, completed `agent()` calls return cached results and the rest run live. Resume works **within the same Claude Code session** — if you exit Claude Code while a workflow is running, the next session starts fresh.
  - The "How a workflow runs" section makes the key architectural point explicit: *"The workflow runtime executes the script in an isolated environment, separate from your conversation. Intermediate results stay in script variables instead of landing in Claude's context."* The runtime tracks each agent's result as the run progresses, which is what makes a run resumable.
- Relevance: **primary source for all of the mechanics questions.** This is the canonical description of script form, lifecycle, agent caps, resume semantics, and what "harness" means in this context.

### 2. Anthropic — *Introducing dynamic workflows* (official launch blog post)
- URL: https://claude.com/blog/introducing-dynamic-workflows-in-claude-code
- Authoring: Thariq Shihipar / Sid Bidasaria, Anthropic engineering (May 28, 2026; now generally available)
- Key findings:
  - One-paragraph architecture summary: *"Claude dynamically writes orchestration scripts that run tens to hundreds of parallel subagents in a single session, checking its work before anything reaches you."* This is the source of the "check before it reaches you" claim that downstream articles paraphrase.
  - Two ways to start: (1) ask Claude to create a workflow directly, or (2) turn on the new Claude Code-specific `ultracode` setting, accessible from the effort menu — it sets effort to `xhigh` and lets Claude auto-decide when to use a workflow.
  - Convergence loop: *"Agents address the problem from independent angles, other agents try to refute what they found, and the run keeps iterating until the answers converge."* This is the **cross-checking mechanism** in Anthropic's own words — adversarial refutation + convergence.
  - Persistence: *"Progress is saved as the run goes, so a job that's interrupted picks up where it left off instead of starting over. Because the coordination happens outside the conversation, the plan stays on track no matter how big the task gets."* — confirms that the script/journal is durable inside the session.
  - Concrete workload data: the Bun port from Zig to Rust by Jarred Sumner: **~750,000 lines of Rust, 11 days, 99.8% test pass rate.** One workflow mapped Rust lifetimes per Zig struct field; the next wrote every `.rs` file as a behavior-identical port (hundreds of agents parallel, two reviewers per file); a fix loop drove build+test until both ran clean.
  - Plan availability: on by default for Max/Team/Enterprise/API; Pro users enable in `/config`.
- Relevance: canonical description of the cross-checked parallelism model and the convergence loop, plus the load-bearing example for scale claims.

### 3. Anthropic — *A harness for every task: dynamic workflows in Claude Code* (companion engineering post)
- URL: https://x.com/trq212/status/2061907337154367865 (Thariq Shihipar tweet linking to claude.com blog post) and https://www.scribd.com/document/1047968480/A-Harness-for-Every-Task-Dynamic-Workflows-Part2 (mirrored text)
- Authoring: Thariq Shihipar & Sid Bidasaria, Anthropic (June 2, 2026)
- Key findings:
  - Defines **"harness"** as the scaffolding around the model — *"the part that decides how a task is planned, divided, checked, and executed."* Dynamic workflows generalize the harness: instead of Anthropic hand-building a harness per scenario, **Claude writes one on the fly for the task you describe**, then a runtime executes it. The harness becomes a **first-class, reusable artifact: a script you can read, rerun, edit, and share.**
  - Contrasts **static vs dynamic harnesses**: with the Claude Agent SDK or `claude -p` you can already coordinate multiple Claude Code instances, but a static workflow has to handle every edge case up front, so it tends to be generic. A dynamic workflow is written for the one task at hand, so it can be specific.
  - Identifies three failure modes a dynamic harness fixes in a single context window: (1) **agentic laziness** — quitting early on a long list, (2) **self-preferential bias** — grading its own work too generously, and (3) **goal drift** — losing the thread as the conversation gets long enough to need compaction. Each fix maps onto a workflow pattern: parallelism kills laziness, independent verification agents kill self-bias, short isolated windows kill goal drift.
  - Notes dynamic workflows ship alongside Claude Opus 4.8, "which is finally intelligent enough to author a custom harness rather than just run inside a generic one."
- Relevance: the most direct official definition of **what a "harness" means in this context**, plus the three failure modes that justify the design.

### 4. Anthropic — *Agent SDK reference: TypeScript — `Workflow` tool*
- URL: https://code.claude.com/docs/en/agent-sdk/typescript
- Authoring: Anthropic (official Agent SDK docs)
- Key findings:
  - The **Workflow tool** is available in Agent SDK v0.3.149+ and runs a dynamic workflow: a script that orchestrates many subagents in the background and returns one consolidated result.
  - Typed input shape: `WorkflowInput = { script?, name?, scriptPath?, args?, resumeFromRunId? }`. At least one of `script`, `name`, or `scriptPath` is required.
  - The `script` field is documented as an **inline workflow script that must begin with `export const meta = { name, description, phases }` as a literal**, followed by the body using `agent()`, `parallel()`, `pipeline()`, and `phase()`.
  - `scriptPath` is described as: *"Every invocation persists its script and returns the path in the result, so you can edit that file and re-invoke with the same `scriptPath` to iterate."* This is the public guarantee behind the iterative edit-and-rerun workflow.
  - `resumeFromRunId` is documented as: *"Run ID of a prior `Workflow` invocation to resume. Completed `agent()` calls with unchanged inputs return cached results; only changed or new calls run live. Same session only."* This is the **deterministic rerun guarantee** in API form.
  - `args` is the input the script reads as the global `args`; arrays/objects are passed as actual JSON values, not JSON-encoded strings.
- Relevance: the SDK exposes the workflow as a real tool, names the script-level primitives, and codifies the persistence + resume contract.

### 5. Anthropic — `claude-code/CHANGELOG.md` (effort levels, ultracode keyword rename)
- URL: https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md (mirrored at https://cc.bruniaux.com/guide/claude-code-releases/)
- Key findings:
  - v2.1.154 — *"Introducing dynamic workflows: ask Claude to create a workflow and it orchestrates work across tens to hundreds of agents in the background, so you can take on larger, more complex tasks. Run `/workflows` to view your runs."*
  - v2.1.160 — *"Renamed the dynamic-workflow trigger keyword from `workflow` to `ultracode`. The word 'workflow' no longer triggers a run; asking for one in your own words still works. The trigger keyword is highlighted in violet in the prompt input."* This is when the keyword that triggers a one-off workflow without changing session effort got its current name.
  - v2.1.160 — *"Fixed `/effort ultracode` incorrectly blaming the dynamic workflows setting when the model cannot run xhigh; ultracode is no longer offered on models that do not support it."* Confirms the **gating rule**: ultracode effort requires the model to support `xhigh`; on other models the `/effort` menu simply does not offer it.
  - v2.1.178 — saving a workflow to the project location writes to the closest `.claude/workflows/` directory between the working directory and the repository root (multi-monorepo support).
  - Recent fix — *"Fixed Workflow tool agent() subagents missing per-agent attribution headers."* Indicates the runtime injects an attribution header into each spawned subagent's context.
- Relevance: pins down version-gated behavior, the keyword-rename history (important — older tutorials use `workflow:` not `ultracode:`), and the model-gating rule for `ultracode` effort.

### 6. GitHub — *Support ultracode effort level and dynamic workflows #725* (ACP client-spec issue)
- URL: https://github.com/agentclientprotocol/claude-agent-acp/issues/725
- Key findings:
  - Third-party client-protocol tracker reporting: *"Claude Code v2.1.154+ introduced ultracode — a new effort level that combines xhigh reasoning with automatic workflow orchestration via dynamic workflows."* Useful as an independent confirmation of the launch pairing.
- Relevance: corroborates the v2.1.154 / Opus 4.8 pairing and gives an outside date stamp.

### 7. alexop.dev — *Claude Code Workflows: Deterministic Multi-Agent Orchestration*
- URL: https://alexop.dev/posts/claude-code-workflows-deterministic-orchestration/
- Authoring: Alex Opalic, independent developer (May 28, 2026)
- Key findings:
  - Built and dissected a real ~130-line workflow (Vue/Nuxt weekly digest) that spawns nine agents in parallel, collects findings, ranks them, and writes a digest. *"Building it taught me how the whole feature fits together."*
  - The "who holds the plan" framing, in his words: *"When you want the control flow itself to be deterministic, not decided turn-by-turn by a model."*
  - Generalizes the script shape to **fan out → reduce → synthesize** with adversarial verifiers.
  - Reproduces a Jarred Sumner quote crediting dynamic workflows + adversarial code review for the Bun port in 6 days.
  - Notes the workflow is a plain JS script with deterministic loops; agents do the LLM work.
- Relevance: the cleanest independent re-implementation/explanation of the script + agent division of labor, and the only third-party source that actually posted working code.

### 8. BuildThisNow — *Claude Code Dynamic Workflows: How to Orchestrate 1,000 Subagents on a Real Codebase*
- URL: https://www.buildthisnow.com/blog/guide/development/claude-code-dynamic-workflows
- Authoring: BuildThisNow (May 30, 2026; updated Jun 14, 2026)
- Key findings:
  - Explicitly enumerates the **six script primitives**: `agent(prompt, opts?)` (single subagent, returns text or validated JSON), `parallel(thunks)` (concurrent array with a synchronization barrier), `pipeline(items, ...stages)` (streaming, no barrier), `workflow(nameOrRef, args?)` (call a saved workflow as a sub-step, **one nesting level max**), `phase(title)` (name a progress group in the `/workflows` dashboard), `log(msg)` (narrator line in workflow output).
  - Shows the **minimum valid script skeleton**: starts with `export const meta = { name: 'security-audit', description: '…' }`.
  - Concise definition of "dynamic" here: *"Claude decides the task decomposition, the agent count, the phasing strategy, and the verification approach in real time for the specific task you described. No two workflow scripts are identical."*
  - States the resume mechanism: *"the resume cache journals every agent() call by its deterministic key."* This is the explicit technical claim for **deterministic rerun / replay**.
  - Hard caps restated: 16 concurrent (local resource cap) + 1,000 total per run (runaway-loop cap).
- Relevance: the most thorough third-party enumeration of script primitives, with the determinism-of-rerun claim grounded in the resume journal.

### 9. LushBinary — *A Harness for Every Task: Claude Code Dynamic Workflows Explained*
- URL: https://lushbinary.com/blog/claude-code-harness-every-task-dynamic-workflows-guide/
- Authoring: LushBinary (June 3, 2026)
- Key findings:
  - Re-analyzes the Anthropic "harness for every task" post and **enumerates six orchestration patterns** Claude composes (parallel fan-out, sequential pipeline, map-reduce over files, adversarial verify, planner–executor split, and cross-checked synthesis).
  - Reaffirms the three failure modes (agentic laziness, self-preferential bias, goal drift) and the design that each pattern fixes.
  - Restates the 1,000-agent per-run limit, 16-concurrent cap, and the ultracode trigger.
  - Use cases: explicitly non-coding — research, security analysis, data labeling, multi-document summarization.
- Relevance: best third-party catalog of the "six orchestration patterns" the docs only hint at; good cross-reference for the harness definition.

### 10. claudefa.st — *Ultracode in Claude Code: Effort Setting Explained*
- URL: https://claudefa.st/blog/guide/development/ultracode
- Key findings:
  - Quotes the canonical Anthropic definition verbatim: *"Ultracode is a Claude Code setting rather than a model effort level: it sends xhigh to the model and additionally has Claude orchestrate dynamic workflows for substantive tasks. It applies to the current session only."*
  - Distills the three failure modes a workflow fixes into a single clean sentence each; documents that ultracode is per-session and resets when a new one starts.
  - Compares ultracode against `xhigh`, `max`, and `ultrathink` — `ultracode` is the only one that *also* flips on auto-workflow orchestration.
- Relevance: the cleanest side-by-side of ultracode vs the other effort levels, useful for the comparative-mechanics writeup.

### 11. Reddit r/ClaudeAI — *Introducing dynamic workflows in Claude Code* (mod announcement)
- URL: https://www.reddit.com/r/ClaudeAI/comments/1tq9ofy/introducing_dynamic_workflows_in_claude_code/
- Date: May 28, 2026
- Key findings:
  - r/ClaudeAI moderator announcement cross-posting the Anthropic blog with the practical instructions: *"At the start of the session type in `/effort` and you can select `ultracode`, Claude then spins up those dynamic workflows whenever it thinks it..."* (truncated; consistent with the official docs).
- Relevance: useful as a launch-day timestamp and a non-official reproduction of the ultracode activation flow.

### 12. Reddit r/ClaudeCode — *Asked Claude Code for a "deep search" in ultracode mode*
- URL: https://www.reddit.com/r/ClaudeAI/comments/1tsqezk/asked_claude_code_for_a_deep_search_in_ultracode/
- Date: May 31, 2026
- Key findings:
  - User reports a real ultracode-mode run on a "deep search" task and shares a `claude-code-dynamic-workflows-guide`. Even though the thread body is behind Reddit's JS challenge, the linked guide is the de facto community reference and the post itself confirms a working run on a real codebase.
- Relevance: independent user-level validation that ultracode+workflows produces actual agent fan-out in practice (not just docs).

### 13. Reddit r/ClaudeCode — *Why 4.8 feels broken if you're not running dynamic workflows*
- URL: https://www.reddit.com/r/ClaudeCode/comments/1tt8mxx/why_48_feels_broken_if_youre_not_running_dynamic/
- Date: May 31, 2026
- Key findings:
  - User-level confirmation: *"4.8 pairs with dynamic workflows: Claude writes a JS orchestration script… ultracode) are Enterprise/Team/Max only. Pro doesn't expose."* Confirms the plan-tier gating for ultracode and the JS-script-orchestration claim.
  - Also restates that Pro users have to enable workflows from `/config` rather than getting them by default.
- Relevance: independent tier-gating and JS-script-orchestration confirmation.

### 14. Reddit r/ClaudeCode — *Ultracode just blew my mind*
- URL: https://www.reddit.com/r/ClaudeCode/comments/1uax5yy/ultracode_just_blew_my_mind/
- Key findings:
  - Quantitative observation: *"Claude code consumed 800K tokens in 15 minutes using the latest ultracode and dynamic workflows."* Useful real-world data point for the cost section.
- Relevance: empirical cost data for an ultracode run.

### 15. Reddit r/ClaudeWorkflows — *Leveraging Claude Code's Dynamic Workflows for Large-Scale Code Tasks*
- URL: https://www.reddit.com/r/ClaudeWorkflows/comments/1tqc0m8/workflow_leveraging_claude_codes_dynamic/
- Date: May 28, 2026
- Key findings:
  - Reproduces the official how-to: enable ultracode or ask for a workflow; the run *"is checkpointed and can be resumed."*
- Relevance: independent confirmation of resumability from a community subreddit dedicated to the feature.

### 16. Reddit r/ClaudeCode — *Introducing dynamic workflows in Claude Code* (r/ClaudeCode cross-post)
- URL: https://www.reddit.com/r/ClaudeCode/comments/1tq9pge/introducing_dynamic_workflows_in_claude_code/
- Key findings:
  - Cross-post of the launch post; the mod comment confirms two trigger paths: explicit ask or `ultracode` setting.
- Relevance: launch-day distribution signal.

### 17. LinkedIn (Nikhil Kassetty) — *Automating with Claude Code: Dynamic Workflows and Parallel Sub-agents*
- URL: https://www.linkedin.com/posts/nikhil-kassetty-905928137_claudecode-softwareengineering-aiagents-activity-7467198446031028224--WMk
- Date: June 1, 2026
- Key findings:
  - Condensed but useful framing: *"In Claude Code, a dynamic workflow spawns subagents, runs them in [parallel]… resume" and a task spec. The workflow is logged and replayable."* Reinforces the "logged + replayable" + resume framing.
- Relevance: external restatement of the resume-and-replay contract.

### 18. LinkedIn (Asshutossh) — *Ultracode vs Claude: Understanding Effort and Orchestration*
- URL: https://www.linkedin.com/posts/asshutossh_i-ran-ultracode-for-3-days-assuming-it-was-activity-7467205890107117568--pLB
- Date: June 1, 2026
- Key findings:
  - Three-day usage report on ultracode. Restates that *"Subagents inside that workflow always run in acceptEdits mode regardless of your session setting"* and that *"Claude Code already externalizes the agentic loop."*
- Relevance: real-user confirmation of the `acceptEdits`-for-subagents detail and the per-run approval flow.

### 19. Medium / alirezarezvani — *Claude Code Workflows: Build Deterministic Agent Runs*
- URL: https://alirezarezvani.medium.com/claude-code-workflows-build-deterministic-agent-runs-eaf2c6ac52d5
- Date: June 1, 2026
- Key findings:
  - Explains the **session-boundary limit** of resume: *"Resume only works inside the same session. If you quit Claude Code while a workflow is running, the next session starts it from scratch."* Provides a workaround pattern (save and re-invoke via `scriptPath`).
- Relevance: independent restatement of the session-boundary contract.

### 20. Substack (Tyler Folkman) — *Claude Code Workflows Are Here. Don't Use Them Like an Intern*
- URL: https://tylerfolkman.substack.com/p/claude-code-workflows-are-here-dont
- Date: June 7, 2026
- Key findings:
  - Argues the shift: *"Claude Code's new Dynamic Workflows fix a real problem: they move orchestration out [of the model]… resume after interruption. That is powerful."*
- Relevance: second-source framing of the architecture shift.

### 21. Medium / Data Science Collective — *What Makes Claude's New Dynamic Workflows Different*
- URL: https://medium.com/data-science-collective/claudes-new-dynamic-workflows-changed-how-i-think-about-ai-coding-e1dc7649e516
- Date: May 29, 2026
- Key findings:
  - *"On May 28, Anthropic shipped a feature in Claude Code called dynamic workflows. You describe a task, and Claude writes a JavaScript [orchestration script]."* Useful concise restatement.
- Relevance: third-party restatement of the script-generation model.

### 22. Substack (levelup.gitconnected) — *Claude's Dynamic Workflows: The Hands-On Playbook (and the three jobs where LangGraph still wins)*
- URL: https://levelup.gitconnected.com/claudes-dynamic-workflows-the-hands-on-playbook-and-the-three-jobs-where-langgraph-still-wins-ab44b85a70ee
- Date: June 3, 2026
- Key findings:
  - Important boundary condition: *"Dynamic Workflows is ephemeral — when the Claude Code session ends, the orchestration evaporates. A workflow you'd schedule as a weekly cron [isn't a workflow]."* This is the cleanest articulation of the **session-scoped lifetime** of the orchestration state itself (as opposed to the script file, which persists).
- Relevance: best external statement of the lifetime boundary between the script (persists) and the orchestration state (dies with the session).

### 23. Medium (No Time) — *Dynamic Workflows vs /goal in Claude Code: What's the Real Difference*
- URL: https://medium.com/no-time/dynamic-workflows-vs-goal-in-claude-code-whats-the-real-difference-24f828b4a4ed
- Date: June 4, 2026
- Key findings:
  - *"Ultracode is the effort setting that activates dynamic workflows automatically. It's not a separate model or a different API endpoint. It's a [session-level toggle]."* Cleanest disambiguation of ultracode from a model or endpoint.
- Relevance: clarifies what ultracode is *not* (not a model, not an API).

### 24. GitHub QwenLM/qwen-code #4721 — *Port Dynamic Workflows / Ultracode from Claude Code 2.1.168*
- URL: https://github.com/QwenLM/qwen-code/issues/4721
- Date: June 2, 2026
- Key findings:
  - Reverse-engineering notes from a competing-agent maintainer. Confirms internal symbol names and telemetry markers: *"Resume telemetry, tengu_workflow_journal_started_hit_respawn"* — the **internal codename for the resume journal is `tengu_workflow_journal`**, and the respawn-hit telemetry proves the deterministic-key caching is observable.
  - Documents a minimum reproduction: *"Minimal Workflow tool: node:vm sandbox + sequential agent() + phase() … parallel() / pipeline() run concurrently with cap enforcement."* — i.e., the runtime is a **Node `vm` sandbox** with **sequential agent() by default and parallel()/pipeline() with cap enforcement**.
  - References Claude Code 2.1.168 as the deltas-vs-2.1.160 baseline.
- Relevance: **gold-mine for internals** — gives the runtime sandbox type (node:vm), the cap-enforcement implementation hint, and the internal telemetry names for the resume journal.

### 25. LangChain forum — *Re-Implement Claude Code's dynamic workflow using LangChain DeepAgents*
- URL: https://forum.langchain.com/t/re-implement-claude-codes-dynamic-workflow-using-langchian-deepagents/3846
- Date: June 2, 2026
- Key findings:
  - Community re-implementation listing the primitives *"on a ctx: agent() (one leaf in a fresh context), parallel() … phase() / log(), a shared token [counter]"* — corroborates BuildThisNow's primitive list and adds the detail that `agent()` is **a leaf in a fresh context** (i.e., each agent gets a clean context window).
  - Confirms a shared token/cost counter is plumbed into the runtime.
- Relevance: independent confirmation of the "fresh context per agent" semantics and the shared token counter.

### 26. GitHub six-ddc/codex-dynamic-workflows/CLAUDE.md
- URL: https://github.com/six-ddc/codex-dynamic-workflows/blob/main/CLAUDE.md
- Key findings:
  - A port/clone repository whose own CLAUDE.md states the architectural axiom: *"The 'orchestrator' is deterministic code (the workflow script), not an LLM. Subagents are isolated and stateless."* Clean third-party articulation of the **determinism property**: the orchestrator is the script, not a model.
- Relevance: external codification of the "script is the orchestrator" property.

### 27. arXiv — *Dive into Claude Code: The Design Space of Today's and Future AI Agents*
- URL: https://arxiv.org/html/2604.14228v1
- Date: April 16, 2026 (pre-dates workflows but the architectural framing applies)
- Key findings:
  - Academic analysis: *"The architecture invests in deterministic infrastructure (context [management]) … Claude Code's context management pipeline is specifically designed to [preserve determinism]."* Useful for the "why a deterministic script orchestrator?" question — the model context is treated as non-deterministic state, the runtime is the deterministic spine.
- Relevance: independent academic framing of the determinism split.

### 28. InfoQ — *Claude Code Adds Dynamic Workflows for Parallel Agent Orchestration*
- URL: https://www.infoq.com/news/2026/06/dynamic-workflows-claude-code/
- Date: June 1, 2026
- Key findings:
  - News write-up confirming the launch date (May 28, 2026) and the multi-tier availability (Pro, Max, Team, Enterprise; Bedrock/Vertex/Foundry).
- Relevance: independent launch timestamp and availability matrix.

### 29. Substack (Ken Huang) — *CLAUDE CODE ORCHESTRATION*
- URL: https://kenhuangus.substack.com/p/claude-code-orchestration-dynamic
- Date: May 28, 2026
- Key findings:
  - *"Claude Code's orchestration model shatters this constraint. With three distinct collaboration primitives — Dynamic Workflows, Subagents, and [Teams]."* Useful for a high-level comparative anatomy.
- Relevance: third-party comparative restatement.

### 30. Hidekazu Konishi — *Claude Code Subagents and Multi-Agent Orchestration Guide*
- URL: https://hidekazu-konishi.com/entry/claude_code_subagents_and_orchestration_guide.html
- Date: June 7, 2026
- Key findings:
  - Documents that subagents inside a workflow inherit the parent's tool allowlist but run in `acceptEdits` mode regardless of the session's permission mode; *"The subagent's prompt replaces the default Claude Code system prompt entirely, and the choice persists when you resume the session."* — confirms the per-agent prompt replacement and the resume-of-persistence property.
- Relevance: restates the agent-prompt-replacement mechanic and the per-agent inheritance rules.

## Local Sources

None. Dynamic workflows / `ultracode` are a closed Anthropic product feature. The relevant local artifact would be a saved workflow file under `.claude/workflows/` or a session journal under `~/.claude/projects/`, which only exists in a user's local checkout at runtime — not something to scan for this research pass. (No such files exist in the parent workspace at `/home/cage/Desktop/Workspaces/HermesDesktop`; the only `md/` file present is the steering document `00-research-ultracode-workflows-steering.md`.)

## Summary

A **dynamic workflow** in Claude Code is a JavaScript orchestration script that the model itself writes on demand, which a background runtime then executes in an isolated environment to coordinate tens to hundreds of subagents. Its lifecycle has four phases. **(1) Trigger.** The user starts a workflow one of three ways: typing the keyword `ultracode:` in a prompt (a one-off, violet-highlighted; the keyword was renamed from `workflow:` in v2.1.160), invoking a saved workflow command like `/deep-research`, or setting the session's effort level to `ultracode` via `/effort ultracode` — the latter pins the per-message reasoning effort to `xhigh` and additionally turns on automatic workflow planning, so Claude decides on its own when to fan out (gated: only available on models that support `xhigh`). **(2) Script generation.** Claude writes the script. The minimum legal form is `export const meta = { name, description, phases } = {…}` at the top, followed by a body that uses six primitives exposed in the runtime: `agent(prompt, opts?)` (spawn one subagent in a fresh context and return its final text or validated JSON), `parallel(thunks)` (run a list concurrently with a synchronization barrier), `pipeline(items, …stages)` (stream items through stages with no barrier), `workflow(nameOrRef, args?)` (call a saved workflow as a sub-step, one nesting level deep), `phase(title)` (label a progress group for the `/workflows` dashboard), and `log(msg)` (narrator line). The runtime executes the script inside a Node `vm` sandbox, separate from the conversation. **(3) Fan-out & cross-checked parallelism.** The script does the orchestration: it loops, branches, and synchronizes. Claude does the LLM work inside each `agent()` call. The runtime enforces two hard caps: **16 concurrent agents** (to bound local resource use; fewer on low-CPU machines) and **1,000 agents total per run** (to prevent runaway loops). This is the 1,000-subagent cap. Subagents always run in `acceptEdits` mode and inherit the user's tool allowlist regardless of the parent session's permission mode, so file edits are auto-approved mid-run. The **cross-checking mechanism** is the convergence loop Anthropic describes: independent agents answer from independent angles, separate agents adversarially try to refute those findings, and the run iterates until the answers converge. Intermediate results live in script variables, not in the conversation, so the main context window stays free of partial outputs. **(4) Resume / rerun.** Every invocation persists its script to a file under `~/.claude/projects/` and returns that path, so users can read it, diff it against prior runs, edit it, and re-invoke via the same `scriptPath`. Internally, the resume machinery is a journal (`tengu_workflow_journal`, per QwenLM's reverse-engineering notes) that records every `agent()` call by a deterministic key; on `resumeFromRunId`, calls with unchanged inputs return cached results and only changed or new calls run live. This is the **deterministic rerun guarantee**: same inputs, same cached outputs, only deltas run. The boundary is sharp — resume is **session-scoped**: if you exit Claude Code while a workflow is running, the next session starts the workflow from scratch (though the saved script file on disk survives). The orchestration state itself is ephemeral; only the script and the journal (inside the same session) are durable. Ultracode resets per session, so dropping back to `/effort high` is a one-line escape from the auto-workflow behavior. Together, these properties — script-as-orchestrator, isolated Node-VM runtime, hard cap on concurrent and total agents, fresh-context per agent, deterministic-key resume journal, adversarial verification as a first-class pattern — are the mechanics by which a single `ultracode`-mode prompt turns into a coordinated fleet that can sweep a 750,000-line codebase in days instead of weeks.
