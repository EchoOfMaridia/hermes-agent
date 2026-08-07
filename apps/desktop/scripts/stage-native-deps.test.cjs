'use strict'

// Regression test for the launch-time crash documented in upstream issue #52735
// (now fixed by 2d206a3a4): packaged Hermes Desktop fails on startup with
//
//   Uncaught Exception:
//   Error: Cannot find module '.../resources/native-deps/vendor/node_modules/simple-git'
//
// when electron/main.cjs tries to require() simple-git at runtime.  The crash
// chain is:
//
//   1. git-review-ops.cjs requires('simple-git')
//   2. First require attempt uses Node's normal lookup from the app's
//      node_modules.  In a packaged build that lookup misses (electron-builder
//      drops the asar's node_modules — the files: package.json field strips
//      them).
//   3. The require falls back to `process.resourcesPath +
//      'native-deps/vendor/node_modules/simple-git'`.
//   4. If scripts/stage-native-deps.cjs didn't stage simple-git during the
//      build, that path doesn't exist and Electron throws the "Cannot find
//      module 'simple-git'" error above, killing the renderer.
//
// pnpm --nodeLinker=isolated (the default) leaves workspace deps in
// <workspace>/node_modules/.pnpm/<pkg>@<v>/node_modules/<pkg>, NOT at the
// repo root where the upstream staging script's resolvePkgDir() searched.
// That breaks the closure walk even when `pnpm install` succeeded and
// simple-git shows up in `pnpm ls`.  The fix widens resolvePkgDir()'s
// search paths to include the workspace's node_modules tree.
//
// This test exercises the script as a child process to mirror how
// `npm run build` invokes it: it spawns the script from the desktop root,
// then asserts the staged tree contains simple-git and its transitive
// deps under vendor/node_modules.  Before the fix, simple-git fails to
// resolve and the script exits 1; after the fix, the tree is populated.
//
// We deliberately do NOT mock `require.resolve` or stub out parts of the
// script — the real failure mode IS the script's behavior, not a unit-level
// function.  Spawning the real script catches both the search-paths bug
// (this test) AND any future regression where the staging script exits
// early for some other reason.  Cost: ~300ms per run in CI; gain: exact
// regression pinning.

const test = require('node:test')
const assert = require('node:assert/strict')
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const DESKTOP_ROOT = path.resolve(__dirname, '..')
const STAGE_ROOT = path.join(DESKTOP_ROOT, 'build', 'native-deps')
const VENDOR_DIR = path.join(STAGE_ROOT, 'vendor', 'node_modules')

function runStageScript() {
  return spawnSync(
    process.execPath,
    [path.join(DESKTOP_ROOT, 'scripts', 'stage-native-deps.cjs')],
    {
      cwd: DESKTOP_ROOT,
      encoding: 'utf8',
      timeout: 60_000
    }
  )
}

