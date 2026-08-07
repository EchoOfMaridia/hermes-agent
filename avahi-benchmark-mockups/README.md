TTT Benchmark Dashboard — Mockups for Avahi
=============================================

Partner handoff. 7 self-contained HTML pages + 1 shared CSS.
No build step. No framework. Open in any modern browser.


WHAT'S IN THE BUNDLE
---------------------

  01-overview.html              Entry point. Three benchmarks at a glance. Head-to-head.
  02-run-comparison.html        5 models x 3 benchmarks x 5 runs cell grid. Statistical
                                significance table. Open-Vend 28-day profit curves.
  03-benchmark-detail.html      Per-benchmark primary metric panel. Tabs for the three
                                different score shapes.
  04-trace-timeline.html        Single run deep-dive. Full event stream. Event schema
                                reference. Latency + context growth charts.
  05-run-replay.html            Two-pane replay. Actual run vs TPipe reference run.
                                The marketing demo page.
  06-refusal-inspector.html     The -4x penalty class. Refusal rate by model, by
                                category, top 10 refusal triggers.
  07-llm-vs-agent-mode.html     Two first-class modes. Tool surface, connector spec,
                                mode comparison table.

  styles.css                    Shared dark theme. Self-contained. ~600 lines.
  README.md                     This file.


HOW TO VIEW
-----------

  $ cd avahi-benchmark-mockups
  $ python3 -m http.server 8000
  $ open http://localhost:8000/01-overview.html

Or just double-click any HTML file in a browser.


DATA SHAPE
-----------

All numbers are mocked but follow the actual rule sets from the four
benchmark documents in the shared Drive folder:

  Drive folder:
    https://drive.google.com/drive/u/0/folders/1u9Blv10R5mMkQe1s_kKkOIsosmBoDkZo

  Documents read:
    - General Concepts (cross-cutting ruleset)
    - Butter-Bench (web nav / UI stress)
    - Drive-Thru-Bench (order parsing / governance, 188 adversarial cases)
    - Open-Vend Specifications (28-day vending machine endurance)

The mockups are designed so the Avahi backend can plug real data in
without restructuring the pages. Every table, chart, and KPI has
a one-to-one mapping to a real metric the benchmark already emits.


DESIGN PRINCIPLES
-----------------

  1. Every page has the same top nav with all 7 pages linked.
     The user can navigate the full surface from anywhere.

  2. Every page has at least one "FOR AVAHI" or "DESIGN NOTE" callout
     explaining WHY the visualization is shaped the way it is.
     These are the load-bearing comments for the partner handoff.

  3. Every chart is inline SVG. No external chart library. Avahi can
     reuse the data structures directly.

  4. The "TPipe reference" or projected TPipe row is always visible.
     The dashboard is the marketing story. The whole point of these
     benchmarks is to show TPipe beating every stock LLM and every
     stock agent framework.

  5. The Refusal Inspector exists because a single refusal = -4x penalty.
     Per General Concepts: "Refusals should be harshly penalized with
     a single refusal being worth 4 positive scores." This is the
     highest-leverage failure class in the suite and gets its own page.

  6. LLM mode vs Agent mode is a real two-mode architecture, not a
     checkbox. Per General Concepts: "all the benchmarks should include
     configurable modes for this, and standardized means of connecting
     and exchanging data." Page 7 makes that contract explicit.


THE DATA PIPELINE AVAHI NEEDS TO BUILD
--------------------------------------

  1. Event ingestion
     - Per-run JSONL stream with the schema from page 4
     - WebSocket feed for live runs
     - Batch upload for offline runs

  2. Aggregation layer
     - 5-min rollup for the live feed
     - 1-hour rollup for the dashboard
     - 7-day rollup for trend lines

  3. Storage
     - PostgreSQL for run metadata + scores
     - S3 for raw JSONL event streams
     - Redis for the live feed (events older than 1h go cold)

  4. API
     - GET /v1/runs?benchmark=...&model=...&seed=...&limit=...
     - GET /v1/runs/{id}/events (paginated, newest first)
     - GET /v1/runs/{id}/score (cached, recomputed on refusal events)
     - POST /v1/runs (agent mode submission)
     - WS  /v1/stream (live event feed for active runs)

  5. Auth
     - API key for read access (everyone gets one for the public site)
     - OAuth for write access (only Avahi + TTT can submit runs)
     - Per-run tokens for agent mode connector


WHAT'S NOT IN THESE MOCKUPS
---------------------------

  - Real data. The numbers are mocked to follow the rules in the spec
    docs. Avahi needs to wire real numbers in.
  - Interactive controls. The buttons, dropdowns, and replay controls
    are visual mocks. The behavior is described in the callouts.
  - Authentication. The dashboard assumes an authenticated session.
  - Mobile / responsive. The dashboard is desktop-first. Mobile is
    a separate design pass.
  - Internationalization. English only. LLM mode prompts are English
    only per the General Concepts freeze.


NEXT STEPS FOR THE PARTNERSHIP
-------------------------------

  1. Avahi reviews the 7 pages, leaves inline comments on the design
  2. We sync for 30 min to align on data shape and event schema
  3. Avahi ships a backend that ingests events from the running
     benchmarks (the connector spec is on page 7)
  4. TTT ships the benchmark runner that emits events in the schema
  5. First end-to-end demo: a single Drive-Thru-Bench run streams
     into the live feed on page 1
  6. Public launch: all three benchmarks running 24/7 against all
     five locked models, agent-mode submissions open


QUESTIONS FOR AVAHI
--------------------

  1. Do you want the dashboard to be a separate Next.js app or embed
     in the existing tentrilliontriangles.com site?
  2. For agent mode, do you want a public leaderboard or only
     approved-by-TTT submissions?
  3. For the prompt-freeze check, do you want a "prompt version" badge
     on every run (currently just shown on page 4)?
  4. For statistical significance, are Mann-Whitney U + Welch's t
     sufficient, or do you need Bayesian posterior intervals?
  5. For the trace timeline, do you want a per-case timeline strip
     on page 3 (similar to page 4's "Worst Case Replays" table) or
     keep the focus on the per-benchmark primary metric?


CONTACTS
--------

  TTT benchmark team:  benchmarks@tentrilliontriangles.com
  Avahi:               [your contact here]
  Repo:                github.com/Ten-Trillion-Triangles/bench-dashboard
  Drive folder:        https://drive.google.com/drive/u/0/folders/1u9Blv10R5mMkQe1s_kKkOIsosmBoDkZo
