# ADR-019: Skill Creator 与 draft 生命周期

- 状态: Accepted
- 日期: 2026-08-12

## 背景

个性化路线图 Phase 4（Skill Creator，Path A）需要用户经 Agent 引导创建/迭代个人 Skill。
ADR-018 确立了个人 Skill 的存储与信任边界（可写、仅组合既有 handler/tool、不覆盖内置名）。
但"创作中"的包可能不完整，不能直接进入受信注册；且激活前必须过 eval 门禁。因此需要独立的
draft 状态与显式晋升路径。

## 决定

### Draft 存储与注册

- 草稿存放在 `PERSONAL_SKILLS_DIR/_drafts/<name>/`。下划线前缀使 `reload()` 的目录扫描跳过它，
  因此草稿永不被当作受信包加载；draft 文件名仍走同一套路径安全校验（无符号链接 / 父路径穿越）。
- `create_draft` / `update_draft` 只做路径安全校验，**不做全量包校验**（草稿允许不完整）。
- `validate_draft` 运行与内置包完全一致的信任校验（manifest / schema / workflow / eval case），
  并把结果**临时注册**进内存 registry，供确定性 eval 的执行器 pin 与运行；promote 前会清掉
  这些临时注册，保证发布的个人包拥有该 (name, version) 槽位并指向个人根。
- `promote_draft` = 全量校验通过 → `create_personal`（复用 ADR-018 内置名/重名拒绝）→ 删除
  `_drafts/<name>`。激活指针复用 `skill_activations`。

### Eval 即激活门禁

- 激活前必须重跑 Phase 1 确定性 eval：`total > 0` 且全部 case passed、零 errored。
- 门禁执行器 `DraftSkillEvalRunner` 复用 `StructuralSkillEvalJudge` 与 case/check/报告类型，
  以确定性 fixture handler（VERIFYING 节点产出符合 output schema 的输出，其余节点 no-op）跑
  `DeterministicWorkflowExecutor`：无模型、无数据库、不序列化正文。agent_loop 草稿不支持
  确定性门禁，如实报告 `SKILL_EVAL_DRAFT_AGENT_LOOP`。
- eval 只验证"包可加载、workflow 可跑到终态、声明 checks 成立"，是结构闸而非语义闸。

### Creator 工具与审批

- `skill_scaffold` / `skill_write` / `skill_draft` / `skill_activate` 声明 `WRITE_KNOWLEDGE`，
  走既有 durable approval；`skill_validate` / `skill_run_eval` 声明 `READ_KNOWLEDGE`。
- 工具始终注册进 assistant 循环（与 workspace 工具并存），handler 返回结构化、body-free
  payload，便于 agent 迭代而不暴露 Prompt/正文。
- `skills/skill_creator/` 是 manifest v2 + `invocation`（`command: create-skill`，别名 `skill`，
  `execution_mode: agent_loop`）。激活后其指令进入 assistant 循环 active contexts，且
  `/create-skill` 命令提交的 turn 由 assistant 循环驱动（与 `/research` 同路径），用六个
  creator 工具完成 scaffold → write → validate → eval → activate。

### 模式建议（Phase 6 前奏）

- `/api/v1/skills/personal/drafts/suggestions` 只读：基于 Phase 2 `usage_patterns`，过滤
  `general` 类别与已绑定 skill 的模式，`frequency ≥ 阈值`，对既有内置/个人/draft 名去重。
- 建议绝不自动创建；点击「创建」预填脚手架并进入人工确认的 creator 流程。

## 备选

- draft 直接复用 `create_personal`：被拒，不完整包无法通过全量校验，且缺独立状态。
- eval 状态持久化到 DB：被拒，门禁确定性可重放，落库反而引入陈旧状态；激活时重跑即可。
- skill_creator 走 `projected` 执行模式：被拒，`_execute_skill` 对非 QA 技能会误路由到
  Grounded QA 适配器；`agent_loop` 让 `/create-skill` 提交的 turn 直接由 assistant 循环
  驱动，不会进入 `_execute_skill`。

## 后果

个人 Skill 创作走 `draft → 校验 → eval 门禁 → 人工确认 → active` 的显式生命周期；激活后的
运行时执行复用 ADR-018 的 Grounded QA 泛化路径。Phase 6 自动提取可在同一 draft 边界上直接
落盘候选。

## 重新评估

当草稿需要多人协作、远程分发或精细权限，或 eval 需要 LLM judge 时重新评估。
