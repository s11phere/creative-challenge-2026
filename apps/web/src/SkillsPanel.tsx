import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  FlaskConical,
  LoaderCircle,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Trash2,
  Wand2,
  XCircle,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  activateDraft,
  createDraft,
  createPersonalSkill,
  deleteDraft,
  deletePersonalSkill,
  fetchDraftFiles,
  fetchDrafts,
  fetchPersonalSkills,
  fetchSkillSuggestions,
  fetchSkills,
  runDraftEval,
  setPersonalSkillActivation,
  setSkillActivation,
  updateDraft,
  updatePersonalSkill,
  fetchDraftEvidence,
  type PersonalSkill,
  type SkillDraft,
  type SkillDraftEvidence,
  type SkillDraftEval,
  type SkillSuggestion,
  type SkillVersion,
} from './qa'

function SkillRow({ skill }: { skill: SkillVersion }) {
  const [expanded, setExpanded] = useState(false)
  const queryClient = useQueryClient()
  const detailsId = `skill-${skill.name}-details`
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['skills'] })
    void queryClient.invalidateQueries({ queryKey: ['assistant-commands'] })
  }
  const activationMutation = useMutation({
    mutationFn: () => setSkillActivation(skill.name, !skill.active),
    onSuccess: invalidate,
  })
  return (
    <div className={`skill-row${expanded ? ' expanded' : ''}`}>
      <button
        type="button"
        className="skill-summary"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-controls={detailsId}
      >
        <span className="skill-icon"><Bot size={18} /></span>
        <span className="skill-name">
          <strong>{skill.name}</strong>
          <span>固定版本 v{skill.version}</span>
        </span>
        <span className="skill-active-version">{skill.active ? '已激活' : '未激活'}</span>
        <ChevronDown className={expanded ? 'skill-chevron skill-chevron-open' : 'skill-chevron'} size={16} aria-hidden="true" />
      </button>
      {expanded && (
        <div className="skill-versions" id={detailsId}>
          <div className="skill-version">
            <div className="skill-version-heading">
              <div>
                <strong>v{skill.version}</strong>
                <span className="skill-active-version">{skill.active ? '已激活' : '未激活'}</span>
              </div>
              <button
                type="button"
                className="panel-action-button"
                onClick={() => activationMutation.mutate()}
                disabled={activationMutation.isPending}
                aria-label={`${skill.active ? '停用' : '激活'} ${skill.name}`}
              >
                <Power size={14} />{skill.active ? '停用' : '激活'}
              </button>
            </div>
            <p>{skill.description}</p>
            <dl className="skill-version-meta">
              <div><dt>权限</dt><dd>{skill.permissions.join(', ') || '无'}</dd></div>
              <div><dt>能力</dt><dd>{skill.required_capabilities.join(', ') || '无'}</dd></div>
              <div><dt>预算</dt><dd>{skill.budget.max_steps} 步 / {skill.budget.max_tool_calls} Tool / {skill.budget.timeout_seconds}s</dd></div>
            </dl>
          </div>
        </div>
      )}
    </div>
  )
}

type SkillEditor = {
  kind: 'personal' | 'draft'
  mode: 'create' | 'edit'
  targetName: string | null
  name: string
  filesText: string
  error: string | null
}

