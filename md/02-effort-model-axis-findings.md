# Findings: Effort & Model Axis

Thread 02 of 4 parallel threads on Claude Code `ultracode` & Dynamic Workflows. Aspect: the `/effort` ladder, how `ultracode` sits inside it, model pairing rules (Opus 4.8 / Sonnet 4.6 / Haiku 4.7/4.5), the "x-high" reasoning mode, fast-mode workflow caps, and real pricing/burn reports.

## Web Sources

1. **Effort — Claude Platform Docs (primary, Anthropic)**
   URL: https://platform.claude.com/docs/en/build-with-claude/effort
   Key findings: Authoritative reference for the API `effort` parameter. Lists the accepted values: `low`, `medium`, `high`, `xhigh`, `max`. Documents that "Ultracode pairs the xhigh effort level with standing permission for Claude Code…" — confirming ultracode is a Claude-Code-side setting, not a 6th API value, and that it forces xhigh reasoning. Recommends max for "absolute highest capability with no constraints" and xhigh for agentic coding, tool-heavy workflows, and code generation. Distinct from `low` and `medium`, which it says are best for "simple queries, quick lookups" and "moderate complexity tasks."
   Relevance: Primary source for the full effort ladder and ultracode's exact placement.

2. **What's new in Claude Opus 4.8 — Claude Platform Docs (primary, Anthropic)**
   URL: https://platform.claude.com/docs/en/about-claude/models/whats-new-claude-4-8
   Key findings: Lists new model `claude-opus-4-8`; 1M-token context default; 128k max output; adaptive thinking. Crucially: "The effort parameter default on Claude Opus 4.8 is `high` on all surfaces, including the Claude API and Claude Code" — i.e., the API default is `high`, but the Claude Code default for Opus 4.8 is still effectively xhigh/ultracode. Also confirms Fast Mode is now available for Opus 4.8 as a research preview on the Claude API at "up to 2.5x higher output tokens per second … at premium pricing" (header `fast-mode-2026-02-01`).
   Relevance: Primary source for the effort default split between API and Claude Code, and for fast-mode on Opus 4.8.

3. **Introducing Claude Opus 4.8 — anthropic.com (primary, Anthropic)**
   URL: https://www.anthropic.com/news/claude-opus-4-8
   Key findings: States "Pricing for regular usage is unchanged from Opus 4.7: $5 per million input tokens and $25 per million output tokens. Pricing for fast mode is …" (cut off in search snippet; Labellerr and CloudZero complete the numbers as $10/$50). Dynamic workflows "available in Claude Code for Enterprise, Team, and Max plans."
   Relevance: Primary pricing source; primary confirmation of plan availability for dynamic workflows.

4. **Anthropic Ships Claude Opus 4.8 Alongside Dynamic Workflows and Cheaper Fast Mode, With Workflows Capped at 1,000 Subagents — MarkTechPost**
   URL: https://www.marktechpost.com/2026/05/28/anthropic-ships-claude-opus-4-8-alongside-dynamic-workflows-and-cheaper-fast-mode-with-workflows-capped-at-1000-subagents/
   Key findings: This is the primary press-side anchor for the "1,000 subagent" cap and the ultracode phrase. Quotes: "Ultracode combines xhigh reasoning effort with automatic workflow orchestration." "The feature requires Claude Code v2.1.154 or later. It runs in the CLI, Desktop, and VS Code extension. It is available on Max, Team, and Enterprise plans. It is on by default on Max and Team. On Enterprise it is off until an admin enables it. It also runs on the Claude API, Amazon Bedrock, Vertex AI, and Microsoft Foundry." Headline demo: Jarred Sumner used dynamic workflows to port Bun from Zig to Rust — 750,000 lines, 99.8% test pass rate, 11 days.
   Relevance: Primary reference for the "1,000-subagent" cap (echoed by every downstream article), the ultracode ↔ xhigh + workflows definition, the v2.1.154 cutoff, and the Bun case study.

