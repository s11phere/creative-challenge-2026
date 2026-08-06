import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QAWorkspace } from './QAWorkspace'
import type { ConversationHistory, QARun } from './qa'

const conversation = {
  conversation_id: 'conversation-1',
  space_id: 'space-1',
  owner_id: 'local',
  created_at: '2026-08-06T10:00:00Z',
  updated_at: '2026-08-06T10:01:00Z',
}

const commands = [
  { name: 'help', aliases: [], kind: 'base', description: 'Show commands', argument_hint: '', input_mode: 'none' },
  { name: 'summarize', aliases: ['summary'], kind: 'skill', description: 'Summarize one document', argument_hint: '<document>', input_mode: 'document' },
]

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function assistantRun(overrides: Record<string, unknown> = {}) {
  return {
    run_id: 'run-1',
    user_message_id: 'message-1',
    status: 'completed',
    run_kind: 'assistant_turn',
    error_code: null,
    selection: { source: 'none', skill: null },
    model_identity: 'fake',
    assistant_message: { message_id: 'assistant-1', content: 'Direct response.' },
    clarification: null,
    usage: { input_tokens: 4, output_tokens: 2, total_tokens: 6, model_latency_ms: 12.5 },
    ...overrides,
  }
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  return render(
    <QueryClientProvider client={client}>
      <QAWorkspace selectedConversationId="conversation-1" />
    </QueryClientProvider>,
  )
}

function baseFetch(
  history: ConversationHistory = { conversations: [{ ...conversation, messages: [], runs: [] }] },
) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
    if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) return Promise.resolve(response(history))
    if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
    return Promise.resolve(response({}))
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('assistant conversation workspace', () => {
  it('opens an accessible command listbox and inserts the selected command', async () => {
    vi.stubGlobal('fetch', baseFetch())
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: '/su' } })

    expect(await screen.findByRole('listbox', { name: '可用指令' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /\/summarize/ })).toBeInTheDocument()
    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })

    expect(composer).toHaveValue('/summarize ')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'LLM Agent' })).not.toBeInTheDocument()
  })

  it('submits a generic v2 turn and renders its durable Run', async () => {
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) return Promise.resolve(response(assistantRun(), 202))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'idempotency-1' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: 'Continue this conversation.' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('Continue this conversation.')).toBeInTheDocument()
    expect(await screen.findByText('已完成')).toBeInTheDocument()
    expect(screen.getByText('Direct response.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v2\/conversations\/conversation-1\/turns$/),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ content: 'Continue this conversation.', idempotency_key: 'idempotency-1' }),
      }),
    )
  })

  it('continues a resource clarification on its existing Run', async () => {
    const waiting = assistantRun({
      status: 'waiting_clarification',
      run_kind: 'skill',
      assistant_message: null,
      clarification: {
        clarification_id: 'clarify-1',
        kind: 'resource_ambiguous',
        message: 'Choose a resource.',
        resource_candidates: [{
          candidate_id: 'candidate-1', resource_type: 'document', label: 'Architecture notes',
          source_label: 'notes.md', version_label: 'published',
        }],
      },
    })
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [{ message_id: 'message-1', role: 'user', content: '/summarize Architecture', run_id: null, created_at: '2026-08-06T10:01:00Z' }],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [{ message_id: 'message-1', role: 'user', content: '/summarize Architecture', run_id: null, created_at: '2026-08-06T10:01:00Z' }], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [waiting] }))
      if (url.endsWith('/clarifications/clarify-1') && init?.method === 'POST') return Promise.resolve(response(assistantRun({ run_kind: 'skill', assistant_message: null }), 202))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    fireEvent.click(await screen.findByRole('button', { name: /Architecture notes/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v2\/runs\/run-1\/clarifications\/clarify-1$/),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ candidate_id: 'candidate-1' }) }),
    ))
  })

  it('shows the evidence sidebar only for a grounded Run with citations', async () => {
    const grounded = assistantRun({
      run_kind: 'grounded_qa',
      assistant_message: null,
      selection: { source: 'command', skill: { name: 'knowledge_qa', version: '0.2.0', content_sha256: 'a'.repeat(64) } },
    })
    const qaRun: QARun = {
      run_id: 'run-1', attempt_id: 'attempt-1', status: 'completed', conversation_id: 'conversation-1', question_message_id: 'message-1', cancellation_requested: false, error_code: null,
      skill: { name: 'knowledge_qa', version: '0.2.0', content_sha256: 'a'.repeat(64) }, fixed_scope: { source_ids: [], document_ids: [], version_ids: [] },
      result: { type: 'answer', text: 'Grounded response.', limitations: [] },
      citations: [{ evidence_id: 'evidence-1', source_id: 'source-1', document_id: 'document-1', version_id: 'version-1', chunk_id: 'chunk-1', locator: { kind: 'lines', start: 1, end: 2 } }],
    }
    const fetchMock = baseFetch({
      conversations: [{ ...conversation, messages: [{ message_id: 'message-1', role: 'user', content: '/ask Question', run_id: null, created_at: '2026-08-06T10:01:00Z' }], runs: [qaRun] }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) return Promise.resolve(response({ conversations: [{ ...conversation, messages: [{ message_id: 'message-1', role: 'user', content: '/ask Question', run_id: null, created_at: '2026-08-06T10:01:00Z' }], runs: [qaRun] }] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [grounded] }))
      if (url.endsWith('/api/v1/qa/runs/run-1')) return Promise.resolve(response(qaRun))
      if (url.endsWith('/sources/source-1/detail')) return Promise.resolve(response({ source: { uri: 'fixture://notes' }, documents: [{ id: 'document-1', display_name: 'notes.md' }] }))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    expect(await screen.findByRole('heading', { name: '引用证据' })).toBeInTheDocument()
    expect(screen.getByText('Grounded response.')).toBeInTheDocument()
  })
})
