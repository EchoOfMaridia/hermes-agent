import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { useElapsedSeconds } from '@/components/chat/activity-timer'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { type Translations, useI18n } from '@/i18n'
import { AlertCircle, CheckCircle2 } from '@/lib/icons'
import { useEnterAnimation } from '@/lib/use-enter-animation'
import { cn } from '@/lib/utils'
import {
  $subagentsBySession,
  type SubagentProgress
} from '@/store/subagents'
import {
  $activeWorkflowRun,
  $workflowRuns,
  setActiveWorkflowRun,
  type WorkflowRun,
  type WorkflowStep
} from '@/store/workflow-runs'

import { Panel, PanelEmpty, PanelHeader } from '../overlays/panel'

// ============================================================================
// WORKFLOWS PANEL — overlay route showing live + recent workflow runs.
// ----------------------------------------------------------------------------
// Mirrors the visual vocabulary of the Agents panel (codicon glyphs,
// running/verified/failed states, breath spinner, FadeText for subtitles).
// Reads from $workflowRuns, which is fed by use-workflow-events.ts via
// the gateway event stream.
// ============================================================================

type StepState = WorkflowStep['state']

function stepGlyph(state: StepState, a: Translations['workflows']): React.ReactNode {
  if (state === 'running') {
    return (
      <GlyphSpinner
        ariaLabel={a.running}
        className="size-3.5 shrink-0 text-[0.95rem] text-muted-foreground/80"
        spinner="breathe"
      />
    )
  }

  if (state === 'failed') {
    return <AlertCircle aria-label={a.failed} className="size-3.5 shrink-0 text-destructive" />
  }

  if (state === 'verified') {
    return <CheckCircle2 aria-label={a.verified} className="size-3.5 shrink-0 text-emerald-600/85 dark:text-emerald-400/85" />
  }

  return (
    <Codicon
      aria-label={a.pending}
      className="size-3.5 shrink-0 text-muted-foreground/50"
      name="circle-outline"
    />
  )
}

function runGlyph(state: WorkflowRun['state'], a: Translations['workflows']): React.ReactNode {
  if (state === 'running') {
    return (
      <GlyphSpinner
        ariaLabel={a.running}
        className="size-4 shrink-0 text-muted-foreground/80"
        spinner="breathe"
      />
    )
  }

  if (state === 'failed') {
    return <AlertCircle aria-label={a.failed} className="size-4 shrink-0 text-destructive" />
  }

  if (state === 'done') {
    return <CheckCircle2 aria-label={a.verified} className="size-4 shrink-0 text-emerald-600/85 dark:text-emerald-400/85" />
  }

  if (state === 'cancelled' || state === 'halted') {
    return <Codicon aria-label={state} className="size-4 shrink-0 text-muted-foreground/60" name="circle-slash" />
  }

  return <Codicon aria-label={state} className="size-4 shrink-0 text-muted-foreground/50" name="circle-outline" />
}

const fmtDuration = (seconds: number, a: Translations['workflows']): string => {
  if (!seconds || seconds <= 0) {return ''}

  if (seconds < 60) {return a.durationSeconds(seconds.toFixed(1))}
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)

  return a.durationMinutes(m, s)
}

interface WorkflowsViewProps {
  onClose: () => void
}

export function WorkflowsView({ onClose }: WorkflowsViewProps) {
  const { t } = useI18n()
  const runs = useStore($workflowRuns)
  const activeRunId = useStore($activeWorkflowRun)
  const now = useState(() => Date.now())[0]

  const runsArray = useMemo(
    () => Object.values(runs).sort((a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0)),
    [runs],
  )

  const activeRun = activeRunId ? runs[activeRunId] ?? null : null

  return (
    <Panel onClose={onClose}>
      <PanelHeader
        subtitle={t.workflows.subtitle(runsArray.length)}
        title={t.workflows.title}
      />
      {runsArray.length === 0 ? (
        <PanelEmpty
          description={t.workflows.emptyDesc}
          icon="inbox"
          title={t.workflows.emptyTitle}
        />
      ) : (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto pr-1">
          {runsArray.map(run => (
            <WorkflowRunRow
              active={run.runId === activeRunId}
              key={run.runId}
              now={now}
              onSelect={() => setActiveWorkflowRun(run.runId)}
              run={run}
            />
          ))}
        </div>
      )}
      {activeRun ? (
        <WorkflowRunDetail key={activeRun.runId} run={activeRun} />
      ) : null}
    </Panel>
  )
}

