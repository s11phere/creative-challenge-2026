import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AgentRunTimeline } from './AgentRunTimeline'
import type { AgentApproval, AgentRunEvent, AssistantRun } from './qa'

const run: AssistantRun = {
  run_id: 'run-1',
  user_message_id: 'message-1',
  status: 'waiting_approval',
  run_kind: 'assistant_turn',
  error_code: null,
  selection: { source: 'none', skill: null },
  model_identity: 'fake',
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
    expect(screen.getByText('python -m pytest')).toBeInTheDocument()
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
})
