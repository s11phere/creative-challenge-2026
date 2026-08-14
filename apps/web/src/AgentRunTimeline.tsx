import {
  AlertCircle,
  BookOpenText,
  Check,
  FilePenLine,
  FileText,
  LoaderCircle,
  Quote,
  Search,
  ShieldCheck,
  Terminal,
  X,
  type LucideIcon,
} from 'lucide-react'
import type { AgentApproval, AgentRunEvent, AssistantRun } from './qa'

type AgentRunTimelineProps = {
  run: AssistantRun
  events: AgentRunEvent[]
  hasGroundedEvidence: boolean
  clarificationPending: boolean
  onSelectClarification: (candidateId: string) => void
  onOpenEvidence: (runId: string) => void
  approvals: AgentApproval[]
  approvalBusy: boolean
  onDecideApproval: (approvalId: string, approved: boolean, alwaysAllow: boolean) => void
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

type CacheUsageItem = {
  iteration: number
  readTokens: number
  writeTokens: number
  mode: string | null
  visibleObservationBytes: number | null
  toolCount: number | null
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

function displayText(payload: Record<string, unknown>, key: string): string | null {
  const value = getText(payload, key)
  if (!value) return null
  const redacted = value
    .replace(/(?:sha(?:256)?[:=\s-]*)?[a-f0-9]{64}\b/gi, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\s+([,.;:])/g, '$1')
    .trim()
  return redacted || null
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

function toolStatus(item: ToolTimelineItem, run: AssistantRun): { label: string; state: string; errorCode: string | null } {
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
  if (run.status === 'failed' || run.status === 'timed_out' || run.status === 'cancelled') {
    return {
      label: run.status === 'timed_out' ? '运行超时' : run.status === 'cancelled' ? '已取消' : '未完成',
      state: 'failed',
      errorCode: run.error_code,
    }
  }
  if (started) return { label: approval ? '审批已通过，执行中' : '执行中', state: 'running', errorCode: null }
  if (approval) return { label: '等待审批', state: 'waiting_approval', errorCode: null }
  return { label: '已请求', state: 'requested', errorCode: null }
}

function toolDetails(item: ToolTimelineItem) {
  const output = item.events.findLast((event) => event.event_type === 'tool_output')
  const entries: Array<[string, string]> = []
  const firstText = (key: string) => {
    for (const event of item.events) {
      const value = displayText(event.payload, key)
      if (value) return value
    }
    return null
  }
  const inputSummary = firstText('input_summary')
  const queryPreview = firstText('query_preview')
  const resourceReference = firstText('resource_reference')
  const path = firstText('path')
  const command = firstText('command')
  const cwd = firstText('cwd')
  const outputSummary = firstText('output_summary')
  const toolFamily = firstText('tool_family')
  const decisionSummary = firstText('decision_summary')
  const duration = toolDuration(item)
  const retryCount = latestNumber(item, 'retry_count')
  const exitCode = latestNumber(item, 'exit_code')
  const errorCode = firstText('error_code')
  if (path) entries.push(['操作对象', path])
  if (command) entries.push(['执行命令', command])
  if (cwd) entries.push(['工作目录', cwd])
  if (resourceReference) entries.push(['目标文档', resourceReference])
  if (queryPreview) entries.push(['检索问题', queryPreview])
  if (toolFamily) entries.push(['能力分组', toolFamilyLabel(toolFamily) ?? toolFamily])
  if (inputSummary) entries.push(['输入摘要', inputSummary])
  if (outputSummary) entries.push(['结果摘要', outputSummary])
  if (decisionSummary) entries.push(['决策摘要', decisionSummary])
  if (duration !== null) entries.push(['耗时', formatDuration(duration)])
  else if (output) entries.push(['耗时', '未记录'])
  if (retryCount !== null) entries.push(['重试', `${retryCount} 次`])
  if (errorCode) entries.push(['错误码', errorCode])
  if (exitCode !== null) entries.push(['Exit code', String(exitCode)])
  return entries
}

function toolOutput(item: ToolTimelineItem): { value: string; truncated: boolean } | null {
  const output = item.events.findLast((event) => event.event_type === 'tool_output')
  if (!output) return null
  const value = displayText(output.payload, 'output_preview')
  if (!value) return null
  return { value, truncated: output.payload.output_truncated === true }
}

function latestNumber(item: ToolTimelineItem, key: string): number | null {
  for (let index = item.events.length - 1; index >= 0; index -= 1) {
    const value = getNumber(item.events[index].payload, key)
    if (value !== null) return value
  }
  return null
}

function toolDuration(item: ToolTimelineItem): number | null {
  const output = item.events.findLast((event) => event.event_type === 'tool_output')
  const reported = output ? getNumber(output.payload, 'duration_ms') : null
  if (reported !== null && reported > 0) return reported

  const started = item.events.find((event) => event.event_type === 'tool_started')
  if (!started || !output) return null
  const startedAt = Date.parse(started.occurred_at)
  const completedAt = Date.parse(output.occurred_at)
  if (!Number.isFinite(startedAt) || !Number.isFinite(completedAt) || completedAt <= startedAt) {
    return null
  }
  return completedAt - startedAt
}

function cacheUsage(events: AgentRunEvent[]): CacheUsageItem[] {
  return events
    .filter((event) => event.event_type === 'cache_used')
    .map((event) => {
      const iteration = getNumber(event.payload, 'iteration')
      if (iteration === null) return null
      return {
        iteration,
        readTokens: getNumber(event.payload, 'cache_read_tokens') ?? 0,
        writeTokens: getNumber(event.payload, 'cache_write_tokens') ?? 0,
        mode: getText(event.payload, 'cache_mode'),
        visibleObservationBytes: getNumber(event.payload, 'visible_observation_bytes'),
        toolCount: getNumber(event.payload, 'tool_count'),
      }
    })
    .filter((item): item is CacheUsageItem => item !== null)
    .sort((left, right) => left.iteration - right.iteration)
}

function terminalKindLabel(value: string | null): string | null {
  if (value === 'direct') return '直接回复'
  if (value === 'grounded') return '知识问答终态'
  return null
}

function toolFamilyLabel(value: string | null): string | null {
  const labels: Record<string, string> = {
    bootstrap: 'Skill 路由',
    knowledge: '知识',
    workspace: '工作区',
    command: '命令',
    skill_creator: 'Skill Creator',
    read: '读取',
  }
  return value && labels[value] ? labels[value] : value
}

function formatDuration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '未记录'
  if (value < 1_000) return `${Math.round(value)} ms`
  return `${(value / 1_000).toFixed(1)} 秒`
}

function runDuration(run: AssistantRun): number | null {
  if (!run.created_at || !run.updated_at) return null
  const createdAt = Date.parse(run.created_at)
  const updatedAt = Date.parse(run.updated_at)
  if (!Number.isFinite(createdAt) || !Number.isFinite(updatedAt) || updatedAt <= createdAt) {
    return null
  }
  return updatedAt - createdAt
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
  approvals,
  approvalBusy,
  onDecideApproval,
}: AgentRunTimelineProps) {
  const accepted = events.find((event) => event.event_type === 'accepted')
  const approvalList = Array.isArray(approvals) ? approvals : []
  const tools = mergeToolEvents(events)
  const activations = skillActivations(events)
  const rootActivations = activations.filter((item) => item.iteration === 0)
  const stopReason = terminalStopReason(events)
  const model = run.reasoning_profile.model || getText(accepted?.payload ?? {}, 'model') || run.model_identity
  const requestedEffort = getText(accepted?.payload ?? {}, 'requested_effort')
  const effectiveEffort = getText(accepted?.payload ?? {}, 'effective_effort')
  const harnessVersion = getText(accepted?.payload ?? {}, 'harness_version')
  const cacheItems = cacheUsage(events)
  const terminalEvent = events.findLast((event) => terminalEventTypes.has(event.event_type))
  const terminalKind = getText(terminalEvent?.payload ?? {}, 'terminal_kind')
  const totalCacheRead = cacheItems.reduce((sum, item) => sum + item.readTokens, 0)
  const totalCacheWrite = cacheItems.reduce((sum, item) => sum + item.writeTokens, 0)
  const totalDuration = runDuration(run)
  const iterations = [...new Set([
    ...events
      .filter((event) => event.event_type === 'iteration_started')
      .map((event) => getNumber(event.payload, 'iteration'))
      .filter((iteration): iteration is number => iteration !== null),
    ...events
      .filter((event) => event.event_type === 'cache_used')
      .map((event) => getNumber(event.payload, 'iteration'))
      .filter((iteration): iteration is number => iteration !== null),
    ...events
      .filter((event) => terminalEventTypes.has(event.event_type))
      .map((event) => getNumber(event.payload, 'iteration'))
      .filter((iteration): iteration is number => iteration !== null),
    ...tools.map((tool) => tool.iteration),
    ...activations.filter((item) => item.iteration > 0).map((item) => item.iteration),
  ])].sort((left, right) => left - right)

  return (
    <section className="chat-agent-run" data-status={run.status} aria-label="Agent 运行时间线">
      <section className="chat-agent-timeline" data-status={run.status}>
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
          {harnessVersion && <div><dt>Agent Harness</dt><dd>{harnessVersion}</dd></div>}
          <div><dt>模型</dt><dd>{model}</dd></div>
          <div><dt>输入 Token</dt><dd>{run.usage.input_tokens.toLocaleString('zh-CN')}</dd></div>
          <div><dt>输出 Token</dt><dd>{run.usage.output_tokens.toLocaleString('zh-CN')}</dd></div>
          <div><dt>实际 Token</dt><dd>{run.usage.total_tokens.toLocaleString('zh-CN')}</dd></div>
          <div><dt>总耗时</dt><dd>{totalDuration === null ? '未记录' : formatDuration(totalDuration)}</dd></div>
          {totalCacheRead > 0 && <div><dt>缓存读取 Token</dt><dd>{totalCacheRead.toLocaleString('zh-CN')}</dd></div>}
          {totalCacheWrite > 0 && <div><dt>缓存写入 Token</dt><dd>{totalCacheWrite.toLocaleString('zh-CN')}</dd></div>}
          {stopReason && <div><dt>停止原因</dt><dd>{stopReason}</dd></div>}
          {terminalKind && <div><dt>终止类型</dt><dd>{terminalKindLabel(terminalKind)}</dd></div>}
        </dl>

        {rootActivations.map((activation) => (
          <p key={activation.event.event_id} className="chat-agent-skill-activation">
            <BookOpenText size={15} aria-hidden="true" />
            <span>Skill 已激活：<strong>{activation.skillName}</strong> v{activation.skillVersion}</span>
          </p>
        ))}
      </section>

      <ol className="chat-agent-iterations" aria-label="Agent 迭代记录">
        {iterations.map((iteration) => {
          const iterationTools = tools.filter((tool) => tool.iteration === iteration)
          const iterationActivations = activations.filter((item) => item.iteration === iteration)
          const iterationCache = cacheItems.filter((item) => item.iteration === iteration)
          return (
            <li key={iteration} className="chat-agent-iteration">
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
                    const status = toolStatus(tool, run)
                    const details = toolDetails(tool)
                    if (status.errorCode && !details.some(([label]) => label === '错误码')) {
                      details.push(['错误码', status.errorCode])
                    }
                    const output = toolOutput(tool)
                    const approvalEvent = tool.events.findLast((event) => event.event_type === 'approval_required')
                    const approvalId = getText(approvalEvent?.payload ?? {}, 'approval_id')
                    const approval = approvalList.find((item) => item.approval_id === approvalId)
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
                        {output && (
                          <details className="chat-agent-tool-output">
                            <summary>查看输出{output.truncated ? '（结果已截断）' : ''}</summary>
                            <pre>{output.value}</pre>
                          </details>
                        )}
                        {approvalEvent && approval?.status === 'pending' && (
                          <div className="chat-agent-approval-actions">
                            <span>需要审批此操作</span>
                            <div>
                              <button
                                type="button"
                                disabled={approvalBusy}
                                onClick={() => onDecideApproval(approval.approval_id, true, false)}
                              >
                                <Check size={15} aria-hidden="true" />批准
                              </button>
                              <button
                                type="button"
                                disabled={approvalBusy}
                                onClick={() => onDecideApproval(approval.approval_id, true, true)}
                              >
                                <ShieldCheck size={15} aria-hidden="true" />始终允许此类操作
                              </button>
                              <button
                                type="button"
                                disabled={approvalBusy}
                                onClick={() => onDecideApproval(approval.approval_id, false, false)}
                              >
                                <X size={15} aria-hidden="true" />拒绝
                              </button>
                            </div>
                          </div>
                        )}
                      </details>
                    )
                  })}
                </div>
              ) : <p className="chat-agent-iteration-empty">正在规划下一步。</p>}
              {iterationCache.map((item) => (
                <details key={`cache-${item.iteration}`} className="chat-agent-cache-details">
                  <summary>缓存详情</summary>
                  <dl>
                    <div><dt>状态</dt><dd>{item.mode ?? 'unsupported'}</dd></div>
                    <div><dt>读取 Token</dt><dd>{item.readTokens.toLocaleString('zh-CN')}</dd></div>
                    <div><dt>写入 Token</dt><dd>{item.writeTokens.toLocaleString('zh-CN')}</dd></div>
                    {(item.visibleObservationBytes !== null || item.toolCount !== null) && (
                      <div>
                        <dt>上下文</dt>
                        <dd>
                          {item.visibleObservationBytes ?? 0} B 可见结果
                          {item.toolCount !== null && ` · ${item.toolCount} 个工具`}
                        </dd>
                      </div>
                    )}
                  </dl>
                </details>
              ))}
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
          <h3>{run.clarification.resource_candidates.length > 0 ? '需要选择' : '需要补充信息'}</h3>
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
