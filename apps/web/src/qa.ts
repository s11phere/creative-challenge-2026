import { apiBaseUrl } from './health'

export const DEFAULT_SPACE_ID = '00000000-0000-0000-0000-000000000000'
export type Conversation = {
  conversation_id: string
  space_id: string
  owner_id: string
  workspace_path?: string | null
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

export type AssistantCommand = {
  name: string
  aliases: string[]
  kind: 'base' | 'skill'
  description: string
  argument_hint: string
  input_mode: string
}

export type AssistantSkillStatus = {
  name: string
  version: string
  description: string
  active: boolean
}

export type AssistantRun = {
  run_id: string
  user_message_id: string
  created_at: string
  updated_at: string
  status: string
  run_kind: 'assistant_turn' | 'grounded_qa' | 'skill' | 'context_compaction'
  error_code: string | null
  selection: {
    source: 'auto' | 'command' | 'none'
    skill: { name: string; version: string; content_sha256: string } | null
  }
  model_identity: string
  reasoning_profile: {
    schema_version: string
    requested_effort: string
    effective_effort: string
    provider: string
    model: string
    mapping_version: string
    mode: string
    downgrade_reason: string
  }
  assistant_message: { message_id: string; content: string } | null
  clarification: {
    clarification_id: string
    kind: string
    message: string
    resource_candidates: Array<{
      candidate_id: string
      resource_type: string
      label: string
      source_label: string | null
      version_label: string | null
    }>
  } | null
  usage: {
    input_tokens: number
    output_tokens: number
    total_tokens: number
    model_latency_ms: number
  }
}

export type AssistantRunEvent = {
  schema_version: 'agent-run-sse-v2'
  event_id: string
  run_id: string
  sequence: number
  occurred_at: string
  type: 'accepted' | 'routing' | 'clarification' | 'skill_started' | 'phase' | 'completed' | 'failed' | 'cancelled'
  payload: Record<string, unknown>
}

export type AgentRunEventType =
  | 'accepted'
  | 'skill_activated'
  | 'iteration_started'
  | 'tool_requested'
  | 'tool_started'
  | 'tool_output'
  | 'approval_required'
  | 'checkpoint_saved'
  | 'finalizing'
  | 'completed'
  | 'clarifying'
  | 'refused'
  | 'failed'
  | 'cancelled'
  | 'timed_out'
  | 'cache_used'

export type AgentRunEvent = {
  schema_version: 'agent-run-sse-v3' | 'agent-run-sse-v4'
  event_id: string
  run_id: string
  sequence: number
  occurred_at: string
  event_type: AgentRunEventType
  payload: Record<string, unknown>
}

export type AgentApproval = {
  approval_id: string
  tool_name: string
  tool_version: string
  status: 'pending' | 'approved' | 'rejected' | 'revoked' | string
  details: Record<string, unknown>
}

type AgentRunEventPage = {
  schema_version: 'agent-run-event-page-v1'
  events: unknown[]
  next_sequence: number | null
  has_more: boolean
}

export type AgentRunEventStreamOptions = {
  afterSequence?: number
  signal?: AbortSignal
  onEvents: (events: AgentRunEvent[]) => void
}

export type AssistantCommandResult = {
  command: string
  status: string
  content: string | null
  conversation_id: string | null
  run: AssistantRun | null
  commands: AssistantCommand[]
  skills: AssistantSkillStatus[]
}

export type AssistantTurnResult = AssistantRun | AssistantCommandResult

export type ExamQuestion = {
  question_id: string
  kind: 'single_choice' | 'multiple_choice' | 'short_answer' | 'calculation' | 'proof' | 'programming'
  prompt: string
  options?: Array<{ option_id: string; text: string }>
  points: number
  required: boolean
  citation_ids: string[]
}

export type ExamInteraction = {
  interaction_id: string
  interaction_version: 'exam-interaction-v1'
  kind: string
  title: string
  instructions: string[]
  progress: { current: number; total: number; label: string }
  paper?: {
    paper_id: string
    paper_version: number
    title: string
    suggested_minutes: number
    total_points: number
    sections: Array<{ section_id: string; title: string; questions: ExamQuestion[] }>
  }
  content?: unknown
  next_action: string | null
}

export type ExamSession = {
  schema_version: 'exam-session-v1'
  session_id: string
  conversation_id: string
  phase: string
  revision: number
  interaction: ExamInteraction
}

export type ExamMessageInteraction = {
  schema_version: 'exam-message-interaction-v1'
  session_id: string
  anchor_run_id: string
  anchor_message_id: string
  interaction: ExamInteraction
  submitted: boolean
}

export type ExamAnswer = {
  question_id: string
  selected_options?: string[]
  response_text?: string
}

function isAssistantRun(value: AssistantTurnResult): value is AssistantRun {
  return 'run_id' in value
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

export function fetchSkills(signal?: AbortSignal): Promise<SkillVersion[]> {
  return request('/api/v1/skills', { signal })
}

export function setSkillActivation(name: string, active: boolean): Promise<SkillVersion> {
  return request(`/api/v1/skills/${encodeURIComponent(name)}/activation`, {
    method: 'PATCH',
    body: JSON.stringify({ active }),
  })
}

export type PersonalSkill = {
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
  invocation: {
    command: string
    aliases: string[]
    argument_hint: string
    input_mode: string
    execution_mode: string
  } | null
}

export function fetchPersonalSkills(signal?: AbortSignal): Promise<PersonalSkill[]> {
  return request('/api/v1/skills/personal', { signal })
}

export function createPersonalSkill(name: string, files: Record<string, string>): Promise<PersonalSkill> {
  return request('/api/v1/skills/personal', {
    method: 'POST',
    body: JSON.stringify({ name, files }),
  })
}

export function updatePersonalSkill(name: string, files: Record<string, string>): Promise<PersonalSkill> {
  return request(`/api/v1/skills/personal/${encodeURIComponent(name)}`, {
    method: 'PUT',
    body: JSON.stringify({ files }),
  })
}

export function deletePersonalSkill(name: string): Promise<{ status: string }> {
  return request(`/api/v1/skills/personal/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export function activatePersonalSkill(name: string): Promise<PersonalSkill> {
  return request(`/api/v1/skills/personal/${encodeURIComponent(name)}/activate`, { method: 'POST' })
}

export function setPersonalSkillActivation(name: string, active: boolean): Promise<PersonalSkill> {
  return request(`/api/v1/skills/personal/${encodeURIComponent(name)}/activation`, {
    method: 'PATCH',
    body: JSON.stringify({ active }),
  })
}

export type SkillDraft = {
  name: string
  description: string
  complete: boolean
  valid: boolean
  file_count: number
  files: string[]
}

export type SkillDraftValidation = {
  name: string
  valid: boolean
  error: string | null
  description: string | null
  version: string | null
  content_sha256: string | null
}

export type SkillDraftEval = {
  skill_name: string
  skill_version: string
  gate_passed: boolean
  metrics: {
    total: number
    passed: number
    failed: number
    inconclusive: number
    errored: number
    pass_rate: number | null
    check_pass_rate: number | null
  }
}

export type SkillSuggestion = {
  name: string
  category: string
  frequency: number
  last_seen_at: string
  description: string
  hint: string
}

export type SkillDraftEvidence = {
  name: string
  evidence: {
    pattern: string
    task_category: string
    tool_sequence: string
    frequency: number
    distinct_conversations: number
    first_seen_at: string
    last_seen_at: string
    exemplars: {
      run_id: string
      conversation_id: string
      input_summary: string
      created_at: string
    }[]
  } | null
}

const DRAFTS_PATH = '/api/v1/skills/personal/drafts'

export function fetchDrafts(signal?: AbortSignal): Promise<SkillDraft[]> {
  return request(DRAFTS_PATH, { signal })
}

export function createDraft(name: string, files: Record<string, string>): Promise<SkillDraft> {
  return request(DRAFTS_PATH, {
    method: 'POST',
    body: JSON.stringify({ name, files }),
  })
}

export function updateDraft(name: string, files: Record<string, string>): Promise<SkillDraft> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}`, {
    method: 'PUT',
    body: JSON.stringify({ files }),
  })
}

export function deleteDraft(name: string): Promise<{ status: string }> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export function fetchDraftFiles(name: string, signal?: AbortSignal): Promise<{ name: string; files: Record<string, string> }> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}/files`, { signal })
}

export function fetchDraftEvidence(name: string, signal?: AbortSignal): Promise<SkillDraftEvidence> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}/evidence`, { signal })
}