function SkillEditorForm({
  editor,
  setEditor,
}: {
  editor: SkillEditor
  setEditor: (editor: SkillEditor | null) => void
}) {
  const queryClient = useQueryClient()
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['personal-skills'] })
    void queryClient.invalidateQueries({ queryKey: ['skill-drafts'] })
  }
  const close = () => setEditor(null)

  const createPersonal = useMutation({
    mutationFn: ({ name, files }: { name: string; files: Record<string, string> }) => createPersonalSkill(name, files),
    onSuccess: () => { invalidate(); close() },
  })
  const updatePersonal = useMutation({
    mutationFn: ({ name, files }: { name: string; files: Record<string, string> }) => updatePersonalSkill(name, files),
    onSuccess: () => { invalidate(); close() },
  })
  const createDraftMutation = useMutation({
    mutationFn: ({ name, files }: { name: string; files: Record<string, string> }) => createDraft(name, files),
    onSuccess: () => { invalidate(); close() },
  })
  const updateDraftMutation = useMutation({
    mutationFn: ({ name, files }: { name: string; files: Record<string, string> }) => updateDraft(name, files),
    onSuccess: () => { invalidate(); close() },
  })

  function parseFiles(): Record<string, string> | null {
    try {
      const value: unknown = JSON.parse(editor.filesText)
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error('package must be a JSON object of {path: content}')
      }
      for (const [path, content] of Object.entries(value as Record<string, unknown>)) {
        if (typeof content !== 'string') {
          throw new Error(`file "${path}" must be a string`)
        }
      }
      return value as Record<string, string>
    } catch (error) {
      setEditor({ ...editor, error: error instanceof Error ? error.message : 'invalid package JSON' })
      return null
    }
  }

  function submit() {
    const files = parseFiles()
    if (files === null) return
    const name = editor.name.trim()
    if (editor.kind === 'draft') {
      if (editor.mode === 'edit' && editor.targetName !== null) {
        updateDraftMutation.mutate({ name: editor.targetName, files })
      } else {
        createDraftMutation.mutate({ name, files })
      }
    } else if (editor.mode === 'edit' && editor.targetName !== null) {
      updatePersonal.mutate({ name: editor.targetName, files })
    } else {
      createPersonal.mutate({ name, files })
    }
  }

  const pending = createPersonal.isPending || updatePersonal.isPending
    || createDraftMutation.isPending || updateDraftMutation.isPending
  const busy = pending || (editor.mode === 'create' && !editor.name.trim())
  const apiError = createPersonal.error ?? updatePersonal.error
    ?? createDraftMutation.error ?? updateDraftMutation.error
  const isDraft = editor.kind === 'draft'

  return (
    <div className="personal-skill-editor">
      <div className="personal-skill-editor-fields">
        <label>
          名称
          <input
            value={editor.targetName ?? editor.name}
            onChange={(event) => setEditor({ ...editor, name: event.target.value })}
            placeholder="my_skill"
            disabled={editor.mode === 'edit'}
            spellCheck={false}
          />
        </label>
        <label>
          包文件 JSON（path → content，必须含 skill.yaml）
          <textarea
            value={editor.filesText}
            onChange={(event) => setEditor({ ...editor, filesText: event.target.value })}
            placeholder='{"skill.yaml": "...", "workflow.yaml": "...", ...}'
            rows={8}
            spellCheck={false}
          />
        </label>
      </div>
      {editor.error !== null && <p className="skill-error">{editor.error}</p>}
      {apiError !== null && <p className="skill-error">保存失败：{apiError.message}</p>}
      <div className="skill-version-actions">
        <button type="button" className="panel-action-button" onClick={submit} disabled={busy}>
          {editor.mode === 'edit' ? '保存修改' : isDraft ? '创建草稿' : '创建个人 Skill'}
        </button>
        <button type="button" className="panel-action-button" onClick={close}>
          取消
        </button>
      </div>
    </div>
  )
}

function PersonalSkillRow({
  skill,
  onEdit,
}: {
  skill: PersonalSkill
  onEdit: (skill: PersonalSkill) => void
}) {
  const queryClient = useQueryClient()
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['personal-skills'] })
    void queryClient.invalidateQueries({ queryKey: ['assistant-commands'] })
  }
  const activationMutation = useMutation({
    mutationFn: () => setPersonalSkillActivation(skill.name, !skill.active),
    onSuccess: invalidate,
  })
  const deleteMutation = useMutation({
    mutationFn: () => deletePersonalSkill(skill.name),
    onSuccess: invalidate,
  })
  const pending = activationMutation.isPending || deleteMutation.isPending

  return (
    <div className="skill-row">
      <div className="skill-summary">
        <span className="skill-icon"><Bot size={18} /></span>
        <span className="skill-name">
          <strong>{skill.name}</strong>
          <span>{skill.active ? '已激活' : '未激活'} · v{skill.version}</span>
        </span>
      </div>
      <div className="skill-version-actions">
        <button
          type="button"
          className="panel-action-button"
          onClick={() => activationMutation.mutate()}
          disabled={pending}
          aria-label={`${skill.active ? '停用' : '激活'} ${skill.name}`}
        >
          <Power size={14} />{skill.active ? '停用' : '激活'}
        </button>
        <button
          type="button"
          className="panel-action-button"
          onClick={() => onEdit(skill)}
          disabled={pending}
          aria-label={`编辑 ${skill.name}`}
          title="编辑个人 Skill（激活状态下直接编辑会同步激活指针）"
        >
          <Pencil size={14} />编辑
        </button>
        <button
          type="button"
          className="panel-action-button"
          onClick={() => deleteMutation.mutate()}
          disabled={pending}
          aria-label={`删除 ${skill.name}`}
          title="删除个人 Skill（激活状态下会同时移除激活记录）"
        >
          <Trash2 size={14} />删除
        </button>
      </div>
    </div>
  )
}

