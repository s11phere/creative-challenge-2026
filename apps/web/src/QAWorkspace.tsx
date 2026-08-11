import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  Bot,
  BookOpenText,
  Check,
  ChevronDown,
  CircleHelp,
  FileText,
  LoaderCircle,
  MessageSquareText,
  Quote,
  RotateCcw,
  Send,
  Square,
  X,
} from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import 'katex/dist/katex.min.css'
import { AgentRunTimeline } from './AgentRunTimeline'
import {
  cancelRun,
  cancelAssistantRun,
  createConversation,
  decideAssistantApproval,
  fetchAssistantApprovals,
  fetchAssistantConversationRuns,
  fetchAssistantCommands,
  fetchAssistantRunEvents,
  fetchAgentRunEvents,
  fetchCitationExcerpt,
  fetchConversationHistory,
  fetchRun,
  isAssistantRun,
  legacyQARunToAssistantRun,
  selectClarificationResource,
  submitQuestion,
  submitAssistantTurn,
  type AssistantCommand,
  type AssistantCommandResult,
  type AssistantRun,
  type AssistantRunEvent,
  type AgentRunEvent,
  type ConversationHistoryItem,
  type QARun,
  streamAgentRunEvents,
} from './qa'
import { assistantDefaultApiMode, type AssistantApiMode } from './assistantRelease'
import { fetchSourceDetail } from './sources'

export type QAWorkspaceProps = {
  selectedConversationId?: string | null
  onConversationSelected?: (conversationId: string | null) => void
  apiMode?: AssistantApiMode
}

type CitationMetadata = {
  displayName: string | null
  uri: string | null
}

type CommandNotice = Pick<AssistantCommandResult, 'command' | 'content' | 'commands'> & {
  notice_id: string
  created_at: string
  order: number
}

type EffortPicker = {
  picker_id: string
  created_at: string
  order: number
}

type ConversationMessage = ConversationHistoryItem['messages'][number]

type TimelineItem =
  | { kind: 'message'; id: string; created_at: string; order: number; message: ConversationMessage }
  | { kind: 'command_notice'; id: string; created_at: string; order: number; notice: CommandNotice }
  | { kind: 'effort_picker'; id: string; created_at: string; order: number; picker: EffortPicker }

const reasoningEfforts = ['low', 'medium', 'high', 'xhigh', 'max'] as const
const initialReasoningEffort = 'medium'

function reportedReasoningEffort(content: string | null): typeof reasoningEfforts[number] | null {
  const requested = /requested effort:\s*(low|medium|high|xhigh|max)\b/i.exec(content ?? '')
  if (requested) {
    return reasoningEfforts.find((effort) => effort === requested[1].toLocaleLowerCase()) ?? null
  }
  const match = /reasoning effort:\s*(low|medium|high|xhigh|max)\b/i.exec(content ?? '')
  return match ? reasoningEfforts.find((effort) => effort === match[1].toLocaleLowerCase()) ?? null : null
}

const activeStatuses = new Set(['created', 'queued', 'running', 'cancel_requested'])
// Approval decisions are persisted asynchronously by the Worker. Keep polling while a
// run waits for approval so the UI observes the resumed execution without navigation.
const refreshingStatuses = new Set([...activeStatuses, 'waiting_approval'])

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    created: '已创建',
    queued: '等待执行',
    running: '正在处理',
    waiting_clarification: '等待选择',
    waiting_approval: '等待审批',
    cancel_requested: '正在取消',
    cancelled: '已取消',
    completed: '已完成',
    refused: '已拒绝',
    failed: '运行失败',
    timed_out: '运行超时',
  }
  return labels[status] ?? status
}

function citationKey(sourceId: string, documentId: string): string {
  return `${sourceId}:${documentId}`
}

function isGroundedRun(run: AssistantRun): boolean {
  return run.run_kind === 'grounded_qa' || run.run_kind === 'skill'
}

function isSkillInvocation(run: AssistantRun): boolean {
  return run.selection.skill !== null
}

function usesAgentLoop(run: AssistantRun): boolean {
  return isSkillInvocation(run) || run.run_kind === 'assistant_turn'
}

function selectionSourceLabel(source: AssistantRun['selection']['source']): string {
  const labels: Record<AssistantRun['selection']['source'], string> = {
    auto: 'Agent 自动路由',
    command: '显式指令',
    none: '兼容入口',
  }
  return labels[source]
}

function eventLabel(event: AssistantRunEvent, skillName: string): string {
  const action = typeof event.payload.action === 'string' ? event.payload.action : null
  const phase = typeof event.payload.phase === 'string' ? event.payload.phase : null
  switch (event.type) {
    case 'accepted':
      return '已接收调用请求'
    case 'routing':
      return action === 'invoke_skill' ? 'Agent 已选择调用此 Skill' : 'Agent 已完成路由'
    case 'skill_started':
      return `开始执行 ${skillName}`
    case 'phase':
      if (phase === 'final_answer') return '已完成最终回答'
      return phase ? `正在执行 ${phase}` : '正在执行任务阶段'
    case 'clarification':
      return '等待补充资源信息'
    case 'completed':
      return '调用已完成'
    case 'failed':
      return '调用失败'
    case 'cancelled':
      return '调用已取消'
  }
}

type SkillRunCardProps = {
  run: AssistantRun
  result: QARun['result']
  hasGroundedEvidence: boolean
  clarificationPending: boolean
  onSelectClarification: (candidateId: string) => void
  onOpenEvidence: (runId: string) => void
  approvalBusy: boolean
  onDecideApproval: (approvalId: string, approved: boolean, alwaysAllow: boolean) => void
}