5. **Ultracode in Claude Code: Effort Setting Explained — ClaudeFast**
   URL: https://claudefa.st/blog/guide/development/ultracode
   Key findings: Quotes Anthropic model-config docs verbatim: "Ultracode is a Claude Code setting rather than a model effort level: it sends `xhigh` to the model and additionally has Claude orchestrate dynamic workflows for substantive tasks. It applies to the current session only." Also: "ultracode is the workflow toggle, left on for the entire session." Shipped in Claude Code v2.1.154 on May 28, 2026, alongside Opus 4.8.
   Relevance: Cleanest paraphrase of the official definition; anchors the "session only" behavior that distinguishes ultracode from xhigh and max (both of which can be saved as defaults).

6. **Model configuration — Claude Code Docs (primary, Anthropic)**
   URL: https://code.claude.com/docs/en/model-config
   Key findings: Authoritative Claude Code settings page. Confirms `/effort [level|auto]` for setting the model effort level. Documents model aliases `haiku`, `sonnet[1m]`, `default`, `opusplan`. Includes "Ultracode is a Claude Code setting rather than a model effort level…" alongside the `Adjust effort level` / `Choose an effort level` sections and the `ultrathink` one-off deep-reasoning keyword.
   Relevance: Primary Claude Code doc; ties the slash command, ultrathink keyword, and ultracode together in one place.

7. **Commands — Claude Code Docs (primary, Anthropic)**
   URL: https://code.claude.com/docs/en/commands
   Key findings: Confirms `/effort [level|auto]` is the canonical slash command. Defines level as `low|medium|high|max` (with `auto` letting Claude pick).
   Relevance: Primary reference for the `/effort` command surface.

8. **Claude Opus 4.8 Effort Levels Explained: Low, Medium, High, Max, and Ultra Code — MindStudio**
   URL: https://www.mindstudio.ai/blog/claude-opus-4-8-effort-levels-explained
   Key findings: States the Opus 4.8 effort ladder as five named levels: Low, Medium, High, Max, and Ultra Code. Explains each as a `budget_tokens` cap on the API's `thinking` parameter. Notes that ultrathink is the per-prompt keyword, not the same as ultracode.
   Relevance: Independent third-party breakdown of the five-level model and what each rungs actually does.

9. **Claude Opus 4.8: Benchmarks, Pricing, and What's New — ClaudeFast**
   URL: https://claudefa.st/blog/models/claude-opus-4-8
   Key findings: "The Claude Code effort dial now exposes four settings: high (default in the Messages API), xhigh (Claude Code default), ultracode (new in 4.8, …)" — confirming the API-vs-Code default split and that ultracode slots in above xhigh/max.
   Relevance: Direct corroboration of the default split and ultracode's position above xhigh.

10. **Claude Opus 4.8 Crushes Coding Benchmarks — Labellerr**
    URL: https://www.labellerr.com/blog/claude-opus-4-8-vs-4-7-comparison/
    Key findings: "Ultracode is defined as xhigh plus workflows, it pairs the highest reasoning effort with dynamic workflows, so Claude plans a large task, spins up hundreds of parallel subagents in one session, and verifies its own outputs." Cites Opus 4.8 benchmarks: SWE-bench Pro 69.2% (vs 64.3% for 4.7), SWE-bench Verified 88.6% (vs 87.6%), Terminal-Bench 2.1 74.6% (vs 66.1%), OSWorld-Verified 83.4% (best-in-class), GDPval-AA 1890, USAMO 2026 96.7% (vs 69.3% — biggest single-cycle jump). Also: "Fast mode pricing is $10/$50 compared to $30/$150 for Opus 4.7's fast tier" — 3x cheaper.
    Relevance: The benchmark numbers; the xhigh + workflows definition; the 4.7 → 4.8 fast-mode price drop.

