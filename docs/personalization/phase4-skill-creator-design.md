# Phase 4：Skill Creator 工具（Path A）

日期：2026-08-12
状态：✅ 已实施（2026-08-12，4 个功能 commit + 验收 commit）
依赖：Phase 1（eval 门禁）、Phase 3（个人 skill 存储）
产出：`skill_creator` skill 包 + 个人 skill 工具 + draft 生命周期 + SkillsPanel 审批 UX

## 目标

用户通过 Agent 引导创建/迭代个人 skill：从模板脚手架 → 生成包文件 → 校验 → eval 门禁 → draft → 用户激活。**creator 本身是一个 skill**（符合项目组合式哲学）。

## 范围

**做**：`skill_creator` skill 包；个人 skill 相关工具；draft 生命周期；SkillsPanel 草稿与审批；轻量"模式建议"（Phase 6 前奏）。
**不做**：全自动提取（Phase 6）、长期记忆（Phase 5）。

## 交付物

1. **`skills/skill_creator/`** skill 包（manifest v2 + workflow + prompts + input/output schema + evals）。工作流：收集需求 → 从 `skills/_template/` 脚手架 → 生成 manifest / workflow / prompts / schemas / evals → 校验 → 跑 Phase 1 eval → 注册为 **draft** → 提示用户审批。
2. **新工具**（注册进 tool registry，写操作走既有审批机制）：`skill_scaffold`、`skill_write`、`skill_validate`、`skill_run_eval`、`skill_activate`、`skill_draft`。写入目标为 Phase 3 的个人 skill 存储根。
3. **draft 生命周期**：新 skill 先落 draft；eval 通过 + 用户确认后转 active。`SkillsPanel.tsx` 展示 draft / active，支持"运行 eval / 激活 / 拒绝"。
4. **轻量模式建议**（Phase 6 前奏）：基于 Phase 2 usage traces，向用户提示"你最近常做 X，是否固化成 skill？"——点击即进入 creator 流程。半自动，必须人工确认。
5. **复用 Phase 1 eval runner** 作为 creator 的验证闸。

## 验收

- 用户在对话里说"帮我建一个做 X 的 skill"，creator 能产出完整包 → 校验通过 → 落 draft。
- 用户可在 SkillsPanel 对 draft 跑 eval 并激活；激活后 v2 assistant 可调用。
- 模式建议只在痕迹证据足够时出现（有频率阈值，不骚扰）。
- 全量门禁通过；测试覆盖 creator 工作流各步骤与各工具校验/拒绝路径。
