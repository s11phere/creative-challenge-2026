import { useQuery } from '@tanstack/react-query'
import { Bot, CircleAlert, LoaderCircle, RefreshCw } from 'lucide-react'
import { fetchSkills, type SkillVersion } from './qa'

function SkillRow({ skill }: { skill: SkillVersion }) {
  return (
    <article className="skill-row">
      <div className="skill-summary">
        <span className="skill-icon"><Bot size={18} /></span>
        <span className="skill-name">
          <strong>{skill.name}</strong>
          <span>v{skill.version}</span>
        </span>
      </div>
      <p>{skill.description}</p>
      <dl className="skill-version-meta">
        <div><dt>摘要</dt><dd title={skill.content_sha256}>{skill.content_sha256}</dd></div>
        <div><dt>权限</dt><dd>{skill.permissions.join(', ') || '无'}</dd></div>
        <div><dt>能力</dt><dd>{skill.required_capabilities.join(', ') || '无'}</dd></div>
        <div><dt>预算</dt><dd>{skill.budget.max_steps} 步 / {skill.budget.max_tool_calls} Tool / {skill.budget.timeout_seconds}s</dd></div>
      </dl>
    </article>
  )
}

export function SkillsPanel() {
  const skillsQuery = useQuery({
    queryKey: ['skills'],
    queryFn: ({ signal }) => fetchSkills(signal),
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
            onClick={() => void skillsQuery.refetch()}
            disabled={skillsQuery.isFetching}
            aria-label="刷新技能"
            title="刷新技能"
          >
            <RefreshCw className={skillsQuery.isFetching ? 'spin' : ''} size={16} />
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
    </section>
  )
}
