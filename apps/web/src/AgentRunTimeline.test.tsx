import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AgentRunTimeline } from './AgentRunTimeline'
import type { AgentApproval, AgentRunEvent, AssistantRun } from './qa'

const run: AssistantRun = {
  run_id: 'run-1',
  user_message_id: 'message-1',
  created_at: '2026-08-11T10:00:00Z',
  updated_at: '2026-08-11T10:00:01Z',
  status: 'waiting_approval',
  run_kind: 'assistant_turn',
  error_code: null,
  selection: { source: 'none', skill: null },
  model_identity: 'fake',
  reasoning_profile: {
    schema_version: 'reasoning-profile-v1', requested_effort: 'auto', effective_effort: 'low',
    provider: 'fake', model: 'fake-fast-chat-v1', mapping_version: 'reasoning-mapping-v1',
    mode: 'native', downgrade_reason: 'none',
  },
  assistant_message: null,
  clarification: null,
  usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0, model_latency_ms: 0 },
}

const events: AgentRunEvent[] = [
  {
    schema_version: 'agent-run-sse-v3',
    event_id: 'event-1',
    run_id: 'run-1',
    sequence: 1,
    occurred_at: '2026-08-11T10:00:00Z',
    event_type: 'tool_requested',
    payload: {
      status: 'requested',
      iteration: 1,
      tool_name: 'shell_exec',
      tool_version: '1.0.0',
      command: 'python -m pytest',
      cwd: '.',
    },
  },
  {
    schema_version: 'agent-run-sse-v3',
    event_id: 'event-2',
    run_id: 'run-1',
    sequence: 2,
    occurred_at: '2026-08-11T10:00:01Z',
    event_type: 'approval_required',
    payload: {
      status: 'waiting_approval',
      iteration: 1,
      tool_name: 'shell_exec',
      tool_version: '1.0.0',
      approval_id: 'approval-1',
    },
  },
  {
    schema_version: 'agent-run-sse-v3',
    event_id: 'event-3',
    run_id: 'run-1',
    sequence: 3,
    occurred_at: '2026-08-11T10:00:02Z',
    event_type: 'tool_output',
    payload: {
      status: 'succeeded',
      iteration: 2,
      tool_name: 'shell_exec',
      tool_version: '1.0.0',
      command: 'python -m pytest',
      cwd: '.',
      exit_code: 0,
      output_preview: 'stdout:\n1 passed',
      output_truncated: true,
    },
  },
  {
    schema_version: 'agent-run-sse-v3',
    event_id: 'event-4',
    run_id: 'run-1',
    sequence: 4,
    occurred_at: '2026-08-11T10:00:03Z',
    event_type: 'tool_output',
    payload: {
      status: 'succeeded',
      iteration: 3,
      tool_name: 'fs_list',
      tool_version: '1.0.0',
      path: 'src',
      output_preview: 'file: src/main.py',
    },
  },
]

const approvals: AgentApproval[] = [
  {
    approval_id: 'approval-1',
    tool_name: 'shell_exec',
    tool_version: '1.0.0',
    status: 'pending',
    details: {},
  },
]

