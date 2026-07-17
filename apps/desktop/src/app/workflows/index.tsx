import { useStore } from '@nanostores/react'
import { useMemo, useState } from 'react'

import { useElapsedSeconds } from '@/components/chat/activity-timer'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { type Translations, useI18n } from '@/i18n'
import { AlertCircle, CheckCircle2 } from '@/lib/icons'
import { useEnterAnimation } from '@/lib/use-enter-animation'
import { cn } from '@/lib/utils'
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