11. **Claude Opus 4.8: Pricing, benchmarks, and which model to actually run — CloudZero**
    URL: https://www.cloudzero.com/blog/claude-opus-4-8-pricing/
    Key findings: "Dynamic Workflows scale with sub-agent count. A 50-agent session does not cost $25 per million output tokens. It costs $25 per million … [per agent]." A $50 single-agent job can become a $2,500 workflow bill. Cites the same fast-mode 3x-cheaper story and the same Bun port.
    Relevance: Clearest engineering-level treatment of how subagent count multiplies Opus 4.8 bill, which is the direct cost consequence of ultracode.

12. **Fable 5 Effort Levels Explained: low to xhigh, and What They Cost You — DevelopersDigest**
    URL: https://www.developersdigest.tech/blog/fable-5-effort-levels-explained
    Key findings: "The API accepts exactly `low`, `medium`, `high`, `xhigh`, and `max`. Ultracode sends xhigh to the model and adds standing permission for Claude Code …" Reinforces that ultracode is not an API value but a Claude Code value built on top of xhigh.
    Relevance: Independent confirmation of the API/Code split.

13. **Claude Code Fast Mode: When 2.5x Speed Is Worth 2x Price — DevelopersDigest**
    URL: https://www.developersdigest.tech/blog/claude-code-fast-mode-worth-it
    Key findings: Fast mode on Opus 4.8 priced at $10/$50 per MTok; "first-enable context charge" and "separate rate limit pools" — i.e., fast mode is its own billing tier, not just a flag.
    Relevance: Confirms fast mode is a distinct cost tier; relevant to whether workflows can run in fast mode.

14. **Is Claude Fast Mode Worth the Cost? — iBuildWith.ai**
    URL: https://ibuildwith.ai/blog/is-claude-fast-mode-worth-the-cost/
    Key findings: "Claude's fast mode runs the same model 2.5 times faster. It also costs six times as much. Anthropic shipped it in February 2026." First shipped with Opus 4.6; carried to 4.7 and now 4.8.
    Relevance: Background on fast mode's price multiplier (6x) versus speed (2.5x) and launch lineage.

15. **Claude Code Fast Mode: Speed Up Opus 4.6 Responses — ClaudeFast**
    URL: https://claudefa.st/blog/guide/performance/fast-mode
    Key findings: Fast mode is "compatible with 1M extended context," pricing increases above 200K tokens, billed to extra usage only on subscription plans.
    Relevance: Confirms fast-mode interaction with the 200K context pricing tier.

16. **Choosing the right model — Claude Platform Docs (primary, Anthropic)**
    URL: https://platform.claude.com/docs/en/about-claude/models/choosing-a-model
    Key findings: "Claude Opus 4.8, Claude Opus 4.7, and Claude Opus 4.6 support fast mode (research preview), which delivers up to 2.5x higher output speed at premium pricing."
    Relevance: Primary doc pinning fast mode to Opus 4.6/4.7/4.8 (not Sonnet/Haiku).

17. **Introducing Claude Opus 4.7 — anthropic.com (primary, Anthropic)**
    URL: https://www.anthropic.com/news/claude-opus-4-7
    Key findings: "In Claude Code, we've raised the default effort level to xhigh for all plans. When testing Opus 4.7 for coding and agentic use cases, we …" Introduces `/ultrareview` (a dedicated review command), and Task Budgets in public beta.
    Relevance: Establishes the xhigh-as-default-for-Claude-Code history on 4.7, which carried into 4.8.

18. **Claude Code Slash Commands: Run Longer, Cleaner Sessions — DataCamp**
    URL: https://www.datacamp.com/tutorial/claude-code-slash-commands
    Key findings: "The max and ultracode effort levels are session-only and cannot be saved as defaults. … Use /effort xhigh or even /effort max when you …" Confirms ultracode is session-only, like max, but unlike low/medium/high/xhigh.
    Relevance: Behavior difference between ultracode and the other levels (cannot be saved as a default).

