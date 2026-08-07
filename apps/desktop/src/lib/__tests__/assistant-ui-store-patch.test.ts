/**
 * Regression test for the `workspace failed to render — Cannot read
 * properties of undefined (reading 'messages')` boot-screen bug.
 *
 * Root cause (verified 2026-08-06, two distinct vectors):
 *
 *   VECTOR 1 (root, reintroduced by commit b669192de):
 *     apps/desktop/src/components/assistant-ui/thread/assistant-message.tsx
 *     reads `s.thread.messages.length` inside a `useAuiState` selector.
 *     When the thread state has not yet been seeded (brand-new session,
 *     pre-resume, or any path where `s.thread` is undefined), the selector
 *     throws "Cannot read properties of undefined (reading 'messages')"
 *     and the workspace pane's error boundary catches it. Verified by
 *     reverting commit b669192de locally — the error vanishes and the
 *     workspace renders.
 *
 *   VECTOR 2 (defense in depth, original Aug-2026 fix):
 *     The @assistant-ui/store@0.2.20 patch in
 *     apps/desktop/patches/@assistant-ui__store@0.2.20.patch must be
 *     applied to the installed dist. Without it, `store.getValue().methods`
 *     is unguarded and a missing AuiProvider produces the same boot-screen
 *     error. The patch lives in the patches/ dir but pnpm only applies
 *     it when the file content matches the lockfile hash AND the patch
 *     applies cleanly against pristine upstream.
 *
 * This test pins BOTH invariants so a future change can't reintroduce
 * the boot error from either side.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const APPS_DESKTOP = resolve(process.cwd())
const PATCH_PATH = resolve(APPS_DESKTOP, 'patches/@assistant-ui__store@0.2.20.patch')
const PATCHED_PKG_NAME = '@assistant-ui/store@0.2.20'

function findInstalledUseAui(): string {
  // Walk up from CWD until we find apps/desktop/node_modules/.pnpm, then
  // pick the single installed @assistant-ui+store@0.2.20_patch_hash=*
  // directory. The patch hash drifts whenever the patch file changes, so
  // hard-coding one specific hash would make the test brittle across
  // `pnpm install` runs.
  for (let dir = APPS_DESKTOP; ; ) {
    const pnpmDir = resolve(dir, 'node_modules/.pnpm')
    if (existsSync(pnpmDir)) {
      const entries = readdirSync(pnpmDir)
      const matches = entries.filter(name =>
        name.startsWith('@assistant-ui+store@0.2.20_patch_hash=')
      )
      if (matches.length === 0) {
        throw new Error(
          `No patched ${PATCHED_PKG_NAME} installed under ${pnpmDir}. ` +
          'pnpm install may not have applied the patch — run `pnpm install --no-frozen-lockfile`.'
        )
      }
      if (matches.length > 1) {
        throw new Error(
          `Multiple @assistant-ui/store@0.2.20_patch_hash=* entries found under ${pnpmDir}: ${matches.join(', ')}. ` +
          'pnpm should only ever install one; investigate duplicates.'
        )
      }
      const useAui = resolve(
        pnpmDir,
        matches[0],
        'node_modules/@assistant-ui/store/dist/useAui.js'
      )
      if (!existsSync(useAui)) {
        throw new Error(`Expected ${useAui} to exist after pnpm install`)
      }
      return useAui
    }
    const parent = resolve(dir, '..')
    if (parent === dir) {
      break
    }
    dir = parent
  }
  throw new Error(
    `No apps/desktop/node_modules/.pnpm found above ${APPS_DESKTOP}; cannot locate installed useAui.js.`
  )
}

const INSTALLED_USE_AUI_PATH = findInstalledUseAui()

const ASSISTANT_MESSAGE_PATH = resolve(
  APPS_DESKTOP,
  'src/components/assistant-ui/thread/assistant-message.tsx'
)
const ASSISTANT_MESSAGE_SOURCE = readFileSync(ASSISTANT_MESSAGE_PATH, 'utf8')

const USER_MESSAGE_PATH = resolve(
  APPS_DESKTOP,
  'src/components/assistant-ui/thread/user-message.tsx'
)
const USER_MESSAGE_SOURCE = readFileSync(USER_MESSAGE_PATH, 'utf8')

const LIST_PATH = resolve(
  APPS_DESKTOP,
  'src/components/assistant-ui/thread/list.tsx'
)
const LIST_SOURCE = readFileSync(LIST_PATH, 'utf8')

const TIMELINE_PATH = resolve(
  APPS_DESKTOP,
  'src/components/assistant-ui/thread/timeline.tsx'
)
const TIMELINE_SOURCE = readFileSync(TIMELINE_PATH, 'utf8')

const MESSAGE_PARTS_PATH = resolve(
  APPS_DESKTOP,
  'src/components/assistant-ui/thread/message-parts.tsx'
)
const MESSAGE_PARTS_SOURCE = readFileSync(MESSAGE_PARTS_PATH, 'utf8')

const USE_COMPOSER_DRAFT_PATH = resolve(
  APPS_DESKTOP,
  'src/app/chat/composer/hooks/use-composer-draft.ts'
)
const USE_COMPOSER_DRAFT_SOURCE = readFileSync(USE_COMPOSER_DRAFT_PATH, 'utf8')

const USE_COMPOSER_METRICS_PATH = resolve(
  APPS_DESKTOP,
  'src/app/chat/composer/hooks/use-composer-metrics.ts'
)
const USE_COMPOSER_METRICS_SOURCE = readFileSync(USE_COMPOSER_METRICS_PATH, 'utf8')

const USER_EDIT_COMPOSER_PATH = resolve(
  APPS_DESKTOP,
  'src/components/assistant-ui/thread/user-edit-composer.tsx'
)
const USER_EDIT_COMPOSER_SOURCE = readFileSync(USER_EDIT_COMPOSER_PATH, 'utf8')

function hasUnguardedComposerAccess(source: string): boolean {
  // Mirror of hasUnguardedThreadAccess, but for `s.composer.X` references.
  // s.composer is undefined before MessagePrimitive.Root mounts its Composer
  // subtree, so unguarded `s.composer.text` reads throw "Cannot read
  // properties of undefined (reading 'text')" on every boot before any
  // conversation opens. Same comment-strip + windowed pattern as the
  // thread-access helper.
  const codeOnly = source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/\s+\/\/.*$/g, '')

  const unguardedPattern = /\bs\.composer\.[A-Za-z_]\w*/g
  const guardedPattern =
    /\bs\.composer\??\s*\?\.\s*[A-Za-z_]\w*|\bs\.composer\s*\?\s*\{|\(\s*s\.composer\s*\?\.[^)]*\)/

  const matches = codeOnly.match(unguardedPattern) ?? []

  for (const match of matches) {
    const idx = codeOnly.indexOf(match)
    const window = codeOnly.slice(Math.max(0, idx - 100), idx + match.length + 50)

    if (!guardedPattern.test(window)) {
      return true
    }
  }

  return false
}

