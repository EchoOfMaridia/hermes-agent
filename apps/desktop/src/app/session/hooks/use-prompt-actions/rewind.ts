/**
 * Shared rewind/interrupt core for the prompt verbs — the ONE implementation
 * of the submit primitive + the pure message math behind cancel / reload /
 * restore / edit / branch-visibility. Both the primary chat (`index.ts`) and
 * session tiles (`session-tile-actions.ts`) build on these so the two surfaces
 * can't silently diverge (the tile's "sends only once" busy-ref bug was exactly
 * that class of drift). The functions here are PURE — planners compute from a
 * `ChatMessage[]`, optimistic transforms map a `ClientSessionState` to the next
 * — so each caller keeps its own state-write + error-handling wiring.
 *
 * PER OPERATOR POLICY 2026-07-16: This file was extended with the contents of
 * the upstream `utils.ts` and `submit.ts` siblings so that `session-tile-actions.ts`
 * can compile against our tree without restoring the upstream folder split
 * (which conflicts with our single-file `use-prompt-actions.ts`). All
 * utils.ts / submit.ts exports are re-exported below.
 */

import type { AppendMessage, ThreadMessage } from '@assistant-ui/react'
import { type MutableRefObject, useCallback } from 'react'

import type { ClientSessionState } from '@/app/types'
import { PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/hermes'
import { translateNow, type Translations } from '@/i18n'
import {
  branchGroupForUser,
  type ChatMessage,
  chatMessageText,
  textPart
} from '@/lib/chat-messages'
import { optimisticAttachmentRef } from '@/lib/chat-runtime'
import { sanitizeComposerInput } from '@/lib/composer-input-sanitize'
import { type CommandsCatalogLike, filterDesktopCommandsCatalog } from '@/lib/desktop-slash-commands'
import { setMutableRef } from '@/lib/mutable-ref'
import { isProviderSetupErrorMessage } from '@/lib/provider-setup-errors'
import {
  $composerAttachments,
  clearComposerAttachments,
  type ComposerAttachment,
  terminalContextBlocksFromDraft
} from '@/store/composer'
import { clearNotifications, notify, notifyError } from '@/store/notifications'
import { requestDesktopOnboarding } from '@/store/onboarding'
import { setAwaitingResponse, setBusy, setMessages } from '@/store/session'

type RequestGateway = <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

// ============================================================================
// PORTED FROM utils.ts (per port-to-rewind.ts policy)
// ============================================================================

export type GatewayRequest = <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

export function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

export function isSessionIdCandidate(value: string): boolean {
  const trimmed = value.trim()

  return /^\d{8}_\d{6}_[A-Fa-f0-9]{6}$/.test(trimmed) || /^[A-Fa-f0-9]{32}$/.test(trimmed)
}

export function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()

    reader.addEventListener('load', () => {
      if (typeof reader.result === 'string') {
        resolve(reader.result)
      } else {
        reject(new Error(translateNow('desktop.audioReadFailed')))
      }
    })
    reader.addEventListener('error', () => reject(reader.error || new Error(translateNow('desktop.audioReadFailed'))))
    reader.readAsDataURL(blob)
  })
}

export function isProviderSetupError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error)

  return isProviderSetupErrorMessage(message)
}

export function inlineErrorMessage(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : fallback

  return (raw.match(/Error invoking remote method '[^']+': Error: (.+)$/)?.[1] ?? raw).replace(/^Error:\s*/, '').trim()
}

export function isSessionNotFoundError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /session not found/i.test(message)
}

// Gateway JSON-RPC calls reject with "request timed out: <method>" when the
// backend event loop is starved (e.g. a poller spin or a heavy async-injected
// turn). For prompt.submit this is indistinguishable from a dead runtime
// session on the client side — recovery must treat it like one (#55578):
// resume the SELECTED stored session and retry, instead of surfacing an error
// that leads to a null activeSessionId and a silently minted new session.
export function isGatewayTimeoutError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /request timed out/i.test(message)
}