interface WorkflowRunRowProps {
  active: boolean
  now: number
  onSelect: () => void
  run: WorkflowRun
}

function WorkflowRunRow({ active, now, onSelect, run }: WorkflowRunRowProps) {
  const { t } = useI18n()
  const isRunning = run.state === 'running'
  const elapsed = useElapsedSeconds(isRunning, `workflow-run:${run.runId}`)

  const effectiveElapsed = (() => {
    if (run.startedAt == null) {return 0}
    const end = run.endedAt ?? now / 1000

    return end - run.startedAt
  })()

  const elapsedText = fmtDuration(isRunning ? elapsed : effectiveElapsed, t.workflows)

  return (
    <button
      aria-pressed={active}
      className={cn(
        'group flex w-full min-w-0 items-start gap-3 rounded-md px-3 py-2.5 text-left transition-colors',
        active
          ? 'bg-(--ui-bg-quaternary) ring-1 ring-(--stroke-nous)'
          : 'hover:bg-(--chrome-action-hover)',
      )}
      data-active={active ? 'true' : undefined}
      data-run-id={run.runId}
      onClick={onSelect}
      type="button"
    >
      <span className="mt-0.5 flex h-[1.1rem] shrink-0 items-center">
        {runGlyph(run.state, t.workflows)}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="wrap-anywhere text-[0.82rem] font-medium leading-[1.1rem] text-foreground/90 transition-colors group-hover:text-foreground">
          {run.workflowName ?? t.workflows.unknownWorkflow}
          <span className="ml-1.5 text-[0.66rem] font-mono text-muted-foreground/55">
            {run.runId}
          </span>
        </span>
        <span className="text-[0.66rem] leading-[1.05rem] text-muted-foreground/65">
          {[
            t.workflows.steps(run.steps.length),
            t.workflows.state(run.state),
            elapsedText,
          ]
            .filter(Boolean)
            .join(' · ')}
        </span>
      </span>
      {isRunning && elapsed > 0 ? (
        <ActivityTimerText className="mt-1 shrink-0 text-[0.6rem]" seconds={elapsed} />
      ) : null}
    </button>
  )
}

interface WorkflowRunDetailProps {
  run: WorkflowRun
}

function WorkflowRunDetail({ run }: WorkflowRunDetailProps) {
  const { t } = useI18n()
  const enterRef = useEnterAnimation(true, `workflow-detail:${run.runId}`)

  return (
    <aside
      className="flex min-w-0 flex-col gap-3 border-t border-(--ui-stroke-tertiary) pt-4"
      data-slot="workflow-detail"
      ref={enterRef}
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-foreground/85">
          {t.workflows.steps(run.steps.length)}
        </h3>
        <span className="text-[0.66rem] font-mono text-muted-foreground/55">
          {run.runId}
        </span>
      </header>
      {run.errorMessage ? (
        <p className="wrap-anywhere text-[0.72rem] leading-relaxed text-destructive/90">
          {t.workflows.error(run.errorMessage)}
        </p>
      ) : null}
      {run.haltReason ? (
        <p className="wrap-anywhere text-[0.72rem] leading-relaxed text-muted-foreground/85">
          {t.workflows.haltReason(run.haltReason)}
        </p>
      ) : null}
      <ol className="flex min-w-0 flex-col gap-2 pl-1">
        {run.steps.map(step => (
          <StepRow key={step.name} step={step} />
        ))}
      </ol>
    </aside>
  )
}

