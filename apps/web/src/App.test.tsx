import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { fetchHealthSnapshot } from './health'

function jsonResponse(body: unknown, status = 200, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

function mockHealthyFetch(model = { healthy: true, code: 'MODEL_FAKE_READY' }) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/health/live')) {
      return Promise.resolve(jsonResponse({ status: 'alive' }))
    }
    if (url.includes('/sources')) {
      return Promise.resolve(jsonResponse({ sources: [] }))
    }
    return Promise.resolve(
      jsonResponse(
        {
          status: 'ready',
          checks: {
            postgresql: { healthy: true, code: 'POSTGRESQL_OK' },
            redis: { healthy: true, code: 'REDIS_OK' },
            model,
          },
        },
        200,
        { 'X-Trace-ID': 'trace-123', 'X-Request-ID': 'request-123' },
      ),
    )
  })
}

function renderApp(initialHash = '#system-status') {
  window.history.replaceState(null, '', initialHash)
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '#system-status')
})

describe('system status workspace', () => {
  it('uses the v2 Assistant conversation workspace as the default entry', async () => {
    vi.stubGlobal('fetch', mockHealthyFetch())

    renderApp('')

    expect(await screen.findByRole('combobox', { name: '消息' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#qa')
    expect(screen.getByRole('combobox', { name: '消息' })).toBeInTheDocument()
  })

  it('shows bounded loading and then all healthy services', async () => {
    const fetchMock = mockHealthyFetch()
    vi.stubGlobal('fetch', fetchMock)

    renderApp()

    expect(screen.getByText('正在检查本地服务')).toBeInTheDocument()
    expect(await screen.findByText('本地服务运行正常')).toBeInTheDocument()
    expect(screen.getByText('4 / 4 项当前可用')).toBeInTheDocument()
    expect(screen.getByText('测试替身')).toBeInTheDocument()
    expect(screen.getByText('trace-123')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('shows degraded dependency state without treating the API as offline', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/health/live')) {
        return Promise.resolve(jsonResponse({ status: 'alive' }))
      }
      return Promise.resolve(
        jsonResponse(
          {
            status: 'degraded',
            checks: {
              postgresql: { healthy: false, code: 'POSTGRESQL_UNREACHABLE' },
              redis: { healthy: true, code: 'REDIS_OK' },
              model: { healthy: false, code: 'MODEL_DISABLED' },
            },
          },
          503,
        ),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    renderApp()

    expect(await screen.findByText('部分本地服务不可用')).toBeInTheDocument()
    expect(screen.getByText('2 / 4 项当前可用')).toBeInTheDocument()
    expect(screen.getByText('已禁用')).toBeInTheDocument()
    expect(screen.queryByText('无法连接本地 API')).not.toBeInTheDocument()
  })

  it('loads conversation history while the initial workspace is open', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/conversations')) {
        return Promise.resolve(jsonResponse({
          conversations: [{
            conversation_id: 'conversation-history',
            space_id: 'space-1',
            owner_id: 'local',
            created_at: '2026-08-02T10:00:00Z',
            updated_at: '2026-08-02T12:00:00Z',
            messages: [{
              message_id: 'message-history',
              role: 'user',
              content: 'previous question',
              run_id: 'run-history',
              created_at: '2026-08-02T12:00:00Z',
            }],
            runs: [],
          }],
        }))
      }
      if (url.endsWith('/health/live')) {
        return Promise.resolve(jsonResponse({ status: 'alive' }))
      }
      return Promise.resolve(jsonResponse({
        status: 'ready',
        checks: {
          postgresql: { healthy: true, code: 'POSTGRESQL_OK' },
          redis: { healthy: true, code: 'REDIS_OK' },
          model: { healthy: true, code: 'MODEL_FAKE_READY' },
        },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderApp()

    expect(await screen.findByText('previous question')).toBeInTheDocument()
  })

  it('exits loading on API failure and supports manual retry', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('network unavailable'))
    vi.stubGlobal('fetch', fetchMock)

    renderApp()

    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地 API')
    expect(screen.getByText('API_UNREACHABLE')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '重新检查' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5))
  })

  it('replaces the loading state when the API response schema is invalid', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input).endsWith('/health/live')) {
        return Promise.resolve(jsonResponse({ status: 'alive' }))
      }
      return Promise.resolve(jsonResponse({ status: 'ready', checks: {} }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderApp()

    expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地 API')
    expect(screen.getByText('INVALID_RESPONSE')).toBeInTheDocument()
  })

  it('aborts health requests at the bounded timeout', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        )
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = expect(fetchHealthSnapshot()).rejects.toMatchObject({ code: 'API_TIMEOUT' })
    await vi.advanceTimersByTimeAsync(8_000)
    await result
  })

  it('allows keyboard focus on the system refresh control', async () => {
    vi.stubGlobal('fetch', mockHealthyFetch())

    renderApp()

    await screen.findByText('本地服务运行正常')
    const refreshButton = screen.getByRole('button', { name: '重新检查系统状态' })
    refreshButton.focus()

    expect(refreshButton).toHaveFocus()
  })

  it('keeps data sources in a separate workspace view', async () => {
    const fetchMock = mockHealthyFetch()
    vi.stubGlobal('fetch', fetchMock)

    renderApp()
    await screen.findByText('本地服务运行正常')
    fireEvent.click(screen.getByRole('link', { name: '数据来源' }))

    expect(screen.getByRole('heading', { level: 1, name: '数据来源' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '来源与文档' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '服务连接' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '数据来源' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('opens the conversation workspace from primary navigation', async () => {
    vi.stubGlobal('fetch', mockHealthyFetch())

    renderApp()
    await screen.findByText('本地服务运行正常')
    fireEvent.click(screen.getByRole('link', { name: '对话' }))

    expect(screen.getByRole('heading', { level: 1, name: '对话' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '消息' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '引用证据' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '对话' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  it('shows duplicate questions as separate conversation history entries', async () => {
    const history = {
      conversations: [
        {
          conversation_id: 'conversation-new',
          space_id: 'space-1',
          owner_id: 'local',
          created_at: '2026-08-02T10:00:00Z',
          updated_at: '2026-08-02T12:00:00Z',
          messages: [{
            message_id: 'message-new',
            role: 'user',
            content: '相同问题',
            run_id: 'run-new',
            created_at: '2026-08-02T12:00:00Z',
          }],
          runs: [],
        },
        {
          conversation_id: 'conversation-old',
          space_id: 'space-1',
          owner_id: 'local',
          created_at: '2026-08-01T10:00:00Z',
          updated_at: '2026-08-01T12:00:00Z',
          messages: [{
            message_id: 'message-old',
            role: 'user',
            content: '相同问题',
            run_id: 'run-old',
            created_at: '2026-08-01T12:00:00Z',
          }],
          runs: [],
        },
      ],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/health/live')) return Promise.resolve(jsonResponse({ status: 'alive' }))
      if (url.includes('/conversations')) return Promise.resolve(jsonResponse(history))
      if (url.endsWith('/skills')) return Promise.resolve(jsonResponse([]))
      return Promise.resolve(jsonResponse({
        status: 'ready',
        checks: {
          postgresql: { healthy: true, code: 'POSTGRESQL_OK' },
          redis: { healthy: true, code: 'REDIS_OK' },
          model: { healthy: true, code: 'MODEL_FAKE_READY' },
        },
      }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderApp()
    fireEvent.click(screen.getByRole('link', { name: '对话' }))

    const entries = await waitFor(() => {
      const items = Array.from(document.querySelectorAll<HTMLButtonElement>('.sidebar-history-select'))
      expect(items).toHaveLength(2)
      return items
    })
    expect(entries[0]).toHaveTextContent('8/2')
    expect(entries[1]).toHaveTextContent('1 个问题')

    fireEvent.click(entries[1])
    expect(screen.getByRole('combobox', { name: '消息' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '引用证据' })).not.toBeInTheDocument()
  })
})