// The gateway refuses prompt.submit while a turn is running (4009 "session
// busy"). It's a transient concurrency guard, never a user-facing error: a
// submit racing the settle edge (or a rewind interrupting mid-turn) just waits
// a beat for the turn to wind down, then lands. Bounded so a genuinely stuck
// turn still surfaces eventually.
export const SESSION_BUSY_RETRY_TIMEOUT_MS = 6_000
export const SESSION_BUSY_RETRY_INTERVAL_MS = 150

export function isSessionBusyError(error: unknown): boolean {
  return /session busy/i.test(error instanceof Error ? error.message : String(error))
}

const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

// Retry a gateway call across transient "session busy" so it never reaches the
// user — the turn settles within the deadline and the call lands.
export async function withSessionBusyRetry<T>(call: () => Promise<T>): Promise<T> {
  const deadline = Date.now() + SESSION_BUSY_RETRY_TIMEOUT_MS

  for (;;) {
    try {
      return await call()
    } catch (err) {
      if (isSessionBusyError(err) && Date.now() < deadline) {
        await sleep(SESSION_BUSY_RETRY_INTERVAL_MS)

        continue
      }

      throw err
    }
  }
}

// Hard guard: at most one prompt.submit in flight per session. Every submit
// path — user Enter, queue drain, busy-retry, slash fallthrough — funnels
// through submitPromptText. Without this, a stalled turn (e.g. a context-bloated
// session whose first call hangs) let the SAME prompt launch several real turns
// at once (the "message stacked 5×" bug). Keyed by stored/active session id.
export const _submitInFlight = new Set<string>()

export function base64FromDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(',')

  return comma >= 0 ? dataUrl.slice(comma + 1) : ''
}

export function imageFilenameFromPath(filePath: string): string {
  return filePath.split(/[\\/]/).filter(Boolean).pop() || 'image.png'
}

// Remote gateway: the local composer-image file lives on THIS machine's disk,
// not the gateway's, so read the bytes here and upload them via
// image.attach_bytes. Returns null when the file can't be read.
export async function readImageForRemoteAttach(
  filePath: string
): Promise<{ contentBase64: string; filename: string } | null> {
  const dataUrl = await window.hermesDesktop?.readFileDataUrl(filePath)
  const contentBase64 = dataUrl ? base64FromDataUrl(dataUrl) : ''

  return contentBase64 ? { contentBase64, filename: imageFilenameFromPath(filePath) } : null
}

// Read a non-image file as a data URL for upload via file.attach. Returns null
// when the desktop bridge can't read the file (e.g. it was moved/deleted).
export async function readFileDataUrlForAttach(filePath: string): Promise<string | null> {
  const reader = window.hermesDesktop?.readFileDataUrl

  if (!reader) {
    return null
  }

  const dataUrl = await reader(filePath)

  return dataUrl || null
}

// The readFileDataUrl IPC base64-loads the whole file into memory and is
// hard-capped (DATA_URL_READ_MAX_BYTES, 16 MB) in electron/hardening.ts, which
// rejects with a raw "file is too large (N bytes; limit M bytes)" string. In
// remote mode every attachment's bytes go through that read, so a big file
// surfaces that internal message verbatim in the failure toast. Translate it
// into a friendly "too large to upload to the remote gateway" line, parsing the
// limit out of the message so it tracks the real cap. Non-cap errors pass
// through unchanged.
export function friendlyRemoteAttachError(err: unknown, label: string): Error {
  const message = err instanceof Error ? err.message : String(err)

  if (!/too large/i.test(message)) {
    return err instanceof Error ? err : new Error(message)
  }

  const limitBytes = Number(message.match(/limit (\d+) bytes/)?.[1])
  const cap = Number.isFinite(limitBytes) && limitBytes > 0 ? ` (max ${Math.floor(limitBytes / (1024 * 1024))} MB)` : ''

  return new Error(`${label} is too large to upload to the remote gateway${cap}.`)
}

