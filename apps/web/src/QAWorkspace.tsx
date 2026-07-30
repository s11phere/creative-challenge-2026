import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertCircle,
  FileText,
  LoaderCircle,
  MessageSquareText,
  Quote,
  Send,
  Square,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { cancelRun, createConversation, fetchRun, submitQuestion, type QARun } from './qa'

type LocalQuestion = {
  id: string
  text: string
  run: QARun
}

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

export function QAWorkspace() {
  const [draft, setDraft] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [question, setQuestion] = useState<LocalQuestion | null>(null)
  const queryClient = useQueryClient()

  const runQuery = useQuery({
    queryKey: ['qa-run', question?.run.run_id],
    queryFn: ({ signal }) => fetchRun(question!.run.run_id, signal),
    enabled: question !== null,
    initialData: question?.run,
    refetchInterval: (query) =>
      activeStatuses.has(query.state.data?.status ?? '') ? 2_000 : false,
  })

  const currentRun = runQuery.data ?? question?.run
  const isActive = currentRun ? activeStatuses.has(currentRun.status) : false

  const submitMutation = useMutation({
    mutationFn: async (text: string) => {
      let currentConversationId = conversationId
      if (!currentConversationId) {
        const conversation = await createConversation()
        currentConversationId = conversation.conversation_id
        setConversationId(currentConversationId)
      }
      const idempotencyKey = crypto.randomUUID()
      const run = await submitQuestion(currentConversationId, text, idempotencyKey)
      return { id: idempotencyKey, text, run }
    },
    onSuccess: (nextQuestion) => {
      setQuestion(nextQuestion)
      setDraft('')
    },
  })

  const cancelMutation = useMutation({
    mutationFn: async () => cancelRun(currentRun!.run_id),
    onSuccess: (run) => {
      setQuestion((current) => (current ? { ...current, run } : current))
      queryClient.setQueryData(['qa-run', run.run_id], run)
    },
  })

  const errorMessage = useMemo(() => {
    const error = submitMutation.error ?? runQuery.error ?? cancelMutation.error
    return error instanceof Error ? error.message : null
  }, [cancelMutation.error, runQuery.error, submitMutation.error])

  const onSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const text = draft.trim()
    if (!text || submitMutation.isPending || isActive) return
    submitMutation.mutate(text)
  }

  return (
    <section className="qa-layout" aria-label="知识问答工作区">
      <div className="qa-conversation">
        <div className="qa-thread" aria-live="polite">
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
              </article>
            </>
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
                disabled={!draft.trim() || submitMutation.isPending}
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
        </div>
        <div className="qa-evidence-empty">
          <FileText size={25} aria-hidden="true" />
          <span>{isActive ? '等待证据校验' : '完成回答后显示引用'}</span>
        </div>
      </aside>
    </section>
  )
}
