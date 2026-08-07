import { attachedImageNotice } from '../domain/messages.js'
import { rewritePositionZeroDollarChain } from '../domain/dollarChain.js'
import { asCommandDispatch } from '../lib/rpc.js'
import type { GatewayClient } from '../gatewayClient.js'
import type { InputDetectDropResponse, PromptSubmitResponse } from '../gatewayTypes.js'
import type { Msg } from '../types.js'

import { turnController } from './turnController.js'
import { getUiState, patchUiState } from './uiStore.js'

const SESSION_BUSY_RE = /session busy|waiting for model response/i

export const isSessionBusyError = (e: unknown) => e instanceof Error && SESSION_BUSY_RE.test(e.message)

export interface SubmitPromptDeps {
  appendMessage: (msg: Msg) => void
  enqueue: (text: string) => void
  expand: (text: string) => string
  gw: GatewayClient
  setLastUserMsg: (value: string) => void
  sys: (text: string) => void
}

// Optimistically flip the session to busy the INSTANT a prompt is accepted for
// submission — synchronously, before we await anything.
//
// This is the fix for the queue-mode race (display.busy_input_mode: queue):
// the submit path first fires an async `input.detect_drop` RPC and only marked
// the session busy inside that RPC's `.then`. A second Enter pressed inside
// that round-trip window read `busy === false` in dispatchSubmission and raced
// a second `prompt.submit` onto the backend instead of landing in the local
// queue. That produced the reported symptom: the second message "waited for
// the first to respond, then went to the queue", and the client lost track of
// it (the backend accepts a mid-turn submit as {status:"queued"} — a success,
// not an error — so the local drain effect that watches the client-side queue
// never fires, leaving the UI stuck on "analyzing…" until Ctrl+C).
//
// Marking busy at the choke point closes the gap for every caller: the mainline
// submit, queue-edit picks, and the drain effect all funnel through here.
export function markSubmitting(): void {
  patchUiState({ busy: true, status: 'running…' })
}

