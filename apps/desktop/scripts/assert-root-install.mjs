import { accessSync, symlinkSync, existsSync } from "fs"
import { resolve, join } from "path"

const root = resolve(import.meta.dirname, "..", "..", "..")
const desktopRoot = resolve(import.meta.dirname, "..")
const rootElectronBin = join(root, "node_modules", ".bin", "electron")
const rootElectronPkg = join(root, "node_modules", "electron")
const desktopElectronBin = join(desktopRoot, "node_modules", ".bin", "electron")
const desktopElectronPkg = join(desktopRoot, "node_modules", "electron")

try {
  accessSync(join(root, "node_modules", "vite", "package.json"))
} catch {
  console.error(`Run from repo root: cd ${root} && npm ci`)
  process.exit(1)
}

// Ensure `node_modules/.bin/electron` resolves from inside apps/desktop.
// npm hoists `electron` to the workspace root when this monorepo installs
// via `npm install --workspace apps/desktop`, so the hermes-desktop launcher
// (which exec()s `node_modules/.bin/electron` relative to apps/desktop) hits
// "no electron binary and no prebuilt app found".  Re-link if either the
// .bin shim or the electron package symlink under apps/desktop is missing.
if (existsSync(rootElectronBin) && existsSync(rootElectronPkg)) {
  if (!existsSync(desktopElectronPkg)) {
    try {
      symlinkSync(rootElectronPkg, desktopElectronPkg)
    } catch (err) {
      if (err.code !== "EEXIST") throw err
    }
  }
  if (!existsSync(desktopElectronBin)) {
    try {
      symlinkSync("../electron/cli.js", desktopElectronBin)
    } catch (err) {
      if (err.code !== "EEXIST") throw err
    }
  }
}