function DraftRow({
  draft,
  onEdit,
}: {
  draft: SkillDraft
  onEdit: (name: string, files: Record<string, string>) => void
}) {
  const queryClient = useQueryClient()
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['skill-drafts'] })
    void queryClient.invalidateQueries({ queryKey: ['personal-skills'] })
    void queryClient.invalidateQueries({ queryKey: ['assistant-commands'] })
  }
  const [evalResult, setEvalResult] = useState<SkillDraftEval | null>(null)
  const [runningEval, setRunningEval] = useState(false)
  const [evalError, setEvalError] = useState<string | null>(null)
  const [evidence, setEvidence] = useState<SkillDraftEvidence['evidence'] | undefined>(undefined)
  useEffect(() => {
    let active = true
    fetchDraftEvidence(draft.name)
      .then(({ evidence: attached }) => {
        if (active) setEvidence(attached)
      })
      .catch(() => {
        if (active) setEvidence(null)
      })
    return () => {
      active = false
    }
  }, [draft.name])
  const activateMutation = useMutation({
    mutationFn: () => activateDraft(draft.name),
    onSuccess: () => { invalidate(); setEvalResult(null) },
  })
  const rejectMutation = useMutation({
    mutationFn: () => deleteDraft(draft.name),
    onSuccess: invalidate,
  })
  const pending = activateMutation.isPending || rejectMutation.isPending || runningEval

  function handleEval() {
    setEvalError(null)
    setRunningEval(true)
    runDraftEval(draft.name)
      .then(setEvalResult)
      .catch((error) => setEvalError(error instanceof Error ? error.message : 'eval failed'))
      .finally(() => setRunningEval(false))
  }

  function handleEdit() {
    setEvalError(null)
    fetchDraftFiles(draft.name)
      .then(({ files }) => onEdit(draft.name, files))
      .catch((error) => setEvalError(error instanceof Error ? error.message : 'load failed'))
  }

  const statusText = !draft.complete ? '不完整' : draft.valid ? '已校验' : '未通过校验'

  return (
    <div className="skill-row">
      <div className="skill-summary">
        <span className="skill-icon"><FlaskConical size={18} /></span>
        <span className="skill-name">
          <strong>{draft.name}</strong>
          <span>{statusText} · {draft.file_count} 文件</span>
          {evidence !== null && evidence !== undefined && (
            <span className="skill-eval-badge">
              {`候选模式 · ${evidence.frequency} 次 / ${evidence.distinct_conversations} 会话 · ${evidence.exemplars.length} 个来源 run`}
            </span>
          )}
        </span>
      </div>
      {evalResult !== null && (
        <div className={`skill-eval-badge${evalResult.gate_passed ? ' eval-passed' : ' eval-failed'}`}>
          {evalResult.gate_passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
          {evalResult.gate_passed
            ? `门禁通过 ${evalResult.metrics.passed}/${evalResult.metrics.total}`
            : `门禁未过 ${evalResult.metrics.passed}/${evalResult.metrics.total}`}
        </div>
      )}
      {evalError !== null && <p className="skill-error">{evalError}</p>}
      <div className="skill-version-actions">
        <button
          type="button"
          className="panel-action-button"
          onClick={handleEval}
          disabled={pending || !draft.complete}
          aria-label={`运行 eval ${draft.name}`}
          title={draft.complete ? '运行确定性 eval 门禁' : '草稿不完整，无法运行 eval'}
        >
          <FlaskConical size={14} />运行 eval
        </button>
        <button
          type="button"
          className="panel-action-button"
          onClick={() => activateMutation.mutate()}
          disabled={pending || !draft.valid}
          aria-label={`激活 ${draft.name}`}
          title={draft.valid ? '校验通过后可激活（后端会重跑 eval 门禁）' : '校验未通过，无法激活'}
        >
          <Power size={14} />激活
        </button>
        <button
          type="button"
          className="panel-action-button"
          onClick={handleEdit}
          disabled={pending}
          aria-label={`编辑草稿 ${draft.name}`}
        >
          <Pencil size={14} />编辑
        </button>
        <button
          type="button"
          className="panel-action-button"
          onClick={() => rejectMutation.mutate()}
          disabled={pending}
          aria-label={`拒绝草稿 ${draft.name}`}
          title="拒绝并删除该草稿"
        >
          <Trash2 size={14} />拒绝
        </button>
      </div>
    </div>
  )
}

