import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  Bot,
  BookOpenText,
  FileText,
  LoaderCircle,
  MessageSquareText,
  Quote,
  RotateCcw,
  Send,
  Search,
  ShieldCheck,
  Square,
  ThumbsDown,
  ThumbsUp,
  Workflow,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import {
  cancelRun,
  createReviewCards,
  decideApproval,
  createConversation,
  fetchCitationExcerpt,
  fetchConversationHistory,
  fetchRun,
  fetchSkills,
  requestApproval,
  resumeRun,
  submitQuestion,
  submitOrganizationSkill,
  submitFeedback,
  type FeedbackDecision,
  type QASkillName,
  type QARun,
  type ConversationHistoryItem,
} from './qa'
import { fetchSourceDetail, fetchSources, type SourceDetail } from './sources'

export type QAWorkspaceProps = {
  selectedConversationId?: string | null
  onConversationSelected?: (conversationId: string | null) => void
}

type LocalQuestion = {
  id: string
  text: string
  run: QARun
}

type CitationMetadata = {
  displayName: string | null
  uri: string | null
}

const resumableStatuses = new Set(['created', 'queued', 'running', 'verifying', 'cancel_requested'])
const activeStatuses = new Set(['created', 'queued', 'running', 'verifying', 'cancel_requested'])

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    created: '已创建',
    queued: '等待执行',
    running: '正在检索与生成',
    verifying: '正在校验证据',
    cancel_requested: '正在取消',
    cancelled: '已取消',
    completed: '已完成',
    refused: '证据不足',
    failed: '运行失败',
    timed_out: '运行超时',
  }
  return labels[status] ?? status
}

function citationStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    source_updated: '来源已更新，显示的是回答时固定版本的原文',
    withdrawn: '来源已撤回，原文不可再访问',
    deleted: '文档已删除，原文不可再访问',
    retention_expired: '固定版本已超出保留期限',
    unavailable: '固定版本或分块当前不可用',
    invalid: '引用内容与固定版本校验不一致',
  }
  return labels[status] ?? `原文当前不可用（${status}）`
}

function questionsFromConversation(conversation: ConversationHistoryItem): LocalQuestion[] {
  return conversation.messages.flatMap((message) => {
    if (message.role !== 'user') return []
    const run = conversation.runs.find((candidate) => candidate.question_message_id === message.message_id)
    return run ? [{ id: message.message_id, text: message.content, run }] : []
  })
}

function citationKey(sourceId: string, documentId: string): string {
  return `${sourceId}:${documentId}`
}