19. **Set Opus 4.8 + /ultracode and Watch Claude Code Go Fully Autonomous — Medium (CodeCoup)**
    URL: https://medium.com/@CodeCoup/set-opus-4-8-ultracode-and-watch-claude-code-go-fully-autonomous-ca754b97833e
    Key findings: "You can also set `/effort ultracode`, a new effort level that runs at xhigh and lets Claude decide on its own when a task warrants a dynamic …" Echoes the official xhigh + auto-workflow definition.
    Relevance: Hands-on developer walk-through of the `/effort ultracode` invocation.

20. **Introducing Claude Opus 4.8 Anthropic Ultra Code Workflows — mlearning.substack**
    URL: https://mlearning.substack.com/p/introducing-claude-opus-48-anthropic-ultra-code-workflows-fast-mode-boss-mode-dynamic-40-best-practices-tips-trics-pro
    Key findings: "How Do You Run ULTRACODE Boss Mode? Type `/effort ultracode`. This pins effort to xhigh and lets Claude decide when to fan out a workflow." Same xhigh+orchestration framing.
    Relevance: Tutorial-style confirmation; "Boss Mode" is community nickname for ultracode.

21. **Be careful with Dynamic Workflows and Ultra Code — r/ClaudeAI**
    URL: https://www.reddit.com/r/ClaudeAI/comments/1tquz3d/be_careful_with_dynamic_workflows_and_ultra_code/
    Key findings: User report: "Launched a large product add via workflows with ultracode enabled and one of the subagents went into a loop for 20 minute burning tokens…" Confirms real-world runaway-agent behavior under ultracode.
    Relevance: Concrete failure-mode report; supports the "session-only" and "burn" cost stories.

22. **AAAAND ITS GONE — r/ClaudeCode**
    URL: https://www.reddit.com/r/ClaudeCode/comments/1typi0t/aaaand_its_gone/
    Key findings: Max 5x ($100) user: "One ultracode session with 30 agents, each calling 600 tools — aaand in 20 minutes, the 5h limit is gone." Quantifies burn rate on a mid-tier subscription.
    Relevance: Subscription-tier burn report.

23. **Ultracode is huge — r/ClaudeAI**
    URL: https://www.reddit.com/r/ClaudeAI/comments/1tqcg9t/ultracode_is_huge/
    Key findings: "Running the 'ultracode' once blew through the entire session's capacity and cost me $15~ in credits. I'm only at ~36% of my weekly limit, but …" API-credit burn on a single ultracode invocation.
    Relevance: API-cost data point.

24. **Opus 4.8 with Ultracode is insane! — r/ClaudeAI**
    URL: https://www.reddit.com/r/ClaudeAI/comments/1u841u4/opus_48_with_ultracode_is_insane/
    Key findings: "Ultracode 4.8 can easily burn $1k+ per hour, significantly more than that if it's bringing in 5-10+ opus agents and looping, like ~$2k+/ hour." Worst-case developer cost report; corroborates CloudZero's "sub-agent count multiplies the bill" framing.
    Relevance: Extreme-cost data point for the most aggressive workloads.

25. **Fable 5 is eating my Max 20x plan at 2% per minute — r/claude**
    URL: https://www.reddit.com/r/claude/comments/1u1cwkl/fable_5_is_eating_my_max_20x_plan_at_2_per_minute/
    Key findings: "One prompt this morning burned my entire 20x 4hr usage in less than 5 minutes and didn't even complete the task. It was on Fable 5 ultracode …" Independent of the 1,000-cap story — shows even a single prompt can chew through the Max 20x 4-hour window.
    Relevance: Subscription-tier burn proof.

26. **Opus 4.8: xhigh vs max vs ultracode for planning and executing — r/ClaudeCode**
    URL: https://www.reddit.com/r/ClaudeCode/comments/1uadbiw/opus_48_xhigh_vs_max_vs_ultracode_for_planning/
    Key findings: "Ultracode is faster and uses fewer tokens for my work because it knows to farm off some of the work to cheaper models. It's not a 'maximum…'" Important counterpoint: ultracode can use less total tokens than max because cheaper subagents do the work.
    Relevance: User reports ultracode is not strictly more expensive than max on real tasks.

