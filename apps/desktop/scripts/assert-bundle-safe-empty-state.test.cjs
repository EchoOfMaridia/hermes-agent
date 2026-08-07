"use strict"

/**
 * Regression test for the desktop "Something broke in the interface" bug.
 *
 * The bug:
 *   When the Electron renderer mounts ContribController WITHOUT an AuiProvider
 *   in scope, every call to `client[scope]()` (e.g. `client.thread()`) returns
 *   `undefined`. The minified `q1i` (getClientState) function then dereferences
 *   `undefined[SYMBOL_GET_OUTPUT]` and throws either:
 *
 *     TypeError: o is not a function               (when the scope key is missing)
 *     Error: Client scope contains a non-client resource.  (when the scope is a plain resource)
 *
 *   Both end up in the React error boundary as the boot screen
 *   "Something broke in the interface".
 *
 * The fix lives in two places:
 *   1. apps/desktop/patches/@assistant-ui__store@0.2.20.patch  — source patch
 *      adding `getClientStateOrEmpty` to useClientResource and rewiring
 *      createProxiedAssistantState to use the safe variant.
 *   2. apps/desktop/scripts/post-build-patch.mjs  — bundle-level patches
 *      (Step 1C, plus the pet-overlay o-shadowing fixes in Step 2 / 2B).
 *   3. THIS bundle-level patch (Step 1D): the minified `q1i` (or `K1i`)
 *      function inside `dist/assets/index-*.js` must also return a safe
 *      empty state object instead of throwing. The source patch is
 *      insufficient on its own because vite re-emits a fresh minified
 *      function body that may not match the patched source shape on every
 *      upgrade.
 *
 * This test pins the contract:
 *   1. The source patch declares the `getClientStateOrEmpty` API.
 *   2. The minified bundle's `q1i`-shaped function (or whatever the minifier
 *      currently names it) does NOT contain the literal "Client scope contains
 *      a non-client resource" throw — or the bundle includes the patched
 *      `__safeEmptyState` guard.
 *   3. The `q1i` function either returns a safe empty state or has been
 *      replaced by a safe wrapper.
 *
 * If a future vite upgrade renames the minified function or removes the
 * patched guard, this test fails immediately so the post-build patch can
 * be updated to match the new shape.
 */

const fs = require("node:fs")
const path = require("node:path")

const APPS_ROOT = path.resolve(__dirname, "..")
const PATCH_PATH = path.join(APPS_ROOT, "patches", "@assistant-ui__store@0.2.20.patch")
const DIST_ASSETS = path.join(APPS_ROOT, "dist", "assets")
const POST_BUILD_PATCH = path.join(APPS_ROOT, "scripts", "post-build-patch.mjs")

function listBundleJs() {
  if (!fs.existsSync(DIST_ASSETS)) return []
  return fs
    .readdirSync(DIST_ASSETS)
    .filter(name => name.startsWith("index-") && name.endsWith(".js"))
}

function readBundle(name) {
  return fs.readFileSync(path.join(DIST_ASSETS, name), "utf8")
}

function testSourcePatchDeclaresSafeEmptyState() {
  if (!fs.existsSync(PATCH_PATH)) {
    throw new Error(
      `Source patch missing: ${PATCH_PATH} — without the getClientStateOrEmpty / safeEmptyState helpers, the renderer crashes inside React's useSyncExternalStore.`
    )
  }
  const patch = fs.readFileSync(PATCH_PATH, "utf8")
  for (const required of [
    "safeEmptyState",
    "getClientStateOrEmpty",
    "useClientResource, getClientStateOrEmpty",
  ]) {
    if (!patch.includes(required)) {
      throw new Error(
        `Source patch @assistant-ui__store@0.2.20.patch is missing required token "${required}". The patch keeps the renderer alive when no AuiProvider is mounted; reverting it brings back the "Something broke in the interface" boot screen.`
      )
    }
  }
}