export function QAWorkspace({
  selectedConversationId,
  onConversationSelected,
}: QAWorkspaceProps = {}) {
  const [draft, setDraft] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [selectedQuestionId, setSelectedQuestionId] = useState<string | null>(null)
  const [localQuestions, setLocalQuestions] = useState<LocalQuestion[]>([])
  const [recoveredQuestionId, setRecoveredQuestionId] = useState<string | null>(null)
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const [selectedSkill, setSelectedSkill] = useState<QASkillName>('knowledge_agent')
  const [selectedSourceId, setSelectedSourceId] = useState('')
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([])
  const [selectedDocumentId, setSelectedDocumentId] = useState('')
  const [focus, setFocus] = useState('')
  const [approval, setApproval] = useState<Awaited<ReturnType<typeof requestApproval>> | null>(null)
  const [feedbackDecision, setFeedbackDecision] = useState<FeedbackDecision | null>(null)
  const [feedbackNote, setFeedbackNote] = useState('')
  const excerptRef = useRef<HTMLDivElement>(null)
  const historyInitializedRef = useRef(false)
  const queryClient = useQueryClient()


  const skillsQuery = useQuery({
    queryKey: ['skills'],
    queryFn: ({ signal }) => fetchSkills(signal),
    staleTime: 60_000,
    retry: false,
  })

  const historyQuery = useQuery({
    queryKey: ['qa-history'],
    queryFn: ({ signal }) => fetchConversationHistory(signal),
    retry: false,
    enabled: selectedConversationId !== null,
  })

  const conversations = Array.isArray(historyQuery.data?.conversations)
    ? historyQuery.data.conversations
    : []
  const selectedConversation = conversations.find(
    (conversation) => conversation.conversation_id === conversationId,
  )
  const storedQuestions = useMemo(
    () => (selectedConversation ? questionsFromConversation(selectedConversation) : []),
    [selectedConversation],
  )
  const visibleQuestions = useMemo(() => {
    const byId = new Map(storedQuestions.map((question) => [question.id, question]))
    for (const question of localQuestions) byId.set(question.id, question)
    return [...byId.values()]
  }, [localQuestions, storedQuestions])
  const currentQuestion =
    visibleQuestions.find((question) => question.id === selectedQuestionId) ??
    visibleQuestions.at(-1)

  useEffect(() => {
    if (selectedConversationId === undefined) {
      if (historyInitializedRef.current || !historyQuery.data) return
      historyInitializedRef.current = true
      const newestConversation = historyQuery.data.conversations?.[0]
      if (!newestConversation) return
      const newestQuestion = questionsFromConversation(newestConversation).at(-1)
      setConversationId(newestConversation.conversation_id)
      setSelectedQuestionId(newestQuestion?.id ?? null)
      setRecoveredQuestionId(newestQuestion?.id ?? null)
      onConversationSelected?.(newestConversation.conversation_id)
      return
    }

    if (conversationId !== selectedConversationId) {
      setConversationId(selectedConversationId)
      setSelectedQuestionId(null)
      setLocalQuestions([])
      setRecoveredQuestionId(null)
      setApproval(null)
      setFeedbackDecision(null)
      setFeedbackNote('')
      setSelectedEvidenceId(null)
      return
    }

    if (selectedQuestionId === null && selectedConversation) {
      const newestQuestion = questionsFromConversation(selectedConversation).at(-1)
      setSelectedQuestionId(newestQuestion?.id ?? null)
      setRecoveredQuestionId(newestQuestion?.id ?? null)
    }
  }, [
    conversationId,
    historyQuery.data,
    onConversationSelected,
    selectedConversation,
    selectedConversationId,
    selectedQuestionId,
  ])

  const runQuery = useQuery({
    queryKey: ['qa-run', currentQuestion?.run.run_id],
    queryFn: ({ signal }) => fetchRun(currentQuestion!.run.run_id, signal),
    enabled: currentQuestion !== undefined,
    initialData: currentQuestion?.run,
    refetchInterval: (query) =>
      activeStatuses.has(query.state.data?.status ?? '') ? 2_000 : false,
  })

  const currentRun = runQuery.data ?? currentQuestion?.run
  useEffect(() => {
    const skillName = currentRun?.skill?.name
    if (
      skillName === 'knowledge_agent' ||
      skillName === 'knowledge_qa' ||
      skillName === 'summarize_document' ||
      skillName === 'compare_sources' ||
      skillName === 'create_review_cards'
    ) {
      setSelectedSkill(skillName)
    }
  }, [currentRun?.skill?.name])

  const activeSkill = skillsQuery.data?.find((skill) => skill.name === selectedSkill)
  const isDocumentSkill = selectedSkill === 'summarize_document' || selectedSkill === 'create_review_cards'
  const isOrganizationSkill = isDocumentSkill || selectedSkill === 'compare_sources'
  const sourcesQuery = useQuery({
    queryKey: ['sources'],
    queryFn: ({ signal }) => fetchSources(signal),
    enabled: isOrganizationSkill,
    retry: false,
  })
  const sourceDetailQuery = useQuery<SourceDetail>({
    queryKey: ['source', selectedSourceId],
    queryFn: ({ signal }) => fetchSourceDetail(selectedSourceId, signal),
    enabled: isDocumentSkill && Boolean(selectedSourceId),
    retry: false,
  })
  const displayedSkill = currentRun?.skill ?? {
    name: selectedSkill,
    version: activeSkill?.active_version ?? null,
  }
  const isActive = currentRun ? activeStatuses.has(currentRun.status) : false
  const citationSourceIds = useMemo(
    () => [...new Set(currentRun?.citations?.map((citation) => citation.source_id) ?? [])],
    [currentRun?.citations],
  )
  const citationMetadataQuery = useQuery<Record<string, CitationMetadata>>({
    queryKey: ['qa-citation-source-details', citationSourceIds],
    queryFn: async ({ signal }) => {
      const details = await Promise.all(
        citationSourceIds.map((sourceId) =>
          fetchSourceDetail(sourceId, signal).catch(() => null),
        ),
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
    queryKey: ['qa-citation', currentRun?.run_id, selectedEvidenceId],
    queryFn: ({ signal }) =>
      fetchCitationExcerpt(currentRun!.run_id, selectedEvidenceId!, signal),
    enabled: Boolean(currentRun?.run_id && selectedEvidenceId),
    retry: false,
  })

  useEffect(() => {
    if (selectedEvidenceId && (citationQuery.data || citationQuery.error)) {
      excerptRef.current?.focus()
    }
  }, [citationQuery.data, citationQuery.error, selectedEvidenceId])

  const submitMutation = useMutation({
    mutationFn: async (text: string) => {
      let currentConversationId = conversationId
      if (!currentConversationId) {
        const conversation = await createConversation()
        currentConversationId = conversation.conversation_id
        setConversationId(currentConversationId)
      }
      const idempotencyKey = crypto.randomUUID()
      const run = selectedSkill === 'create_review_cards'
        ? await createReviewCards(
            currentConversationId,
            selectedDocumentId,
            sourceDetailQuery.data?.documents.find((doc) => doc.id === selectedDocumentId)?.current_version_id ?? '',
            focus.trim() || undefined,
            idempotencyKey,
          )
        : selectedSkill === 'summarize_document'
          ? await submitOrganizationSkill(currentConversationId, selectedSkill, {
              documentId: selectedDocumentId,
              versionId: sourceDetailQuery.data?.documents.find((doc) => doc.id === selectedDocumentId)?.current_version_id ?? undefined,
              focus: focus.trim() || undefined,
              idempotencyKey,
            })
          : selectedSkill === 'compare_sources'
            ? await submitOrganizationSkill(currentConversationId, selectedSkill, {
                sourceIds: selectedSourceIds,
                focus: focus.trim() || undefined,
                idempotencyKey,
              })
        : await submitQuestion(currentConversationId, text, idempotencyKey, selectedSkill)
      return { text, run }
    },
    onSuccess: ({ text, run }) => {
      const nextQuestion = { id: run.question_message_id, text, run }
      setLocalQuestions((current) => [
        ...current.filter((question) => question.id !== nextQuestion.id),
        nextQuestion,
      ])
      setConversationId(run.conversation_id)
      onConversationSelected?.(run.conversation_id)
      setSelectedQuestionId(run.question_message_id)
      setRecoveredQuestionId(null)
      setApproval(null)
      setFeedbackDecision(null)
      setFeedbackNote('')
      setSelectedEvidenceId(null)
      setDraft('')
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
    },
  })

  const resumeMutation = useMutation({
    mutationFn: () => resumeRun(currentRun!.run_id),
    onSuccess: (run) => {
      setLocalQuestions((current) => current.map((question) =>
        question.id === run.question_message_id ? { ...question, run } : question,
      ))
      queryClient.setQueryData(['qa-run', run.run_id], run)
    },
  })

  const approvalMutation = useMutation({
    mutationFn: () => requestApproval(currentRun!.run_id, crypto.randomUUID()),
    onSuccess: setApproval,
  })
  const decisionMutation = useMutation({
    mutationFn: (approved: boolean) => decideApproval(currentRun!.run_id, approval!.approval_id, approved),
    onSuccess: setApproval,
  })

  const feedbackMutation = useMutation({
    mutationFn: ({ decision, note }: { decision: FeedbackDecision; note: string }) =>
      submitFeedback(currentRun!.run_id, decision, crypto.randomUUID(), note),
    onError: () => {
      setFeedbackDecision(null)
    },
  })

  const submitFeedbackDecision = (decision: FeedbackDecision) => {
    setFeedbackDecision(decision)
    feedbackMutation.mutate({ decision, note: feedbackNote })
  }

  const chooseSkill = (skill: QASkillName) => {
    if (isActive || submitMutation.isPending) return
    setSelectedSkill(skill)
    setSelectedSourceId('')
    setSelectedSourceIds([])
    setConversationId(null)
    setSelectedQuestionId(null)
    setLocalQuestions([])
    setRecoveredQuestionId(null)
    setApproval(null)
    setFeedbackDecision(null)
    setFeedbackNote('')
    setSelectedEvidenceId(null)
  }

  const chooseQuestion = (question: LocalQuestion) => {
    setSelectedQuestionId(question.id)
    setRecoveredQuestionId(question.id)
    setApproval(null)
    setFeedbackDecision(null)
    setFeedbackNote('')
    setSelectedEvidenceId(null)
  }

  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const cancelMutation = useMutation({
    mutationFn: async () => cancelRun(currentRun!.run_id),
    onSuccess: (run) => {
      setLocalQuestions((current) => current.map((question) =>
        question.id === run.question_message_id ? { ...question, run } : question,
      ))
      queryClient.setQueryData(['qa-run', run.run_id], run)
    },
  })

  const errorMessage = useMemo(() => {
    const error = submitMutation.error ?? runQuery.error ?? cancelMutation.error ?? resumeMutation.error ?? approvalMutation.error ?? decisionMutation.error ?? feedbackMutation.error
    return error instanceof Error ? error.message : null
  }, [approvalMutation.error, cancelMutation.error, decisionMutation.error, feedbackMutation.error, resumeMutation.error, runQuery.error, submitMutation.error])

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const text = draft.trim()
    if ((!text && !isOrganizationSkill) || submitMutation.isPending || isActive) return
    if (isDocumentSkill && (!selectedDocumentId || !sourceDetailQuery.data?.documents.find((doc) => doc.id === selectedDocumentId)?.current_version_id)) return
    if (selectedSkill === 'compare_sources' && selectedSourceIds.length < 2) return
    const submissionText = text || focus.trim() || (selectedSkill === 'compare_sources' ? '比较所选来源' : '生成文档摘要')
    submitMutation.mutate(submissionText)
  }

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter') return
    if (event.ctrlKey) {
      event.preventDefault()
      const textarea = event.currentTarget
      const start = textarea.selectionStart ?? draft.length
      const end = textarea.selectionEnd ?? start
      const nextDraft = `${draft.slice(0, start)}\n${draft.slice(end)}`
      setDraft(nextDraft)
      requestAnimationFrame(() => {
        textareaRef.current?.focus()
        textareaRef.current?.setSelectionRange(start + 1, start + 1)
      })
      return
    }
    event.preventDefault()
    event.currentTarget.form?.requestSubmit()
  }

  return (
    <section className="qa-layout" aria-label="知识问答工作区">
      <div className="qa-conversation">
        <div className="qa-thread" data-empty={!currentQuestion} aria-live="polite">
          <div className="qa-skill-context">
            <Workflow size={16} aria-hidden="true" />
            <strong>{displayedSkill.name}</strong>
            <code>{displayedSkill.version ? `v${displayedSkill.version}` : '版本不可用'}</code>
            {currentRun?.skill && <span>已固定</span>}
          </div>
          <div className="qa-mode-selector" role="group" aria-label="问答模式">
            <button type="button" aria-pressed={selectedSkill === 'knowledge_agent'} onClick={() => chooseSkill('knowledge_agent')} disabled={submitMutation.isPending || isActive}>
              <Bot size={16} aria-hidden="true" />LLM Agent
            </button>
            <button type="button" aria-pressed={selectedSkill === 'knowledge_qa'} onClick={() => chooseSkill('knowledge_qa')} disabled={submitMutation.isPending || isActive}>
              <Search size={16} aria-hidden="true" />直接问答
            </button>
            <button type="button" aria-pressed={selectedSkill === 'summarize_document'} onClick={() => chooseSkill('summarize_document')} disabled={submitMutation.isPending || isActive}>
              <FileText size={16} aria-hidden="true" />摘要文档
            </button>
            <button type="button" aria-pressed={selectedSkill === 'compare_sources'} onClick={() => chooseSkill('compare_sources')} disabled={submitMutation.isPending || isActive}>
              <BookOpenText size={16} aria-hidden="true" />比较来源
            </button>
            <button type="button" aria-pressed={selectedSkill === 'create_review_cards'} onClick={() => chooseSkill('create_review_cards')} disabled={submitMutation.isPending || isActive}>
              <Workflow size={16} aria-hidden="true" />复习卡
            </button>
          </div>
          {isDocumentSkill && !currentQuestion && (
            <div className="qa-skill-config">
              <label htmlFor="document-source">来源</label>
              <select id="document-source" value={selectedSourceId} onChange={(event) => { setSelectedSourceId(event.target.value); setSelectedDocumentId('') }}>
                <option value="">选择来源</option>
                {sourcesQuery.data?.sources.map((source) => <option key={source.id} value={source.id}>{source.uri}</option>)}
              </select>
              <label htmlFor="document-version">固定文档版本</label>
              <select id="document-version" value={selectedDocumentId} onChange={(event) => setSelectedDocumentId(event.target.value)} disabled={!sourceDetailQuery.data}>
                <option value="">选择文档</option>
                {sourceDetailQuery.data?.documents.filter((doc) => doc.current_version_id).map((doc) => <option key={doc.id} value={doc.id}>{doc.display_name}</option>)}
              </select>
              <label htmlFor="document-focus">重点（可选）</label>
              <input id="document-focus" value={focus} onChange={(event) => setFocus(event.target.value)} maxLength={1000} />
            </div>
          )}
          {selectedSkill === 'compare_sources' && !currentQuestion && (
            <div className="qa-skill-config">
              <label htmlFor="compare-sources">选择至少两个来源</label>
              <div id="compare-sources" className="qa-source-checks" role="group" aria-label="来源列表">
                {sourcesQuery.data?.sources.map((source) => (
                  <label key={source.id}>
                    <input
                      type="checkbox"
                      checked={selectedSourceIds.includes(source.id)}
                      onChange={(event) => setSelectedSourceIds((current) => event.target.checked
                        ? [...current, source.id]
                        : current.filter((id) => id !== source.id))}
                    />
                    <span>{source.uri}</span>
                  </label>
                ))}
              </div>
              <label htmlFor="compare-focus">重点（可选）</label>
              <input id="compare-focus" value={focus} onChange={(event) => setFocus(event.target.value)} maxLength={1000} />
            </div>
          )}
          {!currentQuestion ? (
            <div className="qa-empty">
              <MessageSquareText size={28} aria-hidden="true" />
              <strong>开始一次知识检索</strong>
              <span>回答将依据当前 Space 中已发布的文档。</span>
            </div>
          ) : (
            visibleQuestions.map((item) => {
              const itemRun = item.id === currentQuestion.id ? currentRun ?? item.run : item.run
              return (
                <div key={item.id} className="qa-question-group" data-selected={item.id === currentQuestion.id}>
                  <button type="button" className="qa-message qa-message-user qa-question-button" onClick={() => chooseQuestion(item)}>
                    <span className="qa-message-label">你的问题</span>
                    <p>{item.text}</p>
                  </button>
                  <article className="qa-message qa-message-system" data-status={itemRun?.status}>
                    <div className="qa-run-heading">
                      {activeStatuses.has(itemRun?.status ?? '') ? <LoaderCircle className="spin" size={18} aria-hidden="true" /> : itemRun?.status === 'failed' || itemRun?.status === 'timed_out' ? <AlertCircle size={18} aria-hidden="true" /> : <MessageSquareText size={18} aria-hidden="true" />}
                      <strong>{statusLabel(itemRun?.status ?? 'created')}</strong>
                    </div>
                    {itemRun?.error_code && <code>{itemRun.error_code}</code>}
                    {itemRun?.result && (
                      <div className="qa-answer">
                        <p>{itemRun.result.text ?? itemRun.result.message}</p>
                        {itemRun.result.limitations?.map((limitation) => <small key={limitation}>{limitation}</small>)}
                      </div>
                    )}
                    {item.id === currentQuestion.id && itemRun?.status === 'completed' && itemRun.result?.type === 'answer' && (
                      <div className="qa-feedback" aria-label="回答反馈">
                        <span>回答反馈</span>
                        <div className="qa-feedback-actions">
                          <button
                            type="button"
                            className="icon-button"
                            aria-label="回答有帮助"
                            title="回答有帮助"
                            aria-pressed={feedbackDecision === 'positive'}
                            onClick={() => submitFeedbackDecision('positive')}
                            disabled={feedbackMutation.isPending || feedbackDecision !== null}
                          >
                            <ThumbsUp size={16} aria-hidden="true" />
                          </button>
                          <button
                            type="button"
                            className="icon-button"
                            aria-label="回答需要改进"
                            title="回答需要改进"
                            aria-pressed={feedbackDecision === 'negative'}
                            onClick={() => submitFeedbackDecision('negative')}
                            disabled={feedbackMutation.isPending || feedbackDecision !== null}
                          >
                            <ThumbsDown size={16} aria-hidden="true" />
                          </button>
                        </div>
                        <textarea
                          aria-label="反馈说明（可选）"
                          value={feedbackNote}
                          onChange={(event) => setFeedbackNote(event.target.value)}
                          maxLength={2000}
                          rows={2}
                          placeholder="补充说明（可选）"
                          disabled={feedbackMutation.isPending || feedbackDecision !== null}
                        />
                        {feedbackDecision && <span className="qa-feedback-status" role="status">反馈已提交，等待审核</span>}
                      </div>
                    )}
                  </article>
                </div>
              )
            })
          )}
          {currentRun && recoveredQuestionId === currentQuestion?.id && resumableStatuses.has(currentRun.status) && (
            <button type="button" className="panel-action-button qa-resume-button" onClick={() => resumeMutation.mutate()} disabled={resumeMutation.isPending}>
              <RotateCcw size={15} />{resumeMutation.isPending ? '恢复中…' : '恢复执行'}
            </button>
          )}
          {currentRun?.skill?.name === 'create_review_cards' && currentRun.result && currentRun.citations?.length && !approval && (
            <button type="button" className="panel-action-button qa-approval-button" onClick={() => approvalMutation.mutate()} disabled={approvalMutation.isPending}>
              <ShieldCheck size={15} />申请写入审批
            </button>
          )}
          {approval && approval.status === 'pending' && (
            <div className="qa-approval-actions">
              <span>复习卡写入审批待决策</span>
              <button type="button" className="panel-action-button" onClick={() => decisionMutation.mutate(true)} disabled={decisionMutation.isPending}>批准写入</button>
              <button type="button" className="panel-action-button" onClick={() => decisionMutation.mutate(false)} disabled={decisionMutation.isPending}>拒绝</button>
            </div>
          )}
          {approval && approval.status !== 'pending' && (
            <div className="qa-approval-status" role="status">审批{approval.status === 'approved' ? '已批准' : '已拒绝'}{approval.side_effects ? '，已写入派生知识' : ''}</div>
          )}
          {errorMessage && (
            <div className="qa-error" role="alert">
              <AlertCircle size={18} aria-hidden="true" />
              <span>{errorMessage}</span>
            </div>
          )}
        </div>

        <form className="qa-composer" onSubmit={onSubmit}>
          <label htmlFor="qa-question">问题</label>
          <textarea
            id="qa-question"
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onComposerKeyDown}
            placeholder="输入要在知识库中查找的问题"
            rows={3}
            maxLength={12_000}
            disabled={submitMutation.isPending || isActive}
          />
          <div className="qa-composer-actions">
            <span>{draft.length.toLocaleString('zh-CN')} / 12,000</span>
            {isActive ? (
              <button className="qa-cancel-button" type="button" onClick={() => cancelMutation.mutate()} disabled={cancelMutation.isPending || currentRun?.status === 'cancel_requested'}>
                <Square size={15} fill="currentColor" />取消
              </button>
            ) : (
      <button className="qa-send-button" type="submit" disabled={(!draft.trim() && !isOrganizationSkill) || submitMutation.isPending || (isDocumentSkill && !selectedDocumentId) || (selectedSkill === 'compare_sources' && selectedSourceIds.length < 2)}>
                {submitMutation.isPending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}提问
              </button>
            )}
          </div>
        </form>
      </div>

      <aside className="qa-evidence" aria-labelledby="qa-evidence-title">
        <div className="qa-evidence-heading">
          <Quote size={18} aria-hidden="true" />
          <h2 id="qa-evidence-title">引用证据</h2>
          <span className="qa-evidence-count">{currentRun?.citations?.length ?? 0}</span>
        </div>
        {currentRun?.citations?.length ? (
          <div className="qa-evidence-content">
            <div className="qa-citation-list">
              {currentRun.citations.map((citation) => {
                const metadata = citationMetadataQuery.data?.[citationKey(citation.source_id, citation.document_id)]
                const documentName = metadata?.displayName ?? `文档 ${citation.document_id.slice(0, 8)}`
                return (
                  <button className="qa-citation" data-selected={selectedEvidenceId === citation.evidence_id} key={citation.evidence_id} type="button" aria-pressed={selectedEvidenceId === citation.evidence_id} onClick={() => setSelectedEvidenceId((current) => current === citation.evidence_id ? null : citation.evidence_id)}>
                    <FileText size={17} aria-hidden="true" />
                    <span className="qa-citation-copy">
                      <strong>{documentName}</strong>
                      <span>{metadata?.uri ?? `来源 ${citation.source_id.slice(0, 8)}`}</span>
                      <span>{citation.locator.kind} {citation.locator.start}-{citation.locator.end}</span>
                      <code>版本 {citation.version_id.slice(0, 8)}</code>
                    </span>
                    <BookOpenText size={16} aria-hidden="true" />
                    <span className="sr-only">查看原文：{documentName}</span>
                  </button>
                )
              })}
            </div>
            {selectedEvidenceId && (
              <div className="qa-excerpt" ref={excerptRef} tabIndex={-1} aria-live="polite">
                <button className="qa-excerpt-close" type="button" onClick={() => setSelectedEvidenceId(null)} aria-label="关闭原文" title="关闭原文"><X size={16} aria-hidden="true" /></button>
                {citationQuery.isPending ? <LoaderCircle className="spin" size={18} aria-label="正在加载原文" /> : citationQuery.error ? (
                  <div className="qa-excerpt-status" role="alert">
                    <AlertCircle size={18} aria-hidden="true" />
                    <span>{citationQuery.error instanceof Error ? citationQuery.error.message : '原文加载失败'}</span>
                    <button type="button" onClick={() => void citationQuery.refetch()}><RotateCcw size={14} aria-hidden="true" />重试</button>
                  </div>
                ) : citationQuery.data?.excerpt ? (
                  <>
                    <div className="qa-excerpt-heading"><strong>原文</strong><span>{citationQuery.data.locator.kind} {citationQuery.data.locator.start}-{citationQuery.data.locator.end}</span></div>
                    <pre><mark>{citationQuery.data.excerpt}</mark></pre>
                  </>
                ) : (
                  <div className="qa-excerpt-status"><AlertCircle size={18} aria-hidden="true" /><span>{citationStatusLabel(citationQuery.data?.status ?? 'unavailable')}</span></div>
                )}
              </div>
            )}
          </div>
        ) : (
          <div className="qa-evidence-empty"><FileText size={25} aria-hidden="true" /><span>{isActive ? '等待证据校验' : '当前回答没有可显示的引用'}</span></div>
        )}
      </aside>
    </section>
  )
}
