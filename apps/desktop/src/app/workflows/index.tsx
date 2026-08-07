import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect, useMemo, useState } from 'react'

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

import { WorkflowWireProbe } from './wire-probe'

// ============================================================================
// WORKFLOWS PANEL — overlay route showing live + recent workflow runs.
// ----------------------------------------------------------------------------
// Tabbed UI (added 2026-07-19 per the user's UI request): a left-rail
// tab switcher lets the user toggle between the existing "Runs" view
// (live + recent runs) and a new "Library" view (saved scripts + Run
// button). The Runs tab is default and behavior-preserving.
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

// ----------------------------------------------------------------------------
// Library REST client — goes through the Electron preload bridge
// (`window.hermesDesktop.api`) so the request reaches the live Hermes
// backend at its real baseUrl (the renderer is loaded from a static
// `index.html` and has no /api proxy of its own). The earlier raw
// `fetch('/api/workflows/library', …)` shape failed with "Failed to
// fetch" because the page has no origin serving that path.
// ----------------------------------------------------------------------------

export interface WorkflowLibraryEntry {
  name: string
  description: string
  path: string
  created_at: string
}

export interface WorkflowLibraryResponse {
  entries: WorkflowLibraryEntry[]
}

async function fetchLibrary(): Promise<WorkflowLibraryResponse> {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop : undefined

  if (!bridge?.api) {
    throw new Error('desktop bridge unavailable')
  }

  return bridge.api<WorkflowLibraryResponse>({ path: '/api/workflows/library' })
}

async function startRun(name: string): Promise<{ run_id: string }> {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop : undefined

  if (!bridge?.api) {
    throw new Error('desktop bridge unavailable')
  }

  return bridge.api<{ run_id: string }>({
    method: 'POST',
    path: '/api/workflows/run',
    body: { name, inputs: {} }
  })
}

const $libraryEntries = atom<WorkflowLibraryEntry[]>([])
const $libraryLoading = atom<boolean>(false)
// $libraryError holds LIBRARY-level fetch errors (network down, token
// bad, library file corrupt). Surface it only when there are no
// entries to render — otherwise it's noise over the actual rows.
const $libraryError = atom<string | null>(null)
// $runErrors holds PER-ENTRY run-start errors (e.g. "submit failed:
// workflow script missing"). Always surface inline next to the
// failing row so a failed Run never silently flickers the button
// and disappears — the user has to see WHY nothing happened.
const $runErrors = atom<Record<string, string>>({})
const $startingRun = atom<string | null>(null)
// $runClickCounter increments on every Run click so the WireProbe
// shows "clicks seen" alongside the bridge's translation stats —
// the operator can tell at a glance whether a click reached the UI
// at all, and correlate the click with the backend's bridge log.
const $runClickCounter = atom<number>(0)

async function refreshLibrary(): Promise<void> {
  $libraryLoading.set(true)
  $libraryError.set(null)

  try {
    const data = await fetchLibrary()
    $libraryEntries.set(data.entries ?? [])
  } catch (exc) {
    $libraryError.set(exc instanceof Error ? exc.message : String(exc))
  } finally {
    $libraryLoading.set(false)
  }
}

async function runEntry(name: string): Promise<void> {
  $startingRun.set(name)

  // Trace every click to the desktop log so the operator can correlate
  // a "Run" press with the bridge stats and the backend's bridge.before/
  // bridge.after log line. console.log is captured by the renderer's
  // console capture in apps/desktop/electron and forwarded to
  // /home/cage/.hermes/logs/desktop.log.
   
  console.log(`[workflows] runEntry click name=${name} clickId=${$runClickCounter.get()}`)

  // Clear any prior error for this entry so the user sees a fresh
  // attempt without the old failure text lingering.
  if ($runErrors.get()[name]) {
    const next = { ...$runErrors.get() }
    delete next[name]
    $runErrors.set(next)
  }

  try {
    const result = await startRun(name)
     
    console.log(`[workflows] runEntry success name=${name} runId=${result?.run_id ?? '?'}`)
    // The new run's live progress will arrive via the gateway event
    // stream (use-workflow-events.ts) and populate $workflowRuns.
    // No additional wiring needed.
  } catch (exc) {
     
    console.log(`[workflows] runEntry failure name=${name} error=${exc instanceof Error ? exc.message : String(exc)}`)
    $runErrors.set({ ...$runErrors.get(), [name]: exc instanceof Error ? exc.message : String(exc) })
  } finally {
    $startingRun.set(null)
  }
}

// ----------------------------------------------------------------------------
// Library tab — list + Run button
// ----------------------------------------------------------------------------