function SuggestionRow({
  suggestion,
  onScaffold,
}: {
  suggestion: SkillSuggestion
  onScaffold: (suggestion: SkillSuggestion) => void
}) {
  return (
    <div className="skill-row suggestion-row">
      <div className="skill-summary">
        <span className="skill-icon"><Wand2 size={18} /></span>
        <span className="skill-name">
          <strong>{suggestion.name}</strong>
          <span>{suggestion.description}</span>
        </span>
      </div>
      <div className="skill-version-actions">
        <button
          type="button"
          className="panel-action-button"
          onClick={() => onScaffold(suggestion)}
          aria-label={`从建议创建草稿 ${suggestion.name}`}
        >
          <Plus size={14} />创建
        </button>
      </div>
    </div>
  )
}

export function SkillsPanel() {
  const [editor, setEditor] = useState<SkillEditor | null>(null)
  const skillsQuery = useQuery({
    queryKey: ['skills'],
    queryFn: ({ signal }) => fetchSkills(signal),
    retry: false,
  })
  const personalQuery = useQuery({
    queryKey: ['personal-skills'],
    queryFn: ({ signal }) => fetchPersonalSkills(signal),
    retry: false,
  })
  const draftsQuery = useQuery({
    queryKey: ['skill-drafts'],
    queryFn: ({ signal }) => fetchDrafts(signal),
    retry: false,
  })
  const suggestionsQuery = useQuery({
    queryKey: ['skill-suggestions'],
    queryFn: ({ signal }) => fetchSkillSuggestions(signal),
    retry: false,
  })

  const refresh = () => {
    void skillsQuery.refetch()
    void personalQuery.refetch()
    void draftsQuery.refetch()
    void suggestionsQuery.refetch()
  }
  const isRefreshing = skillsQuery.isFetching || personalQuery.isFetching
    || draftsQuery.isFetching || suggestionsQuery.isFetching

  function openDraftEdit(name: string, files: Record<string, string>) {
    setEditor({
      kind: 'draft',
      mode: 'edit',
      targetName: name,
      name,
      filesText: JSON.stringify(files, null, 2),
      error: null,
    })
  }

  function openSuggestion(suggestion: SkillSuggestion) {
    setEditor({
      kind: 'draft',
      mode: 'create',
      targetName: null,
      name: suggestion.name,
      filesText: JSON.stringify(defaultScaffold(suggestion.name, suggestion.description), null, 2),
      error: null,
    })
  }

  return (
    <section className="status-panel" aria-labelledby="skills-title">
      <div className="panel-heading">
        <div><h2 id="skills-title">技能</h2><p>当前已安装的固定版本</p></div>
        <div className="panel-actions">
          <span className="service-count">{skillsQuery.data?.length ?? 0}</span>
          <button
            type="button"
            className="panel-action-button"
            onClick={refresh}
            disabled={isRefreshing}
            aria-label="刷新技能"
            title="刷新技能"
          >
            <RefreshCw className={isRefreshing ? 'spin' : ''} size={16} />
          </button>
        </div>
      </div>
      {skillsQuery.isLoading ? (
        <div className="loading-row"><LoaderCircle className="spin" size={18} />加载 Skill...</div>
      ) : skillsQuery.isError ? (
        <div className="empty-state"><CircleAlert size={24} /><p>无法加载 Skill</p></div>
      ) : (
        <div className="skills-list">
          {skillsQuery.data?.map((skill) => <SkillRow key={skill.name} skill={skill} />)}
        </div>
      )}

      <div className="panel-heading personal-skills-heading">
        <div><h3>个人 Skill</h3><p>用户可写，仅组合已有 Tool/handler</p></div>
        <div className="panel-actions">
          <button
            type="button"
            className="panel-action-button"
            onClick={() => setEditor({ kind: 'personal', mode: 'create', targetName: null, name: '', filesText: '', error: null })}
            aria-label="新建个人 Skill"
          >
            <Plus size={14} />新建
          </button>
        </div>
      </div>
      {editor !== null && editor.kind === 'personal' ? (
        <SkillEditorForm editor={editor} setEditor={setEditor} />
      ) : null}
      {personalQuery.isLoading ? (
        <div className="loading-row"><LoaderCircle className="spin" size={18} />加载个人 Skill...</div>
      ) : personalQuery.isError ? (
        <div className="empty-state"><CircleAlert size={24} /><p>无法加载个人 Skill</p></div>
      ) : (
        <div className="skills-list">
          {personalQuery.data?.map((skill) => (
            <PersonalSkillRow
              key={skill.name}
              skill={skill}
              onEdit={(target) => setEditor({
                kind: 'personal',
                mode: 'edit',
                targetName: target.name,
                name: target.name,
                filesText: '',
                error: null,
              })}

            />
          ))}
        </div>
      )}

      <div className="panel-heading personal-skills-heading">
        <div><h3>草稿</h3><p>Skill Creator 产物，校验 + eval 门禁通过后可激活</p></div>
        <div className="panel-actions">
          <button
            type="button"
            className="panel-action-button"
            onClick={() => setEditor({ kind: 'draft', mode: 'create', targetName: null, name: '', filesText: '', error: null })}
            aria-label="新建草稿"
          >
            <Plus size={14} />新建草稿
          </button>
        </div>
      </div>
      {editor !== null && editor.kind === 'draft' ? (
        <SkillEditorForm editor={editor} setEditor={setEditor} />
      ) : null}
      {draftsQuery.isLoading ? (
        <div className="loading-row"><LoaderCircle className="spin" size={18} />加载草稿...</div>
      ) : draftsQuery.isError ? (
        <div className="empty-state"><CircleAlert size={24} /><p>无法加载草稿</p></div>
      ) : (draftsQuery.data?.length ?? 0) === 0 ? (
        <div className="empty-state"><p>暂无草稿</p></div>
      ) : (
        <div className="skills-list">
          {draftsQuery.data?.map((draft) => (
            <DraftRow key={draft.name} draft={draft} onEdit={openDraftEdit} />
          ))}
        </div>
      )}

      <div className="panel-heading personal-skills-heading">
        <div><h3>个性化建议</h3><p>基于使用痕迹，达到频率阈值才会出现</p></div>
      </div>
      {suggestionsQuery.isLoading ? (
        <div className="loading-row"><LoaderCircle className="spin" size={18} />加载建议...</div>
      ) : suggestionsQuery.isError ? (
        <div className="empty-state"><CircleAlert size={24} /><p>无法加载建议</p></div>
      ) : (suggestionsQuery.data?.length ?? 0) === 0 ? (
        <div className="empty-state"><p>暂无足够的使用模式建议</p></div>
      ) : (
        <div className="skills-list">
          {suggestionsQuery.data?.map((suggestion) => (
            <SuggestionRow key={suggestion.name} suggestion={suggestion} onScaffold={openSuggestion} />
          ))}
        </div>
      )}
    </section>
  )
}

