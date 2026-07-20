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
const $starting = atom<string | null>(null)

function readSessionToken(): string {
  if (typeof window === 'undefined') {return ''}
  return (window as unknown as { __HERMES_SESSION_TOKEN__?: string })
    .__HERMES_SESSION_TOKEN__ ?? ''
}

async function refreshLibrary(): Promise<void> {
  const token = readSessionToken()
  if (!token) {
    $error.set('no session token')
    return
  }
  $loading.set(true)
  $error.set(null)
  try {
    const r = await fetch('/api/workflows/library', {
      headers: { Authorization: `Bearer ${token}` }
    })
    if (!r.ok) {throw new Error(`library fetch failed: ${r.status}`)}
    const data = (await r.json()) as WorkflowLibraryResponse
    $library.set(data.entries)
  } catch (exc) {
    $error.set(exc instanceof Error ? exc.message : String(exc))
  } finally {
    $loading.set(false)
  }
}

async function startRun(name: string): Promise<void> {
  const token = readSessionToken()
  if (!token) {return}
  $starting.set(name)
  try {
    await fetch('/api/workflows/run', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify({ name, inputs: {} })
    })
  } catch {
    // Surfaced via $error; live-progress events will populate the
    // overlay panel even if the start call briefly errors.
  } finally {
    $starting.set(null)
  }
}

export function WorkflowsTitlebarMenu() {
  const { t } = useI18n()
  const entries = useStore($library)
  const loading = useStore($loading)
  const error = useStore($error)
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
            className="rounded-sm p-0.5 text-muted-foreground/70 hover:bg-(--chrome-action-hover) hover:text-foreground"
            onClick={() => { void refreshLibrary() }}
            type="button"
            aria-label="refresh"
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