27. **How to Manage Token Costs in Claude Code Dynamic Workflows — MindStudio**
    URL: https://www.mindstudio.ai/blog/manage-token-costs-claude-code-dynamic-workflows
    Key findings: "Dynamic workflows can burn millions of tokens fast. Learn how to use Haiku sub-agents, scope bounding, and named deliverables to control costs." Establishes that Haiku is the default cheaper subagent inside a workflow, even when the orchestrator is Opus 4.8.
    Relevance: Confirms Haiku as the cost-control subagent in ultracode-driven workflows.

28. **Ultracode for Codex: Claude-style Dynamic Workflows with a Skill — dev.to**
    URL: https://dev.to/pablonax/ultracode-for-codex-claude-style-dynamic-workflows-with-a-skill-3knk
    Key findings: Independent reproduction: "Claude Code added Dynamic Workflows. Dynamic Workflows are a way to run larger coding tasks as a sequence of planned steps." Notes ultracode is a feature of Claude Code specifically.
    Relevance: Confirms ultracode is Claude-Code-only.

29. **Support ultracode effort level and dynamic workflows #725 — GitHub (agentclientprotocol/claude-agent-acp)**
    URL: https://github.com/agentclientprotocol/claude-agent-acp/issues/725
    Key findings: "Up to 16 concurrent subagents (1,000 total per run) execute in parallel. Verification handle approval, and track subagent work." Same numbers as MarkTechPost; the issue tracks ultracode support in the ACP.
    Relevance: Independent technical confirmation of the 16-concurrent / 1,000-total cap.

30. **Claude Code Dynamic Workflows, explained! — X (Akshay Pachaar)**
    URL: https://x.com/akshay_pachaar/status/2060413985925820525
    Key findings: "Subagents are lightweight workers spawned from a main session. They do a focused task and report back. Up to 16 concurrent agents, 1,000 total …" Confirms cap details.
    Relevance: Concise recap of the cap and subagent semantics.

31. **Task budgets — Claude Platform Docs (primary, Anthropic)**
    URL: https://platform.claude.com/docs/en/build-with-claude/task-budgets
    Key findings: "Task budgets complement the effort parameter: effort controls how thoroughly Claude reasons about each step, while task budgets cap the total work Claude can do …" Important: effort and task budgets are orthogonal; task budgets are a separate cost lever introduced with Opus 4.7.
    Relevance: Distinguishes effort from task-budget cost controls.

32. **Claude Code Pricing 2026: Complete Plans & Cost Guide — Finout**
    URL: https://www.finout.io/blog/claude-code-pricing-2026
    Key findings: "Max 20x breaks even against API at roughly 70M tokens/month of typical …" Sets the breakeven math for Max 20x vs API — directly relevant to whether ultracode's bill is better absorbed by subscription or pay-per-token.
    Relevance: Subscription-vs-API breakeven math.

33. **Claude Code /ultra review: 5 Things You Need to Know Before Running — MindStudio**
    URL: https://www.mindstudio.ai/blog/claude-code-ultra-review-5-things-to-know-before-running
    Key findings: `/ultrareview` "spins parallel reviewer agents but costs $5–$20 per run." Sibling of ultracode: uses the same fan-out agent architecture, but for a focused review pass.
    Relevance: Distinguishes ultracode (general auto-orchestration) from /ultrareview (specific review pass).

