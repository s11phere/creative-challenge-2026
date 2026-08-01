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
  Workflow,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelRun,
  createReviewCards,
  decideApproval,
  createConversation,
  fetchCitationExcerpt,
  fetchRun,
  fetchSkills,
  requestApproval,
  resumeRun,
  submitQuestion,
  type QASkillName,
  type QARun,
} from './qa'
import { fetchSourceDetail, fetchSources, type SourceDetail } from './sources'

type LocalQuestion = {
  id: string
  text: string
  run: QARun
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
    retention_expired: '固定版本已超过保留期限',
    unavailable: '固定版本或分块当前不可用',
    invalid: '引用内容与固定版本校验不一致',
  }
  return labels[status] ?? `原文当前不可用（${status}）`
}

export function QAWorkspace() {
  const [draft, setDraft] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [question, setQuestion] = useState<LocalQuestion | null>(null)
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null)
  const [selectedSkill, setSelectedSkill] = useState<QASkillName>('knowledge_agent')
  const [selectedSourceId, setSelectedSourceId] = useState('')
  const [selectedDocumentId, setSelectedDocumentId] = useState('')
  const [focus, setFocus] = useState('')
  const [approval, setApproval] = useState<Awaited<ReturnType<typeof requestApproval>> | null>(null)
  const excerptRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  const skillsQuery = useQuery({
    queryKey: ['skills'],
    queryFn: ({ signal }) => fetchSkills(signal),
    staleTime: 60_000,
    retry: false,
  })

  const runQuery = useQuery({
    queryKey: ['qa-run', question?.run.run_id],
    queryFn: ({ signal }) => fetchRun(question!.run.run_id, signal),
    enabled: question !== null,
    initialData: question?.run,
    refetchInterval: (query) =>
      activeStatuses.has(query.state.data?.status ?? '') ? 2_000 : false,
  })

  const currentRun = runQuery.data ?? question?.run
  const activeSkill = skillsQuery.data?.find((skill) => skill.name === selectedSkill)
  const sourcesQuery = useQuery({ queryKey: ['sources'], queryFn: ({ signal }) => fetchSources(signal), enabled: selectedSkill === 'create_review_cards', retry: false })
  const sourceDetailQuery = useQuery<SourceDetail>({
    queryKey: ['source', selectedSourceId],
    queryFn: ({ signal }) => fetchSourceDetail(selectedSourceId, signal),
    enabled: selectedSkill === 'create_review_cards' && Boolean(selectedSourceId),
    retry: false,
  })
  const displayedSkill = currentRun?.skill ?? {
    name: selectedSkill,
    version: activeSkill?.active_version ?? null,
  }
  const isActive = currentRun ? activeStatuses.has(currentRun.status) : false
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
        ? await createReviewCards(currentConversationId, selectedDocumentId, sourceDetailQuery.data?.documents.find((doc) => doc.id === selectedDocumentId)?.current_version_id ?? '', focus.trim() || undefined, idempotencyKey)
        : await submitQuestion(currentConversationId, text, idempotencyKey, selectedSkill)
      return { id: idempotencyKey, text, run }
    },
    onSuccess: (nextQuestion) => {
      setQuestion(nextQuestion)
      setApproval(null)
      setSelectedEvidenceId(null)
      setDraft('')
    },
  })

  const resumeMutation = useMutation({
    mutationFn: () => resumeRun(currentRun!.run_id),
    onSuccess: (run) => {
      setQuestion((current) => current ? { ...current, run } : current)
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

  const chooseSkill = (skill: QASkillName) => {
    if (isActive || submitMutation.isPending) return
    setSelectedSkill(skill)
    setQuestion(null)
    setApproval(null)
    setSelectedEvidenceId(null)
  }

  const cancelMutation = useMutation({
    mutationFn: async () => cancelRun(currentRun!.run_id),
    onSuccess: (run) => {
      setQuestion((current) => (current ? { ...current, run } : current))
      queryClient.setQueryData(['qa-run', run.run_id], run)
    },
  })

  const errorMessage = useMemo(() => {
    const error = submitMutation.error ?? runQuery.error ?? cancelMutation.error ?? resumeMutation.error ?? approvalMutation.error ?? decisionMutation.error
    return error instanceof Error ? error.message : null
  }, [approvalMutation.error, cancelMutation.error, decisionMutation.error, resumeMutation.error, runQuery.error, submitMutation.error])

  const onSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const text = draft.trim()
    if (!text || submitMutation.isPending || isActive) return
    if (selectedSkill === 'create_review_cards' && (!selectedDocumentId || !sourceDetailQuery.data?.documents.find((doc) => doc.id === selectedDocumentId)?.current_version_id)) return
    submitMutation.mutate(text)
  }

  return (
    <section className="qa-layout" aria-label="知识问答工作区">
      <div className="qa-conversation">
        <div className="qa-thread" data-empty={!question} aria-live="polite">
          <div className="qa-skill-context">
            <Workflow size={16} aria-hidden="true" />
            <strong>{displayedSkill.name}</strong>
            <code>{displayedSkill.version ? `v${displayedSkill.version}` : '版本不可用'}</code>
            {currentRun?.skill && <span>已固定</span>}
          </div>
          <div className="qa-mode-selector" role="group" aria-label="问答模式">
            <button
              type="button"
              aria-pressed={selectedSkill === 'knowledge_agent'}
              onClick={() => chooseSkill('knowledge_agent')}
              disabled={submitMutation.isPending || isActive}
            >
              <Bot size={16} aria-hidden="true" />
              LLM Agent
            </button>
            <button
              type="button"
              aria-pressed={selectedSkill === 'knowledge_qa'}
              onClick={() => chooseSkill('knowledge_qa')}
              disabled={submitMutation.isPending || isActive}
            >
              <Search size={16} aria-hidden="true" />
              直接问答
            </button>
            <button
              type="button"
              aria-pressed={selectedSkill === 'create_review_cards'}
              onClick={() => chooseSkill('create_review_cards')}
              disabled={submitMutation.isPending || isActive}
            >
              <Workflow size={16} aria-hidden="true" />复习卡
            </button>
          </div>
          {selectedSkill === 'create_review_cards' && !question && (
            <div className="qa-skill-config">
              <label htmlFor="review-source">来源</label>
              <select id="review-source" value={selectedSourceId} onChange={(event) => { setSelectedSourceId(event.target.value); setSelectedDocumentId('') }}>
                <option value="">选择来源</option>
                {sourcesQuery.data?.sources.map((source) => <option key={source.id} value={source.id}>{source.uri}</option>)}
              </select>
              <label htmlFor="review-document">固定文档版本</label>
              <select id="review-document" value={selectedDocumentId} onChange={(event) => setSelectedDocumentId(event.target.value)} disabled={!sourceDetailQuery.data}>
                <option value="">选择文档</option>
                {sourceDetailQuery.data?.documents.filter((doc) => doc.current_version_id).map((doc) => <option key={doc.id} value={doc.id}>{doc.display_name}</option>)}
              </select>
              <label htmlFor="review-focus">重点（可选）</label>
              <input id="review-focus" value={focus} onChange={(event) => setFocus(event.target.value)} maxLength={1000} />
            </div>
          )}
          {!question ? (
            <div className="qa-empty">
              <MessageSquareText size={28} aria-hidden="true" />
              <strong>开始一次知识检索</strong>
              <span>回答将依据当前 Space 中已发布的文档。</span>
            </div>
          ) : (
            <>
              <article className="qa-message qa-message-user">
                <span className="qa-message-label">你的问题</span>
                <p>{question.text}</p>
              </article>
              <article className="qa-message qa-message-system" data-status={currentRun?.status}>
                <div className="qa-run-heading">
                  {isActive ? (
                    <LoaderCircle className="spin" size={18} aria-hidden="true" />
                  ) : currentRun?.status === 'failed' || currentRun?.status === 'timed_out' ? (
                    <AlertCircle size={18} aria-hidden="true" />
                  ) : (
                    <MessageSquareText size={18} aria-hidden="true" />
                  )}
                  <strong>{statusLabel(currentRun?.status ?? 'created')}</strong>
                </div>
                {currentRun?.error_code && <code>{currentRun.error_code}</code>}
                {currentRun?.result && (
                  <div className="qa-answer">
                    <p>{currentRun.result.text ?? currentRun.result.message}</p>
                    {currentRun.result.limitations?.map((limitation) => (
                      <small key={limitation}>{limitation}</small>
                    ))}
                  </div>
                )}
              </article>
            </>
          )}
          {currentRun && resumableStatuses.has(currentRun.status) && (
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
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="输入要在知识库中查找的问题"
            rows={3}
            maxLength={12_000}
            disabled={submitMutation.isPending || isActive}
          />
          <div className="qa-composer-actions">
            <span>{draft.length.toLocaleString('zh-CN')} / 12,000</span>
            {isActive ? (
              <button
                className="qa-cancel-button"
                type="button"
                onClick={() => cancelMutation.mutate()}
                disabled={cancelMutation.isPending || currentRun?.status === 'cancel_requested'}
              >
                <Square size={15} fill="currentColor" />
                取消
              </button>
            ) : (
              <button
                className="qa-send-button"
                type="submit"
                disabled={!draft.trim() || submitMutation.isPending || (selectedSkill === 'create_review_cards' && !selectedDocumentId)}
              >
                {submitMutation.isPending ? (
                  <LoaderCircle className="spin" size={17} />
                ) : (
                  <Send size={17} />
                )}
                提问
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
              {currentRun.citations.map((citation, index) => (
                <button
                  className="qa-citation"
                  data-selected={selectedEvidenceId === citation.evidence_id}
                  key={citation.evidence_id}
                  type="button"
                  aria-pressed={selectedEvidenceId === citation.evidence_id}
                  onClick={() => setSelectedEvidenceId((current) =>
                    current === citation.evidence_id ? null : citation.evidence_id
                  )}
                >
                  <FileText size={17} aria-hidden="true" />
                  <span className="qa-citation-copy">
                    <strong>证据 {index + 1}</strong>
                    <span>{citation.locator.kind} {citation.locator.start}-{citation.locator.end}</span>
                    <code>{citation.document_id.slice(0, 8)} / {citation.version_id.slice(0, 8)}</code>
                  </span>
                  <BookOpenText size={16} aria-hidden="true" />
                  <span className="sr-only">查看原文</span>
                </button>
              ))}
            </div>
            {selectedEvidenceId && (
              <div className="qa-excerpt" ref={excerptRef} tabIndex={-1} aria-live="polite">
                <button className="qa-excerpt-close" type="button" onClick={() => setSelectedEvidenceId(null)} aria-label="关闭原文" title="关闭原文">
                  <X size={16} aria-hidden="true" />
                </button>
                {citationQuery.isPending ? (
                  <LoaderCircle className="spin" size={18} aria-label="正在加载原文" />
                ) : citationQuery.error ? (
                  <div className="qa-excerpt-status" role="alert">
                    <AlertCircle size={18} aria-hidden="true" />
                    <span>{citationQuery.error instanceof Error ? citationQuery.error.message : '原文加载失败'}</span>
                    <button type="button" onClick={() => void citationQuery.refetch()}>
                      <RotateCcw size={14} aria-hidden="true" />重试
                    </button>
                  </div>
                ) : citationQuery.data?.excerpt ? (
                  <>
                    <div className="qa-excerpt-heading">
                      <strong>原文</strong>
                      <span>
                        {citationQuery.data.locator.kind} {citationQuery.data.locator.start}-
                        {citationQuery.data.locator.end}
                      </span>
                    </div>
                    <pre><mark>{citationQuery.data.excerpt}</mark></pre>
                  </>
                ) : (
                  <div className="qa-excerpt-status">
                    <AlertCircle size={18} aria-hidden="true" />
                    <span>{citationStatusLabel(citationQuery.data?.status ?? 'unavailable')}</span>
                  </div>
                )}
              </div>
            )}
          </div>
        ) : (
          <div className="qa-evidence-empty">
            <FileText size={25} aria-hidden="true" />
            <span>{isActive ? '等待证据校验' : '当前回答没有可显示的引用'}</span>
          </div>
        )}
      </aside>
    </section>
  )
}