function LegacySkillRunCard({
  run,
  result,
  hasGroundedEvidence,
  clarificationPending,
  onSelectClarification,
  onOpenEvidence,
}: SkillRunCardProps) {
  const skill = run.selection.skill
  const activityQuery = useQuery({
    queryKey: ['assistant-run-events', run.run_id],
    queryFn: ({ signal }) => fetchAssistantRunEvents(run.run_id, signal),
    retry: false,
    enabled: skill !== null,
    refetchInterval: refreshingStatuses.has(run.status) ? 2_000 : false,
  })
  if (!skill) return null
  const activity = activityQuery.data ?? []

  return (
    <details className="chat-skill-run" data-status={run.status}>
      <summary>
        <span className="chat-skill-run-title">
          <Bot size={17} aria-hidden="true" />
          <span><strong>Skill 调用 · {skill.name}</strong><small>{selectionSourceLabel(run.selection.source)} · {statusLabel(run.status)}</small></span>
        </span>
        <ChevronDown size={16} aria-hidden="true" />
      </summary>
      <div className="chat-skill-run-content">
        <section>
          <h3>调用链</h3>
          <ol className="chat-skill-activity">
            <li><span>发起方式</span><strong>{selectionSourceLabel(run.selection.source)}</strong></li>
            <li><span>固定版本</span><code>{skill.name} {skill.version}</code></li>
            {activity.map((event) => <li key={event.event_id}><span>步骤 {event.sequence}</span><strong>{eventLabel(event, skill.name)}</strong></li>)}
            {activity.length === 0 && <li><span>执行状态</span><strong>{statusLabel(run.status)}</strong></li>}
          </ol>
        </section>
        <section>
          <h3>执行信息</h3>
          <dl className="chat-skill-run-metadata">
            <div><dt>状态</dt><dd>{statusLabel(run.status)}</dd></div>
            <div><dt>模型</dt><dd>{run.model_identity}</dd></div>
            <div><dt>输入 Token</dt><dd>{run.usage.input_tokens.toLocaleString('zh-CN')}</dd></div>
            <div><dt>输出 Token</dt><dd>{run.usage.output_tokens.toLocaleString('zh-CN')}</dd></div>
            <div><dt>模型耗时</dt><dd>{Math.round(run.usage.model_latency_ms).toLocaleString('zh-CN')} ms</dd></div>
          </dl>
        </section>
        {result && skill.name !== 'knowledge_qa' && (result.text || result.message) && (
          <section>
            <h3>Skill 结果</h3>
            <div className="qa-answer">
              <RenderedAssistantAnswer
                content={result.text ?? result.message ?? ''}
                limitations={result.limitations}
              />
            </div>
          </section>
        )}
        {hasGroundedEvidence && (
          <button
            className="chat-evidence-button"
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onOpenEvidence(run.run_id)
            }}
          >
            <Quote size={15} aria-hidden="true" />
            查看引用证据
          </button>
        )}
        {run.clarification && (
          <section className="chat-clarification">
            <h3>需要选择</h3>
            <p>{run.clarification.message}</p>
            {run.clarification.resource_candidates.length > 0 && (
              <div role="group" aria-label="资源选择">
                {run.clarification.resource_candidates.map((candidate) => (
                  <button
                    key={candidate.candidate_id}
                    type="button"
                    disabled={clarificationPending}
                    onClick={() => onSelectClarification(candidate.candidate_id)}
                  >
                    <FileText size={16} aria-hidden="true" />
                    <span><strong>{candidate.label}</strong>{candidate.source_label && <small>{candidate.source_label}</small>}</span>
                  </button>
                ))}
              </div>
            )}
          </section>
        )}
        {run.error_code && <code className="chat-skill-run-error">{run.error_code}</code>}
      </div>
    </details>
  )
}

function mergeAgentRunEvents(
  current: AgentRunEvent[],
  incoming: AgentRunEvent[],
): AgentRunEvent[] {
  const bySequence = new Map(current.map((event) => [event.sequence, event]))
  for (const event of incoming) bySequence.set(event.sequence, event)
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence)
}

function SkillRunCard(props: SkillRunCardProps) {
  const { run } = props
  const skill = run.selection.skill
  const eligibleForAgentTimeline = usesAgentLoop(run)
  const queryClient = useQueryClient()
  const agentEventsQuery = useQuery({
    queryKey: ['agent-run-events', run.run_id],
    queryFn: ({ signal }) => fetchAgentRunEvents(run.run_id, signal),
    retry: false,
    enabled: eligibleForAgentTimeline,
    refetchInterval: refreshingStatuses.has(run.status) ? 3_000 : false,
  })
  const approvalQuery = useQuery({
    queryKey: ['agent-run-approvals', run.run_id],
    queryFn: ({ signal }) => fetchAssistantApprovals(run.run_id, signal),
    retry: false,
    enabled: eligibleForAgentTimeline,
    refetchInterval: run.status === 'waiting_approval' ? 2_000 : false,
  })
  const agentEvents = agentEventsQuery.data ?? []
  const lastSequence = agentEvents.at(-1)?.sequence ?? 0

  useEffect(() => {
    if (!eligibleForAgentTimeline || !activeStatuses.has(run.status) || !agentEventsQuery.isSuccess) return
    const controller = new AbortController()
    void streamAgentRunEvents(run.run_id, {
      afterSequence: lastSequence,
      signal: controller.signal,
      onEvents: (incoming) => {
        queryClient.setQueryData<AgentRunEvent[]>(['agent-run-events', run.run_id], (current = []) =>
          mergeAgentRunEvents(current, incoming),
        )
      },
    })
    return () => controller.abort()
  }, [agentEventsQuery.isSuccess, eligibleForAgentTimeline, lastSequence, queryClient, run.run_id, run.status])

  if (!eligibleForAgentTimeline) return null
  if (agentEvents.length > 0 || run.run_kind === 'assistant_turn') {
    return <AgentRunTimeline {...props} events={agentEvents} approvals={approvalQuery.data ?? []} />
  }
  if (!skill) return null
  return <LegacySkillRunCard {...props} />
}

