/** Data sources panel — list sources, upload files, and track ingestion tasks. */

import { useMutation, useQuery } from '@tanstack/react-query'
import {
  CloudUpload,
  FileText,
  LoaderCircle,
  RefreshCw,
  XCircle,
  CheckCircle2,
  CircleAlert,
  type LucideIcon,
} from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import {
  cancelTask,
  fetchSourceDetail,
  fetchSources,
  fetchTaskStatus,
  retryTask,
  triggerIngestion,
  uploadFile,
  type SourceInfo,
  type TaskInfo,
} from './sources'

const statusMeta: Record<
  string,
  { label: string; color: string; Icon: LucideIcon }
> = {
  succeeded: { label: '成功', color: 'var(--color-success)', Icon: CheckCircle2 },
  failed: { label: '失败', color: 'var(--color-danger)', Icon: CircleAlert },
  cancelled: { label: '已取消', color: 'var(--color-muted)', Icon: XCircle },
  cancel_requested: { label: '取消中', color: 'var(--color-warning)', Icon: LoaderCircle },
  running: { label: '运行中', color: 'var(--color-accent)', Icon: LoaderCircle },
  queued: { label: '排队中', color: 'var(--color-muted)', Icon: LoaderCircle },
  dead_letter: { label: '死信', color: 'var(--color-danger)', Icon: CircleAlert },
}

function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    discover: '发现',
    fingerprint: '指纹',
    parse: '解析',
    normalize: '规范化',
    enrich: '增强',
    chunk: '分块',
    embed: '嵌入',
    index: '索引',
    validate: '验证',
    publish: '发布',
    cleanup: '清理',
  }
  return labels[stage] ?? stage
}

function progressPercent(progress: number): string {
  return `${Math.round(progress * 100)}%`
}

function TaskRow({ taskId }: { taskId: string }) {
  const { data: task, isLoading, refetch } = useQuery<TaskInfo>({
    queryKey: ['task', taskId],
    queryFn: ({ signal }) => fetchTaskStatus(taskId, signal),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (status === 'running' || status === 'queued' || status === 'cancel_requested') {
        return 3000
      }
      return false
    },
  })

  const cancelMut = useMutation({ mutationFn: () => cancelTask(taskId) })
  const retryMut = useMutation({ mutationFn: () => retryTask(taskId) })

  if (isLoading) {
    return (
      <div className="task-row">
        <LoaderCircle className="spin" size={16} />
        <span>加载任务状态…</span>
      </div>
    )
  }

  if (!task) return null

  const meta = statusMeta[task.status] ?? {
    label: task.status,
    color: 'var(--color-muted)',
    Icon: LoaderCircle,
  }

  const isTerminal = ['succeeded', 'failed', 'cancelled', 'dead_letter'].includes(task.status)

  return (
    <div className="task-row" data-status={task.status}>
      <div className="task-status-icon" style={{ color: meta.color }}>
        <meta.Icon size={16} className={task.status === 'running' || task.status === 'queued' ? 'spin' : ''} />
      </div>
      <div className="task-info">
        <div className="task-stage">
          {stageLabel(task.stage)}
          <span className="task-progress">{progressPercent(task.progress)}</span>
        </div>
        <div className="task-meta">
          {task.operation} · 重试 {task.retry_count}/{task.max_retries}
        </div>
        {task.error && <div className="task-error" title={task.error}>{task.error.slice(0, 120)}</div>}
      </div>
      <div className="task-status-label">{meta.label}</div>
      <div className="task-actions">
        {!isTerminal && (
          <button
            type="button"
            className="icon-button"
            onClick={() => cancelMut.mutate()}
            disabled={cancelMut.isPending}
            title="取消任务"
            aria-label="取消任务"
          >
            <XCircle size={16} />
          </button>
        )}
        {(task.status === 'failed' || task.status === 'dead_letter') && (
          <button
            type="button"
            className="icon-button"
            onClick={() => retryMut.mutate()}
            disabled={retryMut.isPending}
            title="重试"
            aria-label="重试"
          >
            <RefreshCw size={16} />
          </button>
        )}
      </div>
      <button type="button" className="icon-button" onClick={() => refetch()} title="刷新">
        <RefreshCw size={14} />
      </button>
    </div>
  )
}