export function renderCommandsCatalog(catalog: CommandsCatalogLike, copy: Translations['desktop']): string {
  const desktopCatalog = filterDesktopCommandsCatalog(catalog)

  const sections = desktopCatalog.categories?.length
    ? desktopCatalog.categories
    : [{ name: copy.desktopCommands, pairs: desktopCatalog.pairs ?? [] }]

  const body = sections
    .filter(section => section.pairs.length > 0)
    .map(section => {
      const rows = section.pairs.map(([cmd, desc]) => `${cmd.padEnd(18)} ${desc}`)

      return [`${section.name}:`, ...rows].join('\n')
    })
    .join('\n\n')

  const tail = [
    desktopCatalog.skill_count ? copy.skillCommandsAvailable(desktopCatalog.skill_count) : '',
    desktopCatalog.warning ? copy.warningLine(desktopCatalog.warning) : ''
  ]
    .filter(Boolean)
    .join('\n')

  return [body || 'No desktop commands available.', tail].filter(Boolean).join('\n\n')
}

export function slashStatusText(command: string, output: string): string {
  return [`slash:${command}`, output.trim()].filter(Boolean).join('\n')
}

export function appendText(message: AppendMessage): string {
  return message.content
    .map(part => ('text' in part ? part.text : ''))
    .join('')
    .trim()
}

export function visibleUserOrdinal(messages: readonly ChatMessage[], end: number): number {
  return messages.slice(0, end).filter(m => m.role === 'user' && !m.hidden).length
}

export function visibleUserIndexAtOrdinal(messages: readonly ChatMessage[], targetOrdinal: number): number {
  let ordinal = 0

  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]

    if (message.role !== 'user' || message.hidden) {
      continue
    }

    if (ordinal === targetOrdinal) {
      return index
    }

    ordinal += 1
  }

  return -1
}

export interface SubmitTextOptions {
  attachments?: ComposerAttachment[]
  fromQueue?: boolean
}

// ============================================================================
// ORIGINAL rewind.ts content (from upstream — current signatures)
// ============================================================================

/**
 * Rewind a turn: `prompt.submit` with an optional `truncate_before_user_ordinal`
 * (drops that user turn + everything after). Idle rewinds submit directly
 * (interrupting an idle agent can leave a stale interrupt flag that cancels the
 * fresh turn); live/stuck turns interrupt first, and a raced "session busy"
 * response interrupts + retries through the shared busy gate.
 */
export async function runRewindSubmit(
  requestGateway: RequestGateway,
  sessionId: string,
  text: string,
  truncateOrdinal: number | undefined,
  interruptFirst: boolean
): Promise<void> {
  const interrupt = async () => {
    try {
      await requestGateway('session.interrupt', { session_id: sessionId })
    } catch {
      // Best-effort. The submit path still gates on the gateway state.
    }
  }

  const submit = () =>
    requestGateway(
      'prompt.submit',
      {
        session_id: sessionId,
        text,
        ...(truncateOrdinal !== undefined && { truncate_before_user_ordinal: truncateOrdinal })
      },
      PROMPT_SUBMIT_REQUEST_TIMEOUT_MS
    )

  if (interruptFirst) {
    await interrupt()
  }

  try {
    await submit()
  } catch (err) {
    if (!isSessionBusyError(err)) {
      throw err
    }

    await interrupt()
    await withSessionBusyRetry(submit)
  }
}

/** Cancel/stop finalize: drop empty pending/stream placeholders, un-pend the rest. */
export function finalizeInterruptedMessages(messages: ChatMessage[], streamId?: null | string): ChatMessage[] {
  return messages
    .filter(message => !((message.pending || message.id === streamId) && !chatMessageText(message).trim()))
    .map(message => (message.pending || message.id === streamId ? { ...message, pending: false } : message))
}

// ---------------------------------------------------------------------------
// Reload (regenerate)
// ---------------------------------------------------------------------------

export interface ReloadPlan {
  branchGroupId: string
  text: string
  truncateOrdinal: number
  userIndex: number
}