function commandNameMatches(command: AssistantCommand, needle: string): boolean {
  const normalized = needle.trim().toLocaleLowerCase()
  if (!normalized) return true
  return [command.name, ...command.aliases]
    .some((value) => value.toLocaleLowerCase().startsWith(normalized))
}

function commandDescriptionMatches(command: AssistantCommand, needle: string): boolean {
  const normalized = needle.trim().toLocaleLowerCase()
  return Boolean(normalized) && command.description.toLocaleLowerCase().includes(normalized)
}

type CommandPrefix = {
  leading: string
  command: string
  trailing: string
}

function validCommandPrefix(value: string, commands: AssistantCommand[]): CommandPrefix | null {
  const match = /^(\s*)(\/[^\s/]+)([\s\S]*)$/.exec(value)
  if (!match) return null
  const token = match[2].slice(1).toLocaleLowerCase()
  const descriptor = commands.find((command) =>
    [command.name, ...command.aliases].some((name) => name.toLocaleLowerCase() === token),
  )
  return descriptor
    ? { leading: match[1], command: match[2], trailing: match[3] }
    : null
}

function CommandText({ value, commands }: { value: string; commands: AssistantCommand[] }) {
  const prefix = validCommandPrefix(value, commands)
  if (!prefix) return <>{value}</>
  return (
    <>
      <span>{prefix.leading}</span>
      <span className="chat-command-token">{prefix.command}</span>
      <span>{prefix.trailing}</span>
    </>
  )
}

function RenderedAssistantAnswer({
  content,
  limitations = [],
}: {
  content: string
  limitations?: string[]
}) {
  return (
    <div className="qa-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        skipHtml
      >
        {content}
      </ReactMarkdown>
      {limitations.map((limitation) => <small key={limitation}>{limitation}</small>)}
    </div>
  )
}

