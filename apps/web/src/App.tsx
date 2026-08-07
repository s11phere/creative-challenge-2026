import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  Bot,
  Boxes,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Database,
  HardDrive,
  LibraryBig,
  LoaderCircle,
  MessageSquareText,
  RefreshCw,
  Server,
  ShieldCheck,
  Trash2,
  WifiOff,
  Plus,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import {
  fetchHealthSnapshot,
  healthApiLabel,
  HealthApiError,
  type DependencyCheck,
  type HealthSnapshot,
} from './health'
import { SourcesPanel } from './SourcesPanel'
import { SkillsPanel } from './SkillsPanel'
import { QAWorkspace } from './QAWorkspace'
import {
  assistantDefaultApiMode,
  assistantV1CompatibilityAvailable,
  type AssistantApiMode,
} from './assistantRelease'
import { deleteConversation, fetchConversationHistory, type ConversationHistoryItem } from './qa'
import './App.css'

type ServiceState = 'available' | 'unavailable' | 'checking'
type WorkspaceView = 'qa' | 'status' | 'sources' | 'skills'

type ServiceRow = {
  key: string
  name: string
  description: string
  code: string
  state: ServiceState
  Icon: LucideIcon
}

const codeLabels: Record<string, string> = {
  POSTGRESQL_OK: '可用',
  POSTGRESQL_UNREACHABLE: '不可用',
  REDIS_OK: '可用',
  REDIS_UNREACHABLE: '不可用',
  MODEL_FAKE_READY: '测试替身',
  MODEL_PROVIDER_CONFIGURED: '已配置',
  MODEL_CAPABILITIES_ROUTED: '混合配置',
  MODEL_DISABLED: '已禁用',
  MODEL_CONFIGURATION_MISSING: '未配置',
  MODEL_POLICY_DENIED: '策略阻止',
}

function dependencyState(check: DependencyCheck): ServiceState {
  return check.healthy ? 'available' : 'unavailable'
}

function serviceRows(data: Awaited<ReturnType<typeof fetchHealthSnapshot>> | undefined): ServiceRow[] {
  if (!data) {
    return [
      ['api', 'API 服务', 'FastAPI', Server],
      ['postgresql', 'PostgreSQL', '数据与索引', Database],
      ['redis', 'Redis', '任务投递', HardDrive],
      ['model', '模型网关', 'Chat 与 Embedding', Bot],
    ].map(([key, name, description, Icon]) => ({
      key: key as string,
      name: name as string,
      description: description as string,
      code: 'CHECKING',
      state: 'checking' as const,
      Icon: Icon as LucideIcon,
    }))
  }

  const { checks } = data.ready
  return [
    {
      key: 'api',
      name: 'API 服务',
      description: 'FastAPI',
      code: 'API_OK',
      state: 'available',
      Icon: Server,
    },
    {
      key: 'postgresql',
      name: 'PostgreSQL',
      description: '数据与索引',
      code: checks.postgresql.code,
      state: dependencyState(checks.postgresql),
      Icon: Database,
    },
    {
      key: 'redis',
      name: 'Redis',
      description: '任务投递',
      code: checks.redis.code,
      state: dependencyState(checks.redis),
      Icon: HardDrive,
    },
    {
      key: 'model',
      name: '模型网关',
      description: 'Chat 与 Embedding',
      code: checks.model.code,
      state: dependencyState(checks.model),
      Icon: Bot,
    },
  ]
}

function statusText(row: ServiceRow): string {
  if (row.state === 'checking') return '检查中'
  return codeLabels[row.code] ?? (row.state === 'available' ? '可用' : '不可用')
}

function formatCheckTime(timestamp: number | undefined): string {
  if (!timestamp) return '--:--:--'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(timestamp)
}

function formatConversationTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function latestQuestion(conversation: ConversationHistoryItem): string {
  const questions = conversation.messages.filter((message) => message.role === 'user')
  return questions.at(-1)?.content.trim() || '新会话'
}

function questionCount(conversation: ConversationHistoryItem): number {
  return conversation.messages.filter((message) => message.role === 'user').length
}

