import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, CheckCircle2, CircleAlert, LoaderCircle, RefreshCw, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'
import {
  activateSkill,
  cleanupSkillVersion,
  fetchSkills,
  fetchSkillVersions,
  QAApiError,
  rollbackSkill,
  type SkillSummary,
} from './qa'

function SkillRow({ skill }: { skill: SkillSummary }) {
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [cleanupVersion, setCleanupVersion] = useState<string | null>(null)
  const versionsQuery = useQuery({
    queryKey: ['skill-versions', skill.name],
    queryFn: ({ signal }) => fetchSkillVersions(skill.name, signal),
    enabled: expanded,
  })
  const changeMutation = useMutation({
    mutationFn: ({ version, rollback }: { version: string; rollback: boolean }) => {
      if (skill.active_revision === null) throw new Error('Skill 活动 revision 不可用')
      return rollback
        ? rollbackSkill(skill.name, version, skill.active_revision)
        : activateSkill(skill.name, version, skill.active_revision)
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['skills'] }),
        queryClient.invalidateQueries({ queryKey: ['skill-versions', skill.name] }),
      ])
    },
  })

  const error = changeMutation.error
  const cleanupMutation = useMutation({
    mutationFn: (version: { version: string; content_sha256: string }) =>
      cleanupSkillVersion(skill.name, version.version, version.content_sha256),
    onSuccess: async () => {
      setCleanupVersion(null)
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
      await queryClient.invalidateQueries({ queryKey: ['skill-versions', skill.name] })
    },
  })

  return (
    <div className={`skill-row ${expanded ? 'expanded' : ''}`}>
      <button
        type="button"
        className="skill-summary"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className="skill-icon"><Bot size={18} /></span>
        <span className="skill-name">
          <strong>{skill.name}</strong>
          <span>{skill.versions.length} 个已安装版本</span>
        </span>
        <span className="skill-active-version">
          {skill.active_version ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
          {skill.active_version ? `v${skill.active_version}` : '未激活'}
        </span>
        <code>rev {skill.active_revision ?? '-'}</code>
      </button>

      {expanded && (
        <div className="skill-versions">
          {versionsQuery.isLoading ? (
            <div className="loading-row"><LoaderCircle className="spin" size={17} />加载版本…</div>
          ) : versionsQuery.isError ? (
            <div className="skill-error" role="alert">无法加载 Skill 版本。</div>
          ) : versionsQuery.data?.map((version) => (
            <div className="skill-version" key={version.version}>
              <div className="skill-version-heading">
                <div>
                  <strong>v{version.version}</strong>
                  {version.active && <span className="active-badge">活动版本</span>}
                </div>
                {!version.active && skill.active_revision !== null && (
                  <div className="skill-version-actions">
                    <button
                      type="button"
                      className="panel-action-button"
                      disabled={changeMutation.isPending}
                      onClick={() => changeMutation.mutate({ version: version.version, rollback: false })}
                    >
                      <CheckCircle2 size={15} />激活
                    </button>
                    <button
                      type="button"
                      className="panel-action-button"
                      disabled={changeMutation.isPending}
                      onClick={() => changeMutation.mutate({ version: version.version, rollback: true })}
                    >
                      <RotateCcw size={15} />回滚至此
                    </button>
                    {cleanupVersion === version.version ? (
                      <>
                        <button
                          type="button"
                          className="panel-action-button danger-action"
                          disabled={cleanupMutation.isPending}
                          onClick={() => cleanupMutation.mutate(version)}
                        >
                          <Trash2 size={15} />确认清理
                        </button>
                        <button type="button" className="panel-action-button" onClick={() => setCleanupVersion(null)}>
                          取消
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="panel-action-button"
                        disabled={cleanupMutation.isPending}
                        onClick={() => setCleanupVersion(version.version)}
                        title="仅能清理非活动且无持久引用的版本"
                      >
                        <Trash2 size={15} />清理
                      </button>
                    )}
                  </div>
                )}
              </div>
              <p>{version.description}</p>
              <dl className="skill-version-meta">
                <div><dt>摘要</dt><dd title={version.content_sha256}>{version.content_sha256}</dd></div>
                <div><dt>权限</dt><dd>{version.permissions.join(', ') || '无'}</dd></div>
                <div><dt>能力</dt><dd>{version.required_capabilities.join(', ') || '无'}</dd></div>
                <div><dt>预算</dt><dd>{version.budget.max_steps} 步 / {version.budget.max_tool_calls} Tool / {version.budget.timeout_seconds}s</dd></div>
              </dl>
            </div>
          ))}
          {error && (
            <div className="skill-error" role="alert">
              操作失败：{error instanceof QAApiError ? error.message : String(error)}
            </div>
          )}
          {cleanupMutation.error && (
            <div className="skill-error" role="alert">
              清理失败：{cleanupMutation.error instanceof QAApiError ? cleanupMutation.error.message : String(cleanupMutation.error)}
            </div>
          )}
        </div>
      )}
    </div>
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
        <div><h2 id="skills-title">技能管理</h2><p>受信 Skill 与活动版本</p></div>
        <div className="panel-actions">
          <span className="service-count">{skillsQuery.data?.length ?? 0}</span>
          <button
            type="button"
            className="panel-action-button"
            onClick={() => void skillsQuery.refetch()}
            disabled={skillsQuery.isFetching}
          >
            <RefreshCw className={skillsQuery.isFetching ? 'spin' : ''} size={16} />刷新
          </button>
        </div>
      </div>
      {skillsQuery.isLoading ? (
        <div className="loading-row"><LoaderCircle className="spin" size={18} />加载 Skill…</div>
      ) : skillsQuery.isError ? (
        <div className="empty-state"><CircleAlert size={24} /><p>无法加载 Skill Catalog</p></div>
      ) : (
        <div className="skills-list">
          {skillsQuery.data?.map((skill) => <SkillRow key={skill.name} skill={skill} />)}
        </div>
      )}
    </section>
  )
}
