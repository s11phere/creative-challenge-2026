import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QAWorkspace } from './QAWorkspace'

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderWorkspace() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
  return render(
    <QueryClientProvider client={client}>
      <QAWorkspace />
    </QueryClientProvider>,
  )
}

const activeSkills = [
  { name: 'knowledge_agent', active_version: '0.1.0', versions: ['0.1.0'] },
  { name: 'knowledge_qa', active_version: '0.1.0', versions: ['0.1.0'] },
  { name: 'create_review_cards', active_version: '0.1.0', versions: ['0.1.0'] },
]

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('QAWorkspace', () => {
  it('creates a conversation, submits a question, and cancels explicitly', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) {
        return Promise.resolve(response(activeSkills))
      }
      if (url.endsWith('/conversations')) {
        return Promise.resolve(
          response({
            conversation_id: 'conversation-1',
            space_id: 'space-1',
            owner_id: 'local',
          }),
        )
      }
      if (url.endsWith('/questions')) {
        return Promise.resolve(
          response(
            {
              run_id: 'run-1',
              attempt_id: 'attempt-1',
              status: 'queued',
              conversation_id: 'conversation-1',
              question_message_id: 'message-1',
              cancellation_requested: false,
              error_code: null,
            },
            202,
          ),
        )
      }
      if (url.endsWith('/cancel') && init?.method === 'POST') {
        return Promise.resolve(
          response({
            run_id: 'run-1',
            attempt_id: 'attempt-1',
            status: 'cancel_requested',
            conversation_id: 'conversation-1',
            question_message_id: 'message-1',
            cancellation_requested: true,
            error_code: null,
          }),
        )
      }
      return Promise.resolve(
        response({
          run_id: 'run-1',
          attempt_id: 'attempt-1',
          status: 'queued',
          conversation_id: 'conversation-1',
          question_message_id: 'message-1',
          cancellation_requested: false,
          error_code: null,
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-1' })

    renderWorkspace()
    fireEvent.change(screen.getByLabelText('问题'), {
      target: { value: '文档中的关键结论是什么？' },
    })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText('文档中的关键结论是什么？')).toBeInTheDocument()
    expect(screen.getByText('等待执行')).toBeInTheDocument()
    expect(screen.getByText('等待证据校验')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() => expect(screen.getByText('正在取消')).toBeInTheDocument())
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/skills/knowledge_agent/runs'),
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/qa/runs/run-1/cancel'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('can switch to direct QA before submitting', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) return Promise.resolve(response(activeSkills))
      if (url.endsWith('/conversations')) {
        return Promise.resolve(
          response({ conversation_id: 'conversation-1', space_id: 'space-1', owner_id: 'local' }),
        )
      }
      return Promise.resolve(
        response({
          run_id: 'run-direct',
          attempt_id: 'attempt-direct',
          status: 'queued',
          conversation_id: 'conversation-1',
          question_message_id: 'message-direct',
          cancellation_requested: false,
          error_code: null,
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-direct' })

    renderWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '直接问答' }))
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: 'Direct question' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/conversations\/conversation-1\/questions$/),
        expect.objectContaining({ method: 'POST' }),
      ),
    )
  })

  it('keeps recovery hidden for a newly submitted run', async () => {
    const queued = {
      run_id: 'run-resume',
      attempt_id: 'attempt-resume',
      status: 'queued',
      conversation_id: 'conversation-1',
      question_message_id: 'message-resume',
      cancellation_requested: false,
      error_code: null,
      skill: { name: 'knowledge_agent', version: '0.1.0', content_sha256: 'a'.repeat(64) },
      fixed_scope: { source_ids: [], document_ids: [], version_ids: [] },
      citations: [],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) return Promise.resolve(response(activeSkills))
      if (url.endsWith('/conversations')) {
        return Promise.resolve(
          response({ conversation_id: 'conversation-1', space_id: 'space-1', owner_id: 'local' }),
        )
      }
      return Promise.resolve(response(queued, url.includes('/runs') ? 202 : 200))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-resume' })

    renderWorkspace()
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: 'Resume this run.' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))
    await screen.findByText('Resume this run.')
    expect(screen.queryByRole('button', { name: '恢复执行' })).not.toBeInTheDocument()
  })

  it('restores a historical conversation and exposes recovery for its active run', async () => {
    const queued = {
      run_id: 'run-history',
      attempt_id: 'attempt-history',
      status: 'queued',
      conversation_id: 'conversation-history',
      question_message_id: 'message-history',
      cancellation_requested: false,
      error_code: null,
      skill: { name: 'knowledge_agent', version: '0.1.0', content_sha256: 'a'.repeat(64) },
      fixed_scope: { source_ids: [], document_ids: [], version_ids: [] },
      citations: [],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) return Promise.resolve(response(activeSkills))
      if (url.includes('/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({
          conversations: [{
            conversation_id: 'conversation-history',
            space_id: 'space-1',
            owner_id: 'local',
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            messages: [{
              message_id: 'message-history',
              role: 'user',
              content: '恢复这次历史问题',
              run_id: null,
              created_at: '2026-01-01T00:00:00Z',
            }],
            runs: [queued],
          }],
        }))
      }
      if (url.includes('/api/v1/qa/runs/run-history')) return Promise.resolve(response(queued))
      if (url.includes('/resume') && init?.method === 'POST') return Promise.resolve(response(queued, 202))
      return Promise.resolve(response(activeSkills))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWorkspace()
    expect(await screen.findByText('恢复这次历史问题')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: '恢复执行' })).toBeInTheDocument()
  })

  it('runs review cards against a fixed version and records an approval decision', async () => {
    const completed = {
      run_id: 'run-review',
      attempt_id: 'attempt-review',
      status: 'completed',
      conversation_id: 'conversation-1',
      question_message_id: 'message-review',
      cancellation_requested: false,
      error_code: null,
      skill: { name: 'create_review_cards', version: '0.1.0', content_sha256: 'c'.repeat(64) },
      fixed_scope: {
        source_ids: ['source-1'],
        document_ids: ['document-1'],
        version_ids: ['version-1'],
      },
      result: { type: 'answer', text: 'Review card preview.', limitations: [] },
      citations: [{
        evidence_id: 'evidence-1', source_id: 'source-1', document_id: 'document-1',
        version_id: 'version-1', chunk_id: 'chunk-1',
        locator: { kind: 'lines', start: 1, end: 2 },
      }],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) return Promise.resolve(response(activeSkills))
      if (url.endsWith('/sources')) {
        return Promise.resolve(response({ sources: [{
          id: 'source-1', space_id: 'space-1', source_type: 'upload',
          uri: 'fixture://review', created_at: '2026-01-01T00:00:00Z',
        }] }))
      }
      if (url.endsWith('/sources/source-1/detail')) {
        return Promise.resolve(response({
          source: { id: 'source-1', uri: 'fixture://review' },
          documents: [{
            id: 'document-1', display_name: 'review.md', status: 'available',
            current_version_id: 'version-1',
          }],
        }))
      }
      if (url.endsWith('/conversations')) {
        return Promise.resolve(
          response({ conversation_id: 'conversation-1', space_id: 'space-1', owner_id: 'local' }),
        )
      }
      if (url.endsWith('/approvals') && init?.method === 'POST') {
        return Promise.resolve(response({
          approval_id: 'approval-1', run_id: 'run-review', status: 'pending',
          side_effects: 0, derived_knowledge_id: null,
        }, 201))
      }
      if (url.endsWith('/approvals/approval-1/decision') && init?.method === 'POST') {
        return Promise.resolve(response({
          approval_id: 'approval-1', run_id: 'run-review', status: 'approved',
          side_effects: 1, derived_knowledge_id: 'derived-1',
        }))
      }
      return Promise.resolve(response(completed, url.includes('/skills/create_review_cards/runs') ? 202 : 200))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-review' })

    renderWorkspace()
    fireEvent.click(screen.getByRole('button', { name: '复习卡' }))
    await screen.findByRole('option', { name: 'fixture://review' })
    fireEvent.change(await screen.findByLabelText('来源'), { target: { value: 'source-1' } })
    await screen.findByRole('option', { name: 'review.md' })
    fireEvent.change(await screen.findByLabelText('固定文档版本'), {
      target: { value: 'document-1' },
    })
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: 'Create cards.' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))
    fireEvent.click(await screen.findByRole('button', { name: '申请写入审批' }))
    fireEvent.click(await screen.findByRole('button', { name: '批准写入' }))

    expect(await screen.findByText('审批已批准，已写入派生知识')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/skills/create_review_cards/runs'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          document_id: 'document-1', version_id: 'version-1', focus: undefined,
          idempotency_key: 'idempotency-review',
        }),
      }),
    )
  })

  it('keeps evidence empty instead of inventing citations', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(response(activeSkills))))
    renderWorkspace()

    expect(await screen.findByText('knowledge_agent')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '引用证据' })).toBeInTheDocument()
    expect(screen.getByText('当前回答没有可显示的引用')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('renders a completed answer and its verified citation identity', async () => {
    const completed = {
      run_id: 'run-1',
      attempt_id: 'attempt-1',
      status: 'completed',
      conversation_id: 'conversation-1',
      question_message_id: 'message-1',
      cancellation_requested: false,
      error_code: null,
      skill: {
        name: 'knowledge_agent',
        version: '0.1.0',
        content_sha256: 'a'.repeat(64),
      },
      result: {
        type: 'answer',
        text: 'A grounded answer.',
        limitations: ['Provisional quality.'],
      },
      citations: [
        {
          evidence_id: 'evidence-1',
          source_id: '00000000-0000-0000-0000-000000000002',
          document_id: '00000000-0000-0000-0000-000000000003',
          version_id: '00000000-0000-0000-0000-000000000004',
          chunk_id: '00000000-0000-0000-0000-000000000005',
          locator: { kind: 'lines', start: 4, end: 8 },
        },
      ],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) {
        return Promise.resolve(response(activeSkills))
      }
      if (url.endsWith('/conversations')) {
        return Promise.resolve(
          response({
            conversation_id: 'conversation-1',
            space_id: 'space-1',
            owner_id: 'local',
          }),
        )
      }
      if (url.includes('/sources/00000000-0000-0000-0000-000000000002/detail')) {
        return Promise.resolve(response({
          source: { id: '00000000-0000-0000-0000-000000000002', uri: 'fixture://supported' },
          documents: [{
            id: '00000000-0000-0000-0000-000000000003',
            display_name: 'supported.md',
          }],
        }))
      }
      if (url.includes('/citations/evidence-1')) {
        return Promise.resolve(
          response({
            ...completed.citations[0],
            status: 'valid',
            excerpt: 'The exact source lines.',
          }),
        )
      }
      return Promise.resolve(response(completed, url.endsWith('/questions') ? 202 : 200))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-1' })

    renderWorkspace()
    expect(await screen.findByText('v0.1.0')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('问题'), {
      target: { value: 'Show the supported conclusion.' },
    })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText('A grounded answer.')).toBeInTheDocument()
    expect(screen.getByText('已固定')).toBeInTheDocument()
    expect(screen.getByText('Provisional quality.')).toBeInTheDocument()
    expect(screen.getByText('lines 4-8')).toBeInTheDocument()
    expect(await screen.findByText('supported.md')).toBeInTheDocument()
    expect(screen.getByText('版本 00000000')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /查看原文/ }))
    expect(await screen.findByText('The exact source lines.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/qa/runs/run-1/citations/evidence-1'),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    fireEvent.click(screen.getByRole('button', { name: '关闭原文' }))
    expect(screen.queryByText('The exact source lines.')).not.toBeInTheDocument()
  })

  it('shows a useful citation error and retries it', async () => {
    const completed = {
      run_id: 'run-error', attempt_id: 'attempt-error', status: 'completed',
      conversation_id: 'conversation-1', question_message_id: 'message-1',
      cancellation_requested: false, error_code: null,
      skill: { name: 'knowledge_agent', version: '0.1.0', content_sha256: 'a'.repeat(64) },
      result: { type: 'answer', text: 'Answer.', limitations: [] },
      citations: [{
        evidence_id: 'evidence-error', source_id: 'source-1', document_id: 'document-1',
        version_id: 'version-1', chunk_id: 'chunk-1',
        locator: { kind: 'lines', start: 1, end: 2 },
      }],
    }
    let citationCalls = 0
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) return Promise.resolve(response(activeSkills))
      if (url.endsWith('/conversations')) {
        return Promise.resolve(response({ conversation_id: 'conversation-1', space_id: 'space-1', owner_id: 'local' }))
      }
      if (url.includes('/citations/evidence-error')) {
        citationCalls += 1
        return citationCalls === 1
          ? Promise.resolve(response({ detail: '固定版本暂时无法读取' }, 503))
          : Promise.resolve(response({ ...completed.citations[0], status: 'valid', excerpt: 'Recovered excerpt.' }))
      }
      return Promise.resolve(response(completed, url.includes('/skills/knowledge_agent/runs') ? 202 : 200))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-error' })

    renderWorkspace()
    fireEvent.change(screen.getByLabelText('问题'), { target: { value: 'Question.' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))
    fireEvent.click(await screen.findByRole('button', { name: /查看原文/ }))

    expect(await screen.findByText('固定版本暂时无法读取')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('Recovered excerpt.')).toBeInTheDocument()
    expect(citationCalls).toBe(2)
  })

  it('submits on Enter and inserts a newline with Ctrl+Enter', async () => {
    const completed = {
      run_id: 'run-keyboard',
      attempt_id: 'attempt-keyboard',
      status: 'completed',
      conversation_id: 'conversation-keyboard',
      question_message_id: 'message-keyboard',
      cancellation_requested: false,
      error_code: null,
      skill: { name: 'knowledge_agent', version: '0.1.0', content_sha256: 'a'.repeat(64) },
      fixed_scope: { source_ids: [], document_ids: [], version_ids: [] },
      result: { type: 'answer', text: 'Done.', limitations: [] },
      citations: [],
    }
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v1/skills')) return Promise.resolve(response(activeSkills))
      if (url.endsWith('/conversations')) return Promise.resolve(response({ conversation_id: 'conversation-keyboard', space_id: 'space-1', owner_id: 'local' }))
      return Promise.resolve(response(completed, url.endsWith('/questions') ? 202 : 200))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-keyboard' })

    renderWorkspace()
    const textarea = screen.getByLabelText('问题')
    fireEvent.change(textarea, { target: { value: 'Send this' } })
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/conversations\/conversation-keyboard\/skills\/knowledge_agent\/runs$/),
      expect.objectContaining({ method: 'POST' }),
    ))

    fireEvent.change(textarea, { target: { value: 'Keep this line' } })
    const ctrlEnter = createEvent.keyDown(textarea, { key: 'Enter', code: 'Enter', ctrlKey: true })
    fireEvent(textarea, ctrlEnter)
    expect(ctrlEnter.defaultPrevented).toBe(true)
    expect(textarea).toHaveValue('Keep this line\n')
  })
})