function App() {
  const [activeView, setActiveView] = useState<WorkspaceView>(() => {
    if (window.location.hash === '#system-status') return 'status'
    if (window.location.hash === '#sources') return 'sources'
    if (window.location.hash === '#skills') return 'skills'
    if (window.location.hash === '#qa') return 'qa'
    return 'qa'
  })
  const [assistantApiMode, setAssistantApiMode] = useState<AssistantApiMode>(assistantDefaultApiMode)
  const [qaConversationId, setQaConversationId] = useState<string | null>(null)
  const [sidebarWidth, setSidebarWidth] = useState(232)
  const [isResizingSidebar, setIsResizingSidebar] = useState(false)
  const sidebarResizeRef = useRef<{ startX: number; startWidth: number } | null>(null)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (activeView !== 'qa') return
    document.documentElement.classList.add('qa-scroll-locked')
    document.body.classList.add('qa-scroll-locked')
    return () => {
      document.documentElement.classList.remove('qa-scroll-locked')
      document.body.classList.remove('qa-scroll-locked')
    }
  }, [activeView])
  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, '', '#qa')
  }, [])
  const healthQuery = useQuery<HealthSnapshot, HealthApiError>({
    queryKey: ['system-health'],
    queryFn: ({ signal }) => fetchHealthSnapshot(signal),
    retry: false,
    enabled: activeView === 'status',
    staleTime: 10_000,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  })
  const qaHistoryQuery = useQuery({
    queryKey: ['qa-history'],
    queryFn: ({ signal }) => fetchConversationHistory(signal),
    retry: false,
  })
  const deleteConversationMutation = useMutation({
    mutationFn: deleteConversation,
    onSuccess: (_result, conversationId) => {
      if (qaConversationId === conversationId) setQaConversationId(null)
      void queryClient.invalidateQueries({ queryKey: ['qa-history'] })
    },
  })

  const rows = serviceRows(healthQuery.data)
  const availableCount = rows.filter((row) => row.state === 'available').length
  const isInitialLoading = healthQuery.isPending
  const hasError = healthQuery.isError
  const localReady = healthQuery.data?.ready.status === 'ready'

  const showView = (view: WorkspaceView) => {
    setActiveView(view)
    const hash = view === 'sources' ? '#sources' : view === 'skills' ? '#skills' : view === 'qa' ? '#qa' : '#system-status'
    window.history.replaceState(null, '', hash)
  }

  const beginSidebarResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    sidebarResizeRef.current = { startX: event.clientX, startWidth: sidebarWidth }
    setIsResizingSidebar(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const resizeSidebar = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = sidebarResizeRef.current
    if (!start) return
    const nextWidth = Math.min(380, Math.max(210, start.startWidth + event.clientX - start.startX))
    setSidebarWidth(nextWidth)
  }

  const endSidebarResize = () => {
    sidebarResizeRef.current = null
    setIsResizingSidebar(false)
  }

  const history = Array.isArray(qaHistoryQuery.data?.conversations)
    ? qaHistoryQuery.data.conversations
    : []
  const shellStyle = { '--sidebar-width': `${sidebarWidth}px` } as CSSProperties

  return (
    <div className="app-shell" data-resizing={isResizingSidebar} data-view={activeView} style={shellStyle}>
      <aside className="sidebar" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <LibraryBig size={21} strokeWidth={1.8} />
          </span>
          <div>
            <strong>知识工作台</strong>
            <span>本地工作区</span>
          </div>
        </div>

        <nav className="sidebar-nav sidebar-primary-nav">
          <a
            href="#qa"
            aria-current={activeView === 'qa' ? 'page' : undefined}
            onClick={(event) => {
              event.preventDefault()
              showView('qa')
            }}
          >
            <MessageSquareText size={18} />
            对话
          </a>
        </nav>

        <section className="sidebar-history" aria-labelledby="sidebar-history-title">
          <div className="sidebar-history-heading">
            <h2 id="sidebar-history-title">对话历史</h2>
            <button
              className="icon-button"
              type="button"
              aria-label="新建会话"
              title="新建会话"
              onClick={() => {
                setQaConversationId(null)
                showView('qa')
              }}
            >
              <Plus size={17} aria-hidden="true" />
            </button>
          </div>
          <div className="sidebar-history-list">
            {history.length > 0 ? history.map((conversation) => (
              <div
                className="sidebar-history-item"
                data-selected={qaConversationId === conversation.conversation_id}
                key={conversation.conversation_id}
              >
                <button
                  className="sidebar-history-select"
                  type="button"
                  onClick={() => {
                    setQaConversationId(conversation.conversation_id)
                    showView('qa')
                  }}
                >
                  <span className="sidebar-history-time">{formatConversationTime(conversation.updated_at)}</span>
                  <strong>{latestQuestion(conversation)}</strong>
                  <span>{questionCount(conversation)} 个问题</span>
                </button>
                <button
                  className="sidebar-history-delete"
                  type="button"
                  aria-label={`删除会话：${latestQuestion(conversation)}`}
                  title="删除会话"
                  disabled={deleteConversationMutation.isPending && deleteConversationMutation.variables === conversation.conversation_id}
                  onClick={() => {
                    if (window.confirm('删除这条会话历史？会话内容将从历史列表中移除。')) {
                      deleteConversationMutation.mutate(conversation.conversation_id)
                    }
                  }}
                >
                  {deleteConversationMutation.isPending && deleteConversationMutation.variables === conversation.conversation_id
                    ? <LoaderCircle className="spin" size={14} aria-hidden="true" />
                    : <Trash2 size={14} aria-hidden="true" />}
                </button>
              </div>
            )) : (
              <div className="sidebar-history-empty">暂无对话</div>
            )}
          </div>
        </section>

        <nav className="sidebar-nav sidebar-secondary-nav">
          <a
            href="#system-status"
            aria-current={activeView === 'status' ? 'page' : undefined}
            onClick={(event) => {
              event.preventDefault()
              showView('status')
            }}
          >
            <Activity size={18} />
            系统状态
          </a>
          <a
            href="#sources"
            aria-current={activeView === 'sources' ? 'page' : undefined}
            onClick={(event) => {
              event.preventDefault()
              showView('sources')
            }}
          >
            <Database size={18} />
            数据来源
          </a>
          <a
            href="#skills"
            aria-current={activeView === 'skills' ? 'page' : undefined}
            onClick={(event) => {
              event.preventDefault()
              showView('skills')
            }}
          >
            <Boxes size={18} />
            技能管理
          </a>
        </nav>

        <div className="local-mode">
          <ShieldCheck size={17} />
          <div>
            <strong>策略受控</strong>
            <span>模型外发由部署配置</span>
          </div>
        </div>
      </aside>

      <div
        className="sidebar-resizer"
        role="separator"
        aria-label="调整侧栏宽度"
        aria-orientation="vertical"
        aria-valuemin={210}
        aria-valuemax={380}
        aria-valuenow={sidebarWidth}
        tabIndex={0}
        onPointerDown={beginSidebarResize}
        onPointerMove={resizeSidebar}
        onPointerUp={endSidebarResize}
        onPointerCancel={endSidebarResize}
      />

      <main
        className={`workspace ${activeView === 'qa' ? 'workspace-qa' : ''}`}
        id={activeView === 'status' ? 'system-status' : activeView}
      >
        <header className="workspace-header">
          <div>
            <p className="eyebrow">
              {activeView === 'status'
                ? '运行概览'
                : activeView === 'qa'
                  ? '当前会话'
                  : activeView === 'skills'
                    ? '技能与版本'
                    : '知识库内容'}
            </p>
            <h1>
              {activeView === 'status'
                ? '系统状态'
                : activeView === 'qa'
                  ? '对话'
                  : activeView === 'skills'
                    ? '技能管理'
                    : '数据来源'}
            </h1>
          </div>
          {activeView === 'qa' && assistantV1CompatibilityAvailable && (
            <div className="assistant-mode-switch" role="group" aria-label="对话模式">
              <button
                type="button"
                aria-pressed={assistantApiMode === 'v2'}
                onClick={() => setAssistantApiMode('v2')}
              >
                Assistant
              </button>
              <button
                type="button"
                aria-pressed={assistantApiMode === 'v1'}
                onClick={() => setAssistantApiMode('v1')}
              >
                兼容问答
              </button>
            </div>
          )}
          {activeView === 'status' && <button
            className="icon-button"
            type="button"
            onClick={() => void healthQuery.refetch()}
            disabled={healthQuery.isFetching}
            aria-label="重新检查系统状态"
            title="重新检查"
          >
            <RefreshCw className={healthQuery.isFetching ? 'spin' : ''} size={19} />
          </button>}
        </header>

        {activeView === 'status' ? (
          <>
        <section className={`summary-band ${hasError ? 'summary-error' : ''}`} aria-live="polite">
          <div className="summary-copy">
            <span className="summary-icon" aria-hidden="true">
              {hasError ? (
                <WifiOff size={22} />
              ) : isInitialLoading ? (
                <LoaderCircle className="spin" size={22} />
              ) : localReady ? (
                <CheckCircle2 size={22} />
              ) : (
                <CircleAlert size={22} />
              )}
            </span>
            <div>
              <strong>
                {hasError
                  ? 'API 连接失败'
                  : isInitialLoading
                    ? '正在检查本地服务'
                    : localReady
                      ? '本地服务运行正常'
                      : '部分本地服务不可用'}
              </strong>
              <span>
                {hasError
                  ? '状态暂时无法读取'
                  : `${availableCount} / ${rows.length} 项当前可用`}
              </span>
            </div>
          </div>
          <div className="checked-time">
            <Clock3 size={16} />
            <span>更新于 {formatCheckTime(healthQuery.data?.checkedAt)}</span>
          </div>
        </section>

        {hasError && (
          <section className="error-panel" role="alert">
            <CircleAlert size={20} aria-hidden="true" />
            <div>
              <h2>无法连接本地 API</h2>
              <p>检查 API 进程和端口配置后重新尝试。</p>
              <code>{healthQuery.error.code}</code>
            </div>
            <button
              type="button"
              className="retry-button"
              onClick={() => void healthQuery.refetch()}
              disabled={healthQuery.isFetching}
            >
              <RefreshCw size={17} />
              重新检查
            </button>
          </section>
        )}

        <section className="status-panel" aria-labelledby="services-title">
          <div className="panel-heading">
            <div>
              <h2 id="services-title">服务连接</h2>
              <p>API 与本地依赖</p>
            </div>
            <span className="service-count">{availableCount}/{rows.length}</span>
          </div>

          <div className="service-list">
            {rows.map((row) => (
              <div className="service-row" key={row.key} data-state={row.state}>
                <span className="service-icon" aria-hidden="true">
                  <row.Icon size={19} />
                </span>
                <div className="service-name">
                  <strong>{row.name}</strong>
                  <span>{row.description}</span>
                </div>
                <div className="service-result">
                  <span className="status-label">
                    {row.state === 'available' ? (
                      <CheckCircle2 size={16} />
                    ) : row.state === 'unavailable' ? (
                      <CircleAlert size={16} />
                    ) : (
                      <LoaderCircle className="spin" size={16} />
                    )}
                    {statusText(row)}
                  </span>
                  <code>{row.code}</code>
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="runtime-section" aria-labelledby="runtime-title">
          <div className="section-heading">
            <h2 id="runtime-title">运行信息</h2>
          </div>
          <dl className="runtime-grid">
            <div>
              <dt>API 地址</dt>
              <dd>{healthApiLabel()}</dd>
            </div>
            <div>
              <dt>Trace ID</dt>
              <dd title={healthQuery.data?.traceId ?? undefined}>
                {healthQuery.data?.traceId ?? '等待检查'}
              </dd>
            </div>
            <div>
              <dt>Request ID</dt>
              <dd title={healthQuery.data?.requestId ?? undefined}>
                {healthQuery.data?.requestId ?? '等待检查'}
              </dd>
            </div>
          </dl>
        </section>
          </>
        ) : activeView === 'sources' ? (
          <SourcesPanel />
        ) : activeView === 'skills' ? (
          <SkillsPanel />
        ) : (
          <QAWorkspace
            selectedConversationId={qaConversationId}
            onConversationSelected={setQaConversationId}
            apiMode={assistantApiMode}
          />
        )}
      </main>
    </div>
  )
}

export default App