function SourceCard({ source }: { source: SourceInfo }) {
  const [isExpanded, setIsExpanded] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [uploadTaskId, setUploadTaskId] = useState<string | null>(null)
  const [ingestTaskId, setIngestTaskId] = useState<string | null>(null)

  const detailQuery = useQuery({
    queryKey: ['source', source.id],
    queryFn: ({ signal }) => fetchSourceDetail(source.id, signal),
    enabled: isExpanded,
  })

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadFile(source.id, file),
    onSuccess: (data) => {
      if (data.task_id) setUploadTaskId(data.task_id)
    },
  })

  const ingestMut = useMutation({
    mutationFn: () => triggerIngestion(source.id),
    onSuccess: (data) => setIngestTaskId(data.task_id),
  })

  const handleUpload = (e: FormEvent) => {
    e.preventDefault()
    const files = fileInputRef.current?.files
    if (files?.length) uploadMut.mutate(files[0])
  }

  const activeTaskId = uploadTaskId ?? ingestTaskId

  return (
    <div className={`source-card ${isExpanded ? 'expanded' : ''}`}>
      <div className="source-header" onClick={() => setIsExpanded(!isExpanded)} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setIsExpanded(!isExpanded) }}>
        <FileText size={18} />
        <div className="source-info">
          <strong>{source.uri || '未命名来源'}</strong>
          <span>{source.source_type} · {source.id.slice(0, 8)}…</span>
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={(e) => { e.stopPropagation(); ingestMut.mutate() }}
          disabled={ingestMut.isPending}
          title="触发摄入"
          aria-label="触发摄入"
        >
          <CloudUpload size={16} />
        </button>
      </div>

      {isExpanded && (
        <div className="source-body">
          {/* Upload form */}
          <form className="upload-form" onSubmit={handleUpload}>
            <input type="file" ref={fileInputRef} className="file-input" />
            <button type="submit" className="action-button" disabled={uploadMut.isPending}>
              {uploadMut.isPending ? '上传中…' : '上传并摄入'}
            </button>
          </form>

          {uploadMut.data && (
            <div className="upload-result">
              文件已登记，哈希 {uploadMut.data.blob_hash.slice(0, 12)}…
              {uploadMut.data.is_unchanged && '（内容未变化）'}
            </div>
          )}

          {/* Task status */}
          {activeTaskId && <TaskRow taskId={activeTaskId} />}

          {/* Documents */}
          {detailQuery.data && (
            <div className="doc-list">
              <h4>文档（{detailQuery.data.documents.length}）</h4>
              {detailQuery.data.documents.map((doc) => (
                <div className="doc-item" key={doc.id}>
                  <span className="doc-key">{doc.stable_key}</span>
                  <span className={`doc-status ${doc.status}`}>{doc.status}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function SourcesPanel() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['sources'],
    queryFn: ({ signal }) => fetchSources(signal),
    refetchInterval: 30_000,
    retry: false,
  })

  const sources = data?.sources ?? []

  return (
    <section className="status-panel" aria-labelledby="sources-title">
      <div className="panel-heading">
        <div>
          <h2 id="sources-title">数据来源</h2>
          <p>文档与摄入任务</p>
        </div>
        <div className="panel-actions">
          <span className="service-count">{sources.length}</span>
          <button type="button" className="icon-button" onClick={() => refetch()} disabled={isLoading} aria-label="刷新来源列表">
            <RefreshCw size={17} className={isLoading ? 'spin' : ''} />
          </button>
        </div>
      </div>

      {isError ? (
        <div className="empty-state">
          <CircleAlert size={24} />
          <p>无法加载来源</p>
          <span>请检查 API 连接后刷新</span>
        </div>
      ) : isLoading ? (
        <div className="loading-row">
          <LoaderCircle className="spin" size={18} />
          <span>加载来源列表…</span>
        </div>
      ) : sources.length === 0 ? (
        <div className="empty-state">
          <FileText size={24} />
          <p>暂无数据来源</p>
          <span>调用 API 创建来源后上传文件开始摄入</span>
        </div>
      ) : (
        <div className="sources-list">
          {sources.map((source) => (
            <SourceCard key={source.id} source={source} />
          ))}
        </div>
      )}
    </section>
  )
}

