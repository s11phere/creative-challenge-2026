import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

it('loads trusted versions and activates one with the current revision', async () => {
  let activeVersion = '0.1.0'
  let revision = 3
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/skills/demo/active') && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body)) as { version: string; expected_revision: number }
      expect(body).toEqual({ version: '0.2.0', expected_revision: 3 })
      activeVersion = body.version
      revision = 4
      return Promise.resolve(response({
        name: 'demo', version: activeVersion, content_sha256: 'b'.repeat(64), revision,
      }))
    }
    if (url.endsWith('/skills/demo/versions')) {
      return Promise.resolve(response([
        {
          name: 'demo', version: '0.1.0', content_sha256: 'a'.repeat(64),
          description: 'Stable workflow.', active: activeVersion === '0.1.0',
          permissions: ['read_knowledge'], required_capabilities: ['fast_chat'],
          budget: { max_steps: 4, max_tool_calls: 1, max_input_tokens: 100, max_output_tokens: 50, timeout_seconds: 30 },
        },
        {
          name: 'demo', version: '0.2.0', content_sha256: 'b'.repeat(64),
          description: 'Candidate workflow.', active: activeVersion === '0.2.0',
          permissions: ['read_knowledge'], required_capabilities: ['fast_chat'],
          budget: { max_steps: 4, max_tool_calls: 1, max_input_tokens: 100, max_output_tokens: 50, timeout_seconds: 30 },
        },
      ]))
    }
    if (url.endsWith('/skills')) {
      return Promise.resolve(response([
        { name: 'demo', active_version: activeVersion, active_revision: revision, versions: ['0.1.0', '0.2.0'] },
      ]))
    }
    return Promise.resolve(response({}, 404))
  })
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()
  fireEvent.click(await screen.findByRole('button', { name: /demo/ }))
  expect(await screen.findByText('Candidate workflow.')).toBeInTheDocument()
  expect(screen.getAllByText('read_knowledge').length).toBe(2)
  fireEvent.click(screen.getByRole('button', { name: '激活' }))

  await waitFor(() => expect(screen.getAllByText('v0.2.0').length).toBeGreaterThan(0))
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/skills/demo/active'),
    expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ version: '0.2.0', expected_revision: 3 }),
    }),
  )
})

it('cleans a non-active version only after explicit confirmation', async () => {
  const sha = 'a'.repeat(64)
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/skills/demo/versions/0.1.0/cleanup') && init?.method === 'POST') {
      return Promise.resolve(response({ name: 'demo', version: '0.1.0', removed: true, references: 0 }))
    }
    if (url.endsWith('/skills/demo/versions')) {
      return Promise.resolve(response([{
        name: 'demo', version: '0.1.0', content_sha256: sha,
        description: 'Old workflow.', active: false, permissions: [],
        required_capabilities: [],
        budget: { max_steps: 1, max_tool_calls: 0, max_input_tokens: 10, max_output_tokens: 10, timeout_seconds: 5 },
      }, {
        name: 'demo', version: '0.2.0', content_sha256: 'b'.repeat(64),
        description: 'Active workflow.', active: true, permissions: [],
        required_capabilities: [],
        budget: { max_steps: 1, max_tool_calls: 0, max_input_tokens: 10, max_output_tokens: 10, timeout_seconds: 5 },
      }]))
    }
    if (url.endsWith('/skills')) {
      return Promise.resolve(response([{
        name: 'demo', active_version: '0.2.0', active_revision: 2,
        versions: ['0.1.0', '0.2.0'],
      }]))
    }
    return Promise.resolve(response({}, 404))
  })
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()
  fireEvent.click(await screen.findByRole('button', { name: /demo/ }))
  const cleanupButtons = await screen.findAllByRole('button', { name: '清理' })
  fireEvent.click(cleanupButtons[0])
  fireEvent.click(screen.getByRole('button', { name: '确认清理' }))

  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/skills/demo/versions/0.1.0/cleanup'),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ content_sha256: sha }) }),
    ),
  )
})
