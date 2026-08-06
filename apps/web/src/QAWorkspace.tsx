import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  BookOpenText,
  Check,
  ChevronDown,
  CircleHelp,
  FileText,
  Info,
  LoaderCircle,
  MessageSquareText,
  Quote,
  RotateCcw,
  Send,
  Square,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  cancelAssistantRun,
  createConversation,
  fetchAssistantConversationRuns,
  fetchAssistantCommands,
  fetchCitationExcerpt,
  fetchConversationHistory,
  fetchRun,
  isAssistantRun,
  selectClarificationResource,
  submitAssistantTurn,
  type AssistantCommand,
  type AssistantCommandResult,
  type AssistantRun,
  type ConversationHistoryItem,
} from './qa'
import { fetchSourceDetail } from './sources'

export type QAWorkspaceProps = {
  selectedConversationId?: string | null
  onConversationSelected?: (conversationId: string | null) => void
}

type CitationMetadata = {
  displayName: string | null
  uri: string | null
}

type CommandNotice = Pick<AssistantCommandResult, 'command' | 'content' | 'commands'>

const activeStatuses = new Set(['created', 'queued', 'running', 'cancel_requested'])

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

function runKindLabel(kind: AssistantRun['run_kind']): string {
  const labels: Record<AssistantRun['run_kind'], string> = {
    assistant_turn: '对话',
    grounded_qa: '检索回答',
    skill: 'Skill',
    context_compaction: '上下文压缩',
  }
  return labels[kind]
}

function citationKey(sourceId: string, documentId: string): string {
  return `${sourceId}:${documentId}`
}

function isGroundedRun(run: AssistantRun): boolean {
  return run.run_kind === 'grounded_qa' || run.run_kind === 'skill'
}

function commandMatches(command: AssistantCommand, needle: string): boolean {
  const normalized = needle.trim().toLocaleLowerCase()
  if (!normalized) return true
  return [command.name, ...command.aliases, command.description]
    .some((value) => value.toLocaleLowerCase().includes(normalized))
}

function RunDetails({ run }: { run: AssistantRun }) {
  return (
    <details className="chat-run-details">
      <summary><Info size={14} aria-hidden="true" />运行信息<ChevronDown size={14} aria-hidden="true" /></summary>
      <dl>
        <div><dt>类型</dt><dd>{runKindLabel(run.run_kind)}</dd></div>
        <div><dt>模型</dt><dd>{run.model_identity}</dd></div>
        <div><dt>Token</dt><dd>{run.usage.input_tokens} 输入 / {run.usage.output_tokens} 输出</dd></div>
        <div><dt>耗时</dt><dd>{Math.round(run.usage.model_latency_ms)} ms</dd></div>
      </dl>
    </details>
  )
}

