"use strict"

/**
 * Regression test for the desktop build black-screen bug.
 *
 * The bug:
 *   `@assistant-ui/core` imports `tapClientResource` and `tapClientLookup`
 *   from `@assistant-ui/store`. These names exist in `@assistant-ui/store@0.2.13`
 *   and later but were absent in some intermediate versions.
 *
 *   pnpm's strict resolution can route `@assistant-ui/core` to a
 *   pnpm-nested `@assistant-ui/store` that doesn't have the required names.
 *   The Vite production build uses rolldown, which respects pnpm's strict
 *   resolution and crashes with `MISSING_EXPORT` from `@assistant-ui/store`.
 *   The build aborts after copying public assets into `dist/`, leaving no
 *   `index.html` and no JS bundle. Electron's `loadFile('dist/index.html')`
 *   then renders a black window because the page is empty.
 *
 * The fix: pin `@assistant-ui/store` to `>=0.2.13` via pnpm overrides. This
 * forces pnpm to resolve every `@assistant-ui/store` import (including
 * transitive ones through `@assistant-ui/core`) to a version that exports
 * the required API names.
 *
 * This test pins the wire contract:
 *   1. `apps/desktop/package.json` declares a pnpm override pinning
 *      `@assistant-ui/store` to a version >= 0.2.13.
 *   2. The pnpm-nested `@assistant-ui/store` actually has the working
 *      exports. After pnpm install with the override, the pnpm-nested copy
 *      at `apps/desktop/node_modules/.pnpm/@assistant-ui+store@<version>/...`
 *      must export `tapClientResource` and `tapClientLookup`.
 *
 * If a future contributor bumps the override to a broken version,
 * this test fails immediately. If someone removes the override,
 * this test fails immediately.
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
  // Match the export statement (handles aliased exports: "foo as bar")
  const matches = [...source.matchAll(/export\s*\{\s*([^}]+)\}\s*;?/g)]
  if (matches.length === 0) {
    return null
  }
  // Collect from all export {} blocks (there may be multiple)
  const tokens = []
  for (const match of matches) {
    for (const token of match[1].split(",")) {
      const trimmed = token.trim()
      if (!trimmed) continue
      // "foo as bar" -> export "bar"; plain "foo" -> export "foo"
      const aliasMatch = trimmed.match(/^(?:.*\s+as\s+)?(\S+)\s*$/)
      if (aliasMatch) {
        tokens.push(aliasMatch[1])
      }
    }
  }
  return tokens
}

function testPnpmOverridePinsStore() {
  const version = pnpmOverriddenStoreVersion()
  if (!version) {
    throw new Error(
      `apps/desktop/package.json must declare pnpm.overrides["@assistant-ui/store"] — without the pin, pnpm resolves @assistant-ui/core to a version that drops the old API, vite build crashes with MISSING_EXPORT, and the renderer is shipped as a black screen. See the comment at the top of this test.`
    )
  }
  // Accept any version >= 0.2.13 (the last version known to export the old API names).
  // The actual pinned version is in pnpm.overrides in package.json — update both together.
  const minVersion = [0, 2, 13]
  const parts = version.replace(/[^\d.]/g, "").split(".").map(Number)
  const valid = parts[0] > minVersion[0] || (parts[0] === minVersion[0] && parts[1] > minVersion[1]) || (parts[0] === minVersion[0] && parts[1] === minVersion[1] && parts[2] >= minVersion[2])
  if (!valid) {
    throw new Error(
      `pnpm.overrides["@assistant-ui/store"] is "${version}" — only >= 0.2.13 is verified to still export the tapClientResource/tapClientLookup names that @assistant-ui/core imports. Update the override to a working version, then update this test's minVersion in lockstep.`
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
        `pnpm-nested @assistant-ui/store variant "${variant}" is missing ${!hasTapResource ? "tapClientResource" : "tapClientLookup"}. @assistant-ui/core imports these names, so vite build (which uses strict pnpm resolution) will crash with MISSING_EXPORT. Either pin the override to a working version (>= 0.2.13) or upgrade @assistant-ui/core to a version that uses the renamed API.`
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