test('stage-native-deps populates build/native-deps/vendor/node_modules/simple-git', () => {
  // Pre-flight: the desktop package must declare simple-git.  This guards
  // against an editor that silently removed the dep (a regression toward
  // issue #52735, where this whole script exists in the first place).
  // We deliberately fail loudly rather than skip — the staging script
  // being a no-op is exactly the original bug class.
  const desktopPkg = JSON.parse(
    fs.readFileSync(path.join(DESKTOP_ROOT, 'package.json'), 'utf8')
  )
  const declaredSimpleGit = (desktopPkg.dependencies || {})['simple-git']
  assert.ok(
    declaredSimpleGit,
    'apps/desktop/package.json must declare simple-git in dependencies; the staging ' +
      'script exists to satisfy that runtime require() (issue #52735)'
  )

  // Pre-flight: the workspace pnpm install actually pulled simple-git into
  // the per-workspace .pnpm store.  Without this, staging fails with the
  // exact launch-time error from #52735.  We do NOT silently skip — a clean
  // clone that hits this needs the loud message "run pnpm install" so the
  // operator fixes the precondition instead of pushing a build that lacks
  // the closure.
  const pnpmSimpleGit = path.join(
    DESKTOP_ROOT,
    'node_modules',
    '.pnpm',
    `simple-git@${declaredSimpleGit.replace(/^\^/, '')}`,
    'node_modules',
    'simple-git'
  )
  // The @<v> suffix on the pnpm dir uses the *exact* installed version,
  // not just whatever range is in package.json.  Walk the .pnpm dir for a
  // simple-git@<something> entry that has the package on disk -- this is
  // robust to a version bump landing in the lockfile (the upstream fix's
  // contract: a version bump can't silently reintroduce the crash).
  const pnpmSimpleGitDirs = fs.existsSync(path.join(DESKTOP_ROOT, 'node_modules', '.pnpm'))
    ? fs
        .readdirSync(path.join(DESKTOP_ROOT, 'node_modules', '.pnpm'))
        .filter(name => /^simple-git@/.test(name))
    : []
  const anyInstalled = pnpmSimpleGitDirs.some(dir =>
    fs.existsSync(
      path.join(DESKTOP_ROOT, 'node_modules', '.pnpm', dir, 'node_modules', 'simple-git', 'package.json')
    )
  )
  if (!anyInstalled) {
    assert.fail(
      'simple-git is declared in apps/desktop/package.json but is not installed ' +
        'in apps/desktop/node_modules/.pnpm/.  Run `pnpm install` at ' +
        'apps/desktop/, then re-run this test.  Without it, scripts/stage-' +
        'native-deps.cjs will throw "Could not resolve \'simple-git\'" and ' +
        'the packaged app will crash on launch with `Cannot find module ' +
        "'simple-git'`.  See upstream issue #52735."
    )
  }
  // Silence the unused-vars lint by referencing the more specific path.
  void pnpmSimpleGit

  // Run the staging script -- this is what `npm run build` does, and it's
  // the exact code path whose failure produces the launch-time crash.
  const result = runStageScript()

  // If the script failed, dump its output so the failure is debuggable.
  // The script logs to stdout (we want to see "vendor/node_modules: N
  // package(s) (...)" on GREEN); on RED it logs the error.message + stack
  // to stderr.
  if (result.status !== 0) {
    assert.fail(
      `stage-native-deps exited ${result.status}\n` +
        `--- stdout ---\n${result.stdout || '(empty)'}\n` +
        `--- stderr ---\n${result.stderr || '(empty)'}`
    )
  }

  // The packaged-app require path is process.resourcesPath +
  // 'native-deps/vendor/node_modules/simple-git'.  Asserting the staged
  // file map lines up with that path (not, e.g., with a stray
  // 'node_modules/...' at the wrong nesting depth) catches the
  // electron-builder hard-drop bug class.  The previous fix (#52735) was
  // about exactly this nesting -- the `vendor/` indirection matters.
  assert.ok(
    fs.existsSync(path.join(VENDOR_DIR, 'simple-git', 'package.json')),
    'simple-git/package.json must be staged under build/native-deps/vendor/node_modules/'
  )

  // simple-git 3.x has an exports map that doesn't expose
  // './package.json' (the reason resolvePkgDir() walks up to find the
  // package root instead of require.resolve('simple-git/package.json')).
  // Re-require simple-git from inside the staged tree to prove the closure
  // is actually loadable by Node, not just present on disk.  This catches
  // a class of "staged files but unresolved transitives" regressions where
  // the copy step succeeds but a transitive node_modules entry is missing.
  const staged = require(path.join(VENDOR_DIR, 'simple-git'))
  assert.equal(typeof staged, 'function', 'simple-git must export a callable factory')
  assert.ok(
    Array.isArray(Object.keys(staged)) && Object.keys(staged).length > 0,
    'simple-git must export a sane named-key surface (sanity guard against an empty stub)'
  )
})