/** The user turn to re-run for a reload from `parentId` (or the last turn). */
export function planReload(messages: ChatMessage[], parentId: null | string): null | ReloadPlan {
  const parentIndex = parentId ? messages.findIndex(m => m.id === parentId) : messages.length - 1

  const userBack =
    parentIndex >= 0 ? [...messages.slice(0, parentIndex + 1)].reverse().findIndex(m => m.role === 'user') : -1

  if (userBack < 0) {
    return null
  }

  const userIndex = parentIndex - userBack
  const userMessage = messages[userIndex]
  const text = userMessage ? chatMessageText(userMessage).trim() : ''

  if (!userMessage || !text) {
    return null
  }

  const targetAssistant =
    parentId && messages[parentIndex]?.role === 'assistant'
      ? messages[parentIndex]
      : messages.slice(userIndex + 1).find(m => m.role === 'assistant')

  return {
    branchGroupId: targetAssistant?.branchGroupId ?? branchGroupForUser(userMessage),
    text,
    truncateOrdinal: visibleUserOrdinal(messages, userIndex),
    userIndex
  }
}

/** Optimistic reload state: keep the user turn, hide the branch's assistants. */
export function applyReloadOptimistic(state: ClientSessionState, plan: ReloadPlan): ClientSessionState {
  const nextUserIndex = state.messages.findIndex((m, i) => i > plan.userIndex && m.role === 'user')
  const end = nextUserIndex < 0 ? state.messages.length : nextUserIndex

  return {
    ...state,
    awaitingResponse: true,
    busy: true,
    interrupted: false,
    messages: [
      ...state.messages.slice(0, plan.userIndex + 1),
      ...state.messages
        .slice(plan.userIndex + 1, end)
        .map(m => (m.role === 'assistant' ? { ...m, branchGroupId: plan.branchGroupId, hidden: true } : m))
    ],
    pendingBranchGroup: plan.branchGroupId,
    sawAssistantPayload: false
  }
}

// ---------------------------------------------------------------------------
// Restore (rewind checkpoint)
// ---------------------------------------------------------------------------

export interface RestoreTarget {
  text?: string
  userOrdinal?: null | number
}

export interface RestorePlan {
  sourceIndex: number
  text: string
  truncateOrdinal: number
}

/** Resolve the user turn to rewind to; throws with a user-facing reason. */
export function planRestore(messages: ChatMessage[], messageId: string, target?: RestoreTarget): RestorePlan {
  const idIndex = messages.findIndex(m => m.id === messageId && m.role === 'user')

  const fallbackIndex =
    target?.userOrdinal === null || target?.userOrdinal === undefined
      ? -1
      : visibleUserIndexAtOrdinal(messages, target.userOrdinal)

  const sourceIndex = idIndex >= 0 ? idIndex : fallbackIndex
  const source = messages[sourceIndex]

  if (!source || source.role !== 'user') {
    throw new Error('Could not find the message to restore.')
  }

  const text = (chatMessageText(source).trim() || target?.text?.trim() || '').trim()

  if (!text) {
    throw new Error('Cannot restore an empty message.')
  }

  const truncateOrdinal =
    target?.userOrdinal === null || target?.userOrdinal === undefined
      ? visibleUserOrdinal(messages, sourceIndex)
      : target.userOrdinal

  return { sourceIndex, text, truncateOrdinal }
}

// ---------------------------------------------------------------------------
// Edit (revert + resubmit with new text)
// ---------------------------------------------------------------------------

export interface EditPlan {
  editedMessage: ChatMessage
  isFailedTurn: boolean
  sourceIndex: number
  text: string
  truncateOrdinal: number | undefined
}

/** Resolve the edited user turn, or null when nothing changed / invalid. */
export function planEdit(messages: ChatMessage[], edited: AppendMessage): EditPlan | null {
  const sourceId = edited.sourceId || edited.parentId
  const text = appendText(edited)

  if (!sourceId || !text || edited.role !== 'user') {
    return null
  }

  const sourceIndex = messages.findIndex(m => m.id === sourceId)
  const source = messages[sourceIndex]

  if (!source || source.role !== 'user' || chatMessageText(source).trim() === text) {
    return null
  }

  // Failed turn: the optimistic user msg never reached the gateway, so a
  // truncate-by-ordinal would 422 — resubmit plainly instead.
  const nextMessage = messages[sourceIndex + 1]
  const isFailedTurn = nextMessage?.role === 'assistant' && Boolean(nextMessage.error)

  return {
    editedMessage: { ...source, parts: [textPart(text)] },
    isFailedTurn,
    sourceIndex,
    text,
    truncateOrdinal: isFailedTurn ? undefined : visibleUserOrdinal(messages, sourceIndex)
  }
}