function StepRow({ step }: { step: WorkflowStep }) {
  const { t } = useI18n()
  const isRunning = step.state === 'running'
  const elapsed = useElapsedSeconds(isRunning, `workflow-step:${step.name}`)

  const durationText = fmtDuration(
    step.durationSeconds ?? (isRunning ? elapsed : 0),
    t.workflows,
  )

  return (
    <li className="flex min-w-0 items-start gap-2.5 text-[0.72rem]">
      <span className="mt-0.5 flex h-[1.1rem] shrink-0 items-center">
        {stepGlyph(step.state, t.workflows)}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="wrap-anywhere font-mono text-foreground/85">{step.name}</span>
        <span className="flex items-center gap-1.5 text-muted-foreground/65">
          {step.state === 'pending' ? (
            <span>{t.workflows.stepPending}</span>
          ) : (
            <>
              <span>{t.workflows.stepState(step.state)}</span>
              {durationText ? (
                <span className="text-muted-foreground/45">·</span>
              ) : null}
              {durationText ? <span>{durationText}</span> : null}
              {step.verifierVerdict ? (
                <>
                  <span className="text-muted-foreground/45">·</span>
                  <span
                    className={cn(
                      'font-medium',
                      step.verifierVerdict === 'pass'
                        ? 'text-emerald-600/85 dark:text-emerald-400/85'
                        : 'text-destructive',
                    )}
                  >
                    {t.workflows.verifier(step.verifierVerdict)}
                  </span>
                </>
              ) : null}
            </>
          )}
        </span>
        {step.verifierReason ? (
          <p className="wrap-anywhere text-muted-foreground/55">{step.verifierReason}</p>
        ) : null}
      </span>
    </li>
  )
}

// Re-export so the file shows up in the AppView system as `WorkflowsView`.
export default WorkflowsView

// ============================================================================
// Subagent live view
// ----------------------------------------------------------------------------
// Inline focused viewer for one subagent. Reads from $subagentsBySession
// keyed by sessionId (the session the subagent was spawned inside). The
// sessionId is the same id that the WorkflowsPanel stores in
// $subagentsByRun[runId][i] when it links a subagent to a run.
//
// Rendered inline in the WorkflowsView when the user clicks a subagent
// badge. A future change can promote this to a separate IPC-routed
// BrowserWindow via `window.hermesDesktop.openSubagentWindow` — the
// nanostore subscription is the same; only the entry point changes.
// ============================================================================

export const $activeSubagent = atom<{ sessionId: string; subagentId: string } | null>(null)

export function openSubagentLiveView(sessionId: string, subagentId: string): void {
  $activeSubagent.set({ sessionId, subagentId })
}

export function closeSubagentLiveView(): void {
  $activeSubagent.set(null)
}

function subagentStatusGlyph(
  status: SubagentProgress['status']
): ReactNode {
  if (status === 'running' || status === 'queued') {
    return (
      <GlyphSpinner
        ariaLabel={status}
        className="size-3.5 shrink-0 text-[0.95rem] text-muted-foreground/80"
        spinner="breathe"
      />
    )
  }

  if (status === 'failed' || status === 'interrupted') {
    return <AlertCircle aria-label={status} className="size-3.5 shrink-0 text-destructive" />
  }

  return (
    <CheckCircle2 aria-label={status} className="size-3.5 shrink-0 text-emerald-600/85 dark:text-emerald-400/85" />
  )
}

interface SubagentLiveViewProps {
  onClose: () => void
  sessionId: string
  subagent: SubagentProgress
}