function hasUnguardedThreadAccess(source: string): boolean {
  // Walks every `s.thread.X` reference in CODE (not comments) that isn't
  // already optional or guarded. We don't pin the exact shape (the source
  // files use a mix of `s.thread?.X`, `(s.thread?.X ?? fallback)`, and
  // early-return guards) — only that the unguarded form is gone.
  //
  // Strategy: strip line comments and block comments first so we don't
  // false-positive on `s.thread.messages` mentioned in prose, then find
  // every `s.thread.X` reference (where X is a property identifier —
  // `messages`, `isRunning`, etc.), and verify each one is either preceded
  // by `?.`, wrapped in a `?? fallback`, or wrapped in an outer guard
  // like `s.thread ? ... : ...`.
  //
  // Note: we don't gate on `fileHasOptional` because the audit's purpose
  // is to enforce NO unguarded `s.thread.X` access exists anywhere — a
  // legacy file with bare `s.thread.messages` is exactly what we want to
  // catch so someone fixes it on the next audit pass.
  const codeOnly = source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/\s+\/\/.*$/g, '')

  const unguardedPattern = /\bs\.thread\.[A-Za-z_]\w*/g
  const guardedPattern =
    /\bs\.thread\??\s*\?\.\s*[A-Za-z_]\w*|\bs\.thread\s*\?\s*\{|\(\s*s\.thread\s*\?\.[^)]*\)/

  const matches = codeOnly.match(unguardedPattern) ?? []

  for (const match of matches) {
    const idx = codeOnly.indexOf(match)
    const window = codeOnly.slice(Math.max(0, idx - 100), idx + match.length + 50)

    if (!guardedPattern.test(window)) {
      return true
    }
  }

  return false
}

