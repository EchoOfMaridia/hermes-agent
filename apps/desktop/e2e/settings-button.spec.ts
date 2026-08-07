/**
 * Regression test: clicking the titlebar settings button must open the
 * settings overlay and NOT open a new session.
 *
 * Reported operator symptom: "the settings button doesn't work. It just
 * opens a new session now." The expected behavior is that the titlebar
 * gear navigates to /settings and renders the SettingsView overlay.
 *
 * This spec piggybacks on the mock backend fixture so the rest of the
 * app is in a representative state.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import { expect, test } from './test'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { expectVisualSnapshot } from './visual-snapshot'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
  await waitForAppReady(fixture!, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test.describe('titlebar settings button', () => {
  test('opens settings overlay, not a new session', async () => {
    const page = fixture!.page

    // The titlebar settings button has aria-label "Open settings" and is
    // always pinned to the right side of the titlebar (system tools cluster).
    const settingsButton = page.getByRole('button', { name: 'Open settings' })
    await settingsButton.waitFor({ state: 'visible', timeout: 10_000 })

    // Anchor: confirm we're on the chat/new-chat route before clicking.
    const beforeUrl = page.url()
    expect(beforeUrl, 'pre-click URL should be the chat route').toMatch(/#\/?$/)

    // Capture nav + console events so a regression is easy to localize.
    const urlTrace: string[] = []
    page.on('framenavigated', frame => {
      if (frame === page.mainFrame()) {
        urlTrace.push(frame.url())
      }
    })
    const consoleEvents: string[] = []
    page.on('console', msg => consoleEvents.push(`[${msg.type()}] ${msg.text()}`))

    // The SettingsView uses OverlayView with a close button that has
    // aria-label="Close settings" (the visible text is the X icon). The view
    // only mounts when the route is /settings, so it must be ABSENT before the
    // click and PRESENT after.
    const settingsOverlay = page.getByRole('button', { name: 'Close settings' })
    await expect(settingsOverlay).toHaveCount(0)

    // Click the titlebar gear.
    await settingsButton.click()

    // Expected: the URL hash becomes /settings.
    await page.waitForFunction(
      () => window.location.hash === '#/settings',
      undefined,
      { timeout: 5_000 }
    )

    // Expected: the SettingsView overlay is mounted.
    await expect(settingsOverlay).toBeVisible({ timeout: 5_000 })

    // Negative: no session was created. The sidebar should still show
    // "No sessions yet" (chat composer has never sent a turn in this fixture).
    await expect(page.getByText('No sessions yet')).toBeVisible()

    // Visual snapshot — locks the rendered overlay shape so a future regression
    // that "silently" passes the URL/button checks but breaks the visual layout
    // still fails this test on the next run.
    await expectVisualSnapshot(page, { name: 'settings-overlay-open', app: fixture!.app })

    // Closing the overlay should land back on the chat view, not on a
    // blank pane or a different route — the close handler is wired to
    // closeOverlayToPreviousRoute which uses the stashed return path.
    await settingsOverlay.click()
    await page.waitForFunction(
      () => window.location.hash === '#/',
      undefined,
      { timeout: 5_000 }
    )
    await expect(settingsOverlay).toHaveCount(0)
    await expect(page.getByText('No sessions yet')).toBeVisible()

    // Surface useful debug info if a future regression bisects here.
    if (process.env.SETTINGS_TEST_DEBUG) {
      console.log('URL trace:', urlTrace)
      console.log('Console events:', consoleEvents.slice(-20))
    }
  })
})