## Local Sources
(none — no local repo artifacts in the HermesDesktop working tree contain `/effort` or `ultracode` definitions; this thread relies entirely on Anthropic's docs, primary press, and developer reports.)

## Summary

**The full effort ladder.** The Messages API `effort` parameter accepts exactly five values: `low`, `medium`, `high`, `xhigh`, and `max` (primary docs at platform.claude.com/docs/en/build-with-claude/effort). Claude Code's `/effort` slash command exposes the same ladder, with `auto` as a sixth option that lets the model pick. Opus 4.7 (April 16, 2026) added `xhigh` as the Claude Code default for all plans; Opus 4.8 (May 28, 2026) kept that default on the Code side while the Messages API default reverted to `high`. `xhigh` is documented as best for "agentic coding, tool-heavy workflows, and code generation"; `max` is for "absolute highest capability with no constraints."

**Where `ultracode` sits.** Ultracode is not a 6th API value. Anthropic's model-config docs (echoed by MarkTechPost, ClaudeFast, and the community) define it as: "Ultracode is a Claude Code setting rather than a model effort level: it sends `xhigh` to the model and additionally has Claude orchestrate dynamic workflows for substantive tasks. It applies to the current session only." It sits above `xhigh` and `max` on the user-visible ladder because it adds automatic Dynamic-Workflow fan-out to the deepest per-call reasoning. It is session-only, like `max`; `low`/`medium`/`high`/`xhigh` can be saved as defaults; `max` and `ultracode` cannot. The setting shipped in Claude Code v2.1.154 on May 28, 2026.

**Model pairing rules.** Ultracode is a Claude-Code-only behavior, but it has no model restriction at the API level — the underlying orchestrator is whichever model the user picked. In practice it is associated with Opus 4.8 (the only model that launched with ultracode on day one) and Opus 4.7. Sub-agents inside an ultracode-driven workflow are not pinned to the orchestrator's model: the default cheaper worker is Haiku (per MindStudio's cost-control guide), and developers route Sonnet 4.6 or Opus to specific worker roles as needed. Sonnet 4.6 is positioned by Anthropic as "the daily driver for 90%+ of tasks; near-Opus coding quality at 40% lower cost ($3/$15 per million tokens)" — commonly used for subagents when the orchestrator is Opus 4.8. Haiku 4.5 (no separate Haiku 4.7 ship has been documented as of June 2026) is the budget/speed tier at $1/$5 per MTok, used for routing and trivial fetches.

**Pricing and quota.** Standard Opus 4.8 pricing is unchanged from 4.7: $5/M input, $25/M output. Fast mode on Opus 4.8 is $10/$50 — 3x cheaper than Opus 4.7's fast tier ($30/$150) and ~6x the standard tier. There is no separate "fast-mode workflow" cost tier; fast mode is a per-request speed flag, not a per-workflow tier, so any ultracode session that contains a fast-mode call is billed at fast-mode rates for that call only. Fast mode is research-preview and is currently supported on Opus 4.6, 4.7, and 4.8 — not on Sonnet or Haiku.

**Fast-mode workflow cap and the 1,000-subagent claim.** The "workflows capped at 1,000 subagents" line traces to MarkTechPost's same-day launch article and is repeated verbatim in the ACP issue tracker and on X. The Anthropic docs (model-config, "What's new in Opus 4.8") do not state the cap; the official number lives in MarkTechPost + community confirmation: 16 concurrent subagents, 1,000 total per run. This cap is independent of the `/effort` setting; the cap is a property of Dynamic Workflows, which ultracode auto-triggers.

**Real cost reports.** The token-burn picture is severe. A Max 5x user reports "one ultracode session with 30 agents, each calling 600 tools — 20 minutes, 5h limit gone." An API user reports "one ultracode invocation blew through the entire session's capacity and cost me ~$15 in credits." A Max 20x user reports "one prompt burned my entire 20x 4hr usage in less than 5 minutes." A worst-case developer estimate: "$1k–$2k+ per hour" when ultracode loops 5–10 Opus subagents. Counter-evidence exists: at least one developer reports ultracode is *cheaper* than `max` because the orchestrator farms out work to cheaper Haiku/Sonnet subagents. CloudZero quantifies the per-agent multiplier: "a 50-agent session costs approximately 50x the tokens of a single-agent equivalent; a $50 single-agent job can become a $2,500 bill as a Dynamic Workflow." The right mental model is that ultracode is not a fixed cost premium but a per-task fan-out multiplier that scales with the number of substantive tasks the session contains.