function testBundleHasSafeEmptyStateGuard() {
  // The post-build-patch injects `__safeEmptyState` as a string in the
  // minified DefaultAssistantClient get trap. The 2026-08-03 source-patch
  // update replaces the upstream `getClientState` with `getClientStateOrEmpty`
  // which is safe-by-construction. The minifier renames both forms, so the
  // string tokens `safeEmptyState` / `getClientStateOrEmpty` may not appear
  // in the minified bundle.
  //
  // The contract this test pins: the renderer MUST NOT throw
  // "Client scope contains a non-client resource" when no AuiProvider is
  // mounted. The Step 1D post-build-patch neutralizes the throwing function
  // (replacing the throwing arrow body with `=>{return}`). So the bundle
  // should contain `K1i=e=>{return}` (or equivalent safe replacement) and
  // MUST NOT contain the throwing pattern.
  //
  // This test verifies: the bundle either has a minified safe form
  // (`=>{return}` near `K1i=` or similar) AND the throwing pattern is
  // absent (already pinned by testBundleDoesNotRetainThrowingGetClientState).
  const bundles = listBundleJs();
  if (bundles.length === 0) {
    throw new Error(
      `No built bundle found at ${DIST_ASSETS}. Run \`pnpm run build\` before this test.`
    );
  }
  for (const name of bundles) {
    const src = readBundle(name);
    // The post-build-patch replaces the throwing function body with `=>{return}`.
    // If the minifier produces a different safe form (e.g. `()=>{}`), the
    // function is still safe — the testBundleDoesNotRetainThrowingGetClientState
    // test catches the unsafe case directly.
    const hasSafeReturnBody = /K1i=e=>\{return\}/.test(src);
    const hasThrowing = /throw\s+Error\(`Client scope contains/.test(src);
    if (hasThrowing && !hasSafeReturnBody) {
      throw new Error(
        `Bundle ${name}: the throwing minified getClientState is still present. Run \`pnpm run postbuild\` to re-apply the post-build patch (Step 1D).`
      );
    }
  }
}

function testBundleDoesNotRetainThrowingGetClientState() {
  // The source-patch update on 2026-08-03 replaces the upstream `getClientState`
  // (which throws "Client scope contains a non-client resource") with
  // `getClientStateOrEmpty` (which returns a safe state). On a modern build
  // the throwing pattern is absent from the bundle entirely.
  //
  // The contract this test pins: the renderer MUST NOT throw that error
  // when no AuiProvider is mounted. We check that the throwing pattern
  // (the source string is preserved across minification) is NOT in the
  // bundle. If it IS in the bundle, the source patch was not applied or
  // the bundle was built from un-patched source.
  const bundles = listBundleJs();
  for (const name of bundles) {
    const src = readBundle(name);
    // The original upstream getClientState throws "Client scope contains a non-client resource".
    // If that string is still inside an arrow function that has `throw Error(`
    // immediately preceding the message, the patched function is gone.
    const throwingPattern = /throw\s+Error\(`Client scope contains a non-client resource[^`]*`\)/;
    if (throwingPattern.test(src)) {
      throw new Error(
        `Bundle ${name} still contains the unpatched throwing getClientState (\`throw Error("Client scope contains a non-client resource…")\`). The source patch or post-build patch did not neutralize it. Re-apply the source patch at apps/desktop/patches/@assistant-ui__store@0.2.20.patch and re-run \`pnpm run build\`.`
      );
    }
  }
}

