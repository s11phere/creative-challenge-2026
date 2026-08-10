import {
  AlertCircle,
  BookOpenText,
  Check,
  Clock3,
  FilePenLine,
  FileText,
  LoaderCircle,
  Quote,
  Search,
  Terminal,
  type LucideIcon,
} from 'lucide-react'
import type { AgentRunEvent, AssistantRun } from './qa'

type AgentRunTimelineProps = {
  run: AssistantRun
  events: AgentRunEvent[]
  hasGroundedEvidence: boolean
  clarificationPending: boolean
  onSelectClarification: (candidateId: string) => void
  onOpenEvidence: (runId: string) => void
}

type ToolKind = 'read' | 'retrieval' | 'write' | 'command'

type ToolTimelineItem = {
  iteration: number
  toolName: string
  toolVersion: string
  events: AgentRunEvent[]
}

type SkillActivationItem = {
  iteration: number
  skillName: string
  skillVersion: string
  event: AgentRunEvent
}

const terminalEventTypes = new Set([
  'completed', 'clarifying', 'refused', 'failed', 'cancelled', 'timed_out',
])

const stopReasonLabels: Record<string, string> = {
  goal_complete: '目标已完成',
  evidence_insufficient: '证据不足',
  evidence_conflict: '证据冲突',
  user_clarification: '等待补充信息',
  policy_refusal: '策略拒绝',
  cancelled: '已取消',
  timed_out: '运行超时',
  failed: '运行失败',
}

const eventStatusLabels: Record<string, string> = {
  requested: '已请求',
  running: '执行中',
  succeeded: '已完成',
  failed: '未完成',
  waiting_approval: '等待审批',
}

function getText(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key]
  return typeof value === 'string' ? value : null
}