function defaultScaffold(name: string, description: string): Record<string, string> {
  const manifest = {
    manifest_version: '1',
    name,
    version: '1.0.0',
    description,
    input_schema: 'schemas/input.json',
    output_schema: 'schemas/output.json',
    required_tools: [],
    required_capabilities: [],
    permissions: ['read_knowledge', 'model'],
    budgets: { max_steps: 8, max_tool_calls: 4, max_input_tokens: 8192, max_output_tokens: 4096, timeout_seconds: 60 },
    entrypoint: 'workflow.yaml',
    compatibility: { runtime: '>=0.1.0,<1.0.0', checkpoint_schema_versions: [1] },
    prompts: ['prompts/system.md'],
    evals: ['evals/cases.jsonl'],
  }
  const workflow = `workflow_version: "1"
start: plan
nodes:
  - id: plan
    step: planning
    handler: grounded_qa_plan
    next: retrieve
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: retrieve
    step: retrieving
    handler: grounded_qa_plan
    next: delegate
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: delegate
    step: executing
    handler: grounded_qa_delegate
    next: verify
    max_retries: 0
    required_permissions: [read_knowledge]
    reserve: {tool_calls: 0, input_tokens: 8192, output_tokens: 4096}
  - id: verify
    step: verifying
    handler: grounded_qa_verify
    next: null
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}`
  return {
    'skill.yaml': JSON.stringify(manifest, null, 2),
    'workflow.yaml': workflow,
    'schemas/input.json': JSON.stringify(
      { type: 'object', additionalProperties: false, properties: { question: { type: 'string', minLength: 1 } }, required: ['question'] },
      null,
      2,
    ),
    'schemas/output.json': JSON.stringify(
      { type: 'object', additionalProperties: false, properties: { status: { type: 'string' }, result: { type: 'string' } }, required: ['status', 'result'] },
      null,
      2,
    ),
    'prompts/system.md': 'Return only the structure declared by the output schema.\n',
    'evals/cases.jsonl': '{"case_id":"scaffold-001","input":{"question":"fixture question"},"expected":"complete","checks":[{"type":"output_has_key","key":"status"},{"type":"output_has_key","key":"result"},{"type":"finalized"}]}\n',
  }
}
