# ADR-018: Personal Skill 存储与信任边界

- 状态: Accepted
- 日期: 2026-08-12

## 背景

ADR-006 把 Skill 定义为 trusted_root 下不可变、只读的版本化包，激活指针经 `skill_activations`
持久化。个性化路线图（Phase 3）需要"用户可写、运行时加载、独立于 trusted_root"的个人 Skill，作为
Phase 4（Skill Creator 引导创建）与 Phase 6（自动提取）的地基。ADR-017 收紧了当前 Skill 契约，
只保留只读 `/api/v1/skills`；个人 Skill 需要新的可写边界。

## 决定

### 存储与加载

- 个人 Skill 存放在独立的可写根 `PERSONAL_SKILLS_DIR`（默认 `./data/personal_skills`），与
  trusted_root（`SKILL_ROOT_PATH`，不可变）并列。
- `FileSystemSkillRegistry` 扩展为双根加载：`PersonalSkillRegistry` 子类在受信校验之上提供
  `create_personal` / `update_personal` / `delete_personal` / `activate_all`。
- 运行时按 assistant turn 重建 registry（`assistant_skill_registry()` 每次调用都重扫磁盘），
  因此新创建的个人 Skill 在下一个 turn 即可被 v2 assistant 路径加载。

### 信任约束（扩展 ADR-006/017）

- 个人 Skill 是**低信任、可写**的包：manifest / workflow / input/output schema / eval case
  全部复用 trusted_root 同一套校验（manifest schema、JSON Schema 本地 `$ref`、eval case、
  UTF-8 与大小上限、无符号链接 / 父路径穿越 / 远程引用）。
- **不得引入新 Python 行为**：entrypoint 必须是声明式 YAML/JSON workflow，handler/tool 只能
  引用已注册项；budget / permissions 沿用 manifest 声明，运行时权限仍由服务端控制。
- **内置名优先**：个人 Skill 不得覆盖内置 Skill 名；同名创建/更新在 reload 与 create 两层均
  拒绝（`SKILL_NAME_CONFLICT`）。
- **激活复用** `skill_activations` 表与 `SkillLifecycleService` 语义；持久化激活在 API 启动与
  worker 每次运行重放（`SkillActivationStore.list()`）。激活中的个人 Skill 拒绝 update/delete
  （`SKILL_CLEANUP_BLOCKED`）。
- update 走 staging 目录先校验后原子替换，失败不破坏旧版本。

### API

- 新增 `/api/v1/skills/personal` CRUD + `/{name}/activate`；写时校验同 trusted_root。
- 校验/信任失败映射稳定错误码（`SKILL_*`），不落完整 Prompt / 私密正文（数据门禁）。

## 备选

- 在 trusted_root 内直接可写：被拒，破坏部署所有权与不可变身份。
- 允许个人 Skill 覆盖内置名：被拒，产生歧义与供应链风险。
- 引入 Python 插件：被拒，无法验证权限与恢复（延续 ADR-006 理由）。

## 后果

个人 Skill 只能在既有 handler/tool 组合内表达工作流，安全边界与内置 Skill 一致；激活与失效由
`skill_activations` 持久化。Phase 4 的引导创建与 eval 门禁、Phase 6 的自动提取将在此边界之上实现。
未来若需要签名分发或沙箱，需另立 ADR。

## 重新评估

当个人 Skill 需要远程分发、签名校验或运行时沙箱，或需要"激活回滚 / 停用"生命周期时重新评估。
