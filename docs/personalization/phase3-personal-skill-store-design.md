# Phase 3：个人 Skill 存储与信任模型

日期：2026-08-12
状态：✅ 已实施（2026-08-12，commit 1958920 / 0c490f1 / 收尾提交）
依赖：无
产出：个人 skill 存储根 + 信任边界 + CRUD API + ADR 扩展

## 目标

确立并实现"个人 skill"的存放与信任边界：用户可写、运行时加载、独立于 trusted_root。为 Phase 4 提供地基。

## 范围

**做**：个人 skill 存储根 + 复用现有校验/摘要 + CRUD API + 激活持久化接入 + ADR 扩展。
**不做**：创作引导 UX（Phase 4）、eval 门禁联动（Phase 4 起）、生成式 skill 行为（仍仅组合已有 handler）。

## 交付物

1. **个人 skill 根**：env `PERSONAL_SKILLS_DIR`（默认 runtime 数据目录，如 `data/personal_skills/`）。运行时加载；复用 content_sha256 摘要与路径安全校验（无符号链接 / 父路径穿越 / 远程 $ref）。
2. **Registry**：扩展 `FileSystemSkillRegistry` 支持多根，或新增 `PersonalSkillRegistry`。**同名冲突规则**：个人 skill 不得覆盖内置 skill 名（内置优先，同名拒绝创建），个人 skill 作为新名进入激活目录。
3. **信任约束**（扩展 ADR-006/017）：个人 skill 低信任、可写、仅组合已有 handler/tool，**不得引入新 Python 行为**；budget / permissions 沿用 manifest 声明。
4. **API**：`/api/v1/skills/personal` CRUD（create / list / get / update / delete），写时校验 manifest + workflow + schema + eval case；激活沿用现有 `skill_activations` 表与 `SkillLifecycleService`。
5. **前端**：`SkillsPanel.tsx` 增加个人 skill 的新建/编辑/删除/激活入口（基础 CRUD，不做引导流）。

## 验收

- 个人 skill 可经 API 创建并激活，被 v2 assistant 路径加载使用。
- 校验失败 / 路径穿越 / 符号链接被拒；内置 skill 名不可被覆盖。
- 全量门禁通过；`export_openapi.py` 后 `git diff --exit-code`。
- 测试覆盖：CRUD、校验、信任边界、同名冲突、激活联动。
