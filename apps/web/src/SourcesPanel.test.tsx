import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SourcesPanel } from './SourcesPanel'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <SourcesPanel />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SourcesPanel task controls', () => {
  it('creates an upload source and uploads a file from the empty state', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({
          source_id: 'browser-source', space_id: 'space-1', source_type: 'upload',
          uri: 'web-upload://browser', is_new: true,
        }))
      }
      if (url.endsWith('/sources/browser-source/upload') && init?.method === 'POST') {
        expect(init.body).toBeInstanceOf(FormData)
        return Promise.resolve(jsonResponse({
          source_id: 'browser-source', document_id: 'doc-1', blob_hash: 'a'.repeat(64),
          is_new_document: true, is_unchanged: false, task_id: 'upload-task',
        }))
      }
      if (url.endsWith('/tasks/upload-task')) {
        return Promise.resolve(jsonResponse({
          task_id: 'upload-task', source_id: 'browser-source', operation: 'ingest',
          status: 'succeeded', stage: 'publish', progress: 1,
          retry_count: 0, max_retries: 3, error_code: null,
          error: null, created_at: '2026-01-01T00:00:00Z',
        }))
      }
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({ sources: [] }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()

    const input = await screen.findByLabelText('选择要上传的文件')
    fireEvent.change(input, {
      target: { files: [new File(['content'], 'notes.md', { type: 'text/markdown' })] },
    })
    expect(screen.getByText('notes.md')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '上传并摄入' }))

    expect(await screen.findByText('成功')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ source_type: 'upload', uri: 'web-upload://browser' }),
      }),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources/browser-source/upload'),
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) }),
    )
  })

  it('switches to the new task after retry and exposes action labels', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'source-1',
            space_id: 'space-1',
            source_type: 'upload',
            uri: 'fixture://test',
            created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/ingest') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ task_id: 'old-task' }))
      }
      if (url.endsWith('/tasks/old-task/retry') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ task_id: 'new-task' }))
      }
      if (url.endsWith('/tasks/old-task')) {
        return Promise.resolve(jsonResponse({
          task_id: 'old-task', source_id: 'source-1', operation: 'ingest',
          status: 'failed', stage: 'parse', progress: 0.15,
          retry_count: 4, max_retries: 3, error_code: 'RUNTIME_ERROR',
          error: 'worker failed', created_at: '2026-01-01T00:00:00Z',
        }))
      }
      if (url.endsWith('/tasks/new-task')) {
        return Promise.resolve(jsonResponse({
          task_id: 'new-task', source_id: 'source-1', operation: 'ingest',
          status: 'succeeded', stage: 'publish', progress: 1,
          retry_count: 0, max_retries: 3, error_code: null,
          error: null, created_at: '2026-01-01T00:00:00Z',
        }))
      }
      if (url.endsWith('/detail')) {
        return Promise.resolve(jsonResponse({
          source: {
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          },
          documents: [],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()

    expect(await screen.findByRole('button', { name: '触发摄入' })).toBeInTheDocument()
    expect(screen.getByText('upload · source-1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新来源列表' })).toHaveTextContent('刷新')
    fireEvent.click(screen.getByRole('button', { name: '触发摄入' }))

    expect(await screen.findByText('失败')).toBeInTheDocument()
    expect(screen.getByText(/自动重试/)).toHaveTextContent('自动重试 3/3')
    const taskRefresh = screen.getByRole('button', { name: '刷新任务' })
    expect(taskRefresh).toHaveClass('task-refresh-button')
    expect(taskRefresh.querySelector('svg')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    await waitFor(() => expect(screen.getByText('成功')).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/tasks/new-task'),
      expect.anything(),
    )
  })

  it('renders a cancellation response as a terminal cancelled task', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/ingest') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ task_id: 'task-1' }))
      }
      if (url.endsWith('/tasks/task-1/cancel') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({
          task_id: 'task-1', source_id: 'source-1', operation: 'ingest',
          status: 'cancelled', stage: 'discover', progress: 0,
          retry_count: 0, max_retries: 3, error_code: null,
          error: null, created_at: '2026-01-01T00:00:00Z',
        }))
      }
      if (url.endsWith('/tasks/task-1')) {
        return Promise.resolve(jsonResponse({
          task_id: 'task-1', source_id: 'source-1', operation: 'ingest',
          status: 'running', stage: 'parse', progress: 0.15,
          retry_count: 0, max_retries: 3, error_code: null,
          error: null, created_at: '2026-01-01T00:00:00Z',
        }))
      }
      if (url.endsWith('/detail')) {
        return Promise.resolve(jsonResponse({
          source: {
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          },
          documents: [],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: '触发摄入' }))
    expect(screen.queryByText(/自动重试/)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '取消任务' }))

    await waitFor(() => expect(screen.getByText('已取消')).toBeInTheDocument())
  })

  it('converges after a cancellation race without showing conflict', async () => {
    let taskReads = 0
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/ingest') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ task_id: 'task-race' }))
      }
      if (url.endsWith('/tasks/task-race/cancel') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ detail: 'conflict' }, 409))
      }
      if (url.endsWith('/tasks/task-race')) {
        taskReads += 1
        return Promise.resolve(jsonResponse({
          task_id: 'task-race', source_id: 'source-1', operation: 'ingest',
          status: taskReads === 1 ? 'running' : 'succeeded', stage: 'publish',
          progress: taskReads === 1 ? 0.9 : 1, retry_count: 0, max_retries: 3,
          error_code: null, error: null, created_at: '2026-01-01T00:00:00Z',
        }))
      }
      if (url.endsWith('/detail')) {
        return Promise.resolve(jsonResponse({
          source: {
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          },
          documents: [],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: '触发摄入' }))
    fireEvent.click(await screen.findByRole('button', { name: '取消任务' }))

    await waitFor(() => expect(screen.getByText('成功')).toBeInTheDocument())
    expect(screen.queryByText(/操作失败/)).not.toBeInTheDocument()
    expect(screen.queryByText(/conflict/i)).not.toBeInTheDocument()
  })

  it('shows a Unicode document display name in the interface font', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/detail')) {
        return Promise.resolve(jsonResponse({
          source: {
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          },
          documents: [{
            id: 'doc-1', stable_key: '.txt', display_name: '中文测试资料.txt',
            current_version_id: 'version-1', status: 'active',
            created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const { container } = renderPanel()
    fireEvent.click(await screen.findByText('fixture://test'))

    expect(await screen.findByText('中文测试资料.txt')).toBeInTheDocument()
    expect(container.querySelector('.doc-key')).toHaveAttribute('title', '稳定键：.txt')
  })

  it('keeps the complete upload hash visible', async () => {
    const hash = '86386fb5317e4f8080cc3b8f3d26c12da4d4e92aa8c5a6c86df4e3e8d2f4a1b2'
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/upload') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({
          source_id: 'source-1', document_id: 'doc-1', blob_hash: hash,
          is_new_document: true, is_unchanged: false, task_id: null,
        }))
      }
      if (url.endsWith('/detail')) {
        return Promise.resolve(jsonResponse({
          source: {
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          },
          documents: [],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()
    fireEvent.click(await screen.findByText('fixture://test'))
    const uploadForm = document.querySelector('.upload-form') as HTMLFormElement
    const input = uploadForm.querySelector('input[type="file"]') as HTMLInputElement
    const file = new File(['content'], 'test.txt', { type: 'text/plain' })
    fireEvent.change(input, { target: { files: [file] } })
    fireEvent.submit(uploadForm)

    expect(await screen.findByText(`文件已登记，哈希 ${hash}`)).toBeInTheDocument()
    expect(screen.queryByText(/86386fb5317e…/)).not.toBeInTheDocument()
  })

  it('deletes a document through the source detail action', async () => {
    let deleted = false
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'source-1',
            space_id: 'space-1',
            source_type: 'upload',
            uri: 'fixture://test',
            created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/documents/doc-1') && init?.method === 'DELETE') {
        deleted = true
        return Promise.resolve(jsonResponse({
          document_id: 'doc-1', status: 'deleted', task_id: 'delete-task',
        }))
      }
      if (url.endsWith('/detail')) {
        return Promise.resolve(jsonResponse({
          source: {
            id: 'source-1', space_id: 'space-1', source_type: 'upload',
            uri: 'fixture://test', created_at: '2026-01-01T00:00:00Z',
          },
          documents: [{
            id: 'doc-1', stable_key: 'notes.md', display_name: 'notes.md',
            current_version_id: deleted ? null : 'version-1',
            status: deleted ? 'deleted' : 'available',
            created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPanel()
    fireEvent.click(await screen.findByText('fixture://test'))
    const deleteButton = await screen.findByRole('button', { name: '删除文档：notes.md' })
    fireEvent.click(deleteButton)

    await waitFor(() => expect(deleted).toBe(true))
    expect(await screen.findByText('已删除')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除文档：notes.md' })).not.toBeInTheDocument()
  })
})