/** Optimistic rewind-to state for restore/edit: drop everything after the
 *  source turn (edit swaps in the edited message; restore keeps the original). */
export function applyRewindOptimistic(
  state: ClientSessionState,
  sourceIndex: number,
  editedMessage?: ChatMessage
): ClientSessionState {
  return {
    ...state,
    awaitingResponse: true,
    busy: true,
    interrupted: false,
    messages: editedMessage
      ? [...state.messages.slice(0, sourceIndex), editedMessage]
      : state.messages.slice(0, sourceIndex + 1),
    pendingBranchGroup: null,
    sawAssistantPayload: false
  }
}

// ---------------------------------------------------------------------------
// Branch visibility (assistant-ui hides non-active branches)
// ---------------------------------------------------------------------------

/** Sync each assistant branch message's `hidden` to what the thread renders. */
export function applyBranchVisibility(state: ClientSessionState, next: readonly ThreadMessage[]): ClientSessionState {
  const visibleIds = new Set(next.map(m => m.id))
  let changed = false

  const messages = state.messages.map(message => {
    if (message.role !== 'assistant' || !message.branchGroupId) {
      return message
    }

    const hidden = !visibleIds.has(message.id)

    if (message.hidden === hidden) {
      return message
    }

    changed = true

    return { ...message, hidden }
  })

  return changed ? { ...state, messages } : state
}

// ============================================================================
// PORTED FROM submit.ts (useSubmitPrompt hook)
// ============================================================================

interface SubmitPromptDeps {
  activeSessionId: string | null
  activeSessionIdRef: MutableRefObject<string | null>
  busyRef: MutableRefObject<boolean>
  copy: Translations['desktop']
  createBackendSessionForSend: (preview?: string | null) => Promise<string | null>
  getRouteToken: () => string
  requestGateway: GatewayRequest
  selectedStoredSessionIdRef: MutableRefObject<string | null>
  syncAttachmentsForSubmit: (
    sessionId: string,
    attachments: ComposerAttachment[],
    options?: { updateComposerAttachments?: boolean }
  ) => Promise<ComposerAttachment[]>
  updateSessionState: (
    sessionId: string,
    updater: (state: ClientSessionState) => ClientSessionState,
    storedSessionId?: string | null
  ) => ClientSessionState
  /** Composer-scope seams: the main chat runs on the module-level globals
   *  (defaults); a session tile injects its own so a tile submit never writes
   *  the primary view's $busy/$messages or clears the main attachment chips. */
  scope?: {
    clearAttachments: () => void
    readAttachments: () => ComposerAttachment[]
    setAwaitingResponse: (awaiting: boolean) => void
    setBusy: (busy: boolean) => void
    setMessages: (updater: (current: ChatMessage[]) => ChatMessage[]) => void
  }
}

// Stable identity — a fresh default object per render would churn the
// useCallback below on every render.
const MAIN_SUBMIT_SCOPE: NonNullable<SubmitPromptDeps['scope']> = {
  clearAttachments: clearComposerAttachments,
  readAttachments: () => $composerAttachments.get(),
  setAwaitingResponse,
  setBusy,
  setMessages
}

