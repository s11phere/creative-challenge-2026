import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SkillsPanel } from './SkillsPanel'

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const builtinSkill = {
  name: 'knowledge_agent', version: '1.0.0', content_sha256: 'a'.repeat(64),
  description: 'Grounded knowledge requests.', permissions: ['read_knowledge'],
  required_capabilities: ['fast_chat'],
  budget: { max_steps: 32, max_tool_calls: 24, max_input_tokens: 100, max_output_tokens: 50, timeout_seconds: 30 },
}

const personalSkill = {
  name: 'my_skill', version: '1.0.0', content_sha256: 'b'.repeat(64),
  description: 'Personal Skill.', permissions: ['read_knowledge'],
  required_capabilities: [], active: false,
  budget: { max_steps: 4, max_tool_calls: 2, max_input_tokens: 100, max_output_tokens: 100, timeout_seconds: 30 },
  invocation: null,
}

const draft = {
  name: 'my_draft', description: 'Draft Skill.', complete: true, valid: true, file_count: 6,
  files: ['skill.yaml', 'workflow.yaml'],
}

const draftEval = {
  skill_name: 'my_draft', skill_version: '1.0.0', gate_passed: true,
  metrics: { total: 1, passed: 1, failed: 0, inconclusive: 0, errored: 0, pass_rate: 1, check_pass_rate: 1 },
}

const suggestion = {
  name: 'summarize_workflow', category: 'summarize', frequency: 5,
  last_seen_at: '2026-08-12T00:00:00+00:00',
  description: '你最近常做「summarize」类任务（5 次），可以固化成个人 Skill。',
  hint: '固化该模式为一个个人 Skill。',
}

type FetchState = {
  personal?: unknown[]
  builtin?: unknown[]
  drafts?: unknown[]
  suggestions?: unknown[]
  draftFiles?: Record<string, string>
  evidence?: unknown
}

function routedFetch(state: FetchState = {}) {
  const { personal = [], builtin = [builtinSkill], drafts = [], suggestions = [], draftFiles = {}, evidence = null } = state
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    if (url.includes('/api/v1/skills/personal/drafts/suggestions')) {
      return Promise.resolve(response(suggestions))
    }
    if (url.includes('/api/v1/skills/personal/drafts')) {
      if (method === 'GET' && url.endsWith('/evidence')) {
        return Promise.resolve(response({ name: 'my_draft', evidence }))
      }
      if (method === 'POST' && url.endsWith('/eval')) {
        return Promise.resolve(response(draftEval))
      }
      if (method === 'POST' && url.endsWith('/activate')) {
        return Promise.resolve(response({ name: 'my_draft', active: true, description: 'Draft Skill.' }))
      }
      if (method === 'DELETE') {
        return Promise.resolve(response({ status: 'rejected' }))
      }
      if (method === 'GET' && url.endsWith('/files')) {
        return Promise.resolve(response({ name: 'my_draft', files: draftFiles }))
      }
      if (method === 'POST') {
        return Promise.resolve(response(draft, 201))
      }
      return Promise.resolve(response(drafts))
    }
    if (url.includes('/api/v1/skills/personal')) {
      return Promise.resolve(response(personal))
    }
    return Promise.resolve(response(builtin))
  })
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SkillsPanel />
    </QueryClientProvider>,
  )
}

afterEach(() => vi.unstubAllGlobals())

it('loads the fixed installed Skill set without version-management controls', async () => {
  const fetchMock = routedFetch()
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()

  const summary = await screen.findByRole('button', { name: /knowledge_agent/ })
  expect(screen.queryByText('Grounded knowledge requests.')).not.toBeInTheDocument()
  fireEvent.click(summary)
  expect(screen.getByText('Grounded knowledge requests.')).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/api/v1/skills'),
    expect.objectContaining({ headers: { 'Content-Type': 'application/json' } }),
  )
})

it('renders personal Skills with activate and delete actions', async () => {
  vi.stubGlobal('fetch', routedFetch({ personal: [personalSkill] }))

  renderPanel()

  expect(await screen.findByText('个人 Skill')).toBeInTheDocument()
  expect(await screen.findByText('my_skill')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /激活 my_skill/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /删除 my_skill/ })).toBeInTheDocument()
})

it('shows the personal Skill create form with a package JSON editor', async () => {
  vi.stubGlobal('fetch', routedFetch())

  renderPanel()

  const createButton = await screen.findByRole('button', { name: /新建个人 Skill/ })
  fireEvent.click(createButton)
  expect(screen.getByPlaceholderText('my_skill')).toBeInTheDocument()
  expect(screen.getByPlaceholderText(/"skill\.yaml"/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '创建个人 Skill' })).toBeInTheDocument()
})

it('renders drafts with eval, activate, edit and reject actions', async () => {
  vi.stubGlobal('fetch', routedFetch({ drafts: [draft] }))

  renderPanel()

  expect(await screen.findByText('草稿')).toBeInTheDocument()
  expect(await screen.findByText('my_draft')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /运行 eval my_draft/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /激活 my_draft/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /编辑草稿 my_draft/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /拒绝草稿 my_draft/ })).toBeInTheDocument()
})

it('runs the deterministic eval and shows the gate result', async () => {
  vi.stubGlobal('fetch', routedFetch({ drafts: [draft] }))

  renderPanel()

  const evalButton = await screen.findByRole('button', { name: /运行 eval my_draft/ })
  fireEvent.click(evalButton)
  expect(await screen.findByText('门禁通过 1/1')).toBeInTheDocument()
})

it('shows a suggestion only from usage evidence and scaffolds a draft on click', async () => {
  vi.stubGlobal('fetch', routedFetch({ suggestions: [suggestion] }))

  renderPanel()

  expect(await screen.findByText('个性化建议')).toBeInTheDocument()
  expect(await screen.findByText('summarize_workflow')).toBeInTheDocument()
  expect(screen.getByText(/可以固化成个人 Skill/)).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: /从建议创建草稿 summarize_workflow/ }))
  expect(await screen.findByDisplayValue('summarize_workflow')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '创建草稿' })).toBeInTheDocument()
  // The editor is prefilled with a valid scaffold the user can submit.
  expect(screen.getByPlaceholderText(/"skill\.yaml"/)).toBeInTheDocument()
})

it('shows pattern-extraction evidence on candidate drafts', async () => {
  const evidence = {
    pattern: 'skill=none|category=summarize|tools=knowledge_search,grounded_answer|input=zh',
    task_category: 'summarize',
    tool_sequence: 'knowledge_search,grounded_answer',
    frequency: 3,
    distinct_conversations: 3,
    first_seen_at: '2026-08-01T00:00:00+00:00',
    last_seen_at: '2026-08-12T00:00:00+00:00',
    exemplars: [{
      run_id: 'a'.repeat(32),
      conversation_id: 'b'.repeat(32),
      input_summary: '请总结这篇文档',
      created_at: '2026-08-01T00:00:00+00:00',
    }],
  }
  vi.stubGlobal('fetch', routedFetch({ drafts: [draft], evidence }))

  renderPanel()

  expect(await screen.findByText('候选模式 · 3 次 / 3 会话 · 1 个来源 run')).toBeInTheDocument()
})
