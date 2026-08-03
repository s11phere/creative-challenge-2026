import { apiBaseUrl } from './health'

export const DEFAULT_SPACE_ID = '00000000-0000-0000-0000-000000000000'
export type QASkillName =
  | 'knowledge_agent'
  | 'knowledge_qa'
  | 'summarize_document'
  | 'compare_sources'
  | 'create_review_cards'

export type Conversation = {
  conversation_id: string
  space_id: string
  owner_id: string
}

export type ConversationMessage = {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  run_id: string | null
  created_at: string
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
    status: 'blocked' | 'persisted'
    code: string | null
    side_effects: 0 | 1
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

export type ConversationHistoryItem = Conversation & {
  created_at: string
  updated_at: string
  messages: ConversationMessage[]
  runs: QARun[]
}

export type ConversationHistory = {
  conversations: ConversationHistoryItem[]
}

export type SkillSummary = {
  name: string
  active_version: string | null
  active_revision: number | null
  versions: string[]
}

export type SkillVersion = {
  name: string
  version: string
  content_sha256: string
  description: string
  active: boolean
  permissions: string[]
  required_capabilities: string[]
  budget: {
    max_steps: number
    max_tool_calls: number
    max_input_tokens: number
    max_output_tokens: number
    timeout_seconds: number
  }
}

export type SkillActivation = {
  name: string
  version: string
  content_sha256: string
  revision: number
}

export type Approval = {
  approval_id: string
  run_id: string
  status: 'pending' | 'approved' | 'rejected'
  side_effects: 0 | 1
  derived_knowledge_id: string | null
}

export type FeedbackDecision = 'positive' | 'negative'

export type FeedbackResponse = {
  feedback_id: string
  run_id: string
  message_id: string
  review_status: 'pending_review' | 'accepted' | 'rejected'
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
  skillName: QASkillName = 'knowledge_qa',
): Promise<QARun> {
  const path =
    skillName === 'knowledge_agent'
      ? `/api/v1/conversations/${conversationId}/skills/knowledge_agent/runs`
      : `/api/v1/conversations/${conversationId}/questions`
  return request(path, {
    method: 'POST',
    body: JSON.stringify({ question, idempotency_key: idempotencyKey }),
  })
}

export function fetchRun(runId: string, signal?: AbortSignal): Promise<QARun> {
  return request(`/api/v1/qa/runs/${runId}`, { signal })
}

export function fetchConversationHistory(signal?: AbortSignal): Promise<ConversationHistory> {
  return request(
    `/api/v1/spaces/${DEFAULT_SPACE_ID}/conversations?owner_id=${encodeURIComponent('local')}`,
    { signal },
  )
}

export function deleteConversation(
  conversationId: string,
): Promise<{ conversation_id: string; status: 'deleted' | 'already_deleted' }> {
  return request(
    `/api/v1/spaces/${DEFAULT_SPACE_ID}/conversations/${conversationId}?owner_id=${encodeURIComponent('local')}`,
    { method: 'DELETE' },
  )
}

export function createReviewCards(
  conversationId: string,
  documentId: string,
  versionId: string,
  focus: string | undefined,
  idempotencyKey: string,
): Promise<QARun> {
  return request(`/api/v1/conversations/${conversationId}/skills/create_review_cards/runs`, {
    method: 'POST',
    body: JSON.stringify({ document_id: documentId, version_id: versionId, focus, idempotency_key: idempotencyKey }),
  })
}

export function cancelRun(runId: string): Promise<QARun> {
  return request(`/api/v1/qa/runs/${runId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function resumeRun(runId: string): Promise<QARun> {
  return request(`/api/v1/qa/runs/${runId}/resume`, { method: 'POST', body: JSON.stringify({}) })
}

export function requestApproval(runId: string, idempotencyKey: string): Promise<Approval> {
  return request(`/api/v1/qa/runs/${runId}/approvals`, {
    method: 'POST',
    body: JSON.stringify({ tool_name: 'write_review_cards', tool_version: '1.0.0', idempotency_key: idempotencyKey }),
  })
}

export function decideApproval(runId: string, approvalId: string, approved: boolean): Promise<Approval> {
  return request(`/api/v1/qa/runs/${runId}/approvals/${approvalId}/decision`, {
    method: 'POST',
    body: JSON.stringify({ approved, decided_by: 'local' }),
  })
}

export function fetchSkills(signal?: AbortSignal): Promise<SkillSummary[]> {
  return request('/api/v1/skills', { signal })
}

export function fetchSkillVersions(skillName: string, signal?: AbortSignal): Promise<SkillVersion[]> {
  return request(`/api/v1/skills/${skillName}/versions`, { signal })
}

export function activateSkill(
  skillName: string,
  version: string,
  expectedRevision: number,
): Promise<SkillActivation> {
  return request(`/api/v1/skills/${skillName}/active`, {
    method: 'PUT',
    body: JSON.stringify({ version, expected_revision: expectedRevision }),
  })
}

export function rollbackSkill(
  skillName: string,
  version: string,
  expectedRevision: number,
): Promise<SkillActivation> {
  return request(`/api/v1/skills/${skillName}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ version, expected_revision: expectedRevision }),
  })
}

export function cleanupSkillVersion(
  skillName: string,
  version: string,
  contentSha256: string,
): Promise<{ name: string; version: string; removed: boolean; references: number }> {
  return request(`/api/v1/skills/${skillName}/versions/${version}/cleanup`, {
    method: 'POST',
    body: JSON.stringify({ content_sha256: contentSha256 }),
  })
}

export function fetchCitationExcerpt(
  runId: string,
  evidenceId: string,
  signal?: AbortSignal,
): Promise<CitationExcerpt> {
  return request(`/api/v1/qa/runs/${runId}/citations/${evidenceId}`, { signal })
}

export function submitOrganizationSkill(
  conversationId: string,
  skillName: 'summarize_document' | 'compare_sources',
  options: {
    focus?: string
    documentId?: string
    versionId?: string
    sourceIds?: string[]
    idempotencyKey: string
  },
): Promise<QARun> {
  return request('/api/v1/runs', {
    method: 'POST',
    body: JSON.stringify({
      conversation_id: conversationId,
      skill_name: skillName,
      focus: options.focus,
      document_id: options.documentId,
      version_id: options.versionId,
      source_ids: options.sourceIds,
      idempotency_key: options.idempotencyKey,
    }),
  })
}

export function submitFeedback(
  runId: string,
  decision: FeedbackDecision,
  idempotencyKey: string,
  note?: string,
): Promise<FeedbackResponse> {
  return request(`/api/v1/qa/runs/${runId}/feedback`, {
    method: 'POST',
    body: JSON.stringify({ decision, idempotency_key: idempotencyKey, note: note?.trim() || undefined }),
  })
}