export function QAWorkspace({
  selectedConversationId,
  onConversationSelected,
  apiMode = assistantDefaultApiMode,
}: QAWorkspaceProps = {}) {
  const [draft, setDraft] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [localMessages, setLocalMessages] = useState<ConversationHistoryItem['messages']>([])
  const [localRuns, setLocalRuns] = useState<AssistantRun[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [evidenceRunId, setEvidenceRunId] = useState<string | null>(null)
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const [commandIndex, setCommandIndex] = useState(0)
  const [defaultReasoningEffort, setDefaultReasoningEffort] = useState<typeof reasoningEfforts[number]>(initialReasoningEffort)
  const [effortIndex, setEffortIndex] = useState(reasoningEfforts.indexOf(initialReasoningEffort))
  const [commandMenuDismissed, setCommandMenuDismissed] = useState(false)
  const [commandNotices, setCommandNotices] = useState<CommandNotice[]>([])
  const [pendingEffortPicker, setPendingEffortPicker] = useState<EffortPicker | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const commandHighlightRef = useRef<HTMLDivElement>(null)
  const threadRef = useRef<HTMLDivElement>(null)
  const effortPickerRef = useRef<HTMLElement>(null)
  const isComposingRef = useRef(false)
  const excerptRef = useRef<HTMLDivElement>(null)
  const historyInitializedRef = useRef(false)
  const evidenceUserClosedRef = useRef(false)
  const timelineSequenceRef = useRef(0)
  const localMessageOrdersRef = useRef(new Map<string, number>())
  const effortPickerConsumedRef = useRef(false)
  const queryClient = useQueryClient()
  const usingLegacyV1 = apiMode === 'v1'

  const historyQuery = useQuery({
    queryKey: ['qa-history'],
    queryFn: ({ signal }) => fetchConversationHistory(signal),
    retry: false,
    enabled: Boolean(conversationId) || selectedConversationId !== null,
  })
  const commandsQuery = useQuery({
    queryKey: ['assistant-commands'],
    queryFn: ({ signal }) => fetchAssistantCommands(signal),
    staleTime: 60_000,
    retry: false,
    enabled: !usingLegacyV1,
  })
  const assistantRunsQuery = useQuery({
    queryKey: ['assistant-runs', conversationId],
    queryFn: ({ signal }) => fetchAssistantConversationRuns(conversationId!, signal),
    enabled: Boolean(conversationId) && !usingLegacyV1,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.some((run) => refreshingStatuses.has(run.status)) ? 2_000 : false,
  })

  const conversations = historyQuery.data?.conversations ?? []
  const selectedConversation = conversations.find(
    (conversation) => conversation.conversation_id === conversationId,
  )
  const runs = useMemo(() => {
    const apiRuns = usingLegacyV1
      ? (selectedConversation?.runs ?? []).map(legacyQARunToAssistantRun)
      : (assistantRunsQuery.data ?? [])
    // The submission response is only an optimistic snapshot. A refreshed API Run is authoritative.
    const values = new Map(localRuns.map((run) => [run.run_id, run]))
    for (const run of apiRuns) values.set(run.run_id, run)
    return [...values.values()]
  }, [assistantRunsQuery.data, localRuns, selectedConversation?.runs, usingLegacyV1])
  const runsByMessage = useMemo(
    () => new Map(runs.map((run) => [run.user_message_id, run])),
    [runs],
  )
  const legacyRunsById = useMemo(
    () => new Map((selectedConversation?.runs ?? []).map((run) => [run.run_id, run])),
    [selectedConversation],
  )
  const currentRun = runs.find((run) => run.run_id === activeRunId) ?? runs.at(-1)
  const currentLegacyRun = currentRun ? legacyRunsById.get(currentRun.run_id) : undefined
  const currentQARunQuery = useQuery({
    queryKey: ['qa-run', currentRun?.run_id],
    queryFn: ({ signal }) => fetchRun(currentRun!.run_id, signal),
    enabled: Boolean(currentRun && (isGroundedRun(currentRun) || currentRun.run_kind === 'assistant_turn')),
    initialData: currentLegacyRun,
    retry: false,
    refetchInterval: (query) => refreshingStatuses.has(query.state.data?.status ?? '') ? 2_000 : false,
  })
  const evidenceLegacyRun = evidenceRunId ? legacyRunsById.get(evidenceRunId) : undefined
  const evidenceRunQuery = useQuery<QARun>({
    queryKey: ['qa-evidence-run', evidenceRunId],
    queryFn: ({ signal }) => fetchRun(evidenceRunId!, signal),
    enabled: Boolean(evidenceRunId),
    initialData: evidenceLegacyRun,
    retry: false,
  })
  const evidenceRun = evidenceRunQuery.data ?? evidenceLegacyRun
  const citationSourceIds = useMemo(
    () => [...new Set(evidenceRun?.citations?.map((citation) => citation.source_id) ?? [])],
    [evidenceRun?.citations],
  )
  const citationMetadataQuery = useQuery<Record<string, CitationMetadata>>({
    queryKey: ['qa-citation-source-details', citationSourceIds],
    queryFn: async ({ signal }) => {
      const details = await Promise.all(
        citationSourceIds.map((sourceId) => fetchSourceDetail(sourceId, signal).catch(() => null)),
      )
      const metadata: Record<string, CitationMetadata> = {}
      details.forEach((detail, index) => {
        if (!detail) return
        const sourceId = citationSourceIds[index]
        for (const document of detail.documents ?? []) {
          metadata[citationKey(sourceId, document.id)] = {
            displayName: document.display_name,
            uri: detail.source?.uri ?? null,
          }
        }
      })
      return metadata
    },
    enabled: citationSourceIds.length > 0,
    retry: false,
  })
  const citationQuery = useQuery({
    queryKey: ['qa-citation', evidenceRun?.run_id, selectedEvidenceId],
    queryFn: ({ signal }) => fetchCitationExcerpt(evidenceRun!.run_id, selectedEvidenceId!, signal),
    enabled: Boolean(evidenceRun?.run_id && selectedEvidenceId),
    retry: false,
  })

  const trimmed = draft.trimStart()
  const commandToken = trimmed.startsWith('/') && !trimmed.startsWith('//')
    ? trimmed.slice(1).split(/\s/, 1)[0]
    : ''
  const commandOptions = useMemo(() => {
    const commands = commandsQuery.data ?? []
    if (!commandToken.trim()) return commands
    const nameMatches = commands.filter((command) => commandNameMatches(command, commandToken))
    return nameMatches.length > 0
      ? nameMatches
      : commands.filter((command) => commandDescriptionMatches(command, commandToken))
  }, [commandToken, commandsQuery.data])
  const commandHasArguments = /\s/.test(trimmed.slice(1))
  const commandMenuOpen = !usingLegacyV1
    && trimmed.startsWith('/')
    && !trimmed.startsWith('//')
    && !commandHasArguments
    && !commandMenuDismissed
  const activeCommand = commandOptions[commandIndex]
  const activeReasoningEffort = reasoningEfforts[effortIndex]
  const commandPrefix = useMemo(
    () => validCommandPrefix(draft, commandsQuery.data ?? []),
    [commandsQuery.data, draft],
  )

  useEffect(() => {
    if (!commandHighlightRef.current || !textareaRef.current) return
    commandHighlightRef.current.scrollTop = textareaRef.current.scrollTop
    commandHighlightRef.current.scrollLeft = textareaRef.current.scrollLeft
  }, [commandPrefix, draft])

  useEffect(() => {
    if (commandIndex >= commandOptions.length) setCommandIndex(0)
  }, [commandIndex, commandOptions.length])

  useEffect(() => {
    if (effortIndex >= reasoningEfforts.length) setEffortIndex(0)
  }, [effortIndex])

  useEffect(() => {
    if (selectedConversationId === undefined) {
      if (historyInitializedRef.current || !historyQuery.data) return
      historyInitializedRef.current = true
      const newest = historyQuery.data.conversations?.[0]
      if (newest) {
        setConversationId(newest.conversation_id)
        onConversationSelected?.(newest.conversation_id)
      }
      return
    }
    if (conversationId !== selectedConversationId) {
      setConversationId(selectedConversationId)
      setLocalMessages([])
      setLocalRuns([])
      setActiveRunId(null)
      evidenceUserClosedRef.current = false
      setEvidenceRunId(null)
      setSelectedEvidenceId(null)
      setCommandNotices([])
      setPendingEffortPicker(null)
      localMessageOrdersRef.current.clear()
    }
  }, [conversationId, historyQuery.data, onConversationSelected, selectedConversationId])

  useEffect(() => {
    setLocalMessages([])
    setLocalRuns([])
    setActiveRunId(null)
    evidenceUserClosedRef.current = false
    setEvidenceRunId(null)
    setSelectedEvidenceId(null)
    setCommandNotices([])
    setPendingEffortPicker(null)
    localMessageOrdersRef.current.clear()
    setCommandMenuDismissed(false)
  }, [apiMode])

  useEffect(() => {
    if (!pendingEffortPicker) return
    const frame = requestAnimationFrame(() => effortPickerRef.current?.focus())
    return () => cancelAnimationFrame(frame)
  }, [pendingEffortPicker])

  useEffect(() => {
    if (currentRun && !activeStatuses.has(currentRun.status)) {
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
    }
  }, [currentRun, queryClient])

  useEffect(() => {
    if (selectedEvidenceId && (citationQuery.data || citationQuery.error)) excerptRef.current?.focus()
  }, [citationQuery.data, citationQuery.error, selectedEvidenceId])

  useEffect(() => {
    const citations = currentQARunQuery.data?.citations ?? currentLegacyRun?.citations ?? []
    if (!evidenceRunId && !evidenceUserClosedRef.current && currentRun?.run_id && citations.length > 0) {
      setEvidenceRunId(currentRun.run_id)
    }
  }, [currentQARunQuery.data?.citations, currentLegacyRun?.citations, currentRun?.run_id, evidenceRunId])

  const nextTimelinePosition = () => ({
    created_at: new Date().toISOString(),
    order: timelineSequenceRef.current++,
  })

  const submitMutation = useMutation({
    mutationFn: async (content: string) => {
      let targetConversationId = conversationId
      if (!targetConversationId) {
        const created = await createConversation()
        targetConversationId = created.conversation_id
      }
      const result = usingLegacyV1
        ? legacyQARunToAssistantRun(
          await submitQuestion(targetConversationId, content, crypto.randomUUID(), true),
        )
        : await submitAssistantTurn(targetConversationId, content, crypto.randomUUID())
      return { content, result, targetConversationId }
    },
    onSuccess: ({ content, result, targetConversationId }) => {
      if (isAssistantRun(result)) {
        const position = nextTimelinePosition()
        setLocalRuns((current) => [...current.filter((run) => run.run_id !== result.run_id), result])
        setActiveRunId(result.run_id)
        localMessageOrdersRef.current.set(result.user_message_id, position.order)
        setLocalMessages((current) => [
          ...current.filter((message) => message.message_id !== result.user_message_id),
          {
            message_id: result.user_message_id,
            role: 'user',
            content,
            run_id: null,
            created_at: position.created_at,
          },
        ])
        setConversationId(targetConversationId)
        onConversationSelected?.(targetConversationId)
      } else {
        // Skill commands already have a durable Run card; base-command notices are
        // useful for commands such as /help and /skills, but would duplicate a Skill turn.
        if (result.command === 'effort') {
          const reported = reportedReasoningEffort(result.content)
          if (reported) {
            setDefaultReasoningEffort(reported)
            setEffortIndex(reasoningEfforts.indexOf(reported))
          }
        }
        if (!result.run) {
          const position = nextTimelinePosition()
          setCommandNotices((current) => [
            ...current,
            {
              notice_id: `command-${position.order}`,
              created_at: position.created_at,
              order: position.order,
              command: result.command,
              content: result.content,
              commands: result.commands,
            },
          ])
        }
        const nextConversationId = result.conversation_id ?? targetConversationId
        if (result.run) {
          const run = result.run
          const position = nextTimelinePosition()
          setLocalRuns((current) => [
            ...current.filter((item) => item.run_id !== run.run_id),
            run,
          ])
          localMessageOrdersRef.current.set(run.user_message_id, position.order)
          setLocalMessages((current) => [
            ...current.filter((message) => message.message_id !== run.user_message_id),
            {
              message_id: run.user_message_id,
              role: 'user',
              content,
              run_id: null,
              created_at: position.created_at,
            },
          ])
          setActiveRunId(run.run_id)
        } else {
          setLocalRuns([])
          setActiveRunId(null)
        }
        setConversationId(nextConversationId)
        onConversationSelected?.(nextConversationId)
      }
      evidenceUserClosedRef.current = false
      setSelectedEvidenceId(null)
      setDraft('')
      setCommandMenuDismissed(false)
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
      void queryClient.invalidateQueries({ queryKey: ['assistant-runs'] })
    },
  })
  const cancelMutation = useMutation({
    mutationFn: async () => {
      if (usingLegacyV1) {
        return legacyQARunToAssistantRun(await cancelRun(currentRun!.run_id))
      }
      return cancelAssistantRun(currentRun!.run_id)
    },
    onSuccess: (run) => {
      setLocalRuns((current) => [...current.filter((item) => item.run_id !== run.run_id), run])
      queryClient.setQueryData<AssistantRun[]>(['assistant-runs', conversationId], (current = []) =>
        [...current.filter((item) => item.run_id !== run.run_id), run],
      )
    },
  })
  const clarificationMutation = useMutation({
    mutationFn: ({ run, candidateId }: { run: AssistantRun; candidateId: string }) =>
      selectClarificationResource(run.run_id, run.clarification!.clarification_id, candidateId),
    onSuccess: (run) => {
      setLocalRuns((current) => [...current.filter((item) => item.run_id !== run.run_id), run])
      setActiveRunId(run.run_id)
      queryClient.setQueryData<AssistantRun[]>(['assistant-runs', conversationId], (current = []) =>
        [...current.filter((item) => item.run_id !== run.run_id), run],
      )
    },
  })
  const approvalMutation = useMutation({
    mutationFn: ({
      runId,
      approvalId,
      approved,
      alwaysAllow,
    }: {
      runId: string
      approvalId: string
      approved: boolean
      alwaysAllow: boolean
    }) => decideAssistantApproval(runId, approvalId, approved, alwaysAllow),
    onSuccess: (_approval, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['agent-run-approvals', variables.runId] })
      void queryClient.invalidateQueries({ queryKey: ['agent-run-events', variables.runId] })
      void queryClient.invalidateQueries({ queryKey: ['assistant-runs', conversationId] })
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
    },
  })

  const chooseCommand = (command: AssistantCommand) => {
    setDraft(`/${command.name} `)
    setCommandMenuDismissed(true)
    requestAnimationFrame(() => {
      textareaRef.current?.focus()
      textareaRef.current?.setSelectionRange(command.name.length + 2, command.name.length + 2)
    })
  }

  const openEffortPicker = () => {
    if (pendingEffortPicker) return
    const position = nextTimelinePosition()
    effortPickerConsumedRef.current = false
    setEffortIndex(reasoningEfforts.indexOf(defaultReasoningEffort))
    setPendingEffortPicker({
      picker_id: `effort-picker-${position.order}`,
      created_at: position.created_at,
      order: position.order,
    })
  }

  const confirmReasoningEffort = (effort: typeof reasoningEfforts[number]) => {
    if (!pendingEffortPicker || submitMutation.isPending || effortPickerConsumedRef.current) return
    effortPickerConsumedRef.current = true
    setEffortIndex(reasoningEfforts.indexOf(effort))
    setPendingEffortPicker(null)
    submitMutation.mutate(`/effort ${effort}`)
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const content = draft.trim()
    if (!content || submitMutation.isPending) return
    if (!usingLegacyV1 && /^\/effort$/i.test(content)) {
      setDraft('')
      setCommandMenuDismissed(false)
      openEffortPicker()
      return
    }
    submitMutation.mutate(content)
  }

  const onEffortPickerKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      const delta = event.key === 'ArrowRight' ? 1 : -1
      setEffortIndex((index) => (index + delta + reasoningEfforts.length) % reasoningEfforts.length)
      return
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      confirmReasoningEffort(activeReasoningEffort)
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      setPendingEffortPicker(null)
    }
  }

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isComposingRef.current) return
    if (event.key === 'Enter' && event.ctrlKey) {
      event.preventDefault()
      const textarea = event.currentTarget
      const start = textarea.selectionStart ?? draft.length
      const end = textarea.selectionEnd ?? start
      setDraft(`${draft.slice(0, start)}\n${draft.slice(end)}`)
      requestAnimationFrame(() => textareaRef.current?.setSelectionRange(start + 1, start + 1))
      return
    }
    if (!usingLegacyV1 && event.key === 'Enter' && /^\/effort\s*$/i.test(draft)) {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
      return
    }
    if (commandMenuOpen && commandOptions.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        const delta = event.key === 'ArrowDown' ? 1 : -1
        setCommandIndex((index) => (index + delta + commandOptions.length) % commandOptions.length)
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setCommandMenuDismissed(true)
        return
      }
      if (event.key === 'Enter' && activeCommand) {
        event.preventDefault()
        chooseCommand(activeCommand)
        return
      }
    }
    if (event.key === 'Enter') {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  const error = submitMutation.error
    ?? cancelMutation.error
    ?? clarificationMutation.error
    ?? approvalMutation.error
  const messages = useMemo(() => {
    const values = new Map((selectedConversation?.messages ?? []).map((message) => [message.message_id, message]))
    for (const message of localMessages) values.set(message.message_id, message)
    // QA results belong to the Run below their originating user message. Rendering their
    // persisted assistant message separately would show the same answer twice.
    return [...values.values()]
      .filter((message) => message.role !== 'assistant' || message.run_id === null)
      .sort((left, right) => left.created_at.localeCompare(right.created_at))
  }, [localMessages, selectedConversation?.messages])

  const timeline = useMemo<TimelineItem[]>(() => [
    ...messages.map((message, index) => ({
      kind: 'message' as const,
      id: message.message_id,
      created_at: message.created_at,
      order: localMessageOrdersRef.current.get(message.message_id) ?? index,
      message,
    })),
    ...commandNotices.map((notice) => ({
      kind: 'command_notice' as const,
      id: notice.notice_id,
      created_at: notice.created_at,
      order: notice.order,
      notice,
    })),
    ...(pendingEffortPicker
      ? [{
          kind: 'effort_picker' as const,
          id: pendingEffortPicker.picker_id,
          created_at: pendingEffortPicker.created_at,
          order: pendingEffortPicker.order,
          picker: pendingEffortPicker,
        }]
      : []),
  ].sort((left, right) => (
    left.created_at.localeCompare(right.created_at) || left.order - right.order
  )), [commandNotices, messages, pendingEffortPicker])

  useEffect(() => {
    if (timeline.length === 0) return
    const frame = requestAnimationFrame(() => {
      const thread = threadRef.current
      if (!thread) return
      thread.scrollTop = thread.scrollHeight
      const last = thread.lastElementChild
      if (last instanceof HTMLElement && typeof last.scrollIntoView === 'function') {
        last.scrollIntoView({ block: 'end' })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [error, timeline.length])

  const currentRunIsActive = currentRun ? activeStatuses.has(currentRun.status) : false
  const visibleCitations = evidenceRun?.citations ?? []
  const openEvidence = (runId: string) => {
    evidenceUserClosedRef.current = false
    setEvidenceRunId(runId)
    setSelectedEvidenceId(null)
  }
  const closeEvidence = () => {
    evidenceUserClosedRef.current = true
    setEvidenceRunId(null)
    setSelectedEvidenceId(null)
  }

  return (
    <section className={`qa-layout chat-layout${evidenceRunId ? ' chat-layout-with-evidence' : ''}`} aria-label="对话工作区">
      <div className="qa-conversation chat-conversation">
        <div ref={threadRef} className="qa-thread chat-thread" data-empty={timeline.length === 0} aria-live="polite">
          {timeline.length === 0 ? (
            <div className="qa-empty"><MessageSquareText size={28} aria-hidden="true" /><strong>开始对话</strong></div>
          ) : timeline.map((item) => {
            if (item.kind === 'command_notice') {
              const commandNotice = item.notice
              return (
                <article key={item.id} className="chat-command-notice" role="status">
                  <CircleHelp size={17} aria-hidden="true" />
                  <div>
                    <strong>/{commandNotice.command}</strong>
                    {commandNotice.content && <p>{commandNotice.content}</p>}
                    {commandNotice.commands.length > 0 && (
                      <ul className="chat-command-results" aria-label="Available commands">
                        {commandNotice.commands.map((command) => (
                          <li key={command.name}>
                            <div className="chat-command-result-heading">
                              <code>/{command.name}</code>
                              {command.aliases.map((alias) => <code key={alias}>/{alias}</code>)}
                            </div>
                            <span>{command.description}</span>
                            {command.argument_hint && <small>{command.argument_hint}</small>}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </article>
              )
            }
            if (item.kind === 'effort_picker') {
              return (
                <article
                  ref={effortPickerRef}
                  key={item.id}
                  className="chat-command-notice chat-effort-picker"
                  role="listbox"
                  aria-label="Reasoning effort options"
                  tabIndex={-1}
                  onKeyDown={onEffortPickerKeyDown}
                >
                  <CircleHelp size={17} aria-hidden="true" />
                  <div>
                    <strong>/effort</strong>
                    <p>选择默认推理强度</p>
                    <div className="chat-effort-options chat-effort-picker-options" role="group" aria-label="Reasoning effort">
                      {reasoningEfforts.map((effort) => (
                        <button
                          key={effort}
                          id={`assistant-effort-${effort}`}
                          type="button"
                          role="option"
                          aria-selected={effort === activeReasoningEffort}
                          onClick={() => confirmReasoningEffort(effort)}
                        >
                          <span>{effort}</span>
                          {effort === defaultReasoningEffort && <small>(default)</small>}
                        </button>
                      ))}
                    </div>
                  </div>
                </article>
              )
            }
            const message = item.message
            const run = message.role === 'user' ? runsByMessage.get(message.message_id) : undefined
            const legacyRun = run ? legacyRunsById.get(run.run_id) : undefined
            const qaRun = run?.run_id === currentRun?.run_id ? currentQARunQuery.data ?? legacyRun : legacyRun
            // New Skills expose only the parent finalizer message. The legacy
            // knowledge_qa projection remains readable for historical v1 Runs.
            const answer = run?.assistant_message?.content ?? (qaRun?.skill?.name === 'knowledge_qa' && qaRun?.result
              ? qaRun.result.text ?? qaRun.result.message ?? null
              : null)
            const limitations = qaRun?.result?.limitations ?? []
            const hasGroundedEvidence = Boolean(qaRun?.result && (qaRun.citations?.length ?? 0) > 0)
            return (
              <div key={message.message_id} className={`chat-message-group chat-message-group-${message.role}`}>
                <div
                  className={`qa-message chat-message ${message.role === 'user' ? 'qa-message-user' : 'chat-message-assistant'}`}
                >
                  {message.role === 'user'
                    ? <p><CommandText value={message.content} commands={commandsQuery.data ?? []} /></p>
                    : <RenderedAssistantAnswer content={message.content} />}
                </div>
                {run && (
                  usesAgentLoop(run) ? (
                    <>
                      <SkillRunCard
                        run={run}
                        result={qaRun?.result ?? null}
                        hasGroundedEvidence={hasGroundedEvidence}
                        clarificationPending={clarificationMutation.isPending}
                        onSelectClarification={(candidateId) => clarificationMutation.mutate({ run, candidateId })}
                        onOpenEvidence={openEvidence}
                        approvalBusy={approvalMutation.isPending}
                        onDecideApproval={(approvalId, approved, alwaysAllow) => approvalMutation.mutate({
                          runId: run.run_id,
                          approvalId,
                          approved,
                          alwaysAllow,
                        })}
                      />
                      {answer && (
                        <article className="chat-final-answer" data-status={run.status}>
                          <div className="qa-run-heading"><Check size={17} aria-hidden="true" /><strong>最终回答</strong></div>
                          <div className="qa-answer"><RenderedAssistantAnswer content={answer} limitations={limitations} /></div>
                          {hasGroundedEvidence && (
                            <button className="chat-evidence-button" type="button" onClick={() => openEvidence(run.run_id)}>
                              <Quote size={15} aria-hidden="true" />查看引用证据
                            </button>
                          )}
                        </article>
                      )}
                    </>
                  ) : (
                    <article className="chat-run" data-status={run.status}>
                      <div className="qa-run-heading">
                        {activeStatuses.has(run.status) ? <LoaderCircle className="spin" size={17} aria-hidden="true" /> : run.status === 'failed' || run.status === 'timed_out' ? <AlertCircle size={17} aria-hidden="true" /> : <Check size={17} aria-hidden="true" />}
                        <strong>{statusLabel(run.status)}</strong>
                      </div>
                      {answer && <div className="qa-answer"><RenderedAssistantAnswer content={answer} limitations={limitations} /></div>}
                      {hasGroundedEvidence && <button className="chat-evidence-button" type="button" onClick={() => openEvidence(run.run_id)}><Quote size={15} aria-hidden="true" />查看引用证据</button>}
                      {run.clarification && (
                        <div className="chat-clarification">
                          <p>{run.clarification.message}</p>
                          {run.clarification.resource_candidates.length > 0 && (
                            <div role="group" aria-label="资源选择">
                              {run.clarification.resource_candidates.map((candidate) => (
                                <button
                                  key={candidate.candidate_id}
                                  type="button"
                                  disabled={clarificationMutation.isPending}
                                  onClick={() => clarificationMutation.mutate({ run, candidateId: candidate.candidate_id })}
                                >
                                  <FileText size={16} aria-hidden="true" />
                                  <span><strong>{candidate.label}</strong>{candidate.source_label && <small>{candidate.source_label}</small>}</span>
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      {run.error_code && <code>{run.error_code}</code>}
                    </article>
                  )
                )}
              </div>
            )
          })}
          {error && <div className="qa-error" role="alert"><AlertCircle size={18} aria-hidden="true" /><span>{error instanceof Error ? error.message : '操作失败'}</span></div>}
        </div>

        <form className="qa-composer chat-composer" onSubmit={onSubmit}>
          <label htmlFor="qa-question">消息</label>
          <div className="chat-composer-editor">
            {commandPrefix && (
              <div ref={commandHighlightRef} className="chat-composer-highlight" aria-hidden="true">
                <span>{commandPrefix.leading}</span>
                <span className="chat-command-token">{commandPrefix.command}</span>
                <span>{commandPrefix.trailing}</span>
              </div>
            )}
            <textarea
            id="qa-question"
            ref={textareaRef}
            className={commandPrefix ? 'chat-composer-textarea-highlighted' : undefined}
            value={draft}
            role={usingLegacyV1 ? undefined : 'combobox'}
            aria-autocomplete={usingLegacyV1 ? undefined : 'list'}
            aria-expanded={usingLegacyV1 ? undefined : commandMenuOpen}
            aria-controls={usingLegacyV1 ? undefined : 'assistant-command-listbox'}
            aria-activedescendant={usingLegacyV1
              ? undefined
              : !commandMenuOpen || !activeCommand
                ? undefined
                : `assistant-command-${activeCommand.name}`}
            onChange={(event) => { setDraft(event.target.value); setCommandMenuDismissed(false) }}
            onCompositionStart={() => { isComposingRef.current = true }}
            onCompositionEnd={() => { isComposingRef.current = false }}
            onScroll={(event) => {
              if (!commandHighlightRef.current) return
              commandHighlightRef.current.scrollTop = event.currentTarget.scrollTop
              commandHighlightRef.current.scrollLeft = event.currentTarget.scrollLeft
            }}
            onKeyDown={onComposerKeyDown}
            placeholder="输入消息"
            rows={3}
            maxLength={12_000}
            disabled={submitMutation.isPending}
            />
          </div>
          {commandMenuOpen && commandOptions.length > 0 && (
            <div className="chat-command-menu" id="assistant-command-listbox" role="listbox" aria-label="可用指令">
              {commandOptions.map((command, index) => (
                <button
                  id={`assistant-command-${command.name}`}
                  key={command.name}
                  type="button"
                  role="option"
                  aria-selected={index === commandIndex}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => chooseCommand(command)}
                >
                  <span>/{command.name}</span><small>{command.description}</small>
                </button>
              ))}
            </div>
          )}
          <div className="qa-composer-actions">
            <span>{draft.length.toLocaleString('zh-CN')} / 12,000</span>
            <div>
              {currentRunIsActive && (
                <button className="qa-cancel-button icon-button" type="button" onClick={() => cancelMutation.mutate()} disabled={cancelMutation.isPending} aria-label="取消当前运行" title="取消当前运行">
                  <Square size={15} fill="currentColor" aria-hidden="true" />
                </button>
              )}
              <button className="qa-send-button" type="submit" disabled={!draft.trim() || submitMutation.isPending}>
                {submitMutation.isPending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}发送
              </button>
            </div>
          </div>
        </form>
      </div>

      {evidenceRunId && (
        <aside className="qa-evidence" aria-labelledby="qa-evidence-title">
          <div className="qa-evidence-heading"><Quote size={18} aria-hidden="true" /><h2 id="qa-evidence-title">引用证据</h2><span className="qa-evidence-count">{visibleCitations.length}</span><button className="qa-evidence-close" type="button" onClick={closeEvidence} aria-label="关闭证据栏" title="关闭证据栏"><X size={16} aria-hidden="true" /></button></div>
          <div className="qa-evidence-content">
            <div className="qa-citation-list">
              {evidenceRunQuery.isPending && <div className="qa-evidence-empty"><LoaderCircle className="spin" size={22} aria-hidden="true" /><span>正在加载引用证据</span></div>}
              {evidenceRunQuery.error && <div className="qa-evidence-empty" role="alert"><AlertCircle size={22} aria-hidden="true" /><span>引用证据暂不可用</span></div>}
              {visibleCitations.map((citation) => {
                const metadata = citationMetadataQuery.data?.[citationKey(citation.source_id, citation.document_id)]
                const documentName = metadata?.displayName ?? `文档 ${citation.document_id.slice(0, 8)}`
                return (
                  <button className="qa-citation" data-selected={selectedEvidenceId === citation.evidence_id} key={citation.evidence_id} type="button" aria-pressed={selectedEvidenceId === citation.evidence_id} onClick={() => setSelectedEvidenceId((current) => current === citation.evidence_id ? null : citation.evidence_id)}>
                    <FileText size={17} aria-hidden="true" /><span className="qa-citation-copy"><strong>{documentName}</strong><span>{metadata?.uri ?? `来源 ${citation.source_id.slice(0, 8)}`}</span><span>{citation.locator.kind} {citation.locator.start}-{citation.locator.end}</span><code>版本 {citation.version_id.slice(0, 8)}</code></span><BookOpenText size={16} aria-hidden="true" /><span className="sr-only">查看原文：{documentName}</span>
                  </button>
                )
              })}
            </div>
            {selectedEvidenceId && (
              <div className="qa-excerpt" ref={excerptRef} tabIndex={-1} aria-live="polite">
                <button className="qa-excerpt-close" type="button" onClick={() => setSelectedEvidenceId(null)} aria-label="关闭原文" title="关闭原文"><X size={16} aria-hidden="true" /></button>
                {citationQuery.isPending ? <LoaderCircle className="spin" size={18} aria-label="正在加载原文" /> : citationQuery.error ? <div className="qa-excerpt-status" role="alert"><AlertCircle size={18} aria-hidden="true" /><span>{citationQuery.error instanceof Error ? citationQuery.error.message : '原文加载失败'}</span><button type="button" onClick={() => void citationQuery.refetch()}><RotateCcw size={14} aria-hidden="true" />重试</button></div> : citationQuery.data?.excerpt ? <><div className="qa-excerpt-heading"><strong>原文</strong><span>{citationQuery.data.locator.kind} {citationQuery.data.locator.start}-{citationQuery.data.locator.end}</span></div><pre><mark>{citationQuery.data.excerpt}</mark></pre></> : <div className="qa-excerpt-status"><AlertCircle size={18} aria-hidden="true" /><span>固定版本当前不可用</span></div>}
              </div>
            )}
          </div>
        </aside>
      )}
    </section>
  )
}
