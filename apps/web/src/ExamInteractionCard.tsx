import { Check, LoaderCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { ExamAnswer, ExamInteraction, ExamQuestion } from './qa'

type Props = {
  interaction: ExamInteraction
  submitted: boolean
  submitting: boolean
  onSubmit: (answers: ExamAnswer[]) => void
}

function answerFor(question: ExamQuestion, value: string | string[]): ExamAnswer {
  return question.kind === 'single_choice' || question.kind === 'multiple_choice'
    ? { question_id: question.question_id, selected_options: Array.isArray(value) ? value : [value] }
    : { question_id: question.question_id, response_text: String(value) }
}

function StructuredContent({ value }: { value: unknown }) {
  if (Array.isArray(value)) return <div className="exam-content-list">{value.map((item, index) => <StructuredContent key={index} value={item} />)}</div>
  if (value && typeof value === 'object') return (
    <dl className="exam-content-grid">
      {Object.entries(value).map(([key, item]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd><StructuredContent value={item} /></dd></div>)}
    </dl>
  )
  return <span>{String(value ?? '')}</span>
}

export function ExamInteractionCard({ interaction, submitted, submitting, onSubmit }: Props) {
  const [values, setValues] = useState<Record<string, string | string[]>>({})
  const questions = useMemo(() => interaction.paper?.sections.flatMap((section) => section.questions) ?? [], [interaction.paper])
  const answers = questions.flatMap((question) => values[question.question_id] === undefined ? [] : [answerFor(question, values[question.question_id])])
  const requiredComplete = questions.every((question) => !question.required || values[question.question_id] !== undefined)

  return (
    <div className="exam-answer-options" aria-labelledby={`exam-${interaction.interaction_id}`}>
      <h3 id={`exam-${interaction.interaction_id}`}>{interaction.title}</h3>
      {interaction.instructions.map((instruction) => <p className="exam-instruction" key={instruction}>{instruction}</p>)}
      {interaction.paper?.sections.map((section) => (
        <section key={section.section_id} aria-label={section.title}>
          <div className="exam-questions">
            {section.questions.map((question) => (
              <fieldset key={question.question_id}>
                <legend><strong>{question.question_id}</strong> {question.prompt} <small>{question.points} 分</small></legend>
                {question.options ? question.options.map((option) => {
                  const multiple = question.kind === 'multiple_choice'
                  const selected = values[question.question_id]
                  const checked = Array.isArray(selected) ? selected.includes(option.option_id) : selected === option.option_id
                  return <label className="exam-option" data-selected={checked} key={option.option_id}><input disabled={submitted} type={multiple ? 'checkbox' : 'radio'} name={question.question_id} checked={checked} onChange={() => setValues((current) => ({ ...current, [question.question_id]: multiple ? (checked ? (current[question.question_id] as string[]).filter((id) => id !== option.option_id) : [...((current[question.question_id] as string[] | undefined) ?? []), option.option_id]) : option.option_id }))} /><span><b>{option.option_id}</b> {option.text}</span></label>
                }) : <textarea aria-label={`${question.question_id} 作答`} disabled={submitted} rows={question.kind === 'programming' ? 8 : 4} value={typeof values[question.question_id] === 'string' ? values[question.question_id] : ''} onChange={(event) => setValues((current) => ({ ...current, [question.question_id]: event.target.value }))} />}
              </fieldset>
            ))}
          </div>
        </section>
      ))}
      {interaction.content !== undefined && <StructuredContent value={interaction.content} />}
      {submitted && <p className="exam-instruction">已通过对话提交</p>}
      {!submitted && interaction.next_action && <button className="exam-primary" type="button" disabled={submitting || !requiredComplete} onClick={() => onSubmit(answers)}>{submitting ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}{questions.length ? '作为消息发送答案' : '继续'}</button>}
    </div>
  )
}
