/** Data sources panel — list sources, upload files, and track ingestion tasks. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CloudUpload,
  FileUp,
  FileText,
  LoaderCircle,
  Pencil,
  RotateCcw,
  RefreshCw,
  Trash2,
  XCircle,
  CheckCircle2,
  CircleAlert,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useRef, useState, type DragEvent, type FormEvent } from 'react'
import {
  cancelTask,
  createFolder,
  deleteDocument,
  deleteFolder,
  deleteSource,
  fetchSourceDetail,
  fetchSources,
  fetchTaskStatus,
  fetchUploadLimits,
  renameSource,
  retryTask,
  SourcesApiError,
  triggerIngestion,
  uploadFile,
  type SourceInfo,
  type SourceDetail,
  type TaskInfo,
} from './sources'

const WEB_UPLOAD_URI = 'web-upload://browser'

const statusMeta: Record<
  string,
  { label: string; color: string; Icon: LucideIcon }
> = {
  succeeded: { label: '成功', color: 'var(--success)', Icon: CheckCircle2 },
  failed: { label: '失败', color: 'var(--danger)', Icon: CircleAlert },
  cancelled: { label: '已取消', color: 'var(--muted)', Icon: XCircle },
  cancel_requested: { label: '取消中', color: 'var(--warning)', Icon: LoaderCircle },
  running: { label: '运行中', color: 'var(--accent)', Icon: LoaderCircle },
  queued: { label: '排队中', color: 'var(--muted)', Icon: LoaderCircle },
  dead_letter: { label: '死信', color: 'var(--danger)', Icon: CircleAlert },
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

const sourceTypeLabels: Record<string, string> = {
  upload: '浏览器上传',
  folder: '文件夹',
}

function sourceTypeLabel(sourceType: string): string {
  return sourceTypeLabels[sourceType] ?? sourceType
}

function formatSourceTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  }).format(date)
}

const terminalTaskStatuses = new Set(['succeeded', 'failed', 'cancelled', 'dead_letter'])

function TaskRow({
  taskId,
  onTaskChange,
  onTaskSettled,
}: {
  taskId: string
  onTaskChange: (taskId: string) => void
  onTaskSettled?: () => void
}) {
  const queryClient = useQueryClient()
  const settledStatusRef = useRef<string | null>(null)
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

  const cancelMut = useMutation({
    mutationFn: () => cancelTask(taskId),
    onSuccess: (updated) => {
      queryClient.setQueryData(['task', taskId], updated)
    },
  })
  const retryMut = useMutation({
    mutationFn: () => retryTask(taskId),
    onSuccess: (result) => {
      queryClient.removeQueries({ queryKey: ['task', taskId] })
      onTaskChange(result.task_id)
    },
  })

  useEffect(() => {
    settledStatusRef.current = null
    cancelMut.reset()
    retryMut.reset()
    // Mutation state belongs to a task ID and must not leak into the next task.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])

  useEffect(() => {
    const status = task?.status
    if (!status || !terminalTaskStatuses.has(status) || settledStatusRef.current === status) return
    settledStatusRef.current = status
    onTaskSettled?.()
  }, [onTaskSettled, task?.status])

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
    color: 'var(--muted)',
    Icon: LoaderCircle,
  }

  const isTerminal = terminalTaskStatuses.has(task.status)
  const retriesUsed = Math.min(task.retry_count, task.max_retries)
  const operationError = cancelMut.error ?? retryMut.error

  return (
    <div className="task-row" data-status={task.status}>
      <div className="task-status-icon" style={{ color: meta.color }}>
        <meta.Icon size={16} className={task.status === 'running' || task.status === 'queued' || task.status === 'cancel_requested' ? 'spin' : ''} />
      </div>
      <div className="task-info">
        <div className="task-stage">
          {stageLabel(task.stage)}
          <span className="task-progress">{progressPercent(task.progress)}</span>
        </div>
        <div className="task-meta">
          <span>{task.operation === 'ingest' ? '摄入' : task.operation}</span>
          {task.retry_count > 0 && (
            <span>自动重试 {retriesUsed}/{task.max_retries}</span>
          )}
        </div>
        {!isTerminal && (
          <div className="task-progress-bar" aria-hidden="true">
            <div
              className="task-progress-fill"
              style={{ width: `${Math.min(100, Math.max(0, Math.round(task.progress * 100)))}%` }}
            />
          </div>
        )}
        {task.error && <div className="task-error">{task.error}</div>}
        {operationError && (
          <div className="task-error" role="alert">
            操作失败：{operationError instanceof SourcesApiError ? operationError.message : String(operationError)}
          </div>
        )}
      </div>
      <div className="task-status-label">{meta.label}</div>
      <div className="task-actions">
        {!isTerminal && (
          <button
            type="button"
            className="task-action-button"
            onClick={() => {
              retryMut.reset()
              cancelMut.mutate()
            }}
            disabled={cancelMut.isPending}
            aria-label="取消任务"
          >
            <XCircle size={16} />
            <span>{task.status === 'cancel_requested' ? '取消中…' : '取消'}</span>
          </button>
        )}
        {(task.status === 'failed' || task.status === 'dead_letter') && (
          <button
            type="button"
            className="task-action-button"
            onClick={() => {
              cancelMut.reset()
              retryMut.mutate()
            }}
            disabled={retryMut.isPending}
            aria-label="重试"
          >
            <RotateCcw size={16} />
            <span>{retryMut.isPending ? '提交中…' : '重试'}</span>
          </button>
        )}
      </div>
      <button
        type="button"
        className="task-action-button task-refresh-button"
        onClick={() => {
          cancelMut.reset()
          retryMut.reset()
          void refetch()
        }}
        aria-label="刷新任务"
      >
        <RefreshCw size={16} />
        <span>刷新</span>
      </button>
    </div>
  )
}

function DirectUpload({ sources }: { sources: SourceInfo[] }) {
  const queryClient = useQueryClient()
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null)
  const [activeSourceId, setActiveSourceId] = useState<string | null>(null)

  const limitsQuery = useQuery({
    queryKey: ['upload-limits'],
    queryFn: ({ signal }) => fetchUploadLimits(signal),
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
  const maxUploadSizeMb = limitsQuery.data?.max_upload_size_mb

  const folders = sources
  const [selectedFolderId, setSelectedFolderId] = useState<string>('')
  const [newFolderName, setNewFolderName] = useState<string>('')
  const effectiveFolderId =
    selectedFolderId === '__new__'
      ? '__new__'
      : folders.some((folder) => folder.id === selectedFolderId)
        ? selectedFolderId
        : folders[0]?.id ?? '__new__'
  const isNewFolder = effectiveFolderId === '__new__'

  // Tracks a folder created by the in-flight upload so a failed upload can
  // roll it back instead of leaving an empty folder card behind.
  const createdFolderRef = useRef<string | null>(null)

  const uploadMut = useMutation({
    mutationFn: async (selected: File) => {
      let folderId = effectiveFolderId
      if (folderId === '__new__') {
        const created = await createFolder(newFolderName.trim() || '默认文件夹')
        createdFolderRef.current = created.source_id
        folderId = created.source_id
      }
      return { sourceId: folderId, result: await uploadFile(folderId, selected) }
    },
    onSuccess: ({ sourceId, result }) => {
      createdFolderRef.current = null
      setActiveSourceId(sourceId)
      void queryClient.invalidateQueries({ queryKey: ['sources'] })
      void queryClient.invalidateQueries({ queryKey: ['source', sourceId] })
      setActiveTaskId(result.task_id)
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
    },
    onError: async () => {
      const createdId = createdFolderRef.current
      createdFolderRef.current = null
      if (!createdId) return
      try {
        await deleteSource(createdId)
      } catch {
        // Best-effort rollback. If the API refuses (e.g. the source already
        // holds content) the empty folder stays visible for manual management.
      }
      void queryClient.invalidateQueries({ queryKey: ['sources'] })
    },
  })

  const selectFile = (selected: File | undefined) => {
    uploadMut.reset()
    setFile(selected ?? null)
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setIsDragging(false)
    selectFile(event.dataTransfer.files[0])
  }

  return (
    <div className="direct-upload">
      <div className="folder-picker-row">
        <label className="folder-picker-label" htmlFor="direct-upload-folder">
          上传到
        </label>
        <select
          id="direct-upload-folder"
          className="folder-select"
          aria-label="选择文件夹"
          value={effectiveFolderId}
          onChange={(event) => setSelectedFolderId(event.target.value)}
        >
          {folders.map((folder) => (
            <option key={folder.id} value={folder.id}>
              {folder.name || sourceTypeLabel(folder.source_type)}
            </option>
          ))}
          <option value="__new__">＋ 新建文件夹</option>
        </select>
        {isNewFolder && (
          <input
            className="folder-name-input"
            aria-label="新文件夹名称"
            placeholder="输入新文件夹名称"
            value={newFolderName}
            onChange={(event) => setNewFolderName(event.target.value)}
          />
        )}
      </div>
      <div
        className={`direct-upload-target ${isDragging ? 'is-dragging' : ''}`}
        onDragEnter={(event) => {
          event.preventDefault()
          setIsDragging(true)
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
      >
        <input
          ref={inputRef}
          type="file"
          className="file-input"
          accept=".md,.markdown,.txt,.pdf,text/markdown,text/plain,application/pdf"
          aria-label="选择要上传的文件"
          onChange={(event) => selectFile(event.target.files?.[0])}
        />
        <span className="direct-upload-icon" aria-hidden="true">
          <FileUp size={20} />
        </span>
        <div className="direct-upload-copy">
          <strong>{file?.name ?? '上传知识文件'}</strong>
          <span>
            {file
              ? `${(file.size / 1024).toFixed(1)} KB${maxUploadSizeMb && file.size > maxUploadSizeMb * 1024 * 1024 ? '（超过大小限制）' : ''}`
              : maxUploadSizeMb
                ? `PDF、Markdown 或文本文件 · 单个文件不超过 ${maxUploadSizeMb} MB`
                : 'PDF、Markdown 或文本文件'}
          </span>
        </div>
        <button type="button" className="file-picker-button" onClick={() => inputRef.current?.click()}>
          <FileUp size={16} />
          选择文件
        </button>
        <button
          type="button"
          className="upload-action-button"
          disabled={!file || uploadMut.isPending}
          onClick={() => file && uploadMut.mutate(file)}
        >
          {uploadMut.isPending ? <LoaderCircle className="spin" size={16} /> : <CloudUpload size={16} />}
          {uploadMut.isPending ? '上传中' : '上传并摄入'}
        </button>
      </div>
      {uploadMut.isError && (
        <div className="upload-error" role="alert">
          上传失败：{uploadMut.error instanceof SourcesApiError
            ? `${uploadMut.error.code}: ${uploadMut.error.message}`
            : String(uploadMut.error)}
        </div>
      )}
      {uploadMut.data && !uploadMut.data.result.task_id && (
        <div className="upload-result">文件内容未变化，无需重复摄入。</div>
      )}
      {activeTaskId && (
        <TaskRow
          taskId={activeTaskId}
          onTaskChange={setActiveTaskId}
          onTaskSettled={() => {
            void queryClient.invalidateQueries({ queryKey: ['sources'] })
            if (activeSourceId) {
              void queryClient.invalidateQueries({ queryKey: ['source', activeSourceId] })
            }
          }}
        />
      )}
    </div>
  )
}

const documentStatusLabels: Record<string, string> = {
  active: '可用',
  available: '可用',
  unavailable: '不可用',
  failed: '不可用',
  deleted: '已删除',
}

function SourceCard({ source }: { source: SourceInfo }) {
  const queryClient = useQueryClient()
  const [isExpanded, setIsExpanded] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [activeTaskIds, setActiveTaskIds] = useState<string[]>([])
  const [isRenaming, setIsRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState(source.name)
  const suppressBlurRef = useRef(false)

  const docCount = source.doc_count ?? 0
  const availableCount = source.available_count ?? 0
  const failedCount = source.failed_count ?? 0
  const sourceTitle =
    source.name
    || source.primary_document_name
    || (source.uri && source.uri !== WEB_UPLOAD_URI ? source.uri : null)
    || sourceTypeLabel(source.source_type)

  const detailQuery = useQuery({
    queryKey: ['source', source.id],
    queryFn: ({ signal }) => fetchSourceDetail(source.id, signal),
    enabled: isExpanded,
    refetchInterval: isExpanded && activeTaskIds.length > 0 ? 3_000 : false,
  })

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadFile(source.id, file),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ['source', source.id] })
      if (data.task_id) setActiveTaskIds([data.task_id])
      setSelectedFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
    },
  })

  const ingestMut = useMutation({
    mutationFn: () => triggerIngestion(source.id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['source', source.id] })
      setActiveTaskIds(data.task_ids)
    },
  })

  const deleteDocumentMut = useMutation({
    mutationFn: (documentId: string) => deleteDocument(source.id, documentId),
    onSuccess: (_result, documentId) => {
      queryClient.setQueryData<SourceDetail>(['source', source.id], (current) => {
        if (!current) return current
        return {
          ...current,
          documents: current.documents.filter((document) => document.id !== documentId),
        }
      })
      void queryClient.invalidateQueries({ queryKey: ['source', source.id] })
      void queryClient.invalidateQueries({ queryKey: ['sources'] })
    },
  })

  const renameMut = useMutation({
    mutationFn: (name: string) => renameSource(source.id, name),
    onSuccess: () => {
      setIsRenaming(false)
      void queryClient.invalidateQueries({ queryKey: ['sources'] })
    },
  })

  const deleteFolderMut = useMutation({
    mutationFn: () => deleteFolder(source.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['sources'] })
    },
  })

  const commitRename = () => {
    const name = renameValue.trim()
    if (name && name !== source.name) renameMut.mutate(name)
    else setIsRenaming(false)
  }

  const handleUpload = (e: FormEvent) => {
    e.preventDefault()
    if (selectedFile) uploadMut.mutate(selectedFile)
  }

  const visibleDocuments = detailQuery.data?.documents?.filter((doc) => doc.status !== 'deleted') ?? []

  return (
    <div className={`source-card ${isExpanded ? 'expanded' : ''}`}>
      <div className="source-header" onClick={() => setIsExpanded(!isExpanded)} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setIsExpanded(!isExpanded) }}>
        <FileText size={18} />
        <div className="source-info">
          <strong className="source-title">
            {isRenaming ? (
              <input
                className="folder-rename-input"
                value={renameValue}
                autoFocus
                aria-label="重命名文件夹"
                onChange={(event) => setRenameValue(event.target.value)}
                onFocus={() => {
                  suppressBlurRef.current = false
                }}
                onBlur={() => {
                  if (!suppressBlurRef.current) commitRename()
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    event.stopPropagation()
                    commitRename()
                  } else if (event.key === 'Escape') {
                    event.preventDefault()
                    event.stopPropagation()
                    suppressBlurRef.current = true
                    setRenameValue(source.name)
                    setIsRenaming(false)
                  }
                }}
              />
            ) : (
              sourceTitle
            )}
            {failedCount > 0 && (
              <span className="source-failed-badge" title={`${failedCount} 个文档不可用`}>
                {failedCount} 个异常
              </span>
            )}
          </strong>
          <span>{sourceTypeLabel(source.source_type)} · {docCount} 个文档 · {availableCount} 可用 · {formatSourceTime(source.created_at)}</span>
        </div>
        <div className="source-actions">
          {!isRenaming && (
            <button
              type="button"
              className="source-action-button"
              aria-label="重命名文件夹"
              title="重命名文件夹"
              onClick={(event) => {
                event.stopPropagation()
                setRenameValue(source.name)
                setIsRenaming(true)
              }}
            >
              <Pencil size={16} />
            </button>
          )}
          <button
            type="button"
            className="source-delete-button"
            aria-label="删除文件夹"
            title="删除文件夹"
            disabled={deleteFolderMut.isPending}
            onClick={(event) => {
              event.stopPropagation()
              if (window.confirm(`删除文件夹“${sourceTitle}”及其全部 ${docCount} 个文档？`)) {
                deleteFolderMut.mutate()
              }
            }}
          >
            {deleteFolderMut.isPending
              ? <LoaderCircle className="spin" size={16} aria-hidden="true" />
              : <Trash2 size={16} aria-hidden="true" />}
          </button>
          <button
            type="button"
            className="source-action-button"
            onClick={(e) => {
              e.stopPropagation()
              setIsExpanded(true)
              ingestMut.mutate()
            }}
            disabled={ingestMut.isPending}
            aria-label="触发摄入"
          >
            <CloudUpload size={16} />
            <span>{ingestMut.isPending ? '提交中…' : '触发摄入'}</span>
          </button>
        </div>
      </div>

      {isExpanded && (
        <div className="source-body">
          {/* Upload form */}
          <form className="upload-form" onSubmit={handleUpload}>
            <input
              type="file"
              ref={fileInputRef}
              className="file-input"
              accept=".md,.markdown,.txt,.pdf,text/markdown,text/plain,application/pdf"
              onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
            />
            <button
              type="button"
              className="file-picker-button"
              onClick={() => fileInputRef.current?.click()}
            >
              <FileUp size={16} />
              选择文件
            </button>
            <span className={`selected-file-name ${selectedFile ? '' : 'empty'}`} title={selectedFile?.name}>
              {selectedFile?.name ?? '未选择文件'}
            </span>
            <button
              type="submit"
              className="upload-action-button"
              disabled={uploadMut.isPending || !selectedFile}
            >
              <CloudUpload size={16} />
              {uploadMut.isPending ? '上传中…' : '上传并摄入'}
            </button>
          </form>

          {uploadMut.data && (
            <div className="upload-result">
              文件已登记{uploadMut.data.is_unchanged ? '（内容未变化）' : ''}
            </div>
          )}
          {uploadMut.isError && (
            <div className="upload-error">
              上传失败：{uploadMut.error instanceof SourcesApiError
                ? `${uploadMut.error.code}: ${uploadMut.error.message}`
                : String(uploadMut.error ?? '未知错误')}
            </div>
          )}
          {ingestMut.isError && (
            <div className="upload-error">
              触发摄入失败：{ingestMut.error instanceof SourcesApiError
                ? `${ingestMut.error.code}: ${ingestMut.error.message}`
                : String(ingestMut.error ?? '未知错误')}
            </div>
          )}

          {/* Task status */}
          {activeTaskIds.map((taskId) => (
            <TaskRow
              key={taskId}
              taskId={taskId}
              onTaskChange={(nextId) =>
                setActiveTaskIds((ids) => ids.map((id) => (id === taskId ? nextId : id)))}
              onTaskSettled={() => {
                void queryClient.invalidateQueries({ queryKey: ['source', source.id] })
              }}
            />
          ))}

          {/* Documents */}
          {detailQuery.data && (
            <div className="doc-list">
              <h4>文档（{visibleDocuments.length}）</h4>
              {deleteDocumentMut.isError && (
                <div className="upload-error" role="alert">
                  删除文档失败：{deleteDocumentMut.error instanceof SourcesApiError
                    ? deleteDocumentMut.error.message
                    : String(deleteDocumentMut.error)}
                </div>
              )}
              {visibleDocuments.map((doc) => (
                <div className="doc-item" key={doc.id}>
                  <span className="doc-key" title={`稳定键：${doc.stable_key}`}>
                    {doc.display_name}
                  </span>
                  <span className={`doc-status ${doc.status}`}>
                    {documentStatusLabels[doc.status] ?? doc.status}
                  </span>
                  {doc.status !== 'deleted' && (
                    <button
                      type="button"
                      className="doc-delete-button"
                      aria-label={`删除文档：${doc.display_name}`}
                      title="删除文档"
                      disabled={deleteDocumentMut.isPending && deleteDocumentMut.variables === doc.id}
                      onClick={() => {
                        if (window.confirm(`删除文档“${doc.display_name}”？`)) {
                          deleteDocumentMut.mutate(doc.id)
                        }
                      }}
                    >
                      {deleteDocumentMut.isPending && deleteDocumentMut.variables === doc.id
                        ? <LoaderCircle className="spin" size={15} aria-hidden="true" />
                        : <Trash2 size={15} aria-hidden="true" />}
                    </button>
                  )}
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
          <h2 id="sources-title">来源与文档</h2>
          <p>上传文件并管理摄入任务</p>
        </div>
        <div className="panel-actions">
          <span className="service-count">{sources.length}</span>
          <button type="button" className="panel-action-button" onClick={() => refetch()} disabled={isLoading} aria-label="刷新来源列表">
            <RefreshCw size={17} className={isLoading ? 'spin' : ''} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {!isLoading && !isError && <DirectUpload sources={sources} />}

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
          <p>暂无文件夹</p>
          <span>选择上方文件即可创建文件夹并开始摄入</span>
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