function getNumber(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function classifyTool(name: string): ToolKind {
  const normalized = name.toLowerCase()
  if (/(write|save|create|update|delete|patch)/.test(normalized)) return 'write'
  if (/(shell|exec|command|terminal)/.test(normalized)) return 'command'
  if (/(search|retriev|grounded|evidence|citation)/.test(normalized)) return 'retrieval'
  return 'read'
}

function toolVisual(kind: ToolKind): { label: string; Icon: LucideIcon } {
  switch (kind) {
    case 'retrieval':
      return { label: '检索', Icon: Search }
    case 'write':
      return { label: '写入', Icon: FilePenLine }
    case 'command':
      return { label: '命令', Icon: Terminal }
    case 'read':
      return { label: '读取', Icon: FileText }
  }
}

function mergeToolEvents(events: AgentRunEvent[]): ToolTimelineItem[] {
  const items = new Map<string, ToolTimelineItem>()
  for (const event of events) {
    if (!['tool_requested', 'tool_started', 'tool_output', 'approval_required'].includes(event.event_type)) continue
    const iteration = getNumber(event.payload, 'iteration')
    const toolName = getText(event.payload, 'tool_name')
    const toolVersion = getText(event.payload, 'tool_version')
    if (iteration === null || !toolName || !toolVersion) continue
    const key = `${iteration}:${toolName}:${toolVersion}`
    const item = items.get(key) ?? { iteration, toolName, toolVersion, events: [] }
    item.events.push(event)
    items.set(key, item)
  }
  return [...items.values()]
    .map((item) => ({ ...item, events: item.events.sort((left, right) => left.sequence - right.sequence) }))
    .sort((left, right) => left.iteration - right.iteration)
}

function skillActivations(events: AgentRunEvent[]): SkillActivationItem[] {
  return events
    .filter((event) => event.event_type === 'skill_activated')
    .map((event) => ({
      iteration: getNumber(event.payload, 'iteration'),
      skillName: getText(event.payload, 'skill_name'),
      skillVersion: getText(event.payload, 'skill_version'),
      event,
    }))
    .filter((item): item is SkillActivationItem => (
      item.iteration !== null && item.skillName !== null && item.skillVersion !== null
    ))
    .sort((left, right) => left.event.sequence - right.event.sequence)
}

function toolStatus(item: ToolTimelineItem): { label: string; state: string; errorCode: string | null } {
  const output = item.events.findLast((event) => event.event_type === 'tool_output')
  const started = item.events.findLast((event) => event.event_type === 'tool_started')
  const approval = item.events.findLast((event) => event.event_type === 'approval_required')
  const errorCode = output ? getText(output.payload, 'error_code') : null
  if (output) {
    if (errorCode?.toLowerCase().includes('approval') && errorCode.toLowerCase().includes('reject')) {
      return { label: '审批已拒绝，无副作用', state: 'rejected', errorCode }
    }
    const status = getText(output.payload, 'status') ?? 'failed'
    return { label: eventStatusLabels[status] ?? status, state: status, errorCode }
  }
  if (started) return { label: approval ? '审批已通过，执行中' : '执行中', state: 'running', errorCode: null }
  if (approval) return { label: '等待审批', state: 'waiting_approval', errorCode: null }
  return { label: '已请求', state: 'requested', errorCode: null }
}

function toolDetails(item: ToolTimelineItem) {
  const latest = item.events.at(-1)
  const output = item.events.findLast((event) => event.event_type === 'tool_output')
  const source = output ?? latest
  if (!source) return []
  const entries: Array<[string, string]> = []
  const inputSummary = getText(source.payload, 'input_summary')
  const queryPreview = getText(source.payload, 'query_preview')
  const resourceReference = getText(source.payload, 'resource_reference')
  const outputSummary = getText(source.payload, 'output_summary')
  const duration = getNumber(source.payload, 'duration_ms')
  const retryCount = getNumber(source.payload, 'retry_count')
  const errorCode = getText(source.payload, 'error_code')
  if (resourceReference) entries.push(['目标文档', resourceReference])
  if (queryPreview) entries.push(['检索问题', queryPreview])
  if (inputSummary) entries.push(['输入摘要', inputSummary])
  if (outputSummary) entries.push(['结果摘要', outputSummary])
  if (duration !== null) entries.push(['耗时', formatDuration(duration)])
  if (retryCount !== null) entries.push(['重试', `${retryCount} 次`])
  if (errorCode) entries.push(['错误码', errorCode])
  return entries
}

function formatDuration(value: number): string {
  if (value < 1_000) return `${Math.round(value)} ms`
  return `${(value / 1_000).toFixed(1)} 秒`
}

function runStatusLabel(status: string): string {
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

function terminalStopReason(events: AgentRunEvent[]): string | null {
  const event = events.findLast((item) => terminalEventTypes.has(item.event_type))
    ?? events.findLast((item) => item.event_type === 'finalizing')
  if (!event) return null
  const reason = getText(event.payload, 'stop_reason')
  return reason ? stopReasonLabels[reason] ?? reason : null
}

function TimelineStatusIcon({ status }: { status: string }) {
  if (status === 'running' || status === 'created' || status === 'queued' || status === 'cancel_requested') {
    return <LoaderCircle className="spin" size={17} aria-hidden="true" />
  }
  if (status === 'failed' || status === 'timed_out' || status === 'refused') {
    return <AlertCircle size={17} aria-hidden="true" />
  }
  return <Check size={17} aria-hidden="true" />
}

export function AgentRunTimeline({
  run,
  events,
  hasGroundedEvidence,
  clarificationPending,
  onSelectClarification,
  onOpenEvidence,
}: AgentRunTimelineProps) {
  const accepted = events.find((event) => event.event_type === 'accepted')
  const tools = mergeToolEvents(events)
  const activations = skillActivations(events)
  const rootActivations = activations.filter((item) => item.iteration === 0)
  const iterations = [...new Set([
    ...events
      .filter((event) => event.event_type === 'iteration_started')
      .map((event) => getNumber(event.payload, 'iteration'))
      .filter((iteration): iteration is number => iteration !== null),
    ...tools.map((tool) => tool.iteration),
    ...activations.filter((item) => item.iteration > 0).map((item) => item.iteration),
  ])].sort((left, right) => left - right)
  const stopReason = terminalStopReason(events)
  const model = getText(accepted?.payload ?? {}, 'model') ?? run.model_identity
  const requestedEffort = getText(accepted?.payload ?? {}, 'requested_effort')
  const effectiveEffort = getText(accepted?.payload ?? {}, 'effective_effort')

  return (
    <section className="chat-agent-timeline" data-status={run.status} aria-label="Agent 运行时间线">
      <header className="chat-agent-timeline-header">
        <span className="chat-agent-timeline-title">
          <BookOpenText size={17} aria-hidden="true" />
          <span><strong>Agent 运行时间线</strong></span>
        </span>
        <span className="chat-agent-timeline-status" data-status={run.status}>
          <TimelineStatusIcon status={run.status} />{runStatusLabel(run.status)}
        </span>
      </header>

      <dl className="chat-agent-run-metadata">
        {requestedEffort && <div><dt>请求强度</dt><dd>{requestedEffort}</dd></div>}
        {effectiveEffort && <div><dt>实际强度</dt><dd>{effectiveEffort}</dd></div>}
        <div><dt>模型</dt><dd>{model}</dd></div>
        <div><dt>输入 Token</dt><dd>{run.usage.input_tokens.toLocaleString('zh-CN')}</dd></div>
        <div><dt>输出 Token</dt><dd>{run.usage.output_tokens.toLocaleString('zh-CN')}</dd></div>
        <div><dt>实际 Token</dt><dd>{run.usage.total_tokens.toLocaleString('zh-CN')}</dd></div>
        <div><dt>模型耗时</dt><dd>{formatDuration(run.usage.model_latency_ms)}</dd></div>
        {stopReason && <div><dt>停止原因</dt><dd>{stopReason}</dd></div>}
      </dl>

      {rootActivations.map((activation) => (
        <p key={activation.event.event_id} className="chat-agent-skill-activation">
          <BookOpenText size={15} aria-hidden="true" />
          <span>Skill 已激活：<strong>{activation.skillName}</strong> v{activation.skillVersion}</span>
        </p>
      ))}

      <ol className="chat-agent-iterations" aria-label="Agent 迭代记录">
        {iterations.map((iteration) => {
          const iterationTools = tools.filter((tool) => tool.iteration === iteration)
          const iterationActivations = activations.filter((item) => item.iteration === iteration)
          return (
            <li key={iteration} className="chat-agent-iteration">
              <div className="chat-agent-iteration-heading"><Clock3 size={15} aria-hidden="true" /><strong>第 {iteration} 轮</strong></div>
              {iterationActivations.map((activation) => (
                <p key={activation.event.event_id} className="chat-agent-skill-activation">
                  <BookOpenText size={15} aria-hidden="true" />
                  <span>Skill 已激活：<strong>{activation.skillName}</strong> v{activation.skillVersion}</span>
                </p>
              ))}
              {iterationTools.length > 0 ? (
                <div className="chat-agent-tools">
                  {iterationTools.map((tool) => {
                    const visual = toolVisual(classifyTool(tool.toolName))
                    const ToolIcon = visual.Icon
                    const status = toolStatus(tool)
                    const details = toolDetails(tool)
                    return (
                      <details key={`${tool.iteration}:${tool.toolName}:${tool.toolVersion}`} className="chat-agent-tool" data-kind={classifyTool(tool.toolName)} data-status={status.state}>
                        <summary>
                          <span className="chat-agent-tool-title">
                            <ToolIcon size={16} aria-hidden="true" />
                            <span><strong>{tool.toolName}</strong><small>{visual.label} · {tool.toolVersion}</small></span>
                          </span>
                          <span className="chat-agent-tool-status">{status.label}</span>
                        </summary>
                        {details.length > 0 && (
                          <dl className="chat-agent-tool-details">
                            {details.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
                          </dl>
                        )}
                      </details>
                    )
                  })}
                </div>
              ) : <p className="chat-agent-iteration-empty">正在规划下一步。</p>}
            </li>
          )
        })}
      </ol>

      {hasGroundedEvidence && (
        <button className="chat-evidence-button" type="button" onClick={() => onOpenEvidence(run.run_id)}>
          <Quote size={15} aria-hidden="true" />查看引用证据
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
    </section>
  )
}
