import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import {
  Popover,
  PopoverContent,
  PopoverTrigger
} from '@/components/ui/popover'
import { TitleMenuTrigger } from '@/components/ui/title-menu-trigger'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

// ============================================================================
// WorkflowsTitlebarMenu — titlebar (top menu bar) dropdown for the
// workflow library. Click "Workflows ▾" → list of saved scripts →
// click Run → starts a run via REST → live progress shows up in the
// WorkflowsView overlay panel.
// ----------------------------------------------------------------------------
// Added 2026-07-19 per the user's request: "tabbed panel on the left of
// the workflows window, OR some settings options or slider at the top
// menu bar." This is the second affordance (in addition to the
// Library tab inside WorkflowsView).
// ============================================================================

interface WorkflowLibraryEntry {
  name: string
  description: string
  path: string
  created_at: string
}

interface WorkflowLibraryResponse {
  entries: WorkflowLibraryEntry[]
}

const $library = atom<WorkflowLibraryEntry[]>([])
const $loading = atom<boolean>(false)
const $error = atom<string | null>(null)
// Per-entry run-start errors. Surfaces inline next to the failing
// row so a failed Run is never silently invisible while entries are
// loaded (the Library-level $error only renders when entries is
// empty, which is the wrong default for a populated library).
const $runErrors = atom<Record<string, string>>({})
const $starting = atom<string | null>(null)

interface DesktopBridge {
  api?: <T>(request: { method?: string; path: string; body?: unknown }) => Promise<T>
}

function getBridge(): DesktopBridge['api'] | undefined {
  if (typeof window === 'undefined') {
    return undefined
  }

  return (window as unknown as { hermesDesktop?: DesktopBridge }).hermesDesktop?.api
}

async function refreshLibrary(): Promise<void> {
  const api = getBridge()

  if (!api) {
    $error.set('desktop bridge unavailable')

    return
  }

  $loading.set(true)
  $error.set(null)

  try {
    const data = await api<WorkflowLibraryResponse>({ path: '/api/workflows/library' })
    $library.set(data.entries ?? [])
  } catch (exc) {
    $error.set(exc instanceof Error ? exc.message : String(exc))
  } finally {
    $loading.set(false)
  }
}

async function startRun(name: string): Promise<void> {
  const api = getBridge()

  if (!api) {
    $runErrors.set({ ...$runErrors.get(), [name]: 'desktop bridge unavailable' })

    return
  }

  $starting.set(name)

  // Clear any prior error for this entry.
  if ($runErrors.get()[name]) {
    const next = { ...$runErrors.get() }
    delete next[name]
    $runErrors.set(next)
  }

  try {
    await api<{ run_id: string }>({
      method: 'POST',
      path: '/api/workflows/run',
      body: { name, inputs: {} }
    })
    // Live-progress events will populate the overlay panel even if the
    // start call briefly errors.
  } catch (exc) {
    $runErrors.set({ ...$runErrors.get(), [name]: exc instanceof Error ? exc.message : String(exc) })
  } finally {
    $starting.set(null)
  }
}

export function WorkflowsTitlebarMenu() {
  const { t } = useI18n()
  const entries = useStore($library)
  const loading = useStore($loading)
  const error = useStore($error)
  const runErrors = useStore($runErrors)
  const starting = useStore($starting)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (open && entries.length === 0 && !loading && !error) {
      void refreshLibrary()
    }
  }, [open, entries.length, loading, error])

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <TitleMenuTrigger>
          {t.workflows.titlebarMenuLabel}
        </TitleMenuTrigger>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-1">
        <div className="flex items-center justify-between px-2 py-1.5">
          <div className="font-medium text-foreground text-xs uppercase tracking-wide">
            {t.workflows.title}
          </div>
          <button
            aria-label="refresh"
            className="rounded-sm p-0.5 text-muted-foreground/70 hover:bg-(--chrome-action-hover) hover:text-foreground"
            onClick={() => { void refreshLibrary() }}
            type="button"
          >
            <Codicon className="size-3" name="refresh" />
          </button>
        </div>
        <div className="max-h-72 overflow-y-auto">
          {loading && entries.length === 0 ? (
            <div className="flex items-center gap-2 px-2 py-3 text-muted-foreground/80 text-xs">
              <GlyphSpinner className="size-3" spinner="breathe" />
              <span>Loading…</span>
            </div>
          ) : error && entries.length === 0 ? (
            <div className="px-2 py-3 text-destructive text-xs">{error}</div>
          ) : entries.length === 0 ? (
            <div className="px-2 py-3 text-muted-foreground/80 text-xs">
              {t.workflows.libraryEmptyTitle}
            </div>
          ) : (
            <ul className="flex flex-col">
              {entries.map(entry => (
                <li key={entry.name}>
                  <div
                    className={cn(
                      'group flex w-full min-w-0 items-start gap-2 rounded-sm px-2 py-1.5',
                      'hover:bg-(--chrome-action-hover)'
                    )}
                  >
                    <Codicon
                      aria-hidden="true"
                      className="mt-0.5 size-3.5 shrink-0 text-muted-foreground/70"
                      name="file-code"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium text-foreground text-sm">
                        {entry.name}
                      </div>
                      <div className="line-clamp-1 text-muted-foreground/85 text-xs">
                        {entry.description}
                      </div>
                    </div>
                    <button
                      className={cn(
                        'inline-flex items-center gap-1 rounded-sm px-2 py-0.5 font-medium text-xs transition-colors',
                        'bg-primary/15 text-primary hover:bg-primary/25',
                        'disabled:opacity-50'
                      )}
                      disabled={starting === entry.name}
                      onClick={() => { void startRun(entry.name) }}
                      type="button"
                    >
                      {starting === entry.name ? (
                        <GlyphSpinner
                          ariaLabel={t.workflows.running}
                          className="size-3"
                          spinner="breathe"
                        />
                      ) : (
                        <Codicon aria-hidden="true" className="size-3" name="play" />
                      )}
                      <span>{t.workflows.runButton}</span>
                    </button>
                  </div>
                  {runErrors[entry.name] ? (
                    <div
                      aria-live="polite"
                      className="ml-5 mt-1 break-words text-[11px] text-destructive"
                      data-testid="workflow-run-error"
                      role="status"
                    >
                      {runErrors[entry.name]}
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}

// Expose the stores for testing + for other components that want to
// subscribe to the same library fetch (e.g. the WorkflowsView Library
// tab — both surfaces share the same underlying library data).
export const workflowTitlebarStores = {
  $library,
  $loading,
  $error,
  $starting,
  refresh: refreshLibrary,
  start: startRun,
}