function LibraryTab() {
  const { t } = useI18n()
  const entries = useStore($libraryEntries)
  const loading = useStore($libraryLoading)
  const error = useStore($libraryError)
  const runErrors = useStore($runErrors)
  const startingRun = useStore($startingRun)
  const runs = useStore($workflowRuns)

  useEffect(() => {
    void refreshLibrary()
  }, [])

  // True when a run is currently in flight for this entry — either
  // the submit is still pending (`$startingRun`) or the runtime has
  // already accepted it and the run is `running` in $workflowRuns.
  // Either way, the Run button stays disabled until the run settles,
  // so the user sees the spinner instead of a one-frame flicker.
  const inFlight = (entryName: string): boolean => {
    if (startingRun === entryName) {return true}

    return Object.values(runs).some(
      r => r.workflowName === entryName && r.state === 'running'
    )
  }

  if (loading && entries.length === 0) {
    return (
      <PanelEmpty
        description={t.workflows.libraryLoadingDesc}
        icon="loading"
        title={t.workflows.libraryLoadingTitle}
      />
    )
  }

  if (error && entries.length === 0) {
    return (
      <PanelEmpty
        description={error}
        icon="error"
        title={t.workflows.libraryErrorTitle}
      />
    )
  }

  if (entries.length === 0) {
    return (
      <PanelEmpty
        description={t.workflows.libraryEmptyDesc}
        icon="inbox"
        title={t.workflows.libraryEmptyTitle}
      />
    )
  }

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-y-auto pr-1">
      {entries.map(entry => {
        const runError = runErrors[entry.name]
        const busy = inFlight(entry.name)

        return (
          <div
            className="group flex w-full min-w-0 flex-col gap-1 rounded-md px-3 py-2.5 hover:bg-(--chrome-action-hover)"
            key={entry.name}
          >
            <div className="flex w-full min-w-0 items-start gap-3">
              <Codicon
                aria-hidden="true"
                className="mt-0.5 size-4 shrink-0 text-muted-foreground/70"
                name="file-code"
              />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium text-foreground text-sm">
                  {entry.name}
                </div>
                <div className="line-clamp-2 text-muted-foreground/85 text-xs">
                  {entry.description || t.workflows.unknownWorkflow}
                </div>
              </div>
              <button
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 font-medium text-xs transition-colors',
                  'bg-primary/15 text-primary hover:bg-primary/25',
                  'disabled:opacity-50'
                )}
                disabled={busy}
                onClick={() => {
                  $runClickCounter.set($runClickCounter.get() + 1)
                  void runEntry(entry.name)
                }}
                type="button"
              >
                {busy ? (
                  <GlyphSpinner
                    ariaLabel={t.workflows.running}
                    className="size-3"
                    spinner="breathe"
                  />
                ) : (
                  <Codicon
                    aria-hidden="true"
                    className="size-3"
                    name="play"
                  />
                )}
                <span>{t.workflows.runButton}</span>
              </button>
            </div>
            {runError ? (
              <div
                aria-live="polite"
                className="ml-7 break-words text-[11px] text-(--ui-danger,#f87171)"
                data-testid="workflow-run-error"
                role="status"
              >
                {runError}
              </div>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

interface WorkflowsViewProps {
  onClose?: () => void
}

type Tab = 'runs' | 'library'

export function WorkflowsView({ onClose }: WorkflowsViewProps) {
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('runs')
  const runs = useStore($workflowRuns)
  const activeRunId = useStore($activeWorkflowRun)
  const now = useState(() => Date.now())[0]
  // Bumps on every Run click so the WireProbe re-polls /api/workflows/diag
  // — gives the operator an immediate read of the bridge stats after a
  // click, even when no events have arrived at the renderer yet.
  const clickCounter = useStore($runClickCounter)

  const runsArray = useMemo(
    () => Object.values(runs).sort((a, b) => (b.startedAt ?? 0) - (a.startedAt ?? 0)),
    [runs],
  )

  const activeRun = activeRunId ? runs[activeRunId] ?? null : null

  return (
    <Panel onClose={onClose ?? (() => {})}>
      <PanelHeader
        subtitle={t.workflows.subtitle(runsArray.length)}
        title={t.workflows.title}
      />
      <WorkflowWireProbe clickCounter={clickCounter} />
      <div
        aria-label={t.workflows.title}
        className="flex min-h-0 min-w-0 flex-1 flex-row"
        role="tablist"
      >
        <div className="flex w-32 shrink-0 flex-col gap-1 border-r border-(--stroke-faint) pr-2">
          <button
            aria-selected={tab === 'runs'}
            className={cn(
              'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors',
              tab === 'runs'
                ? 'bg-(--ui-bg-quaternary) font-medium text-foreground'
                : 'text-muted-foreground hover:bg-(--chrome-action-hover) hover:text-foreground'
            )}
            onClick={() => { setTab('runs') }}
            role="tab"
            type="button"
          >
            <Codicon aria-hidden="true" className="size-3.5" name="history" />
            <span>{t.workflows.runsTab}</span>
          </button>
          <button
            aria-selected={tab === 'library'}
            className={cn(
              'flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors',
              tab === 'library'
                ? 'bg-(--ui-bg-quaternary) font-medium text-foreground'
                : 'text-muted-foreground hover:bg-(--chrome-action-hover) hover:text-foreground'
            )}
            onClick={() => { setTab('library') }}
            role="tab"
            type="button"
          >
            <Codicon aria-hidden="true" className="size-3.5" name="library" />
            <span>{t.workflows.libraryTab}</span>
          </button>
        </div>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col pl-3" role="tabpanel">
          {tab === 'runs' ? (
            <>
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
            </>
          ) : (
            <LibraryTab />
          )}
        </div>
      </div>
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
      key={run.runId}
      onClick={onSelect}
      type="button"
    >
      <span className="mt-0.5 shrink-0">{runGlyph(run.state, t.workflows)}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="truncate font-medium text-foreground text-sm">
            {run.workflowName || t.workflows.unknownWorkflow}
          </span>
          <ActivityTimerText className="font-mono text-muted-foreground/70 text-xs" isRunning={isRunning} text={elapsedText} />
        </div>
        <div className="line-clamp-1 text-muted-foreground/85 text-xs">
          {run.runId} · {t.workflows.steps(run.steps.length)}
        </div>
      </div>
    </button>
  )
}

interface WorkflowRunDetailProps {
  run: WorkflowRun
}

function WorkflowRunDetail({ run }: WorkflowRunDetailProps) {
  const { t } = useI18n()
  const { enterClass } = useEnterAnimation('fade-in', 200)
  const subagents = useStore($subagentsBySession)
  const stepState = run.steps.map(s => s.state)
  const completedCount = stepState.filter(state => state === 'verified').length
  const failedCount = stepState.filter(state => state === 'failed').length
  const runningCount = stepState.filter(state => state === 'running').length

  return (
    <div className={cn('mt-3 flex max-h-72 flex-col gap-2 overflow-y-auto rounded-md border border-(--stroke-faint) bg-(--ui-bg-secondary) p-3', enterClass)}>
      <div className="flex items-center justify-between gap-3">
        <div className="truncate font-medium text-foreground text-sm">
          {run.workflowName || t.workflows.unknownWorkflow}
        </div>
        <div className="flex items-center gap-2 text-muted-foreground/80 text-xs">
          {completedCount > 0 ? <span className="text-emerald-500/80">{t.workflows.steps(completedCount)} ✓</span> : null}
          {runningCount > 0 ? <span>{t.workflows.steps(runningCount)} ⟳</span> : null}
          {failedCount > 0 ? <span className="text-destructive">{t.workflows.steps(failedCount)} ✗</span> : null}
        </div>
      </div>
      <ol className="flex flex-col gap-1.5">
        {run.steps.map(step => (
          <li className="flex items-center gap-2 text-sm" key={step.name}>
            <span className="shrink-0">{stepGlyph(step.state, t.workflows)}</span>
            <span className="truncate text-foreground/90">{step.name}</span>
            {step.error ? <span className="text-destructive text-xs">{t.workflows.error(step.error)}</span> : null}
            {step.verifierVerdict ? <span className="text-muted-foreground/80 text-xs">· {t.workflows.verifier(step.verifierVerdict)}</span> : null}
          </li>
        ))}
      </ol>
      {Object.keys(subagents[run.runId] ?? {}).length > 0 ? (
        <SubagentList runId={run.runId} subagents={Object.values(subagents[run.runId] ?? {})} />
      ) : null}
    </div>
  )
}

interface SubagentListProps {
  runId: string
  subagents: SubagentProgress[]
}

function SubagentList({ subagents }: SubagentListProps) {
  const { t } = useI18n()

  if (subagents.length === 0) {return null}

  return (
    <div className="flex flex-col gap-1 border-(--stroke-faint) border-t pt-2">
      <div className="font-medium text-muted-foreground/80 text-xs uppercase tracking-wide">Subagents</div>
      <ul className="flex flex-col gap-1">
        {subagents.map(sa => (
          <li className="flex items-center gap-2 text-xs" key={sa.subagentId}>
            <Codicon aria-hidden="true" className="size-3 text-muted-foreground/60" name="robot" />
            <span className="truncate">{sa.subagentId}</span>
            {sa.startedAt && !sa.endedAt ? <GlyphSpinner ariaLabel={t.workflows.running} className="size-3" spinner="breathe" /> : null}
            {sa.endedAt ? <span className="text-muted-foreground/60">✓</span> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}