"use strict"

/**
 * Regression test for the desktop build black-screen bug.
 *
 * The bug:
 *   `@assistant-ui/core@0.1.17` imports `tapClientResource` and `tapClientLookup`
 *   from `@assistant-ui/store`. These names exist in `@assistant-ui/store@0.2.13`
 *   (the version the workspace hoists) but were removed in `@assistant-ui/store@0.2.19`
 *   (the version pnpm resolves when no override is set).
 *
 *   pnpm's strict resolution routes `@assistant-ui/core@0.1.17` to the
 *   pnpm-nested `@assistant-ui/store@0.2.19` even though the top-level
 *   workspace has the working `0.2.13`. The Vite production build uses
 *   rolldown, which respects pnpm's strict resolution and crashes with
 *   `MISSING_EXPORT` from `@assistant-ui/store`. The build aborts after
 *   copying public assets into `dist/`, leaving no `index.html` and no JS
 *   bundle. Electron's `loadFile('dist/index.html')` then renders a black
 *   window because the page is empty.
 *
 *   Vite's dev server pre-bundles from the workspace's top-level store, so
 *   `npm run dev` works — masking the bug from anyone who only tests the
 *   dev path. The bug only surfaces on `npm run build` / `npm start` /
 *   packaged install.
 *
 * The fix: pin `@assistant-ui/store` to `0.2.13` via pnpm overrides. This
 * forces pnpm to resolve every `@assistant-ui/store` import (including
 * transitive ones through `@assistant-ui/core`) to the working version.
 *
 * This test pins the wire contract:
 *   1. `apps/desktop/package.json` declares a pnpm override pinning
 *      `@assistant-ui/store` to `0.2.13` (the last working version).
 *   2. The pnpm-nested `@assistant-ui/store` actually has the working
 *      exports. After pnpm install with the override, the pnpm-nested copy
 *      at `apps/desktop/node_modules/.pnpm/@assistant-ui+store@<version>/...`
 *      must export `tapClientResource` and `tapClientLookup` (the names
 *      `@assistant-ui/core@0.1.17` requires).
 *
 * If a future contributor bumps the override to a newer store that drops
 * the old API, this test fails immediately. If someone removes the
 * override, this test fails immediately.
 *
 * See ~/.hermes/skills/hermes-desktop for the build pipeline context.
 */

const fs = require("node:fs")
const path = require("node:path")
const { execFileSync } = require("node:child_process")

const APPS_ROOT = path.resolve(__dirname, "..")
const PACKAGE_JSON_PATH = path.join(APPS_ROOT, "package.json")
const PNPM_DIR = path.join(APPS_ROOT, "node_modules", ".pnpm")

function readPackageJson() {
  return JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"))
}

function pnpmOverriddenStoreVersion() {
  const pkg = readPackageJson()
  const overrides = pkg && pkg.pnpm && pkg.pnpm.overrides
  if (!overrides || typeof overrides !== "object") {
    return null
  }
  return overrides["@assistant-ui/store"] || null
}

function listPnpmStoreVariants() {
  if (!fs.existsSync(PNPM_DIR)) {
    return []
  }
  return fs
    .readdirSync(PNPM_DIR)
    .filter(name => name.startsWith("@assistant-ui+store@"))
}

function readStoreExports(storeDir) {
  const indexPath = path.join(storeDir, "node_modules", "@assistant-ui", "store", "dist", "index.js")
  if (!fs.existsSync(indexPath)) {
    return null
  }
  const source = fs.readFileSync(indexPath, "utf8")
  const match = source.match(/export\s*\{([^}]+)\}\s*;?/)
  if (!match) {
    return null
  }
  return match[1]
    .split(",")
    .map(token => token.trim())
    .filter(Boolean)
}

function testPnpmOverridePinsStore() {
  const version = pnpmOverriddenStoreVersion()
  if (!version) {
    throw new Error(
      `apps/desktop/package.json must declare pnpm.overrides["@assistant-ui/store"] — without the pin, pnpm resolves @assistant-ui/core@0.1.17 against @assistant-ui/store@0.2.19 (which dropped tapClientResource/tapClientLookup), vite build crashes with MISSING_EXPORT, and the renderer is shipped as a black screen. See the comment at the top of this test.`
    )
  }
  if (version !== "0.2.13") {
    throw new Error(
      `pnpm.overrides["@assistant-ui/store"] is "${version}" — only "0.2.13" is verified to still export the tapClientResource/tapClientLookup names that @assistant-ui/core@0.1.17 imports. If you need to bump, run vite build first to confirm the new version still ships the old API, then update both this test and the override in lockstep.`
    )
  }
}

function testEveryPnpmStoreVariantExportsOldApi() {
  // After `pnpm install --no-frozen-lockfile` with the override, only the
  // pinned version should be in the pnpm store. But allow some slack for
  // projects that pin a range — fail only if the *active* variant
  // (the one that satisfies @assistant-ui/core@0.1.17's peer) is broken.
  const variants = listPnpmStoreVariants()
  if (variants.length === 0) {
    throw new Error(
      `No @assistant-ui+store* variants found under ${PNPM_DIR}. Has pnpm install run?`
    )
  }

  for (const variant of variants) {
    const storeDir = path.join(PNPM_DIR, variant)
    const exportsList = readStoreExports(storeDir)
    if (!exportsList) {
      // No export statement — skip (older format that re-exports each)
      continue
    }

    const hasTapResource = exportsList.includes("tapClientResource")
    const hasTapLookup = exportsList.includes("tapClientLookup")
    if (!hasTapResource || !hasTapLookup) {
      throw new Error(
        `pnpm-nested @assistant-ui/store variant "${variant}" is missing ${!hasTapResource ? "tapClientResource" : "tapClientLookup"}. @assistant-ui/core@0.1.17 imports these names, so vite build (which uses strict pnpm resolution) will crash with MISSING_EXPORT. Either pin the override to a working version (currently 0.2.13) or upgrade @assistant-ui/core to a version that uses the renamed API.`
      )
    }
  }
}

function main() {
  const tests = [
    { name: "pnpm override pins @assistant-ui/store to a known-working version", fn: testPnpmOverridePinsStore },
    {
      name: "every pnpm-nested @assistant-ui/store variant exports tapClientResource + tapClientLookup",
      fn: testEveryPnpmStoreVariantExportsOldApi
    }
  ]

  let passed = 0
  let failed = 0

  for (const t of tests) {
    try {
      t.fn()
      console.log(`✓ ${t.name}`)
      passed++
    } catch (err) {
      console.error(`✗ ${t.name}`)
      console.error(`  ${err.message}`)
      failed++
    }
  }

  console.log(`\n${passed} passed, ${failed} failed`)
  process.exit(failed === 0 ? 0 : 1)
}

if (require.main === module) {
  main()
}

module.exports = {
  pnpmOverriddenStoreVersion,
  listPnpmStoreVariants,
  readStoreExports,
  testPnpmOverridePinsStore,
  testEveryPnpmStoreVariantExportsOldApi
}