export function QAWorkspace({
  selectedConversationId,
  onConversationSelected,
}: QAWorkspaceProps = {}) {
  const [draft, setDraft] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [localMessages, setLocalMessages] = useState<ConversationHistoryItem['messages']>([])
  const [localRuns, setLocalRuns] = useState<AssistantRun[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const [commandIndex, setCommandIndex] = useState(0)
  const [commandMenuDismissed, setCommandMenuDismissed] = useState(false)
  const [commandNotice, setCommandNotice] = useState<CommandNotice | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const isComposingRef = useRef(false)
  const excerptRef = useRef<HTMLDivElement>(null)
  const historyInitializedRef = useRef(false)
  const queryClient = useQueryClient()

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
  })
  const assistantRunsQuery = useQuery({
    queryKey: ['assistant-runs', conversationId],
    queryFn: ({ signal }) => fetchAssistantConversationRuns(conversationId!, signal),
    enabled: Boolean(conversationId),
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.some((run) => activeStatuses.has(run.status)) ? 2_000 : false,
  })

  const conversations = historyQuery.data?.conversations ?? []
  const selectedConversation = conversations.find(
    (conversation) => conversation.conversation_id === conversationId,
  )
  const runs = useMemo(() => {
    const values = new Map((assistantRunsQuery.data ?? []).map((run) => [run.run_id, run]))
    for (const run of localRuns) values.set(run.run_id, run)
    return [...values.values()]
  }, [assistantRunsQuery.data, localRuns])
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
    enabled: Boolean(currentRun && isGroundedRun(currentRun)),
    initialData: currentLegacyRun,
    retry: false,
    refetchInterval: (query) => activeStatuses.has(query.state.data?.status ?? '') ? 2_000 : false,
  })
  const currentEvidenceRun = currentQARunQuery.data ?? currentLegacyRun
  const citationSourceIds = useMemo(
    () => [...new Set(currentEvidenceRun?.citations?.map((citation) => citation.source_id) ?? [])],
    [currentEvidenceRun?.citations],
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
    queryKey: ['qa-citation', currentEvidenceRun?.run_id, selectedEvidenceId],
    queryFn: ({ signal }) => fetchCitationExcerpt(currentEvidenceRun!.run_id, selectedEvidenceId!, signal),
    enabled: Boolean(currentEvidenceRun?.run_id && selectedEvidenceId),
    retry: false,
  })

  const trimmed = draft.trimStart()
  const commandToken = trimmed.startsWith('/') && !trimmed.startsWith('//')
    ? trimmed.slice(1).split(/\s/, 1)[0]
    : ''
  const commandOptions = useMemo(
    () => (commandsQuery.data ?? []).filter((command) => commandMatches(command, commandToken)),
    [commandToken, commandsQuery.data],
  )
  const commandMenuOpen = trimmed.startsWith('/') && !trimmed.startsWith('//') && !commandMenuDismissed
  const activeCommand = commandOptions[commandIndex]

  useEffect(() => {
    if (commandIndex >= commandOptions.length) setCommandIndex(0)
  }, [commandIndex, commandOptions.length])

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
      setSelectedEvidenceId(null)
      setCommandNotice(null)
    }
  }, [conversationId, historyQuery.data, onConversationSelected, selectedConversationId])

  useEffect(() => {
    if (currentRun && !activeStatuses.has(currentRun.status)) {
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
    }
  }, [currentRun, queryClient])

  useEffect(() => {
    if (selectedEvidenceId && (citationQuery.data || citationQuery.error)) excerptRef.current?.focus()
  }, [citationQuery.data, citationQuery.error, selectedEvidenceId])

  const submitMutation = useMutation({
    mutationFn: async (content: string) => {
      let targetConversationId = conversationId
      if (!targetConversationId) {
        const created = await createConversation()
        targetConversationId = created.conversation_id
      }
      const result = await submitAssistantTurn(targetConversationId, content, crypto.randomUUID())
      return { content, result, targetConversationId }
    },
    onSuccess: ({ content, result, targetConversationId }) => {
      if (isAssistantRun(result)) {
        setLocalRuns((current) => [...current.filter((run) => run.run_id !== result.run_id), result])
        setActiveRunId(result.run_id)
        setLocalMessages((current) => [
          ...current.filter((message) => message.message_id !== result.user_message_id),
          {
            message_id: result.user_message_id,
            role: 'user',
            content,
            run_id: null,
            created_at: new Date().toISOString(),
          },
        ])
        setConversationId(targetConversationId)
        onConversationSelected?.(targetConversationId)
      } else {
        setCommandNotice({ command: result.command, content: result.content, commands: result.commands })
        const nextConversationId = result.conversation_id ?? targetConversationId
        if (result.run) {
          const run = result.run
          setLocalRuns((current) => [
            ...current.filter((item) => item.run_id !== run.run_id),
            run,
          ])
          setActiveRunId(run.run_id)
        } else {
          setLocalRuns([])
          setActiveRunId(null)
        }
        setConversationId(nextConversationId)
        onConversationSelected?.(nextConversationId)
      }
      setSelectedEvidenceId(null)
      setDraft('')
      setCommandMenuDismissed(false)
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
      void queryClient.invalidateQueries({ queryKey: ['assistant-runs'] })
    },
  })
  const cancelMutation = useMutation({
    mutationFn: () => cancelAssistantRun(currentRun!.run_id),
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

  const chooseCommand = (command: AssistantCommand) => {
    setDraft(`/${command.name} `)
    setCommandMenuDismissed(true)
    requestAnimationFrame(() => {
      textareaRef.current?.focus()
      textareaRef.current?.setSelectionRange(command.name.length + 2, command.name.length + 2)
    })
  }

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const content = draft.trim()
    if (!content || submitMutation.isPending) return
    submitMutation.mutate(content)
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

  const error = submitMutation.error ?? cancelMutation.error ?? clarificationMutation.error
  const messages = useMemo(() => {
    const values = new Map((selectedConversation?.messages ?? []).map((message) => [message.message_id, message]))
    for (const message of localMessages) values.set(message.message_id, message)
    return [...values.values()].sort((left, right) => left.created_at.localeCompare(right.created_at))
  }, [localMessages, selectedConversation?.messages])
  const currentRunIsActive = currentRun ? activeStatuses.has(currentRun.status) : false
  const visibleCitations = currentEvidenceRun?.citations ?? []

  return (
    <section className={`qa-layout chat-layout${visibleCitations.length > 0 ? ' chat-layout-with-evidence' : ''}`} aria-label="对话工作区">
      <div className="qa-conversation chat-conversation">
        <div className="qa-thread chat-thread" data-empty={messages.length === 0 && !commandNotice} aria-live="polite">
          {commandNotice && (
            <article className="chat-command-notice" role="status">
              <CircleHelp size={17} aria-hidden="true" />
              <div><strong>/{commandNotice.command}</strong>{commandNotice.content && <p>{commandNotice.content}</p>}</div>
            </article>
          )}
          {messages.length === 0 && !commandNotice ? (
            <div className="qa-empty"><MessageSquareText size={28} aria-hidden="true" /><strong>开始对话</strong></div>
          ) : messages.map((message) => {
            const run = message.role === 'user' ? runsByMessage.get(message.message_id) : undefined
            const legacyRun = run ? legacyRunsById.get(run.run_id) : undefined
            const qaRun = run?.run_id === currentRun?.run_id ? currentQARunQuery.data ?? legacyRun : legacyRun
            const hasStoredAssistant = run?.assistant_message && messages.some(
              (item) => item.message_id === run.assistant_message?.message_id,
            )
            return (
              <div key={message.message_id} className="chat-message-group">
                <button
                  className={`qa-message chat-message ${message.role === 'user' ? 'qa-message-user' : 'chat-message-assistant'}`}
                  type="button"
                  onClick={() => run && setActiveRunId(run.run_id)}
                  disabled={!run}
                >
                  <span className="qa-message-label">{message.role === 'user' ? '你' : '助手'}</span>
                  <p>{message.content}</p>
                </button>
                {run && (
                  <article className="chat-run" data-selected={run.run_id === currentRun?.run_id} data-status={run.status}>
                    <div className="qa-run-heading">
                      {activeStatuses.has(run.status) ? <LoaderCircle className="spin" size={17} aria-hidden="true" /> : run.status === 'failed' || run.status === 'timed_out' ? <AlertCircle size={17} aria-hidden="true" /> : <Check size={17} aria-hidden="true" />}
                      <strong>{statusLabel(run.status)}</strong>
                      <span>{runKindLabel(run.run_kind)}</span>
                    </div>
                    {run.assistant_message && !hasStoredAssistant && <div className="qa-answer"><p>{run.assistant_message.content}</p></div>}
                    {qaRun?.result && (
                      <div className="qa-answer">
                        <p>{qaRun.result.text ?? qaRun.result.message}</p>
                        {qaRun.result.limitations?.map((limitation) => <small key={limitation}>{limitation}</small>)}
                      </div>
                    )}
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
                    <RunDetails run={run} />
                  </article>
                )}
              </div>
            )
          })}
          {error && <div className="qa-error" role="alert"><AlertCircle size={18} aria-hidden="true" /><span>{error instanceof Error ? error.message : '操作失败'}</span></div>}
        </div>

        <form className="qa-composer chat-composer" onSubmit={onSubmit}>
          <label htmlFor="qa-question">消息</label>
          <textarea
            id="qa-question"
            ref={textareaRef}
            value={draft}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={commandMenuOpen}
            aria-controls="assistant-command-listbox"
            aria-activedescendant={commandMenuOpen && activeCommand ? `assistant-command-${activeCommand.name}` : undefined}
            onChange={(event) => { setDraft(event.target.value); setCommandMenuDismissed(false) }}
            onCompositionStart={() => { isComposingRef.current = true }}
            onCompositionEnd={() => { isComposingRef.current = false }}
            onKeyDown={onComposerKeyDown}
            placeholder="输入消息"
            rows={3}
            maxLength={12_000}
            disabled={submitMutation.isPending}
          />
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

      {visibleCitations.length > 0 && (
        <aside className="qa-evidence" aria-labelledby="qa-evidence-title">
          <div className="qa-evidence-heading"><Quote size={18} aria-hidden="true" /><h2 id="qa-evidence-title">引用证据</h2><span className="qa-evidence-count">{visibleCitations.length}</span></div>
          <div className="qa-evidence-content">
            <div className="qa-citation-list">
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