function SubagentLiveViewImpl({ onClose, sessionId, subagent }: SubagentLiveViewProps) {
  const { t } = useI18n()
  const isRunning = subagent.status === 'running' || subagent.status === 'queued'
  const elapsed = useElapsedSeconds(isRunning, `subagent:${subagent.id}`)
  const enterRef = useEnterAnimation(true, `subagent-live:${subagent.id}`)
  const visibleRows = subagent.stream.slice(-12)

  const fileLines = [
    ...subagent.filesWritten.map(p => `+ ${p}`),
    ...subagent.filesRead.map(p => `· ${p}`),
  ]

  // Auto-close once the subagent has reached a terminal status AND the
  // stream has had no new entries for 5s. Per AGENTS.md: "never navigate
  // because something happened in the background. Offer; don't hijack."
  // The auto-close is a UX hint, not a navigation event.
  useEffect(() => {
    if (isRunning) {return}

    if (visibleRows.length === 0) {return}
    const last = subagent.stream[subagent.stream.length - 1]

    if (!last?.at) {return}
    const delayMs = 5_000

    const handle = window.setTimeout(() => {
      onClose()
    }, delayMs)

    return () => window.clearTimeout(handle)
  }, [isRunning, onClose, subagent.stream, visibleRows.length])

  return (
    <aside
      aria-label={t.workflows.subagentTitle(subagent.id)}
      className="flex min-w-0 flex-col gap-3 border-t border-(--ui-stroke-tertiary) pt-4"
      data-run-id={subagent.id}
      data-session-id={sessionId}
      data-slot="subagent-live-view"
      ref={enterRef}
    >
      <header className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 className="wrap-anywhere text-xs font-semibold text-foreground/85">
            {subagent.goal}
          </h3>
          <span className="font-mono text-[0.6rem] text-muted-foreground/55">
            {sessionId}
          </span>
        </div>
        <button
          aria-label={t.workflows.subagentClose}
          className="shrink-0 rounded-md px-2 py-1 text-[0.66rem] text-muted-foreground/75 hover:bg-(--chrome-action-hover)"
          onClick={onClose}
          type="button"
        >
          {t.workflows.subagentClose}
        </button>
      </header>
      <div className="flex items-center gap-2 text-[0.7rem] text-muted-foreground/75">
        {subagentStatusGlyph(subagent.status)}
        <span>{subagent.status}</span>
        {subagent.model ? (
          <>
            <span className="text-muted-foreground/45">·</span>
            <span className="font-mono">{subagent.model}</span>
          </>
        ) : null}
        {isRunning ? (
          <>
            <span className="text-muted-foreground/45">·</span>
            <ActivityTimerText seconds={elapsed} />
          </>
        ) : null}
        {subagent.currentTool ? (
          <>
            <span className="text-muted-foreground/45">·</span>
            <span className="font-mono text-foreground/80">
              {subagent.currentTool}
            </span>
          </>
        ) : null}
      </div>
      {subagent.summary ? (
        <p className="wrap-anywhere text-[0.72rem] leading-relaxed text-foreground/85">
          {subagent.summary}
        </p>
      ) : null}
      {visibleRows.length > 0 ? (
        <ol className="flex min-w-0 flex-col gap-1 pl-1">
          {visibleRows.map((entry, i) => (
            <li
              className="wrap-anywhere font-mono text-[0.68rem] leading-relaxed text-foreground/85"
              data-stream-kind={entry.kind}
              key={`${entry.kind}:${entry.at}:${i}`}
            >
              {entry.kind === 'tool' ? '⚙ ' : ''}
              {entry.text}
            </li>
          ))}
        </ol>
      ) : null}
      {fileLines.length > 0 ? (
        <ul className="flex flex-col gap-0.5 pl-1">
          {fileLines.slice(0, 8).map(line => (
            <li
              className="font-mono text-[0.66rem] leading-relaxed text-muted-foreground/75"
              key={line}
            >
              {line}
            </li>
          ))}
          {fileLines.length > 8 ? (
            <li className="font-mono text-[0.66rem] leading-relaxed text-muted-foreground/55">
              {t.workflows.subagentMoreFiles(fileLines.length - 8)}
            </li>
          ) : null}
        </ul>
      ) : null}
    </aside>
  )
}

/** Public subagent live view — reads $activeSubagent and $subagentsBySession. */
export function SubagentLiveView({ onClose }: { onClose: () => void }) {
  const active = useStore($activeSubagent)
  const bySession = useStore($subagentsBySession)

  if (!active) {return null}
  const items = bySession[active.sessionId] ?? []
  const subagent = items.find(item => item.id === active.subagentId) ?? null

  if (!subagent) {
    // Stale selection (subagent cleared from the store); treat as closed.
    return null
  }

  return (
    <SubagentLiveViewImpl
      onClose={onClose}
      sessionId={active.sessionId}
      subagent={subagent}
    />
  )
}

// Re-export the active-subagent atom so callers can subscribe without
// importing the store layer.
export const $workflowSubagentFocused = $activeSubagent