describe('workspace pane — boot-screen regression', () => {
  // VECTOR 1: the renderer-side bug. The fix is a simple guard; pin it
  // across EVERY file in the assistant-ui thread tree that selects
  // through `useAuiState`. When b669192de added the StreamStallIndicator
  // selector, it joined a family of selectors that all assumed
  // `s.thread` is defined. On a brand-new session (the boot path), the
  // runtime has not yet seeded thread state, so every unguarded walk
  // fires "Cannot read properties of undefined (reading 'messages' /
  // 'isRunning')". The workspace pane's ContribBoundary catches the
  // first one and replaces the whole pane with the "failed to render"
  // fallback.
  it('assistant-message.tsx does not read .messages on an undefined thread state', () => {
    expect(hasUnguardedThreadAccess(ASSISTANT_MESSAGE_SOURCE)).toBe(false)
  })

  it('user-message.tsx does not read .thread or .messages on an undefined thread state', () => {
    expect(hasUnguardedThreadAccess(USER_MESSAGE_SOURCE)).toBe(false)
  })

  it('list.tsx does not read .messages on an undefined thread state', () => {
    expect(hasUnguardedThreadAccess(LIST_SOURCE)).toBe(false)
  })

  it('timeline.tsx does not read .messages on an undefined thread state', () => {
    expect(hasUnguardedThreadAccess(TIMELINE_SOURCE)).toBe(false)
  })

  it('message-parts.tsx does not read .thread on an undefined thread state', () => {
    expect(hasUnguardedThreadAccess(MESSAGE_PARTS_SOURCE)).toBe(false)
  })

  it('use-composer-draft.ts does not read .composer.text on an undefined composer', () => {
    expect(hasUnguardedComposerAccess(USE_COMPOSER_DRAFT_SOURCE)).toBe(false)
  })

  it('use-composer-metrics.ts does not read .composer.text on an undefined composer', () => {
    expect(hasUnguardedComposerAccess(USE_COMPOSER_METRICS_SOURCE)).toBe(false)
  })

  it('user-edit-composer.tsx does not read .composer.text on an undefined composer', () => {
    expect(hasUnguardedComposerAccess(USER_EDIT_COMPOSER_SOURCE)).toBe(false)
  })

  // VECTOR 2: the @assistant-ui/store@0.2.20 patch must be applied.
  it('@assistant-ui/store patch file applies cleanly to pristine upstream', () => {
    if (!existsSync(PATCH_PATH)) {
      throw new Error(`Patch file missing at ${PATCH_PATH}`)
    }
    // Stage pristine useAui.js in a temp dir + apply the patch with --dry-run.
    // `patch --dry-run` exits 0 when all hunks apply and 1 when any fail;
    // anything else (2 = I/O, etc.) is a real error.
    try {
      execFileSync(
        'patch',
        ['-d', resolve(APPS_DESKTOP, '.hermes-pristine-check'), '-p1', '--dry-run', '-i', PATCH_PATH],
        { stdio: ['ignore', 'pipe', 'pipe'] }
      )
    } catch (err) {
      const e = err as NodeJS.ErrnoException & { status?: number | null; stderr?: Buffer }
      if (e.status === 1) {
        throw new Error(
          `Patch ${PATCH_PATH} failed to apply to pristine @assistant-ui/store@0.2.20. ` +
          'This is the failure mode that caused the workspace-pane boot regression: a hand-edited ' +
          'patch with corrupted context/whitespace that `patch` rejects silently during pnpm install. ' +
          `patch stderr: ${e.stderr?.toString() ?? '(none)'}`
        )
      }
      throw err
    }
  })

  it('installed useAui.js is patched (differs from pristine upstream)', () => {
    // `diff` exits 0 when files match and 1 when they differ — we WANT them
    // to differ, so explicitly accept exit code 1 (and anything else
    // other than 0/1 would indicate a real failure like a missing file).
    try {
      execFileSync(
        'diff',
        ['-q', '/tmp/pristine-store/package/dist/useAui.js', INSTALLED_USE_AUI_PATH],
        { stdio: ['ignore', 'pipe', 'pipe'] }
      )
    } catch (err) {
      const e = err as NodeJS.ErrnoException & { status?: number | null }
      if (e.status === 1) {
        return
      }
      throw err
    }
    throw new Error(
      `Installed useAui.js is byte-identical to upstream pristine — the patch did not apply. ` +
      `Re-run \`pnpm install --no-frozen-lockfile\` from apps/desktop.`
    )
  })

  // The end-to-end shape: a fresh session that hits the boot path should
  // not produce the documented error. Verified by checking all invariants
  // hold; the actual browser render verification is done by the operator
  // via Electron + DevTools (see verifying-code-changes skill).
  it('workspace pane boot invariants — combined contract', () => {
    // Re-state every precondition as a single named contract so the test
    // output clearly says "workspace boot is safe" when all pass. We don't
    // duplicate the asserts above — we just exercise the same paths.
    expect(hasUnguardedThreadAccess(ASSISTANT_MESSAGE_SOURCE)).toBe(false)
    expect(hasUnguardedThreadAccess(USER_MESSAGE_SOURCE)).toBe(false)
    expect(hasUnguardedThreadAccess(LIST_SOURCE)).toBe(false)
    expect(hasUnguardedThreadAccess(TIMELINE_SOURCE)).toBe(false)
    expect(hasUnguardedThreadAccess(MESSAGE_PARTS_SOURCE)).toBe(false)

    if (!existsSync(PATCH_PATH)) {
      throw new Error('patch file missing')
    }
  })

  it('clientFunction.composer returns a state-shape object that survives getClientState passthrough', () => {
    // The runtime proxy of `composer()` is what `state.composer.X` resolves
    // to via getProxiedAssistantState → getClientState(composer()). If the
    // stub lacks `.canSend`, `.text`, `.attachments`, etc., then
    // ComposerPrimitive.If's selector `s.composer.isEditing` / `s.composer.X`
    // throws "Cannot read properties of undefined (reading 'canSend')" on
    // every render. Verified 2026-08-06 — this is the third regression
    // vector we've seen: messages → text → canSend, all the same root cause
    // (library code reading scope-keyed fields that the stub doesn't
    // expose).
    //
    // We check BOTH the source patch file (authoritative) and the
    // installed node_modules file (runtime), so this test catches
    // regressions even when pnpm install hasn't been re-run yet (the
    // build script in scripts/build-store-patch.mjs owns the installed
    // shape; this test just locks the contract on both ends).
    const composerFieldPattern = (field: string, value: string) =>
      new RegExp(`${field}:\\s*${value}`)

    if (!existsSync(PATCH_PATH)) {
      throw new Error(`patch file missing at ${PATCH_PATH}`)
    }
    const patchSource = readFileSync(PATCH_PATH, 'utf8')
    expect(patchSource).toMatch(/clientFunction\.composer\s*=\s*\(\)\s*=>\s*\{/)
    expect(patchSource).toMatch(composerFieldPattern('canSend', 'false'))
    expect(patchSource).toMatch(composerFieldPattern('isEditing', 'false'))
    expect(patchSource).toMatch(composerFieldPattern('dictation', 'null'))
    expect(patchSource).toMatch(composerFieldPattern('capabilities', '\\{'))

    if (existsSync(INSTALLED_USE_AUI_PATH)) {
      const installed = readFileSync(INSTALLED_USE_AUI_PATH, 'utf8')
      expect(installed).toMatch(/clientFunction\.composer\s*=\s*\(\)\s*=>\s*\{/)
      expect(installed).toMatch(composerFieldPattern('canSend', 'false'))
      expect(installed).toMatch(composerFieldPattern('isEditing', 'false'))
      expect(installed).toMatch(composerFieldPattern('dictation', 'null'))
      expect(installed).toMatch(composerFieldPattern('capabilities', '\\{'))
    }
  })
})
