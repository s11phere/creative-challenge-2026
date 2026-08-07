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
  { name: 'help', aliases: [], kind: 'base', description: 'Show active Skills and commands', argument_hint: '', input_mode: 'none' },
  { name: 'summarize', aliases: ['summary'], kind: 'skill', description: 'Summarize one document', argument_hint: '<document>', input_mode: 'document' },
]

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function eventStream(events: unknown[]): Response {
  return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''), {
    headers: { 'Content-Type': 'text/event-stream' },
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

function renderWorkspace(apiMode: 'v1' | 'v2' = 'v2') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  return render(
    <QueryClientProvider client={client}>
      <QAWorkspace selectedConversationId="conversation-1" apiMode={apiMode} />
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

  it('prefers command-name prefixes over description matches', async () => {
    vi.stubGlobal('fetch', baseFetch())
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: '/s' } })

    expect(await screen.findByRole('option', { name: /\/summarize/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /\/help/ })).not.toBeInTheDocument()
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
    expect(screen.getByText('Continue this conversation.').closest('.chat-message-group')).toHaveClass(
      'chat-message-group-user',
    )
    expect(screen.queryByText('你')).not.toBeInTheDocument()
    expect(screen.queryByText('助手')).not.toBeInTheDocument()
  })

  it('renders a persisted Run answer only below its user message', async () => {
    const completed = assistantRun({
      run_kind: 'grounded_qa',
      assistant_message: { message_id: 'assistant-1', content: 'One grounded answer.' },
      selection: { source: 'auto', skill: { name: 'knowledge_qa', version: '0.2.0', content_sha256: 'a'.repeat(64) } },
    })
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [
          { message_id: 'message-1', role: 'user', content: 'Question.', run_id: null, created_at: '2026-08-06T10:00:00Z' },
          { message_id: 'assistant-1', role: 'assistant', content: 'One grounded answer.', run_id: 'run-1', created_at: '2026-08-06T10:01:00Z' },
        ],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{
          ...conversation,
          messages: [
            { message_id: 'message-1', role: 'user', content: 'Question.', run_id: null, created_at: '2026-08-06T10:00:00Z' },
            { message_id: 'assistant-1', role: 'assistant', content: 'One grounded answer.', run_id: 'run-1', created_at: '2026-08-06T10:01:00Z' },
          ],
          runs: [],
        }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [completed] }))
      if (url.endsWith('/api/v1/qa/runs/run-1')) return Promise.resolve(response({ status: 'completed', result: { type: 'answer', text: 'One grounded answer.', limitations: [] } }))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    expect(await screen.findByText('One grounded answer.')).toBeInTheDocument()
    expect(screen.getAllByText('One grounded answer.')).toHaveLength(1)
    expect(screen.getByText('Question.').closest('.chat-message-group')).toHaveClass('chat-message-group-user')
  })

  it('keeps a collapsed Skill invocation card with its persisted activity and final answer', async () => {
    const completed = assistantRun({
      run_kind: 'skill',
      selection: { source: 'auto', skill: { name: 'knowledge_agent', version: '0.2.0', content_sha256: 'a'.repeat(64) } },
      assistant_message: { message_id: 'assistant-1', content: 'Architecture answer.' },
    })
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [
          { message_id: 'message-1', role: 'user', content: 'Explain the architecture.', run_id: null, created_at: '2026-08-06T10:00:00Z' },
          { message_id: 'assistant-1', role: 'assistant', content: 'Architecture answer.', run_id: 'run-1', created_at: '2026-08-06T10:01:00Z' },
        ],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{
          ...conversation,
          messages: [
            { message_id: 'message-1', role: 'user', content: 'Explain the architecture.', run_id: null, created_at: '2026-08-06T10:00:00Z' },
            { message_id: 'assistant-1', role: 'assistant', content: 'Architecture answer.', run_id: 'run-1', created_at: '2026-08-06T10:01:00Z' },
          ],
          runs: [],
        }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [completed] }))
      if (url.endsWith('/api/v2/runs/run-1/events')) return Promise.resolve(eventStream([
        { schema_version: 'agent-run-sse-v2', event_id: 'event-1', run_id: 'run-1', sequence: 1, occurred_at: '2026-08-06T10:00:01Z', type: 'routing', payload: { status: 'running', action: 'invoke_skill' } },
        { schema_version: 'agent-run-sse-v2', event_id: 'event-2', run_id: 'run-1', sequence: 2, occurred_at: '2026-08-06T10:00:02Z', type: 'skill_started', payload: { status: 'running', skill: 'knowledge_agent' } },
        { schema_version: 'agent-run-sse-v2', event_id: 'event-3', run_id: 'run-1', sequence: 3, occurred_at: '2026-08-06T10:00:03Z', type: 'completed', payload: { status: 'completed', action: 'invoke_skill' } },
      ]))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    const title = await screen.findByText('Skill 调用 · knowledge_agent')
    const card = title.closest('details')
    if (!card) throw new Error('Skill invocation card not rendered')
    expect(card).not.toHaveAttribute('open')

    fireEvent.click(title)

    expect(card).toHaveAttribute('open')
    expect(await screen.findByText('Agent 已选择调用此 Skill')).toBeInTheDocument()
    expect(screen.getByText('开始执行 knowledge_agent')).toBeInTheDocument()
    expect(screen.getByText('调用已完成')).toBeInTheDocument()
    expect(screen.getByText('Architecture answer.')).toBeInTheDocument()
  })

  it('uses the refreshed API Run instead of an optimistic created snapshot', async () => {
    let submitted = false
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) {
        return Promise.resolve(response({ runs: submitted ? [assistantRun()] : [] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) {
        submitted = true
        return Promise.resolve(response(assistantRun({ status: 'created', assistant_message: null }), 202))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'authoritative-run-id' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: 'Show the completed status.' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('已完成')).toBeInTheDocument()
    expect(screen.queryByText('已创建')).not.toBeInTheDocument()
    expect(screen.getByText('Direct response.')).toBeInTheDocument()
  })

  it('renders the command details returned by a base command', async () => {
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) {
        return Promise.resolve(response({
          command: 'skills',
          status: 'completed',
          content: 'Current active Skills.',
          conversation_id: 'conversation-1',
          run: null,
          commands: [commands[1]],
        }, 202))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'command-idempotency-1' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox')
    fireEvent.change(composer, { target: { value: '/skills' } })
    const sendButton = composer.closest('form')?.querySelector('button[type="submit"]')
    if (!sendButton) throw new Error('send control not rendered')
    fireEvent.click(sendButton)

    expect(await screen.findByText('Current active Skills.')).toBeInTheDocument()
    expect(screen.getByText('/summarize')).toBeInTheDocument()
    expect(screen.getByText('Summarize one document')).toBeInTheDocument()
    expect(screen.getByText('<document>')).toBeInTheDocument()
  })

  it('uses v1 only when the compatibility mode is explicitly selected', async () => {
    const legacyRun: QARun = {
      run_id: 'legacy-run-1',
      attempt_id: 'legacy-attempt-1',
      status: 'queued',
      conversation_id: 'conversation-1',
      question_message_id: 'legacy-message-1',
      cancellation_requested: false,
      error_code: null,
      skill: { name: 'knowledge_qa', version: '0.1.0', content_sha256: null },
      fixed_scope: { source_ids: [], document_ids: [], version_ids: [] },
    }
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v1/conversations/conversation-1/questions') && init?.method === 'POST') {
        return Promise.resolve(response(legacyRun, 202))
      }
      if (url.endsWith('/api/v1/qa/runs/legacy-run-1')) return Promise.resolve(response(legacyRun))
      if (url.endsWith('/api/v1/qa/runs/legacy-run-1/cancel') && init?.method === 'POST') {
        return Promise.resolve(response({ ...legacyRun, status: 'cancelled', cancellation_requested: true }))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'legacy-idempotency-1' })
    const view = renderWorkspace('v1')

    const composer = await screen.findByRole('textbox', { name: '消息' })
    fireEvent.change(composer, { target: { value: 'Legacy question.' } })
    fireEvent.click(screen.getByRole('button', { name: '发送' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/conversations\/conversation-1\/questions$/),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ question: 'Legacy question.', idempotency_key: 'legacy-idempotency-1' }),
      }),
    ))
    const cancelButton = await waitFor(() => {
      const element = view.container.querySelector('button.qa-cancel-button')
      if (!element) throw new Error('cancel control not rendered')
      return element
    })
    fireEvent.click(cancelButton)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/qa\/runs\/legacy-run-1\/cancel$/),
      expect.objectContaining({ method: 'POST' }),
    ))
    const requestedUrls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(requestedUrls.some((url) => url.includes('/api/v2/commands'))).toBe(false)
    expect(requestedUrls.some((url) => url.includes('/api/v2/conversations/conversation-1/turns'))).toBe(false)
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
