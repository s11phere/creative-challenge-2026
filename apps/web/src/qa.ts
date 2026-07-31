import { apiBaseUrl } from './health'

export const DEFAULT_SPACE_ID = '00000000-0000-0000-0000-000000000000'

export type Conversation = {
  conversation_id: string
  space_id: string
  owner_id: string
}

export type QARun = {
  run_id: string
  attempt_id: string
  status: string
  conversation_id: string
  question_message_id: string
  cancellation_requested: boolean
  error_code: string | null
  skill: {
    name: string
    version: string
    content_sha256: string | null
  }
  fixed_scope: {
    source_ids: string[]
    document_ids: string[]
    version_ids: string[]
  }
  write?: {
    status: 'blocked'
    code: 'SKILL_WRITE_PORT_UNAVAILABLE'
    side_effects: 0
  } | null
  result?: {
    type: 'answer' | 'refusal' | 'conflict'
    text?: string
    message?: string
    limitations?: string[]
  } | null
  citations?: Array<{
    evidence_id: string
    source_id: string
    document_id: string
    version_id: string
    chunk_id: string
    locator: { kind: string; start: number; end: number }
  }>
}

export type SkillSummary = {
  name: string
  active_version: string | null
  active_revision: number | null
  versions: string[]
}

export type CitationExcerpt = NonNullable<QARun['citations']>[number] & {
  status: string
  excerpt: string | null
}

export class QAApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'QAApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new QAApiError(body?.detail ?? response.statusText, response.status)
  }
  return response.json() as Promise<T>
}

export function createConversation(): Promise<Conversation> {
  return request(`/api/v1/spaces/${DEFAULT_SPACE_ID}/conversations`, {
    method: 'POST',
    body: JSON.stringify({ owner_id: 'local' }),
  })
}

export function submitQuestion(
  conversationId: string,
  question: string,
  idempotencyKey: string,
): Promise<QARun> {
  return request(`/api/v1/conversations/${conversationId}/questions`, {
    method: 'POST',
    body: JSON.stringify({ question, idempotency_key: idempotencyKey }),
  })
}

export function fetchRun(runId: string, signal?: AbortSignal): Promise<QARun> {
  return request(`/api/v1/qa/runs/${runId}`, { signal })
}

export function cancelRun(runId: string): Promise<QARun> {
  return request(`/api/v1/qa/runs/${runId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function fetchSkills(signal?: AbortSignal): Promise<SkillSummary[]> {
  return request('/api/v1/skills', { signal })
}

export function fetchCitationExcerpt(
  runId: string,
  evidenceId: string,
  signal?: AbortSignal,
): Promise<CitationExcerpt> {
  return request(`/api/v1/qa/runs/${runId}/citations/${evidenceId}`, { signal })
}
