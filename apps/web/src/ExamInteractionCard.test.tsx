import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ExamInteractionCard } from './ExamInteractionCard'
import type { ExamSession } from './qa'

const session: ExamSession = {
  schema_version: 'exam-session-v1', session_id: 'session', conversation_id: 'conversation', phase: 'broad_quiz', revision: 2,
  interaction: {
    interaction_id: 'interaction', interaction_version: 'exam-interaction-v1', kind: 'quiz', title: '快速诊断', instructions: ['答案隔离'], progress: { current: 2, total: 9, label: '诊断' }, next_action: 'submit_broad_answers',
    paper: { paper_id: 'paper', paper_version: 1, title: '快速诊断', suggested_minutes: 10, total_points: 10, sections: [{ section_id: 'objective', title: '选择题', questions: [{ question_id: 'Q1', kind: 'single_choice', prompt: '复杂度？', points: 10, required: true, citation_ids: ['evidence'], options: [{ option_id: 'A', text: 'O(1)' }, { option_id: 'B', text: 'O(n)' }] }] }] },
  },
}

describe('ExamInteractionCard', () => {
  it('requires answers and submits a numbered objective payload', () => {
    const submit = vi.fn()
    render(<ExamInteractionCard interaction={session.interaction} submitted={false} submitting={false} onSubmit={submit} />)
    const button = screen.getByRole('button', { name: '作为消息发送答案' })
    expect(button).toBeDisabled()
    fireEvent.click(screen.getByLabelText(/B O\(n\)/))
    fireEvent.click(button)
    expect(submit).toHaveBeenCalledWith([{ question_id: 'Q1', selected_options: ['B'] }])
  })

  it('renders inline without the fixed workflow progress control', () => {
    const { container } = render(<ExamInteractionCard interaction={session.interaction} submitted={false} submitting={false} onSubmit={() => undefined} />)
    expect(container.querySelector('progress')).not.toBeInTheDocument()
    expect(container.querySelector('.exam-answer-options')).toBeInTheDocument()
  })
})
