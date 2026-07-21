import { act, cleanup, render } from '@testing-library/react'
import { memo, useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useLiveWiringActions } from './live-actions'
import type { WiringActions } from './types'

interface HarnessProps {
  onCancel: WiringActions['onCancel']
  onSubmit: WiringActions['onSubmit']
  publishActions: (actions: WiringActions) => void
}

const MemoizedActionConsumer = memo(function MemoizedActionConsumer({
  actions,
  publishActions
}: {
  actions: WiringActions
  publishActions: (actions: WiringActions) => void
}) {
  useEffect(() => publishActions(actions), [actions, publishActions])

  return null
})

function Harness({ onCancel, onSubmit, publishActions }: HarnessProps) {
  const currentActions = { onCancel, onSubmit } as unknown as WiringActions
  const liveActions = useLiveWiringActions(currentActions)

  return <MemoizedActionConsumer actions={liveActions} publishActions={publishActions} />
}

describe('useLiveWiringActions', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('dispatches a callback captured by a memoized consumer to the latest implementation', async () => {
    const initialSubmit = vi.fn(async () => false).mockName('initialSubmit')
    const latestSubmit = vi.fn(async () => true).mockName('latestSubmit')
    let capturedActions: WiringActions | null = null

    const publishActions = (actions: WiringActions) => {
      capturedActions = actions
    }

    const view = render(
      <Harness onCancel={() => undefined} onSubmit={initialSubmit} publishActions={publishActions} />
    )

    const capturedSubmit = capturedActions!.onSubmit

    view.rerender(<Harness onCancel={() => undefined} onSubmit={latestSubmit} publishActions={publishActions} />)

    await expect(capturedSubmit('second turn')).resolves.toBe(true)
    expect(initialSubmit).not.toHaveBeenCalled()
    expect(latestSubmit).toHaveBeenCalledWith('second turn')
  })

  it('keeps the bridge object and each callback identity stable across rerenders', () => {
    let capturedActions: WiringActions | null = null

    const publishActions = vi.fn((actions: WiringActions) => {
      capturedActions = actions
    })

    const initialSubmit = vi.fn(async () => true)

    const view = render(
      <Harness onCancel={() => undefined} onSubmit={initialSubmit} publishActions={publishActions} />
    )

    const initialActions = capturedActions!
    const initialSubmitDelegate = initialActions.onSubmit

    view.rerender(
      <Harness onCancel={() => undefined} onSubmit={vi.fn(async () => true)} publishActions={publishActions} />
    )

    expect(capturedActions).toBe(initialActions)
    expect(capturedActions!.onSubmit).toBe(initialSubmitDelegate)
    expect(publishActions).toHaveBeenCalledTimes(1)
  })

  it('preserves asynchronous rejections from the latest implementation', async () => {
    const expectedError = new Error('submit failed')
    let capturedActions: WiringActions | null = null

    const publishActions = (actions: WiringActions) => {
      capturedActions = actions
    }

    const view = render(
      <Harness onCancel={() => undefined} onSubmit={async () => true} publishActions={publishActions} />
    )

    view.rerender(
      <Harness
        onCancel={() => undefined}
        onSubmit={async () => {
          throw expectedError
        }}
        publishActions={publishActions}
      />
    )

    await expect(capturedActions!.onSubmit('second turn')).rejects.toBe(expectedError)
  })

  it('keeps action keys isolated while implementations change', async () => {
    const initialCancel = vi.fn()
    const latestCancel = vi.fn()
    const initialSubmit = vi.fn(async () => false)
    const latestSubmit = vi.fn(async () => true)
    let capturedActions: WiringActions | null = null

    const publishActions = (actions: WiringActions) => {
      capturedActions = actions
    }

    const view = render(
      <Harness onCancel={initialCancel} onSubmit={initialSubmit} publishActions={publishActions} />
    )

    view.rerender(<Harness onCancel={latestCancel} onSubmit={latestSubmit} publishActions={publishActions} />)

    await act(async () => capturedActions!.onCancel())
    await expect(capturedActions!.onSubmit('second turn')).resolves.toBe(true)

    expect(initialCancel).not.toHaveBeenCalled()
    expect(initialSubmit).not.toHaveBeenCalled()
    expect(latestCancel).toHaveBeenCalledTimes(1)
    expect(latestSubmit).toHaveBeenCalledWith('second turn')
  })
})