export function validateDraft(name: string): Promise<SkillDraftValidation> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}/validate`, { method: 'POST' })
}

export function runDraftEval(name: string): Promise<SkillDraftEval> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}/eval`, { method: 'POST' })
}

export function activateDraft(name: string): Promise<{ name: string; active: boolean; description: string }> {
  return request(`${DRAFTS_PATH}/${encodeURIComponent(name)}/activate`, { method: 'POST' })
}

export function fetchSkillSuggestions(signal?: AbortSignal): Promise<SkillSuggestion[]> {
  return request(`${DRAFTS_PATH}/suggestions`, { signal })
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

export function fetchAssistantCommands(signal?: AbortSignal): Promise<AssistantCommand[]> {
  return request<{ commands: AssistantCommand[] }>('/api/v2/commands', { signal }).then(
    (response) => response.commands ?? [],
  )
}

export function submitAssistantTurn(
  conversationId: string,
  content: string,
  idempotencyKey: string,
): Promise<AssistantTurnResult> {
  return request(`/api/v2/conversations/${conversationId}/turns`, {
    method: 'POST',
    body: JSON.stringify({ content, idempotency_key: idempotencyKey }),
  })
}

export function fetchAssistantRun(runId: string, signal?: AbortSignal): Promise<AssistantRun> {
  return request(`/api/v2/runs/${runId}`, { signal })
}

export async function fetchAssistantRunEvents(
  runId: string,
  signal?: AbortSignal,
): Promise<AssistantRunEvent[]> {
  const response = await fetch(`${apiBaseUrl}/api/v2/runs/${runId}/events`, {
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!response.ok) throw new QAApiError(response.statusText, response.status)
  return parseAssistantRunEvents(await response.text())
}

export async function fetchAgentRunEvents(
  runId: string,
  signal?: AbortSignal,
): Promise<AgentRunEvent[]> {
  const events: AgentRunEvent[] = []
  let afterSequence = 0

  while (true) {
    const page = await request<AgentRunEventPage>(
      `/api/v3/runs/${runId}/events?after_sequence=${afterSequence}&limit=200`,
      { signal },
    )
    for (const value of page.events ?? []) {
      const event = parseAgentRunEvent(value)
      if (event) events.push(event)
    }
    const nextSequence = page.next_sequence
    if (!page.has_more || nextSequence === null || !Number.isInteger(nextSequence) || nextSequence <= afterSequence) {
      return mergeAgentRunEvents([], events)
    }
    afterSequence = nextSequence
  }
}

export async function streamAgentRunEvents(
  runId: string,
  { afterSequence = 0, signal, onEvents }: AgentRunEventStreamOptions,
): Promise<void> {
  let cursor = afterSequence

  while (!signal?.aborted) {
    try {
      const headers: Record<string, string> = { Accept: 'text/event-stream' }
      if (cursor > 0) headers['Last-Event-ID'] = String(cursor)
      const response = await fetch(`${apiBaseUrl}/api/v3/runs/${runId}/events/stream`, {
        headers,
        signal,
      })
      if (!response.ok) throw new QAApiError(response.statusText, response.status)

      const received = parseAgentRunEventStream(await response.text())
      if (received.length > 0) {
        cursor = received.at(-1)!.sequence
        onEvents(received)
      }
      await waitForAgentEventReconnect(signal, response.headers.get('X-Agent-Event-Has-More') === 'true' ? 0 : 1_000)
    } catch (error) {
      if (signal?.aborted || isAbortError(error)) return
      if (error instanceof QAApiError && (error.status === 404 || error.status === 409)) return
      await waitForAgentEventReconnect(signal, 1_000)
    }
  }
}

export function fetchAssistantConversationRuns(
  conversationId: string,
  signal?: AbortSignal,
): Promise<AssistantRun[]> {
  return request<{ runs: AssistantRun[] }>(`/api/v2/conversations/${conversationId}/runs`, {
    signal,
  }).then((response) => response.runs ?? [])
}

export function fetchExamSessions(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ExamSession[]> {
  return request(`/api/v3/conversations/${conversationId}/exam-sessions`, { signal })
}

export function fetchExamMessageInteractions(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ExamMessageInteraction[]> {
  return request(`/api/v4/conversations/${conversationId}/exam-interactions`, { signal })
}

export function submitExamAction(
  session: ExamSession,
  answers: ExamAnswer[],
): Promise<ExamSession> {
  const paper = session.interaction.paper
  const nonce = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return request(`/api/v3/exam-sessions/${session.session_id}/actions`, {
    method: 'POST',
    body: JSON.stringify({
      interaction_id: session.interaction.interaction_id,
      action: session.interaction.next_action,
      idempotency_key: nonce,
      submission_id: paper ? nonce : undefined,
      paper_id: paper?.paper_id,
      paper_version: paper?.paper_version,
      answers,
    }),
  })
}

export function cancelAssistantRun(runId: string): Promise<AssistantRun> {
  return request(`/api/v2/runs/${runId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export function selectClarificationResource(
  runId: string,
  clarificationId: string,
  candidateId: string,
): Promise<AssistantRun> {
  return request(`/api/v2/runs/${runId}/clarifications/${clarificationId}`, {
    method: 'POST',
    body: JSON.stringify({ candidate_id: candidateId }),
  })
}

function parseAssistantRunEvents(stream: string): AssistantRunEvent[] {
  const events: AssistantRunEvent[] = []
  for (const line of stream.split(/\r?\n/)) {
    if (!line.startsWith('data: ')) continue
    try {
      const event = JSON.parse(line.slice('data: '.length)) as AssistantRunEvent
      if (event.schema_version === 'agent-run-sse-v2' && typeof event.sequence === 'number') {
        events.push(event)
      }
    } catch {
      // A malformed event cannot replace the persisted Run state used by the workspace.
    }
  }
  return events
}

export function fetchAssistantApprovals(
  runId: string,
  signal?: AbortSignal,
): Promise<AgentApproval[]> {
  return request<AgentApproval[]>(`/api/v2/runs/${runId}/approvals`, { signal })
}

export function decideAssistantApproval(
  runId: string,
  approvalId: string,
  approved: boolean,
  alwaysAllow = false,
): Promise<AgentApproval> {
  return request<AgentApproval>(`/api/v2/runs/${runId}/approvals/${approvalId}/decision`, {
    method: 'POST',
    body: JSON.stringify({ approved, always_allow: alwaysAllow, decided_by: 'web' }),
  })
}

function parseAgentRunEventStream(stream: string): AgentRunEvent[] {
  const events: AgentRunEvent[] = []
  for (const block of stream.split(/\r?\n\r?\n/)) {
    const data = block.split(/\r?\n/).find((line) => line.startsWith('data: '))
    if (!data) continue
    try {
      const event = parseAgentRunEvent(JSON.parse(data.slice('data: '.length)))
      if (event) events.push(event)
    } catch {
      // The durable history endpoint remains authoritative after a malformed stream frame.
    }
  }
  return mergeAgentRunEvents([], events)
}

function parseAgentRunEvent(value: unknown): AgentRunEvent | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const event = value as Record<string, unknown>
    const sequence = event.sequence
    if (
      (event.schema_version !== 'agent-run-sse-v3' && event.schema_version !== 'agent-run-sse-v4')
    || typeof event.event_id !== 'string'
    || typeof event.run_id !== 'string'
    || typeof sequence !== 'number'
    || !Number.isInteger(sequence)
    || sequence < 1
    || typeof event.occurred_at !== 'string'
    || !isAgentRunEventType(event.event_type)
    || !isFlatPayload(event.payload)
  ) return null
  return {
    schema_version: event.schema_version,
    event_id: event.event_id,
    run_id: event.run_id,
    sequence,
    occurred_at: event.occurred_at,
    event_type: event.event_type,
    payload: event.payload,
  }
}

function isAgentRunEventType(value: unknown): value is AgentRunEventType {
  return typeof value === 'string' && [
    'accepted', 'skill_activated', 'iteration_started', 'tool_requested', 'tool_started', 'tool_output',
    'approval_required', 'checkpoint_saved', 'finalizing', 'completed', 'clarifying',
      'refused', 'failed', 'cancelled', 'timed_out',
      'cache_used',
  ].includes(value)
}

function isFlatPayload(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  return Object.values(value).every((item) => ['string', 'number', 'boolean'].includes(typeof item))
}

function mergeAgentRunEvents(
  current: AgentRunEvent[],
  incoming: AgentRunEvent[],
): AgentRunEvent[] {
  const bySequence = new Map(current.map((event) => [event.sequence, event]))
  for (const event of incoming) bySequence.set(event.sequence, event)
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence)
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function waitForAgentEventReconnect(signal: AbortSignal | undefined, delay: number): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, delay)
    signal?.addEventListener('abort', () => {
      clearTimeout(timer)
      resolve()
    }, { once: true })
  })
}

export { isAssistantRun }