/** The prompt submit pipeline, extracted from usePromptActions. */
export function useSubmitPrompt(deps: SubmitPromptDeps) {
  const {
    activeSessionId,
    activeSessionIdRef,
    busyRef,
    copy,
    createBackendSessionForSend,
    getRouteToken,
    requestGateway,
    selectedStoredSessionIdRef,
    syncAttachmentsForSubmit,
    updateSessionState,
    scope = MAIN_SUBMIT_SCOPE
  } = deps

  return useCallback(
    async (rawText: string, options?: SubmitTextOptions) => {
      const visibleText = sanitizeComposerInput(rawText).trim()
      const usingComposerAttachments = !options?.attachments

      // Drop undefined/null holes a session switch or draft restore can leave in
      // the attachments array (same bug class as AttachmentList #49624). Without
      // this, the sibling iterations below (a.kind / a.label / a.refText, and the
      // sync step) throw "Cannot read properties of undefined (reading 'refText')"
      // and break the chat surface.
      const attachments = (options?.attachments ?? scope.readAttachments()).filter((a): a is ComposerAttachment =>
        Boolean(a)
      )

      const terminalContextBlocks = terminalContextBlocksFromDraft(rawText).join('\n\n')
      const hasImage = attachments.some(a => a.kind === 'image')

      // Refs are recomputed after sync (file.attach rewrites @file: refs to
      // workspace-relative paths the remote gateway can resolve). Seed the
      // optimistic message with the pre-sync refs, then rewrite once synced.
      // Images use their base64 preview so the thumbnail renders inline without
      // a (remote-mode 403-prone) /api/media fetch — see optimisticAttachmentRef.
      let attachmentRefs = attachments.map(optimisticAttachmentRef).filter((r): r is string => Boolean(r))

      const buildContextText = (atts: ComposerAttachment[]): string => {
        // atts may be the post-sync array, which can reintroduce holes; filter
        // before touching a.refText / a.kind.
        const present = atts.filter((a): a is ComposerAttachment => Boolean(a))

        const contextRefs = present
          .map(a => a.refText)
          .filter(Boolean)
          .join('\n')

        return (
          [contextRefs, terminalContextBlocks, visibleText].filter(Boolean).join('\n\n') ||
          (present.some(a => a.kind === 'image') ? 'What do you see in this image?' : '')
        )
      }

      // Queue drains fire on the busy→false settle edge, where busyRef (synced
      // from $busy by a separate effect) may still read true — honoring it would
      // bounce the drained send. The drain lock serializes them; the user path
      // keeps the guard so a stray Enter mid-turn can't double-submit.
      const hasSendable = Boolean(visibleText || terminalContextBlocks || attachments.length || hasImage)

      if (!hasSendable || (!options?.fromQueue && busyRef.current)) {
        return false
      }

      // Pin the session context for the whole async submit pipeline. Without
      // this, a fast session switch during session.resume / file.attach can
      // redirect the user's text into a different chat (#54527). Mutable —
      // not const — because a new-chat submit legitimately re-homes to the
      // session it creates (see the re-pin after createBackendSessionForSend).
      const startingActiveSessionId = activeSessionIdRef.current
      let startingStoredSessionId = selectedStoredSessionIdRef.current
      let startingRouteToken = getRouteToken()

      const sessionContextDrifted = (): boolean =>
        selectedStoredSessionIdRef.current !== startingStoredSessionId || getRouteToken() !== startingRouteToken

      // One submit in flight per session — drop any concurrent re-fire so a
      // stalled turn can't stack the same prompt into multiple real turns.
      const submitLockKey = startingStoredSessionId || startingActiveSessionId || '__pending_new__'

      if (_submitInFlight.has(submitLockKey)) {
        return false
      }

      _submitInFlight.add(submitLockKey)
      let submitLockReleased = false

      const releaseSubmitLock = () => {
        if (!submitLockReleased) {
          submitLockReleased = true
          _submitInFlight.delete(submitLockKey)
        }
      }

      const optimisticId = `user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

      const buildUserMessage = (): ChatMessage => ({
        id: optimisticId,
        role: 'user',
        parts: [textPart(visibleText || (attachmentRefs.length ? '' : attachments.map(a => a.label).join(', ')))],
        attachmentRefs
      })

      const releaseBusy = () => {
        releaseSubmitLock()
        setMutableRef(busyRef, false)
        scope.setBusy(false)
        scope.setAwaitingResponse(false)
      }

      // Idempotent optimistic insert — re-running with the resolved sessionId
      // after createBackendSessionForSend just overwrites with the same id.
      const seedOptimistic = (sid: string) =>
        updateSessionState(
          sid,
          state => ({
            ...state,
            messages: state.messages.some(m => m.id === optimisticId)
              ? state.messages
              : [...state.messages, buildUserMessage()],
            busy: true,
            awaitingResponse: true,
            pendingBranchGroup: null,
            sawAssistantPayload: false,
            // Fresh submit = new turn — clear any leftover interrupt flag, else
            // mutateStream/completeAssistantMessage drop every delta of this turn
            // (what made drained-after-interrupt sends go silent).
            interrupted: false
          }),
          startingStoredSessionId
        )

      // After sync rewrites refs, refresh the optimistic message in place so the
      // transcript shows the resolved @file: ref rather than the local path.
      const rewriteOptimistic = (sid: string) =>
        updateSessionState(
          sid,
          state => ({
            ...state,
            messages: state.messages.map(message => (message.id === optimisticId ? buildUserMessage() : message))
          }),
          startingStoredSessionId
        )

      const dropOptimistic = (sid: null | string) => {
        if (!sid) {
          scope.setMessages(current => current.filter(m => m.id !== optimisticId))

          return
        }

        updateSessionState(
          sid,
          state => ({
            ...state,
            messages: state.messages.filter(m => m.id !== optimisticId),
            busy: false,
            awaitingResponse: false,
            pendingBranchGroup: null
          }),
          startingStoredSessionId
        )
      }

      const abortForSessionSwitch = (optimisticSessionId: null | string): false => {
        dropOptimistic(optimisticSessionId)
        releaseBusy()

        return false
      }

      setMutableRef(busyRef, true)
      scope.setBusy(true)
      scope.setAwaitingResponse(true)
      clearNotifications()

      let sessionId: null | string = activeSessionId

      if (sessionId) {
        seedOptimistic(sessionId)
      } else {
        scope.setMessages(current => [...current, buildUserMessage()])
      }

      if (!sessionId && startingStoredSessionId) {
        // A stored session is SELECTED but its runtime binding is gone (the
        // live session was orphan-reaped, or a timeout/reconnect cleared
        // activeSessionId). Continuing the selected conversation must mean
        // resuming it — minting a brand-new backend session here silently
        // splits the user's chat in two (#55578 symptom b). Only fall through
        // to session creation when NO stored session is selected (a genuine
        // new-chat draft).
        try {
          const resumed = await requestGateway<{ session_id: string }>('session.resume', {
            session_id: startingStoredSessionId
          })

          if (sessionContextDrifted()) {
            return abortForSessionSwitch(sessionId)
          }

          if (resumed?.session_id) {
            sessionId = resumed.session_id
            activeSessionIdRef.current = sessionId
          }
        } catch {
          // Resume failed (session gone from state.db, gateway hiccup) —
          // fall through to creating a fresh session rather than dead-ending
          // the user's message.
        }

        if (sessionContextDrifted()) {
          return abortForSessionSwitch(sessionId)
        }

        if (sessionId) {
          seedOptimistic(sessionId)
        }
      }

      if (!sessionId) {
        try {
          sessionId = await createBackendSessionForSend(visibleText)
        } catch (err) {
          dropOptimistic(null)
          releaseBusy()
          notifyError(err, copy.sessionUnavailable)

          return false
        }

        if (!sessionId) {
          // createBackendSessionForSend returns null when the user switched
          // sessions mid-create (it closes the orphaned session itself) —
          // abort silently. Anything else is a real failure worth a toast.
          if (sessionContextDrifted()) {
            return abortForSessionSwitch(null)
          }

          dropOptimistic(null)
          releaseBusy()
          notify({ kind: 'error', title: copy.sessionUnavailable, message: copy.createSessionFailed })

          return false
        }

        // A successful create re-homes selection + route to the chat it just
        // minted, so the pre-create baseline can't tell our own re-home from
        // a user switch (judging it drift aborted EVERY first send of a new
        // chat: no prompt.submit, no DB row, a stranded route that 404s
        // "Session not found"). The drift signal for this window is the
        // active ref instead: every switch path re-nulls or retargets it
        // synchronously, so it only still equals the id create returned when
        // nobody re-homed since.
        if (activeSessionIdRef.current !== sessionId) {
          return abortForSessionSwitch(sessionId)
        }

        // Re-pin the baseline to the created chat for the rest of the
        // pipeline; the closures (seedOptimistic et al) see the new value.
        startingStoredSessionId = selectedStoredSessionIdRef.current
        startingRouteToken = getRouteToken()

        seedOptimistic(sessionId)
      }

      try {
        const syncedAttachments = await syncAttachmentsForSubmit(sessionId, attachments, {
          updateComposerAttachments: usingComposerAttachments
        })

        if (sessionContextDrifted()) {
          return abortForSessionSwitch(sessionId)
        }

        // Rewrite the optimistic message + prompt text with the synced refs so
        // the gateway receives @file: paths that resolve in its workspace.
        // (Images keep their inline base64 preview — see optimisticAttachmentRef.)
        attachmentRefs = syncedAttachments.map(optimisticAttachmentRef).filter((r): r is string => Boolean(r))
        rewriteOptimistic(sessionId)
        const text = buildContextText(syncedAttachments)

        // On sleep/wake the gateway's in-memory session may have been cleared
        // while the desktop app still holds the old session ID. Detect this,
        // resume the stored session to re-register it, and retry once.
        let submitErr: unknown = null

        try {
          await withSessionBusyRetry(() =>
            requestGateway('prompt.submit', { session_id: sessionId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
          )
        } catch (firstErr) {
          if ((isSessionNotFoundError(firstErr) || isGatewayTimeoutError(firstErr)) && startingStoredSessionId) {
            // Re-register the session in the gateway and get a fresh live ID.
            // Timeouts recover the same way as "session not found": a starved
            // backend loop (#55578 symptom d) rejects the submit even though
            // the stored session is fine — resume + retry instead of erroring
            // out and losing the session binding.
            const resumed = await requestGateway<{ session_id: string }>('session.resume', {
              session_id: startingStoredSessionId,
              source: 'desktop'
            })

            if (sessionContextDrifted()) {
              return abortForSessionSwitch(sessionId)
            }

            const recoveredId = resumed?.session_id

            if (recoveredId) {
              activeSessionIdRef.current = recoveredId
              await withSessionBusyRetry(() =>
                requestGateway('prompt.submit', { session_id: recoveredId, text }, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
              )
            } else {
              submitErr = firstErr
            }
          } else {
            submitErr = firstErr
          }
        }

        if (submitErr !== null) {
          throw submitErr
        }

        if (usingComposerAttachments) {
          scope.clearAttachments()
        }

        // Submit landed — the turn now runs (busy stays true), but the submit
        // window is closed, so release the lock for the next (sequential) send.
        releaseSubmitLock()

        return true
      } catch (err) {
        releaseBusy()

        // A queued drain that raced a not-yet-settled turn gets a transient
        // "session busy" (4009). Don't surface an error bubble/toast — the entry
        // stays queued and the composer's bounded auto-drain retries when idle.
        if (options?.fromQueue && isSessionBusyError(err)) {
          return false
        }

        const message = inlineErrorMessage(err, copy.promptFailed)

        updateSessionState(sessionId, state => ({
          ...state,
          messages: [
            ...state.messages,
            {
              id: `assistant-error-${Date.now()}`,
              role: 'assistant',
              parts: [],
              error: message || copy.promptFailed,
              branchGroupId: state.pendingBranchGroup ?? undefined
            }
          ],
          busy: false,
          awaitingResponse: false,
          pendingBranchGroup: null,
          sawAssistantPayload: true
        }))

        if (isProviderSetupError(err)) {
          requestDesktopOnboarding(copy.providerCredentialRequired)

          return false
        }

        notifyError(err, copy.promptFailed)

        return false
      }
    },
    [
      activeSessionId,
      activeSessionIdRef,
      busyRef,
      copy,
      createBackendSessionForSend,
      getRouteToken,
      requestGateway,
      scope,
      selectedStoredSessionIdRef,
      syncAttachmentsForSubmit,
      updateSessionState
    ]
  )
}