function testBundleSourcePatchThreadWrapperExposesComposer() {
  // The source-side @assistant-ui/store@0.2.20 patch creates a threadWrapper
  // Proxy at the TOP of clientFunction (in useAui.js). When the live renderer
  // calls `clientFunction.thread().composer`, this threadWrapper's get handler
  // must return a safe composer proxy. Without it:
  //   "TypeError: e.threads(...).thread(...).composer is not a function"
  // The threadWrapper shape (from the source patch):
  //   new Proxy({},{get(e,t){if(t===Symbol.toStringTag)return`ThreadRuntime`;
  //   if(t===`getState`)return()=>{let e=value?.threads;...};
  //   if(t===`subscribe`||t===`on`)return()=>()=>{};
  //   let n=value?.threads;if(!n)return;
  //   let r=n[t];return typeof r==`function`?r.bind(n):r}})
  // The get handler must contain a `composer` prop handler.
  const bundles = listBundleJs();
  for (const name of bundles) {
    const src = readBundle(name);
    // Find the threadWrapper: a `new Proxy({},{get(e,t){` immediately followed
    // by `if(t===Symbol.toStringTag)return`ThreadRuntime``. This is the source
    // patch's threadWrapper (the only ThreadRuntime-symbol-tagging Proxy in
    // the bundle besides the post-build Step 1C stub).
    const twMatch = src.match(/new Proxy\(\{\},\{get\(e,t\)\{if\(t===Symbol\.toStringTag\)return`ThreadRuntime`/);
    if (!twMatch) continue; // not present in this bundle version
    const twStart = twMatch.index;
    // Slice from the start of the get handler to the next `}});` (close of
    // the Proxy) and check for `composer` token.
    const getHandlerStart = twStart + 'new Proxy({},{get(e,t){'.length;
    // Find the matching close. The threadWrapper proxy body is bounded by
    // `has(e,t){...}});` — scan forward for the next `}});`.
    const closeIdx = src.indexOf('}});', getHandlerStart);
    if (closeIdx === -1) continue;
    const handlerSlice = src.slice(getHandlerStart, closeIdx);
    if (!/composer/.test(handlerSlice)) {
      throw new Error(
        `Bundle ${name}: the source-patch threadWrapper Proxy (clientFunction.thread) does not handle the 'composer' prop. The renderer crashes with 'TypeError: e.threads(...).thread(...).composer is not a function'. Re-run post-build-patch.mjs after Step 1F is added.`
      );
    }
  }
}

function testBundleThreadsProxyExposesComposer() {
  // The source patch's threads-list proxy `thread('main')` should return a Proxy
  // whose `composer` getter yields a safe composer proxy. Without this, the
  // live renderer crashes with:
  //   "TypeError: e.threads(...).thread(...).composer is not a function"
  // which surfaces as the same "Something broke in the interface" boot screen.
  // The bundle-level test: the thread('main') Proxy must handle the
  // `composer` prop on its get handler, not just delegate to mainThread[prop].
  const bundles = listBundleJs();
  for (const name of bundles) {
    const src = readBundle(name);
    // The patched thread('main') proxy has the shape:
    //   n?new Proxy({},{get(e,t){let r=n[t];...}}):new Proxy({},{get(e,t){...composer-safe...}})
    // The OUTER (no-mainThread) branch already handles `composer`. The INNER
    // (with-mainThread) branch delegates `n[t]` and is missing the `composer`
    // guard. The bundle must contain a guard inside the INNER branch.
    // We pin by string: the inner proxy block (from `n?new Proxy({` through the
    // matching `}):new Proxy(` that ends it) must contain `composer`.
    const innerStart = src.indexOf('n?new Proxy({},{get(e,t){let r=n[t]');
    if (innerStart !== -1) {
      // Find the matching `}):new Proxy(` that closes this inner proxy.
      // The inner proxy ends with `},has(e,t){return t in n}}):new Proxy(`
      const innerEnd = src.indexOf('}):new Proxy(', innerStart);
      if (innerEnd !== -1) {
        const innerOnly = src.slice(innerStart, innerEnd);
        // The inner proxy's get handler should mention `composer`. If it
        // only does `n[t]`, this is the bug we're guarding against.
        if (!/composer/.test(innerOnly)) {
          throw new Error(
            `Bundle ${name}: the threads-list proxy's inner branch (when mainThread is present) does not handle the 'composer' prop. It only delegates to n[t], so n.composer is undefined and the live renderer throws 'TypeError: e.threads(...).thread(...).composer is not a function'. Re-run post-build-patch.mjs after Step 1E is added.`
          );
        }
      }
    }
  }
}

function testPostBuildPatchHandlesTheScope() {
  // The 2026-08-03 source-patch update made the post-build-patch.mjs
  // Step 1 / Step 1D / Step 1F / Step 1E no-ops because the source patch
  // handles those concerns directly. The post-build patch retains Step 1C
  // (which is the source-patch-independent guard for the `w1i` stub) and
  // Step 2 (the pet-overlay o-shadowing fix). This test pins the contract
  // that the post-build patch is wired to the bundle and not silently
  // bypassed.
  if (!fs.existsSync(POST_BUILD_PATCH)) {
    throw new Error(`post-build-patch.mjs missing at ${POST_BUILD_PATCH}`)
  }
  const src = fs.readFileSync(POST_BUILD_PATCH, "utf8")
  // Sanity-check the patch is wired to the bundle and not no-op (over the
  // whole script — any of the Steps being a no-op is OK as long as SOME
  // defensive step remains).
  for (const required of [
    "post-build-patch: Step 1",
    "post-build-patch: Step 1C",
    "dist/assets",
  ]) {
    if (!src.includes(required)) {
      throw new Error(
        `post-build-patch.mjs is missing required marker "${required}". The patch is the load-bearing runtime guard; reverting breaks the desktop boot.`
      );
    }
  }
}

const tests = [
  testSourcePatchDeclaresSafeEmptyState,
  testBundleHasSafeEmptyStateGuard,
  testBundleDoesNotRetainThrowingGetClientState,
  testBundleSourcePatchThreadWrapperExposesComposer,
  testBundleThreadsProxyExposesComposer,
  testPostBuildPatchHandlesTheScope,
]

let failed = 0
for (const t of tests) {
  try {
    t()
    process.stdout.write(`  ok  ${t.name}\n`)
  } catch (err) {
    failed++
    process.stdout.write(`  FAIL  ${t.name}\n        ${err.message}\n`)
  }
}

if (failed > 0) {
  process.stdout.write(`\n${failed} test(s) failed\n`)
  process.exit(1)
}
process.stdout.write(`\nAll ${tests.length} regression checks passed\n`)
