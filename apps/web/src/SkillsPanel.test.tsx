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

function routedFetch(personal: unknown[], builtin: unknown[] = [builtinSkill]) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
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
  const fetchMock = routedFetch([])
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
  const fetchMock = routedFetch([personalSkill])
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()

  expect(await screen.findByText('个人 Skill')).toBeInTheDocument()
  expect(await screen.findByText('my_skill')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /激活 my_skill/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /删除 my_skill/ })).toBeInTheDocument()
})

it('shows the personal Skill create form with a package JSON editor', async () => {
  const fetchMock = routedFetch([])
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()

  const createButton = await screen.findByRole('button', { name: /新建个人 Skill/ })
  fireEvent.click(createButton)
  expect(screen.getByPlaceholderText('my_skill')).toBeInTheDocument()
  expect(screen.getByPlaceholderText(/"skill\.yaml"/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '创建个人 Skill' })).toBeInTheDocument()
})
