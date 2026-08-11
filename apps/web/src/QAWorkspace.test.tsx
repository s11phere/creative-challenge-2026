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
  { name: 'effort', aliases: [], kind: 'base', description: 'Set reasoning effort', argument_hint: '[low|medium|high|xhigh|max]', input_mode: 'optional_effort' },
  { name: 'help', aliases: [], kind: 'base', description: 'Show active Skills and commands', argument_hint: '', input_mode: 'none' },
  { name: 'summarize', aliases: ['summary'], kind: 'skill', description: 'Summarize one document', argument_hint: '<document>', input_mode: 'document' },
]

const commandsWithAsk = [
  ...commands,
  { name: 'ask', aliases: [], kind: 'skill', description: 'Answer using the workspace knowledge base', argument_hint: '<question>', input_mode: 'question' },
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

  it('submits a Skill command with its full original content when Enter is pressed', async () => {
    const submittedBodies: unknown[] = []
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands: commandsWithAsk }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) {
        submittedBodies.push(JSON.parse(String(init?.body)))
        return Promise.resolve(response({
          command: 'ask',
          status: 'completed',
          content: null,
          conversation_id: 'conversation-1',
          run: assistantRun({
            run_kind: 'skill',
            selection: { source: 'command', skill: { name: 'knowledge_agent', version: '0.3.0', content_sha256: 'a'.repeat(64) } },
          }),
          commands: [],
        }, 202))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'ask-idempotency-1' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: '/ask Explain the architecture.' } })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })

    await waitFor(() => expect(submittedBodies).toEqual([{
      content: '/ask Explain the architecture.',
      idempotency_key: 'ask-idempotency-1',
    }]))
    const displayedCommand = await screen.findByText('/ask')
    expect(displayedCommand).toHaveClass('chat-command-token')
    expect(displayedCommand.parentElement).toHaveTextContent('/ask Explain the architecture.')
    expect(document.querySelector('.chat-command-notice')).not.toBeInTheDocument()
  })

  it('highlights only a valid command prefix in the composer', async () => {
    vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands: commandsWithAsk }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      return Promise.resolve(response({}))
    })
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: '/askx question' } })
    expect(document.querySelector('.chat-composer-highlight .chat-command-token')).not.toBeInTheDocument()

    fireEvent.change(composer, { target: { value: '/ask question' } })
    expect(document.querySelector('.chat-composer-highlight .chat-command-token')).toHaveTextContent('/ask')
  })

  it('renders Markdown and LaTeX in an Assistant answer', async () => {
    const markdown = '# Answer\n\n- first item\n\n| key | value |\n| --- | --- |\n| x | 1 |\n\n```ts\nconst x = 1\n```\n\nInline $x^2$. '
    const completed = assistantRun({ assistant_message: { message_id: 'assistant-1', content: markdown } })
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [
          { message_id: 'message-1', role: 'user', content: 'Show formatted output.', run_id: null, created_at: '2026-08-06T10:00:00Z' },
          { message_id: 'assistant-1', role: 'assistant', content: markdown, run_id: 'run-1', created_at: '2026-08-06T10:01:00Z' },
        ],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) return Promise.resolve(response({ conversations: [{ ...conversation, messages: [{ message_id: 'message-1', role: 'user', content: 'Show formatted output.', run_id: null, created_at: '2026-08-06T10:00:00Z' }, { message_id: 'assistant-1', role: 'assistant', content: markdown, run_id: 'run-1', created_at: '2026-08-06T10:01:00Z' }], runs: [] }] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [completed] }))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    expect(await screen.findByText('Answer')).toBeInTheDocument()
    expect(document.querySelector('.qa-markdown h1')).toHaveTextContent('Answer')
    expect(document.querySelector('.qa-markdown li')).toHaveTextContent('first item')
    expect(document.querySelector('.qa-markdown table')).toBeInTheDocument()
    expect(document.querySelector('.qa-markdown pre code')).toHaveTextContent('const x = 1')
    expect(document.querySelector('.katex')).toBeInTheDocument()
  })

  it('renders a persisted Run answer only below its user message', async () => {
    const completed = assistantRun({
      run_kind: 'grounded_qa',
      assistant_message: { message_id: 'assistant-1', content: 'One grounded answer.' },
      selection: { source: 'auto', skill: { name: 'knowledge_agent', version: '1.0.0', content_sha256: 'a'.repeat(64) } },
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
      selection: { source: 'auto', skill: { name: 'knowledge_agent', version: '1.0.0', content_sha256: 'a'.repeat(64) } },
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

  it('opens one temporary effort picker and leaves only the confirmed result', async () => {
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) {
        expect(JSON.parse(String(init?.body)).content).toBe('/effort medium')
        return Promise.resolve(response({
          command: 'effort',
          status: 'completed',
          content: 'Model: fake-reasoner | reasoning effort: medium.',
          conversation_id: 'conversation-1',
          run: null,
          commands: [],
        }, 202))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'effort-idempotency-1' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox', { name: '消息' })
    fireEvent.change(composer, { target: { value: '/effort' } })
    fireEvent.keyDown(composer, { key: 'Enter' })

    expect(await screen.findByRole('listbox', { name: 'Reasoning effort options' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'medium (default)' })).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(5)
    expect(screen.queryByRole('option', { name: 'auto' })).not.toBeInTheDocument()
    const mediumOption = screen.getByRole('option', { name: 'medium (default)' })
    fireEvent.click(mediumOption)
    fireEvent.click(mediumOption)
    await waitFor(() => expect(screen.getByText('Model: fake-reasoner | reasoning effort: medium.')).toBeInTheDocument())
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes('/api/v2/conversations/conversation-1/turns'))).toHaveLength(1)
    expect(screen.queryByRole('listbox', { name: 'Reasoning effort options' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'medium (default)' })).not.toBeInTheDocument()
  })

  it('uses left and right arrows plus Enter to confirm the effort picker', async () => {
    const fetchMock = baseFetch()
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{ ...conversation, messages: [], runs: [] }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) {
        expect(JSON.parse(String(init?.body)).content).toBe('/effort high')
        return Promise.resolve(response({
          command: 'effort',
          status: 'completed',
          content: 'Model: fake-reasoner | reasoning effort: high.',
          conversation_id: 'conversation-1',
          run: null,
          commands: [],
        }, 202))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'effort-enter-idempotency-1' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox')
    fireEvent.change(composer, { target: { value: '/effort' } })
    fireEvent.submit(composer.closest('form')!)
    const picker = await screen.findByRole('listbox', { name: 'Reasoning effort options' })
    fireEvent.keyDown(picker, { key: 'ArrowRight' })
    expect(screen.getByRole('option', { name: 'high' })).toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(picker, { key: 'Enter' })

    await waitFor(() => expect(screen.getByText('Model: fake-reasoner | reasoning effort: high.')).toBeInTheDocument())
    expect(screen.queryByRole('listbox', { name: 'Reasoning effort options' })).not.toBeInTheDocument()
  })

  it('recovers automatic paged v3 history into a collapsed Agent timeline before the final answer', async () => {
    const completed = assistantRun({
      run_kind: 'assistant_turn',
      selection: { source: 'auto', skill: null },
      assistant_message: { message_id: 'assistant-1', content: 'Architecture answer.' },
      usage: { input_tokens: 120, output_tokens: 48, total_tokens: 168, model_latency_ms: 1240 },
    })
    const events = [
      { schema_version: 'agent-run-sse-v3', event_id: 'event-1', run_id: 'run-1', sequence: 1, occurred_at: '2026-08-09T10:00:01Z', event_type: 'accepted', payload: { status: 'accepted', requested_effort: 'high', effective_effort: 'medium', model: 'fake-reasoner' } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-2', run_id: 'run-1', sequence: 2, occurred_at: '2026-08-09T10:00:02Z', event_type: 'iteration_started', payload: { status: 'planning', iteration: 1, tool_call_count: 0, observation_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-3', run_id: 'run-1', sequence: 3, occurred_at: '2026-08-09T10:00:03Z', event_type: 'tool_requested', payload: { status: 'requested', iteration: 1, tool_name: 'knowledge_search', tool_version: '1.0.0', input_summary: 'sha256:input-1', query_preview: 'How is the architecture indexed?', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-4', run_id: 'run-1', sequence: 4, occurred_at: '2026-08-09T10:00:04Z', event_type: 'tool_output', payload: { status: 'succeeded', iteration: 1, tool_name: 'knowledge_search', tool_version: '1.0.0', input_summary: 'sha256:input-1', query_preview: 'How is the architecture indexed?', output_summary: 'sha256:output-1', retry_count: 1, duration_ms: 126 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-5', run_id: 'run-1', sequence: 5, occurred_at: '2026-08-09T10:00:05Z', event_type: 'iteration_started', payload: { status: 'planning', iteration: 2, tool_call_count: 1, observation_count: 1 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-6', run_id: 'run-1', sequence: 6, occurred_at: '2026-08-09T10:00:06Z', event_type: 'tool_requested', payload: { status: 'requested', iteration: 2, tool_name: 'write_file', tool_version: '1.0.0', input_summary: 'sha256:input-2', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-7', run_id: 'run-1', sequence: 7, occurred_at: '2026-08-09T10:00:07Z', event_type: 'approval_required', payload: { status: 'waiting_approval', iteration: 2, tool_name: 'write_file', tool_version: '1.0.0', input_summary: 'sha256:input-2', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-8', run_id: 'run-1', sequence: 8, occurred_at: '2026-08-09T10:00:08Z', event_type: 'iteration_started', payload: { status: 'planning', iteration: 3, tool_call_count: 2, observation_count: 1 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-9', run_id: 'run-1', sequence: 9, occurred_at: '2026-08-09T10:00:09Z', event_type: 'tool_requested', payload: { status: 'requested', iteration: 3, tool_name: 'shell_exec', tool_version: '1.0.0', input_summary: 'sha256:input-3', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-10', run_id: 'run-1', sequence: 10, occurred_at: '2026-08-09T10:00:10Z', event_type: 'approval_required', payload: { status: 'waiting_approval', iteration: 3, tool_name: 'shell_exec', tool_version: '1.0.0', input_summary: 'sha256:input-3', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-11', run_id: 'run-1', sequence: 11, occurred_at: '2026-08-09T10:00:11Z', event_type: 'tool_started', payload: { status: 'running', iteration: 3, tool_name: 'shell_exec', tool_version: '1.0.0', input_summary: 'sha256:input-3', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-12', run_id: 'run-1', sequence: 12, occurred_at: '2026-08-09T10:00:12Z', event_type: 'iteration_started', payload: { status: 'planning', iteration: 4, tool_call_count: 3, observation_count: 1 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-13', run_id: 'run-1', sequence: 13, occurred_at: '2026-08-09T10:00:13Z', event_type: 'tool_requested', payload: { status: 'requested', iteration: 4, tool_name: 'write_file', tool_version: '1.0.0', input_summary: 'sha256:input-4', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-14', run_id: 'run-1', sequence: 14, occurred_at: '2026-08-09T10:00:14Z', event_type: 'tool_output', payload: { status: 'failed', iteration: 4, tool_name: 'write_file', tool_version: '1.0.0', input_summary: 'sha256:input-4', output_summary: 'sha256:unavailable', error_code: 'APPROVAL_REJECTED', retry_count: 0, duration_ms: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-15', run_id: 'run-1', sequence: 15, occurred_at: '2026-08-09T10:00:15Z', event_type: 'finalizing', payload: { status: 'finalizing', iteration: 4, stop_reason: 'goal_complete', goal_complete: true, evidence_sufficient: true, has_conflict: false, publication_id: 'publication-1' } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-16', run_id: 'run-1', sequence: 16, occurred_at: '2026-08-09T10:00:16Z', event_type: 'completed', payload: { status: 'completed', iteration: 4, stop_reason: 'goal_complete', publication_id: 'publication-1' } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-17', run_id: 'run-1', sequence: 17, occurred_at: '2026-08-09T10:00:17Z', event_type: 'skill_activated', payload: { status: 'activated', iteration: 0, skill_name: 'assistant_agent', skill_version: '0.1.0' } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-18', run_id: 'run-1', sequence: 18, occurred_at: '2026-08-09T10:00:18Z', event_type: 'tool_requested', payload: { status: 'requested', iteration: 5, tool_name: 'summarize_document', tool_version: '1.1.0', input_summary: 'sha256:input-5', resource_reference: 'CLAUDE.md', retry_count: 0 } },
      { schema_version: 'agent-run-sse-v3', event_id: 'event-19', run_id: 'run-1', sequence: 19, occurred_at: '2026-08-09T10:00:19Z', event_type: 'tool_output', payload: { status: 'succeeded', iteration: 5, tool_name: 'summarize_document', tool_version: '1.1.0', input_summary: 'sha256:input-5', resource_reference: 'CLAUDE.md', output_summary: 'sha256:output-5', retry_count: 0, duration_ms: 126 } },
    ]
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [
          { message_id: 'message-1', role: 'user', content: 'Explain the architecture.', run_id: null, created_at: '2026-08-09T10:00:00Z' },
          { message_id: 'assistant-1', role: 'assistant', content: 'Architecture answer.', run_id: 'run-1', created_at: '2026-08-09T10:01:00Z' },
        ],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) return Promise.resolve(response({ conversations: [{ ...conversation, messages: [{ message_id: 'message-1', role: 'user', content: 'Explain the architecture.', run_id: null, created_at: '2026-08-09T10:00:00Z' }, { message_id: 'assistant-1', role: 'assistant', content: 'Architecture answer.', run_id: 'run-1', created_at: '2026-08-09T10:01:00Z' }], runs: [] }] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [completed] }))
      if (url.includes('/api/v3/runs/run-1/events?after_sequence=0')) return Promise.resolve(response({ schema_version: 'agent-run-event-page-v1', events: events.slice(0, 8), next_sequence: 8, has_more: true }))
      if (url.includes('/api/v3/runs/run-1/events?after_sequence=8')) return Promise.resolve(response({ schema_version: 'agent-run-event-page-v1', events: events.slice(8), next_sequence: 19, has_more: false }))
      if (url.endsWith('/api/v2/runs/run-1/events')) return Promise.resolve(eventStream([]))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    const timeline = await screen.findByLabelText('Agent 运行时间线')
    await waitFor(() => expect(screen.getByText('请求强度')).toBeInTheDocument())
    expect(screen.getByText('实际强度')).toBeInTheDocument()
    expect(screen.getByText('实际 Token')).toBeInTheDocument()
    expect(screen.getByText('目标已完成')).toBeInTheDocument()
    expect(screen.getByText('等待审批')).toBeInTheDocument()
    expect(screen.getByText('审批已通过，执行中')).toBeInTheDocument()
    expect(screen.getByText('审批已拒绝，无副作用')).toBeInTheDocument()
    const retrievalTool = screen.getByText('knowledge_search').closest('details')
    if (!retrievalTool) throw new Error('retrieval Tool card not rendered')
    expect(retrievalTool).not.toHaveAttribute('open')
    expect(retrievalTool).toHaveAttribute('data-kind', 'retrieval')
    expect(retrievalTool.querySelector('.chat-agent-tool-details')).not.toBeVisible()
    fireEvent.click(screen.getByText('knowledge_search'))
    expect(retrievalTool.querySelector('.chat-agent-tool-details')).toBeVisible()
    expect(screen.getByText('检索问题')).toBeInTheDocument()
    expect(screen.getByText('How is the architecture indexed?')).toBeInTheDocument()
    expect(screen.getByText(/Skill 已激活/)).toBeInTheDocument()
    const summaryTool = screen.getByText('summarize_document').closest('details')
    if (!summaryTool) throw new Error('summary Tool card not rendered')
    fireEvent.click(screen.getByText('summarize_document'))
    expect(screen.getByText('目标文档')).toBeInTheDocument()
    expect(screen.getByText('CLAUDE.md')).toBeInTheDocument()
    const answer = screen.getByText('Architecture answer.')
    expect(timeline.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  })

  it('reconnects an active Agent Run stream from its durable event cursor', async () => {
    const active = assistantRun({
      status: 'running',
      run_kind: 'assistant_turn',
      assistant_message: null,
      selection: { source: 'auto', skill: null },
    })
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [{ message_id: 'message-1', role: 'user', content: 'Inspect sources.', run_id: null, created_at: '2026-08-09T10:00:00Z' }],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) return Promise.resolve(response({ conversations: [{ ...conversation, messages: [{ message_id: 'message-1', role: 'user', content: 'Inspect sources.', run_id: null, created_at: '2026-08-09T10:00:00Z' }], runs: [] }] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [active] }))
      if (url.includes('/api/v3/runs/run-1/events?after_sequence=0')) return Promise.resolve(response({ schema_version: 'agent-run-event-page-v1', events: [{ schema_version: 'agent-run-sse-v3', event_id: 'event-1', run_id: 'run-1', sequence: 1, occurred_at: '2026-08-09T10:00:01Z', event_type: 'accepted', payload: { status: 'accepted', requested_effort: 'low', effective_effort: 'low', model: 'fake-reasoner' } }], next_sequence: 1, has_more: false }))
      if (url.endsWith('/api/v3/runs/run-1/events/stream')) return Promise.resolve(new Response('id: 2\nevent: iteration_started\ndata: {"schema_version":"agent-run-sse-v3","event_id":"event-2","run_id":"run-1","sequence":2,"occurred_at":"2026-08-09T10:00:02Z","event_type":"iteration_started","payload":{"status":"planning","iteration":1,"tool_call_count":0,"observation_count":0}}\n\n', { headers: { 'Content-Type': 'text/event-stream', 'X-Agent-Event-Has-More': 'false' } }))
      if (url.endsWith('/api/v2/runs/run-1/events')) return Promise.resolve(eventStream([]))
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWorkspace()

    expect(await screen.findByText('第 1 轮')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v3\/runs\/run-1\/events\/stream$/),
      expect.objectContaining({ headers: expect.objectContaining({ 'Last-Event-ID': '1' }) }),
    ))
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
          commands: [commands[2]],
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

  it('keeps command notices in chronological order and scrolls to new timeline items', async () => {
    const fetchMock = baseFetch({
      conversations: [{
        ...conversation,
        messages: [{ message_id: 'message-1', role: 'user', content: 'Earlier question.', run_id: null, created_at: '2026-08-06T10:00:00Z' }],
        runs: [],
      }],
    })
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v2/commands')) return Promise.resolve(response({ commands }))
      if (url.includes('/api/v1/spaces/') && url.includes('/conversations?')) {
        return Promise.resolve(response({ conversations: [{
          ...conversation,
          messages: [{ message_id: 'message-1', role: 'user', content: 'Earlier question.', run_id: null, created_at: '2026-08-06T10:00:00Z' }],
          runs: [],
        }] }))
      }
      if (url.endsWith('/api/v2/conversations/conversation-1/runs')) return Promise.resolve(response({ runs: [] }))
      if (url.endsWith('/api/v2/conversations/conversation-1/turns')) {
        const content = JSON.parse(String(init?.body)).content
        if (content !== '/skills') {
          return Promise.resolve(response(assistantRun({
            run_id: 'run-2',
            user_message_id: 'message-2',
            assistant_message: { message_id: 'assistant-2', content: 'Later answer.' },
          }), 202))
        }
        return Promise.resolve(response({
          command: 'skills',
          status: 'completed',
          content: 'Current active Skills.',
          conversation_id: 'conversation-1',
          run: null,
          commands: [],
        }, 202))
      }
      return Promise.resolve(response({}))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: () => 'command-order-idempotency-1' })
    renderWorkspace()

    const composer = await screen.findByRole('combobox')
    const thread = document.querySelector('.chat-thread')
    if (!thread) throw new Error('thread not rendered')
    Object.defineProperty(thread, 'scrollHeight', { configurable: true, value: 777 })

    fireEvent.change(composer, { target: { value: '/skills' } })
    fireEvent.submit(composer.closest('form')!)
    await waitFor(() => expect(screen.getByText('Current active Skills.')).toBeInTheDocument())
    await waitFor(() => expect(thread.scrollTop).toBe(777))
    expect(thread.lastElementChild).toHaveClass('chat-command-notice')

    fireEvent.change(composer, { target: { value: 'Later question.' } })
    fireEvent.submit(composer.closest('form')!)
    await screen.findByText('Later question.')
    const timelineItems = [...thread.children]
    const commandNoticeIndex = timelineItems.findIndex((item) => item.classList.contains('chat-command-notice'))
    const laterMessageIndex = timelineItems.findIndex((item) => item.textContent?.includes('Later question.'))
    expect(document.querySelectorAll('.chat-command-notice')).toHaveLength(1)
    expect(commandNoticeIndex).toBeGreaterThanOrEqual(0)
    expect(laterMessageIndex).toBeGreaterThan(commandNoticeIndex)
    expect(thread.lastElementChild).toBe(timelineItems[laterMessageIndex])
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
      selection: { source: 'command', skill: { name: 'knowledge_agent', version: '1.0.0', content_sha256: 'a'.repeat(64) } },
    })
    const qaRun: QARun = {
      run_id: 'run-1', attempt_id: 'attempt-1', status: 'completed', conversation_id: 'conversation-1', question_message_id: 'message-1', cancellation_requested: false, error_code: null,
      skill: { name: 'knowledge_agent', version: '1.0.0', content_sha256: 'a'.repeat(64) }, fixed_scope: { source_ids: [], document_ids: [], version_ids: [] },
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
