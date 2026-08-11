import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { SkillsPanel } from './SkillsPanel'

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
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
  const fetchMock = vi.fn(() => Promise.resolve(response([{
    name: 'knowledge_agent', version: '1.0.0', content_sha256: 'a'.repeat(64),
    description: 'Grounded knowledge requests.', permissions: ['read_knowledge'],
    required_capabilities: ['fast_chat'],
    budget: { max_steps: 32, max_tool_calls: 24, max_input_tokens: 100, max_output_tokens: 50, timeout_seconds: 30 },
  }])))
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()

  expect(await screen.findByText('knowledge_agent')).toBeInTheDocument()
  expect(screen.getByText('Grounded knowledge requests.')).toBeInTheDocument()
  expect(screen.queryByText(/回滚|激活|清理/)).not.toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/api/v1/skills'),
    expect.objectContaining({ headers: { 'Content-Type': 'application/json' } }),
  )
})