// Submit a ready prompt (already resolved to be neither a slash command nor a
// shell escape, with a live session). Pulled out of useSubmission so the
// synchronous-busy invariant above is unit-testable without React test infra.
export function submitPrompt(text: string, deps: SubmitPromptDeps, showUserMessage = true): void {
  // Position-0 `$a $b do XYZ` rewrites to `/a /b do XYZ` BEFORE anything
  // else sees the text. The gateway's existing `slash.exec` → `command.dispatch`
  // path then resolves the stacked-skill / bundle invocation. Mid-prose
  // `$tokens` are NOT rewritten — the completion adapter treats them as
  // inline references.
  const rewrittenText = rewritePositionZeroDollarChain(text)

  // After the `$` rewrite, the rewritten text may BE a slash command (starts
  // with `/`). Slash commands must route through `slash.exec` (the gateway's
  // stacked-skill + bundle + plugin dispatch surface), NOT `prompt.submit`
  // (which treats the text as a plain user message and never loads any
  // skill bodies). The TUI checks for `/` at submit time — see `useSubmission.ts`
  // for the dispatch — but the REWRITTEN text needs the same check, because
  // the user's original `$skill` looks nothing like a slash command.
  //
  // The slash.exec → command.dispatch fallthrough is the gateway's standard
  // pattern (see `createSlashHandler.ts:135-161`): slash.exec rejects skill
  // commands with JSON-RPC error 4018 ("skill command: use command.dispatch
  // for /…"). The client rejects JSON-RPC errors as promise failures, so
  // we catch that and retry via command.dispatch — which is what actually
  // loads the skill bodies via the existing stacked-skill scaffold.
  if (rewrittenText !== text && rewrittenText.startsWith('/')) {
    const slashArg = rewrittenText.replace(/^\/+/, '')
    const liveSid = getUiState().sid
    if (!liveSid) return
    deps.gw
      .request('slash.exec', { command: slashArg, session_id: liveSid })
      .then((raw: unknown) => {
        const dispatch = asCommandDispatch(raw)
        if (!dispatch) return
        if (dispatch.type === 'send' && dispatch.message) {
          deps.sys(dispatch.notice ?? 'loading stacked skills')
          deps.gw
            .request('prompt.submit', { session_id: liveSid, text: dispatch.message })
            .catch(() => undefined)
        } else if (dispatch.type === 'skill' && dispatch.message) {
          deps.sys(`⚡ loading skill: ${dispatch.name}`)
          deps.gw
            .request('prompt.submit', { session_id: liveSid, text: dispatch.message })
            .catch(() => undefined)
        } else if (dispatch.type === 'exec' || dispatch.type === 'plugin') {
          deps.sys(dispatch.output ?? '(no output)')
        } else if (dispatch.type === 'alias' && dispatch.target) {
          deps.gw
            .request('slash.exec', { command: dispatch.target, session_id: liveSid })
            .catch(() => undefined)
        }
      })
      .catch(() => {
        // slash.exec rejects skill commands with JSON-RPC error 4018 —
        // the gateway's signal to fall through to command.dispatch. This
        // is where the actual stacked-skill scaffold gets built.
        const tokens = slashArg.split(/\s+/)
        const firstName = tokens[0]
        const rest = tokens.slice(1).join(' ')
        deps.gw
          .request('command.dispatch', {
            name: firstName,
            arg: rest,
            session_id: liveSid,
          })
          .then((raw: unknown) => {
            const dispatch = asCommandDispatch(raw)
            if (!dispatch) {
              deps.sys(`/${firstName}: not a known command`)
              return
            }
            if (dispatch.type === 'send' && dispatch.message) {
              deps.sys(dispatch.notice ?? 'loading stacked skills')
              deps.gw
                .request('prompt.submit', { session_id: liveSid, text: dispatch.message })
                .catch(() => undefined)
            } else if (dispatch.type === 'skill' && dispatch.message) {
              deps.sys(`⚡ loading skill: ${dispatch.name}`)
              deps.gw
                .request('prompt.submit', { session_id: liveSid, text: dispatch.message })
                .catch(() => undefined)
            } else if (dispatch.type === 'exec' || dispatch.type === 'plugin') {
              deps.sys(dispatch.output ?? '(no output)')
            } else if (dispatch.type === 'alias' && dispatch.target) {
              deps.gw
                .request('slash.exec', { command: dispatch.target, session_id: liveSid })
                .catch(() => undefined)
            }
          })
          .catch(() => undefined)
      })
    return
  }

  const sid = getUiState().sid

  if (!sid) {
    return deps.sys('session not ready yet')
  }

  // Close the async-busy gap up front, before the detect_drop round-trip.
  markSubmitting()

  const startSubmit = (displayText: string, submitText: string, show = true) => {
    const liveSid = getUiState().sid

    if (!liveSid) {
      return deps.sys('session not ready yet')
    }

    turnController.clearStatusTimer()
    deps.setLastUserMsg(text)

    if (show) {
      deps.appendMessage({ role: 'user', text: displayText })
    }

    patchUiState({ busy: true, status: 'running…' })
    turnController.bufRef = ''
    turnController.interrupted = false

    deps.gw
      .request<PromptSubmitResponse>('prompt.submit', { session_id: liveSid, text: submitText })
      .catch((e: Error) => {
        // Defensive: prompt.submit no longer rejects a mid-turn send with
        // "session busy" (the gateway queues it and returns success), but keep
        // the re-queue path as a safety net for any future/legacy gateway that
        // still errors, so a message is never silently dropped.
        if (isSessionBusyError(e)) {
          deps.enqueue(submitText)
          patchUiState({ busy: true, status: 'queued for next turn' })

          return deps.sys(`queued: "${submitText.slice(0, 50)}${submitText.length > 50 ? '…' : ''}"`)
        }

        deps.sys(`error: ${e.message}`)
        patchUiState({ busy: false, status: 'ready' })
      })
  }

  // Always ask the backend whether this looks like a file drop. The backend's
  // _detect_file_drop handles paths with spaces, quotes, Windows drive letters,
  // and escaped characters correctly.
  deps.gw
    .request<InputDetectDropResponse>('input.detect_drop', { session_id: sid, text })
    .then(r => {
      if (!r?.matched) {
        return startSubmit(rewrittenText, deps.expand(rewrittenText), showUserMessage)
      }

      if (r.is_image) {
        turnController.pushActivity(attachedImageNotice(r))
      } else {
        turnController.pushActivity(`detected file: ${r.name}`)
      }

      startSubmit(r.text || rewrittenText, deps.expand(r.text || rewrittenText), showUserMessage)
    })
    .catch(() => startSubmit(rewrittenText, deps.expand(rewrittenText), showUserMessage))
}
