import { useRef } from 'react'

import type { WiringActions } from './types'

type WiringActionDelegate = (...parameters: unknown[]) => unknown

/**
 * Returns stable action delegates that invoke the latest controller callbacks.
 *
 * Memoized surfaces can capture these delegates once without retaining closures
 * from the render that created them. The bridge object and every delegate keep
 * stable identity; only the ref-backed implementation changes between renders.
 */
export function useLiveWiringActions(currentActions: WiringActions): WiringActions {
  const currentActionsRef = useRef(currentActions)
  currentActionsRef.current = currentActions

  const liveActionsRef = useRef<WiringActions | null>(null)

  if (!liveActionsRef.current) {
    const delegates = new Map<string, WiringActionDelegate>()

    liveActionsRef.current = new Proxy({} as WiringActions, {
      get(_target, propertyName: string | symbol) {
        if (typeof propertyName !== 'string') {
          return undefined
        }

        const existingDelegate = delegates.get(propertyName)

        if (existingDelegate) {
          return existingDelegate
        }

        const delegate: WiringActionDelegate = (...parameters) => {
          const implementation: unknown = currentActionsRef.current[propertyName as keyof WiringActions]

          if (typeof implementation !== 'function') {
            throw new TypeError(`Wiring action "${propertyName}" is not callable`)
          }

          return (implementation as WiringActionDelegate)(...parameters)
        }

        delegates.set(propertyName, delegate)

        return delegate
      }
    })
  }

  return liveActionsRef.current
}
