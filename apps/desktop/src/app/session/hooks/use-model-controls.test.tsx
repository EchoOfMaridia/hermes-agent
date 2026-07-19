import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { cleanup, render, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { getGlobalModelInfo } from '@/hermes'
import { $activeSessionId, $currentModel, $currentProvider, setCurrentModel, setCurrentProvider } from '@/store/session'

import { useModelControls } from './use-model-controls'

const setGlobalModel = vi.fn()
const notifyError = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: vi.fn(),
  setGlobalModel: (...args: Parameters<typeof setGlobalModel>) => setGlobalModel(...args)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      desktop: {
        modelSwitchFailed: 'Model switch failed'
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({
  notifyError: (...args: Parameters<typeof notifyError>) => notifyError(...args)
}))

type Controls = ReturnType<typeof useModelControls>

function Harness({
  activeSessionId,
  onReady,
  queryClient: queryClientProp,
  requestGateway
}: {
  activeSessionId: string | null
  onReady: (controls: Controls) => void
  queryClient?: QueryClient
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const controls = useModelControls({
    activeSessionId,
    queryClient: queryClientProp ?? new QueryClient(),
    requestGateway
  })

  onReady(controls)

  return null
}

describe('useModelControls', () => {
  beforeEach(() => {
    $activeSessionId.set(null)
    setCurrentModel('')
    setCurrentProvider('')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $activeSessionId.set(null)
    setCurrentModel('')
    setCurrentProvider('')
  })

  it('applies the global model when there is no active runtime session', async () => {
    vi.mocked(getGlobalModelInfo).mockResolvedValue({
      model: 'openai/gpt-5.5',
      provider: 'openai-codex'
    })

    const { result } = renderHook(() =>
      useModelControls({
        activeSessionId: null,
        queryClient: new QueryClient(),
        requestGateway: vi.fn()
      })
    )

    await result.current.refreshCurrentModel()

    expect($currentModel.get()).toBe('openai/gpt-5.5')
    expect($currentProvider.get()).toBe('openai-codex')
  })

  it('does not clobber the active session footer state with global model info', async () => {
    setCurrentModel('deepseek/deepseek-v4-pro')
    setCurrentProvider('deepseek')
    $activeSessionId.set('runtime-1')
    vi.mocked(getGlobalModelInfo).mockResolvedValue({
      model: 'openai/gpt-5.5',
      provider: 'openai-codex'
    })

    const { result } = renderHook(() =>
      useModelControls({
        activeSessionId: 'runtime-1',
        queryClient: new QueryClient(),
        requestGateway: vi.fn()
      })
    )

    await result.current.refreshCurrentModel()

    expect($currentModel.get()).toBe('deepseek/deepseek-v4-pro')
    expect($currentProvider.get()).toBe('deepseek')
  })

  it('routes active-session picker changes through config.set with an explicit session-scoped provider', async () => {
    const requestGateway = vi.fn(async () => ({ key: 'model', value: 'claude-sonnet-4.6' }) as never)
    let controls!: Controls

    render(
      <Harness activeSessionId="session-1" onReady={value => (controls = value)} requestGateway={requestGateway} />
    )

    await expect(
      controls.selectModel({
        model: 'claude-sonnet-4.6',
        provider: 'anthropic'
      })
    ).resolves.toBe(true)

    expect(requestGateway).toHaveBeenCalledWith('config.set', {
      session_id: 'session-1',
      key: 'model',
      value: 'claude-sonnet-4.6 --provider anthropic --session'
    })
    expect(requestGateway).not.toHaveBeenCalledWith('slash.exec', expect.anything())
  })

  it('session-scopes MoA preset selections so they cannot persist as the global gateway default', async () => {
    const requestGateway = vi.fn(async () => ({ key: 'model', value: 'BeastMode' }) as never)
    let controls!: Controls

    render(
      <Harness activeSessionId="session-1" onReady={value => (controls = value)} requestGateway={requestGateway} />
    )

    await expect(
      controls.selectModel({
        model: 'BeastMode',
        provider: 'moa'
      })
    ).resolves.toBe(true)

    expect(requestGateway).toHaveBeenCalledWith('config.set', {
      session_id: 'session-1',
      key: 'model',
      value: 'BeastMode --provider moa --session'
    })
  })

  it('stores a no-session pick as UI state with no gateway or global write', async () => {
    const requestGateway = vi.fn()
    let controls!: Controls

    render(<Harness activeSessionId={null} onReady={value => (controls = value)} requestGateway={requestGateway} />)

    await expect(
      controls.selectModel({
        model: 'claude-sonnet-4.6',
        provider: 'anthropic'
      })
    ).resolves.toBe(true)

    // The pick is plain UI state; session.create ships it later. Nothing touches
    // the gateway or the profile default here.
    expect($currentModel.get()).toBe('claude-sonnet-4.6')
    expect($currentProvider.get()).toBe('anthropic')
    expect(requestGateway).not.toHaveBeenCalled()
    expect(setGlobalModel).not.toHaveBeenCalled()
  })

  it('seeds an empty composer model from global but never clobbers a pick', async () => {
    vi.mocked(getGlobalModelInfo).mockResolvedValue({ model: 'openai/gpt-5.5', provider: 'openai-codex' })

    const { result } = renderHook(() =>
      useModelControls({
        activeSessionId: null,
        queryClient: new QueryClient(),
        requestGateway: vi.fn()
      })
    )

    // Empty → seeds the default.
    await result.current.refreshCurrentModel()
    expect($currentModel.get()).toBe('openai/gpt-5.5')

    // A user pick must survive the lifecycle refreshes that fire on boot / fresh
    // draft / session events.
    setCurrentModel('anthropic/claude-sonnet-4.6')
    setCurrentProvider('anthropic')
    await result.current.refreshCurrentModel()
    expect($currentModel.get()).toBe('anthropic/claude-sonnet-4.6')

    // A profile swap forces a reseed to the new profile's default.
    await result.current.refreshCurrentModel(true)
    expect($currentModel.get()).toBe('openai/gpt-5.5')
  })

  it('keeps the picked model as current after a successful live-session switch, even when the gateway reports the old model first', async () => {
    // Reproduces the "instant snap back to M3" symptom: config.set succeeds,
    // but the gateway's model.options RPC still reports the prior model
    // until the backend applies the change. The renderer must NOT regress
    // to the old model — neither in the store mirror, nor in the cache
    // that currentPickerSelection reads.
    //
    // The test mounts an active model-options observer so invalidateQueries
    // actually triggers a refetch (otherwise the optimistic patch sticks
    // because no observer schedules a re-fetch in jsdom).
    const staleQueryData = {
      providers: [] as unknown[],
      model: 'MiniMax-M3',
      provider: 'MiniMax'
    }

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'config.set') {
        return { key: 'model', value: 'sonnet --provider anthropic --session' } as never
      }

      if (method === 'model.options') {
        // Stale response: backend hasn't propagated config.set yet.
        return staleQueryData as never
      }

      return {} as never
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    queryClient.setQueryData(['model-options', 'session-1'], staleQueryData)

    const Observer = () => {
      useQuery({
        queryKey: ['model-options', 'session-1'],
        queryFn: () => requestGateway('model.options'),
        staleTime: 0,
        refetchOnMount: 'always'
      })

      return null
    }

    let controls!: Controls

    render(
      <QueryClientProvider client={queryClient}>
        <Observer />
        <Harness
          activeSessionId="session-1"
          onReady={value => (controls = value)}
          queryClient={queryClient}
          requestGateway={requestGateway}
        />
      </QueryClientProvider>
    )

    // Wait for the observer's first refetch to settle so the cache contains
    // the stale M3 data before selectModel runs.
    await waitFor(() => {
      expect(queryClient.getQueryData(['model-options', 'session-1'])).toEqual(staleQueryData)
    })

    await expect(
      controls.selectModel({
        model: 'sonnet',
        provider: 'anthropic'
      })
    ).resolves.toBe(true)

    // Give the post-config.set invalidateQueries refetch a chance to fire and
    // (under the bug) clobber the optimistic patch.
    await new Promise(resolve => setTimeout(resolve, 50))

    // The user's pick survives in the UI mirror.
    expect($currentModel.get()).toBe('sonnet')
    expect($currentProvider.get()).toBe('anthropic')

    // The model-options cache reflects the pick, not the stale RPC response.
    // The optimistic patch from updateModelOptionsCache must NOT be clobbered
    // by the post-config.set invalidateQueries refetch.
    const cached = queryClient.getQueryData<{ model?: string; provider?: string }>(['model-options', 'session-1'])

    expect(cached?.model).toBe('sonnet')
    expect(cached?.provider).toBe('anthropic')
  })
})
