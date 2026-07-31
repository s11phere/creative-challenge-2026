import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  { name: 'knowledge_qa', active_version: '0.1.0', versions: ['0.1.0'] },
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
      expect.stringContaining('/api/v1/qa/runs/run-1/cancel'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('keeps evidence empty instead of inventing citations', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(response(activeSkills))))
    renderWorkspace()

    expect(await screen.findByText('knowledge_qa')).toBeInTheDocument()
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
        name: 'knowledge_qa',
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
    expect(screen.getByText('00000000 / 00000000')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /查看原文/ }))
    expect(await screen.findByText('The exact source lines.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/qa/runs/run-1/citations/evidence-1'),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
  })
})
