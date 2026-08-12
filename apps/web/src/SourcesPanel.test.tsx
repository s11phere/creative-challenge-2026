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
          source_id: 'browser-source', space_id: 'space-1', source_type: 'folder',
          uri: '', name: '默认文件夹', is_new: true,
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
        body: JSON.stringify({ source_type: 'folder', name: '默认文件夹' }),
      }),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources/browser-source/upload'),
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) }),
    )
  })

  it('rolls back a freshly created source when the upload fails', async () => {
    let sourceDeleted = false
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({
          source_id: 'browser-source', space_id: 'space-1', source_type: 'folder',
          uri: '', name: '默认文件夹', is_new: true,
        }))
      }
      if (url.endsWith('/sources/browser-source/upload') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({ detail: 'boom' }, 500))
      }
      if (url.endsWith('/sources/browser-source') && init?.method === 'DELETE') {
        sourceDeleted = true
        return Promise.resolve(jsonResponse({ source_id: 'browser-source', status: 'deleted' }))
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
      target: { files: [new File(['content'], 'fail.md', { type: 'text/markdown' })] },
    })
    fireEvent.click(screen.getByRole('button', { name: '上传并摄入' }))

    expect(await screen.findByText(/上传失败/)).toBeInTheDocument()
    await waitFor(() => expect(sourceDeleted).toBe(true))
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources/browser-source'),
      expect.objectContaining({ method: 'DELETE' }),
    )
    // The rolled-back folder must not appear as a new card below.
    expect(screen.queryByText(/默认文件夹/)).not.toBeInTheDocument()
  })

  it('shows the configured upload size limit in the direct upload hint', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/config/limits')) {
        return Promise.resolve(jsonResponse({
          max_upload_size_mb: 50, max_upload_size_bytes: 52428800,
        }))
      }
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({ sources: [] }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()

    expect(await screen.findByText(/单个文件不超过 50 MB/)).toBeInTheDocument()
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
        return Promise.resolve(jsonResponse({ task_ids: ['old-task'] }))
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
    expect(screen.getByText('fixture://test')).toBeInTheDocument()
    expect(screen.getByText(/浏览器上传 · 0 个文档 · 0 可用/)).toBeInTheDocument()
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
        return Promise.resolve(jsonResponse({ task_ids: ['task-1'] }))
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
        return Promise.resolve(jsonResponse({ task_ids: ['task-race'] }))
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

  it('reports a registered upload without leaking the blob hash', async () => {
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

    expect(await screen.findByText('文件已登记')).toBeInTheDocument()
    expect(screen.queryByText(new RegExp(hash))).not.toBeInTheDocument()
  })

  it('refreshes document status after the ingestion task succeeds', async () => {
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
      if (url.endsWith('/upload') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({
          source_id: 'source-1', document_id: 'doc-1', blob_hash: 'a'.repeat(64),
          is_new_document: true, is_unchanged: false, task_id: 'task-1',
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
            current_version_id: taskReads >= 2 ? 'version-1' : null,
            status: taskReads >= 2 ? 'available' : 'unavailable',
            created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      if (url.endsWith('/tasks/task-1')) {
        taskReads += 1
        const succeeded = taskReads >= 2
        return Promise.resolve(jsonResponse({
          task_id: 'task-1', source_id: 'source-1', operation: 'ingest',
          status: succeeded ? 'succeeded' : 'running',
          stage: succeeded ? 'publish' : 'parse',
          progress: succeeded ? 1 : 0.2,
          retry_count: 0, max_retries: 3, error_code: null, error: null,
          created_at: '2026-01-01T00:00:00Z',
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()
    fireEvent.click(await screen.findByText('fixture://test'))
    await waitFor(() => expect(document.querySelector('.doc-status.unavailable')).not.toBeNull())

    const uploadForm = document.querySelector('.upload-form') as HTMLFormElement
    const input = uploadForm.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['content'], 'notes.md')] } })
    fireEvent.submit(uploadForm)

    await waitFor(() => expect(document.querySelector('.task-row[data-status="running"]')).not.toBeNull())
    fireEvent.click(document.querySelector('.task-refresh-button')!)

    await waitFor(() => expect(document.querySelector('.task-row[data-status="succeeded"]')).not.toBeNull())
    await waitFor(() => expect(document.querySelector('.doc-status.available')).not.toBeNull())
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
    await waitFor(() => expect(screen.queryByText('notes.md')).not.toBeInTheDocument())
    expect(document.querySelector('.doc-list h4')).toHaveTextContent('0')
  })

  it('renames a folder through the inline input and reflects the new name', async () => {
    let sourcesPayload: { sources: unknown[] } = {
      sources: [{
        id: 'folder-1', space_id: 'space-1', source_type: 'folder', uri: '',
        name: '文档', created_at: '2026-01-01T00:00:00Z',
      }],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources/folder-1') && init?.method === 'PATCH') {
        expect(JSON.parse(String(init.body))).toEqual({ name: '新名字' })
        sourcesPayload = {
          sources: [{
            id: 'folder-1', space_id: 'space-1', source_type: 'folder', uri: '',
            name: '新名字', created_at: '2026-01-01T00:00:00Z',
          }],
        }
        return Promise.resolve(jsonResponse({ source_id: 'folder-1', name: '新名字', status: 'renamed' }))
      }
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse(sourcesPayload))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: '重命名文件夹' }))
    const input = await screen.findByRole('textbox', { name: '重命名文件夹' })
    fireEvent.change(input, { target: { value: '新名字' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(await screen.findByText('新名字', { selector: '.source-title' })).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources/folder-1'),
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: '新名字' }) }),
    )
  })

  it('cancels folder rename on Escape without calling the API', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'folder-1', space_id: 'space-1', source_type: 'folder', uri: '',
            name: '文档', created_at: '2026-01-01T00:00:00Z',
          }],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: '重命名文件夹' }))
    const input = await screen.findByRole('textbox', { name: '重命名文件夹' })
    fireEvent.change(input, { target: { value: '改动' } })
    fireEvent.keyDown(input, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('textbox', { name: '重命名文件夹' })).not.toBeInTheDocument())
    const patchCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')
    expect(patchCalls).toHaveLength(0)
    expect(screen.getByText('文档', { selector: '.source-title' })).toBeInTheDocument()
  })

  it('creates a new folder and uploads into it from the direct upload', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources') && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toEqual({ source_type: 'folder', name: '自定义' })
        return Promise.resolve(jsonResponse({
          source_id: 'folder-new', space_id: 'space-1', source_type: 'folder',
          uri: '', name: '自定义', is_new: true,
        }))
      }
      if (url.endsWith('/sources/folder-new/upload') && init?.method === 'POST') {
        return Promise.resolve(jsonResponse({
          source_id: 'folder-new', document_id: 'doc-1', blob_hash: 'a'.repeat(64),
          is_new_document: true, is_unchanged: false, task_id: 'upload-task',
        }))
      }
      if (url.endsWith('/tasks/upload-task')) {
        return Promise.resolve(jsonResponse({
          task_id: 'upload-task', source_id: 'folder-new', operation: 'ingest',
          status: 'succeeded', stage: 'publish', progress: 1,
          retry_count: 0, max_retries: 3, error_code: null, error: null,
          created_at: '2026-01-01T00:00:00Z',
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderPanel()

    const nameInput = await screen.findByRole('textbox', { name: '新文件夹名称' })
    fireEvent.change(nameInput, { target: { value: '自定义' } })
    const fileInput = await screen.findByLabelText('选择要上传的文件')
    fireEvent.change(fileInput, {
      target: { files: [new File(['content'], 'notes.md', { type: 'text/markdown' })] },
    })
    fireEvent.click(screen.getByRole('button', { name: '上传并摄入' }))

    expect(await screen.findByText('成功')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources/folder-new/upload'),
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) }),
    )
  })

  it('deletes a folder and its documents after confirmation', async () => {
    let cleared = false
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/sources/folder-1/contents') && init?.method === 'DELETE') {
        cleared = true
        return Promise.resolve(jsonResponse({ source_id: 'folder-1', status: 'deleted', documents_cleared: 2 }))
      }
      if (url.endsWith('/sources')) {
        return Promise.resolve(jsonResponse({
          sources: [{
            id: 'folder-1', space_id: 'space-1', source_type: 'folder', uri: '',
            name: '文档', created_at: '2026-01-01T00:00:00Z', doc_count: 2,
          }],
        }))
      }
      return Promise.resolve(jsonResponse({ sources: [] }))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: '删除文件夹' }))

    await waitFor(() => expect(cleared).toBe(true))
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/sources/folder-1/contents'),
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
