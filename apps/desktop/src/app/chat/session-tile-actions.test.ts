import { describe, expect, it } from 'vitest'

import { useSessionTileActions } from './session-tile-actions'

describe('session-tile-actions port (rewind.ts carries the missing functions)', () => {
  it('imports useSessionTileActions successfully', () => {
    expect(typeof useSessionTileActions).toBe('function')
  })
})