describe('AgentRunTimeline', () => {
  it('shows workspace details and lets a pending Tool be always allowed', () => {
    const onDecideApproval = vi.fn()
    const { container } = render(
      <AgentRunTimeline
        run={run}
        events={events}
        hasGroundedEvidence={false}
        clarificationPending={false}
        onSelectClarification={vi.fn()}
        onOpenEvidence={vi.fn()}
        approvals={approvals}
        approvalBusy={false}
        onDecideApproval={onDecideApproval}
      />,
    )

    const shellCards = screen.getAllByText('shell_exec').map((element) => element.closest('details'))
    const [pendingShellCard, outputShellCard] = shellCards
    if (!pendingShellCard || !outputShellCard) throw new Error('shell Tool cards were not rendered')
    fireEvent.click(pendingShellCard.querySelector('summary')!)

    const approvalButtons = pendingShellCard.querySelectorAll('.chat-agent-approval-actions button')
    expect(approvalButtons).toHaveLength(3)
    fireEvent.click(approvalButtons[1])
    expect(onDecideApproval).toHaveBeenCalledWith('approval-1', true, true)

    fireEvent.click(outputShellCard.querySelector('summary')!)
    expect(outputShellCard).toHaveTextContent('python -m pytest')
    expect(screen.getByText('Exit code')).toBeInTheDocument()

    const output = outputShellCard.querySelector('.chat-agent-tool-output')
    if (!output) throw new Error('shell output preview was not rendered')
    fireEvent.click(output.querySelector('summary')!)
    const outputText = output.querySelector('pre')
    if (!outputText) throw new Error('shell output text was not rendered')
    expect(outputText.textContent).toBe('stdout:\n1 passed')

    const listCard = screen.getByText('fs_list').closest('details')
    if (!listCard) throw new Error('list Tool card was not rendered')
    fireEvent.click(listCard.querySelector('summary')!)
    expect(screen.getByText('src')).toBeInTheDocument()
    const listOutput = listCard.querySelector('.chat-agent-tool-output')
    if (!listOutput) throw new Error('directory output preview was not rendered')
    fireEvent.click(listOutput.querySelector('summary')!)
    expect(screen.getByText('file: src/main.py')).toBeInTheDocument()
    expect(container.querySelectorAll('.chat-agent-tool-output')).toHaveLength(2)
  })

  it('does not present a generic clarification as a resource selector', () => {
    render(
      <AgentRunTimeline
        run={{
          ...run,
          status: 'waiting_clarification',
          clarification: {
            clarification_id: 'clarify-1',
            kind: 'input_required',
            message: 'Please provide the missing detail.',
            resource_candidates: [],
          },
        }}
        events={[]}
        hasGroundedEvidence={false}
        clarificationPending={false}
        onSelectClarification={vi.fn()}
        onOpenEvidence={vi.fn()}
        approvals={[]}
        approvalBusy={false}
        onDecideApproval={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: '需要补充信息' })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: '资源选择' })).not.toBeInTheDocument()
  })

  it('projects an unfinished Tool as failed when its parent Run has failed', () => {
    render(
      <AgentRunTimeline
        run={{ ...run, status: 'failed', error_code: 'QA_STRUCTURED_RESPONSE_INVALID' }}
        events={[
          {
            schema_version: 'agent-run-sse-v3',
            event_id: 'event-running',
            run_id: 'run-1',
            sequence: 1,
            occurred_at: '2026-08-12T04:05:00Z',
            event_type: 'tool_started',
            payload: {
              status: 'running',
              iteration: 1,
              tool_name: 'grounded_answer',
              tool_version: '1.1.0',
            },
          },
        ]}
        hasGroundedEvidence={false}
        clarificationPending={false}
        onSelectClarification={vi.fn()}
        onOpenEvidence={vi.fn()}
        approvals={[]}
        approvalBusy={false}
        onDecideApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('未完成')).toBeInTheDocument()
    expect(screen.getAllByText('QA_STRUCTURED_RESPONSE_INVALID')).toHaveLength(2)
    expect(screen.queryByText('执行中')).not.toBeInTheDocument()
  })

  it('renders v4 native Tool-use projections without raw bodies', () => {
    const v4Events: AgentRunEvent[] = [
      {
        schema_version: 'agent-run-sse-v4',
        event_id: 'v4-accepted',
        run_id: 'run-1',
        sequence: 1,
        occurred_at: '2026-08-13T05:00:00Z',
        event_type: 'accepted',
        payload: { status: 'accepted', harness_version: 'native-tool-use-v2' },
      },
      {
        schema_version: 'agent-run-sse-v4',
        event_id: 'v4-skill',
        run_id: 'run-1',
        sequence: 2,
        occurred_at: '2026-08-13T05:00:01Z',
        event_type: 'skill_activated',
        payload: {
          status: 'activated',
          iteration: 1,
          skill_name: 'knowledge_agent',
          skill_version: '2.0.0',
        },
      },
      {
        schema_version: 'agent-run-sse-v4',
        event_id: 'v4-tool-started',
        run_id: 'run-1',
        sequence: 3,
        occurred_at: '2026-08-13T05:00:02Z',
        event_type: 'tool_started',
        payload: {
          status: 'running',
          iteration: 1,
          tool_name: 'knowledge_retrieve',
          tool_version: '1.0.0',
          tool_family: 'knowledge',
        },
      },
      {
        schema_version: 'agent-run-sse-v4',
        event_id: 'v4-tool-output',
        run_id: 'run-1',
        sequence: 4,
        occurred_at: '2026-08-13T05:00:03Z',
        event_type: 'tool_output',
        payload: {
          status: 'succeeded',
          iteration: 1,
          tool_name: 'knowledge_retrieve',
          tool_version: '1.0.0',
          tool_family: 'knowledge',
          decision_summary: 'Coverage: 1 matched across 1 searches.',
          visible_observation_bytes: 80,
        },
      },
      {
        schema_version: 'agent-run-sse-v4',
        event_id: 'v4-cache',
        run_id: 'run-1',
        sequence: 5,
        occurred_at: '2026-08-13T05:00:04Z',
        event_type: 'cache_used',
        payload: {
          iteration: 1,
          cache_mode: 'requested',
          cache_read_tokens: 3,
          cache_write_tokens: 2,
          context_digest: 'sha256:' + 'a'.repeat(64),
        },
      },
      {
        schema_version: 'agent-run-sse-v4',
        event_id: 'v4-completed',
        run_id: 'run-1',
        sequence: 6,
        occurred_at: '2026-08-13T05:00:05Z',
        event_type: 'completed',
        payload: {
          status: 'completed',
          iteration: 1,
          stop_reason: 'goal_complete',
          terminal_kind: 'grounded',
        },
      },
    ]

    render(
      <AgentRunTimeline
        run={{ ...run, status: 'completed' }}
        events={v4Events}
        hasGroundedEvidence={false}
        clarificationPending={false}
        onSelectClarification={vi.fn()}
        onOpenEvidence={vi.fn()}
        approvals={[]}
        approvalBusy={false}
        onDecideApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('native-tool-use-v2')).toBeInTheDocument()
    expect(screen.getByText('fake-fast-chat-v1')).toBeInTheDocument()
    expect(document.querySelector('.chat-agent-timeline .chat-agent-iterations')).toBeNull()
    expect(document.querySelector('.chat-agent-run > .chat-agent-iterations')).not.toBeNull()
    expect(document.querySelector('.chat-agent-iteration-heading')).toBeNull()
    const nativeTool = screen.getByText('knowledge_retrieve').closest('details')
    const nativeCache = document.querySelector('.chat-agent-cache-details')
    if (!nativeTool || !nativeCache) throw new Error('native Tool or cache details were not rendered')
    expect(nativeTool.compareDocumentPosition(nativeCache) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByText('模型耗时')).not.toBeInTheDocument()
    expect(screen.getByText('Coverage: 1 matched across 1 searches.')).toBeInTheDocument()
    expect(screen.getByText('缓存详情')).toBeInTheDocument()
    expect(screen.getByText('知识问答终态')).toBeInTheDocument()
    expect(document.querySelector('.chat-agent-cache-details')).not.toBeNull()
  })

  it('uses wall-clock and event timing while merging safe Tool details without hashes', () => {
    const digest = 'a'.repeat(64)
    const timingEvents: AgentRunEvent[] = [
      {
        schema_version: 'agent-run-sse-v3', event_id: 'timing-request', run_id: 'run-1', sequence: 1,
        occurred_at: '2026-08-14T10:00:01Z', event_type: 'tool_requested',
        payload: {
          status: 'requested', iteration: 1, tool_name: 'knowledge_search', tool_version: '1.0.0',
          query_preview: 'Which sources describe the architecture?', input_summary: `sha256:${digest}`,
        },
      },
      {
        schema_version: 'agent-run-sse-v3', event_id: 'timing-start', run_id: 'run-1', sequence: 2,
        occurred_at: '2026-08-14T10:00:02Z', event_type: 'tool_started',
        payload: {
          status: 'running', iteration: 1, tool_name: 'knowledge_search', tool_version: '1.0.0',
          input_summary: `sha256:${digest}`,
        },
      },
      {
        schema_version: 'agent-run-sse-v3', event_id: 'timing-output', run_id: 'run-1', sequence: 3,
        occurred_at: '2026-08-14T10:00:04Z', event_type: 'tool_output',
        payload: {
          status: 'succeeded', iteration: 1, tool_name: 'knowledge_search', tool_version: '1.0.0',
          output_summary: 'Found 2 matching sources.', duration_ms: 0, retry_count: 1,
        },
      },
      {
        schema_version: 'agent-run-sse-v3', event_id: 'timing-cache', run_id: 'run-1', sequence: 4,
        occurred_at: '2026-08-14T10:00:04Z', event_type: 'cache_used',
        payload: {
          iteration: 1, cache_mode: 'requested', cache_read_tokens: 4, cache_write_tokens: 1,
          context_digest: `sha256:${digest}`, visible_observation_bytes: 96, tool_count: 3,
        },
      },
      {
        schema_version: 'agent-run-sse-v3', event_id: 'timing-completed', run_id: 'run-1', sequence: 5,
        occurred_at: '2026-08-14T10:00:05Z', event_type: 'completed',
        payload: { status: 'completed', iteration: 1, stop_reason: 'goal_complete' },
      },
    ]

    render(
      <AgentRunTimeline
        run={{
          ...run,
          status: 'completed',
          created_at: '2026-08-14T10:00:00Z',
          updated_at: '2026-08-14T10:00:05Z',
        }}
        events={timingEvents}
        hasGroundedEvidence={false}
        clarificationPending={false}
        onSelectClarification={vi.fn()}
        onOpenEvidence={vi.fn()}
        approvals={[]}
        approvalBusy={false}
        onDecideApproval={vi.fn()}
      />,
    )

    expect(screen.getByText('总耗时')).toBeInTheDocument()
    expect(screen.getByText('5.0 秒')).toBeInTheDocument()
    const cacheDetails = document.querySelector('.chat-agent-cache-details')
    if (!cacheDetails) throw new Error('cache details were not rendered')
    fireEvent.click(cacheDetails.querySelector('summary')!)
    expect(cacheDetails).toHaveTextContent('上下文')
    expect(cacheDetails).toHaveTextContent('96 B')
    expect(cacheDetails).toHaveTextContent('3 个工具')
    expect(screen.queryByText(digest)).not.toBeInTheDocument()
    expect(screen.queryByText(`sha256:${digest}`)).not.toBeInTheDocument()

    const toolCard = screen.getByText('knowledge_search').closest('details')
    if (!toolCard) throw new Error('knowledge search Tool card was not rendered')
    fireEvent.click(toolCard.querySelector('summary')!)
    expect(screen.getByText('Which sources describe the architecture?')).toBeInTheDocument()
    expect(screen.getByText('Found 2 matching sources.')).toBeInTheDocument()
    expect(screen.getByText('2.0 秒')).toBeInTheDocument()
    expect(screen.queryByText('0 ms')).not.toBeInTheDocument()
  })
})
