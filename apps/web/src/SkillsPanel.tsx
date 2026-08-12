import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, ChevronDown, CircleAlert, LoaderCircle, Pencil, Plus, Power, RefreshCw, Trash2 } from 'lucide-react'
import { useState } from 'react'
import {
  activatePersonalSkill,
  createPersonalSkill,
  deletePersonalSkill,
  fetchPersonalSkills,
  fetchSkills,
  type PersonalSkill,
  type SkillVersion,
} from './qa'

function SkillRow({ skill }: { skill: SkillVersion }) {
  const [expanded, setExpanded] = useState(false)
  const detailsId = `skill-${skill.name}-details`
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
        <ChevronDown className={expanded ? 'skill-chevron skill-chevron-open' : 'skill-chevron'} size={16} aria-hidden="true" />
      </button>
      {expanded && (
        <div className="skill-versions" id={detailsId}>
          <div className="skill-version">
            <div className="skill-version-heading">
              <div>
                <strong>v{skill.version}</strong>
              </div>
            </div>
            <p>{skill.description}</p>
            <dl className="skill-version-meta">
              <div><dt>摘要</dt><dd title={skill.content_sha256}>{skill.content_sha256}</dd></div>
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
  mode: 'closed' | 'create' | 'edit'
  target: PersonalSkill | null
  name: string
  filesText: string
  error: string | null
}

const CLOSED_EDITOR: SkillEditor = { mode: 'closed', target: null, name: '', filesText: '', error: null }
const CREATE_EDITOR: SkillEditor = { mode: 'create', target: null, name: '', filesText: '', error: null }

function PersonalSkillEditor({
  editor,
  setEditor,
}: {
  editor: SkillEditor
  setEditor: (editor: SkillEditor) => void
}) {
  const queryClient = useQueryClient()
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['personal-skills'] })
  const close = () => setEditor(CLOSED_EDITOR)
  const createMutation = useMutation({
    mutationFn: ({ name, files }: { name: string; files: Record<string, string> }) => createPersonalSkill(name, files),
    onSuccess: () => { invalidate(); close() },
  })
  const updateMutation = useMutation({
    mutationFn: ({ name, files }: { name: string; files: Record<string, string> }) => updatePersonalSkill(name, files),
    onSuccess: () => { invalidate(); close() },
  })
  const pending = createMutation.isPending || updateMutation.isPending

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
    if (editor.mode === 'edit' && editor.target !== null) {
      updateMutation.mutate({ name: editor.target.name, files })
    } else {
      createMutation.mutate({ name: editor.name.trim(), files })
    }
  }

  const busy = pending || (editor.mode === 'create' && !editor.name.trim())
  const apiError = createMutation.error ?? updateMutation.error

  return (
    <div className="personal-skill-editor">
      <div className="personal-skill-editor-fields">
        <label>
          名称
          <input
            value={editor.target?.name ?? editor.name}
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
            rows={5}
            spellCheck={false}
          />
        </label>
      </div>
      {editor.error !== null && <p className="skill-error">{editor.error}</p>}
      {apiError !== null && <p className="skill-error">保存失败：{apiError.message}</p>}
      <div className="skill-version-actions">
        <button type="button" className="panel-action-button" onClick={submit} disabled={busy}>
          {editor.mode === 'edit' ? '保存修改' : '创建个人 Skill'}
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
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['personal-skills'] })
  const activateMutation = useMutation({
    mutationFn: () => activatePersonalSkill(skill.name),
    onSuccess: invalidate,
  })
  const deleteMutation = useMutation({
    mutationFn: () => deletePersonalSkill(skill.name),
    onSuccess: invalidate,
  })
  const pending = activateMutation.isPending || deleteMutation.isPending

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
        {skill.active ? (
          <span className="skill-active-version">激活中</span>
        ) : (
          <button
            type="button"
            className="panel-action-button"
            onClick={() => activateMutation.mutate()}
            disabled={pending}
            aria-label={`激活 ${skill.name}`}
          >
            <Power size={14} />激活
          </button>
        )}
        <button
          type="button"
          className="panel-action-button"
          onClick={() => onEdit(skill)}
          disabled={pending || skill.active}
          aria-label={`编辑 ${skill.name}`}
          title={skill.active ? '激活中的个人 Skill 不可编辑' : '编辑个人 Skill'}
        >
          <Pencil size={14} />编辑
        </button>
        <button
          type="button"
          className="panel-action-button"
          onClick={() => deleteMutation.mutate()}
          disabled={pending || skill.active}
          aria-label={`删除 ${skill.name}`}
          title={skill.active ? '激活中的个人 Skill 不可删除' : '删除个人 Skill'}
        >
          <Trash2 size={14} />删除
        </button>
      </div>
    </div>
  )
}

export function SkillsPanel() {
  const [editor, setEditor] = useState<SkillEditor>(CLOSED_EDITOR)
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

  return (
    <section className="status-panel" aria-labelledby="skills-title">
      <div className="panel-heading">
        <div><h2 id="skills-title">技能</h2><p>当前已安装的固定版本</p></div>
        <div className="panel-actions">
          <span className="service-count">{skillsQuery.data?.length ?? 0}</span>
          <button
            type="button"
            className="panel-action-button"
            onClick={() => { void skillsQuery.refetch(); void personalQuery.refetch() }}
            disabled={skillsQuery.isFetching || personalQuery.isFetching}
            aria-label="刷新技能"
            title="刷新技能"
          >
            <RefreshCw className={skillsQuery.isFetching || personalQuery.isFetching ? 'spin' : ''} size={16} />
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
            onClick={() => setEditor(CREATE_EDITOR)}
            aria-label="新建个人 Skill"
          >
            <Plus size={14} />新建
          </button>
        </div>
      </div>
      {editor.mode !== 'closed' ? (
        <PersonalSkillEditor editor={editor} setEditor={setEditor} />
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
              onEdit={(target) => setEditor({ mode: 'edit', target, name: target.name, filesText: '', error: null })}
            />
          ))}
        </div>
      )}
    </section>
  )
}
