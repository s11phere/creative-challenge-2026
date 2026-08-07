# 阶段 4/5 收尾看板

## 2026-08-04 Implementation Status Update

All requested Stage 4/5 engineering functions are implemented, including Web entry points for all
five registered Skills, Web feedback controls,
persistent human feedback review (`pending_review -> accepted/rejected`), Space isolation,
idempotency/conflict handling, and the controlled metadata-only candidate exporter. Stage 5
Runtime/approval/derived-knowledge/Skill cleanup code was re-regressed across process-facing tests.

The remaining labels are evidence labels, not implementation deferrals: formal retrieval/answer/Skill
holdouts remain unrun or non-passing under ADR-010/011, and browser Playwright coverage is unavailable
in the current environment.

## 2026-08-06 Assistant Conversation Evolution Step 5

Step 5 adds `ConversationContextService`, append-only versioned rolling summaries, and the
`context_compaction` Worker use case. A summary records one conversation/Space, its covered message
range, digest, prompt/model version, and inherited `private_local` sensitivity. Original messages
remain queryable and are never rewritten or deleted.

Routing, direct response, resource-reference handling, and the Skill standalone request now share
the same bounded snapshot: rolling summary, recent window, and current request. QA keeps its
separate evidence `ContextBuilder`; standalone Skill requests do not cause full-chat history to be
copied into QA/Skill execution. `/compact` creates an idempotent durable Run, automatic compaction
uses the same Worker queue after a soft watermark, and lease recovery/cancellation/failure retain a
bounded recent-window fallback. The implementation remains provisional under ADR-010/ADR-011; no
formal retrieval, answer, or Skill holdout was run.

## 2026-08-06 Assistant Conversation Evolution Step 6

Step 6 replaces the fixed QA Skill-mode entry with a general conversation workspace. `/` opens a
searchable ARIA combobox/listbox with keyboard, pointer, and IME-safe selection. Direct replies,
Skill runs, clarifications, approvals, failures, and grounded citations each use their own rendered
state; the citation panel appears only for a grounded result with citations. Collapsed Run details
show actual model identity, input/output token usage, and latency only.

Resource clarifications render safe inline candidates. The server keeps the continuation state
private, revalidates a selected candidate in the original Run's Space, and resumes the same parent
Run rather than creating another conversation. `GET /api/v2/conversations/{conversation_id}/runs`
restores persisted v2 Runs and pending clarifications after a refresh. This is provisional engineering
work under ADR-010/ADR-011: no formal retrieval, answer, or Skill holdout was run, and browser
Playwright desktop/mobile evidence remains unavailable in the current environment.

## 2026-08-06 Assistant Conversation Evolution Step 7

Step 7 adds `assistant-operational-metrics-v1` counters for router decisions, explicit commands,
clarifications, compaction, actual token usage, latency, and terminal reasons. Counters use only
safe labels and aggregate numeric values. The Agent, command API, clarification continuation, and
compaction Worker emit these records through the existing structured logging path without recording
conversation text, prompts, document content, Provider output, or internal IDs.

`scripts/evaluate_assistant_routing.py` validates the hash-pinned `assistant-routing-v1` synthetic
development dataset and aggregates body-free prediction metadata into `assistant-metrics-report-v1`.
The manifest requires `content_policy=synthetic_only`, `formal_runs_enabled=false`, and a
`development` split; reports remain `provisional` and cannot execute a formal holdout or call a
Provider. Focused security and regression coverage covers command routing, policy/approval/cancel
boundaries, Worker compaction and metric privacy. Browser Playwright desktop/mobile evidence remains
unavailable in the current environment, so this engineering step does not close the existing formal
quality or browser-E2E gaps.

Step 7 verification used only fake/synthetic paths: `mypy apps packages` passed for 121 source files;
the complete backend suite passed `735 passed, 52 skipped`; an isolated PostgreSQL/Redis environment
completed `upgrade head -> downgrade base -> upgrade head` and `51` integration tests; Web lint,
typecheck, Vitest (`26` tests), and production build passed. OpenAPI export had no diff and the
synthetic evaluator reported eight development cases with `formal_run_eligible=false`. The full
repository formatting check still reports 18 pre-existing, out-of-scope files; all Assistant
Conversation Evolution files are formatted.

## 2026-08-07 Assistant Conversation Evolution Step 8

Step 8 changes the Web default entry to the API v2 Assistant conversation workspace. The old QA
surface remains available through an explicit `兼容问答` selector until the Vite-configured UTC
deadline (`2026-09-30T23:59:59Z` by default); invalid or expired values fail closed to v2. The
compatibility path reuses the existing v1 conversation, question, Run, cancellation, and citation
ports, so no parallel QA workflow or persistence protocol is introduced.

The Web image receives `VITE_ASSISTANT_DEFAULT_API_MODE` and
`VITE_ASSISTANT_V1_COMPATIBILITY_UNTIL` as build arguments. Release verification is limited to
fake/local Provider paths; external Chat remains governed by `MODEL_ALLOW_EXTERNAL`, source and
deployment policy, and visible consent. Rollback is configuration-only and preserves v2 data,
historical Runs, active pointers, and Skill packages. Monitoring requirements are recorded for
routing misfires, clarification loops, cancellation, recovery, token usage, and latency. This step
remains provisional under ADR-010/ADR-011; no formal retrieval, answer, or Skill holdout was run.

Step 8 verification: Web lint, typecheck, Vitest (`31` tests), and production build passed. The
focused v1 compatibility tests cover explicit submit/cancel behavior and the v2 default-entry test
covers empty-hash startup. OpenAPI has no endpoint change and remains unchanged; browser Playwright
desktop/mobile evidence is still unavailable in the current environment.

## 2026-08-07 Skill Invocation Trace Cards

The v2 Web conversation now keeps one collapsible, default-closed card for every persisted Skill Run.
The card remains in the conversation after completion, failure, cancellation, or clarification and
is reconstructed from the same `ConversationRun` after refresh. Expanded details show the safe
`agent-run-sse-v2` activity chain, pinned Skill identity, execution status/model/actual usage, and
the final answer or server-authored clarification. Raw prompts, Tool payloads, document excerpts,
and internal budgets remain excluded. This is a Web presentation change only; the existing Run,
Worker, QA Application Port, and SSE persistence contracts are reused.

## 2026-08-06 Assistant Conversation Evolution Step 1

按 `agent-conversation-evolution-plan.md` 的 Step 1，新增了共享 `ConversationRun` 父身份和 API v2
骨架。旧 `qa_runs.id` 保持不变并成为 `grounded_qa` 投影；QA 消息、Runtime、审批和派生知识改为
引用该父 ID。v2 目前只能原子持久化/读取/取消无模型 turn，尚未执行自动路由、直接模型回答、命令、
澄清续答或 v2 SSE。这是工程契约演进，不改变 ADR-010/ADR-011 的 provisional 边界，也不构成任何
正式检索、回答或 Skill 质量验收。

## 2026-08-06 Assistant Conversation Evolution Step 2

按 `agent-conversation-evolution-plan.md` 的 Step 2，v2 已形成 ordinary direct-conversation 的
provisional 纵向闭环：`AssistantAgentService` 以冻结的基础 prompt 和
`assistant-router-decision-v1` 严格解析模型输出；本步只接受 `respond` 与 `clarify`，
`invoke_skill` 或无效 JSON 都以稳定失败码结束，绝不伪造 assistant 终态。直接回复、父 Run
终态和实际 usage 在同一事务中写入；澄清使用服务端生成的确定性 ID 与安全元数据。

新增 `assistant_events` 和 `agent-run-sse-v2`，只持久化状态、动作和错误码，不写入用户原文、
回复正文或模型原始输出。`assistant_run` 复用既有 `qa` Dramatiq 队列和 Worker lease/recovery
机制；API 仅投递 `run_id`、`trace_id` 与事件版本。fake provider 对该冻结 router prompt 返回
确定性 `respond` JSON，CI 不依赖外部模型。v2 公开了 `GET /api/v2/runs/{run_id}/events`。

该步骤未开启 Skill catalog、自动 Skill 调用、slash command、资源解析或会话压缩，仍不改变
ADR-010/ADR-011 的 provisional 边界，也不构成任何正式检索、回答或 Skill 质量验收。工程复核为
`718 passed, 52 skipped`，Ruff、mypy、OpenAPI、单一 Alembic head 和 diff check 均通过；未运行
formal holdout、未读取私有正文、未调用外部 Provider。

## 2026-08-06 Assistant Conversation Evolution Step 3

按 `agent-conversation-evolution-plan.md` 的 Step 3，新增 manifest v2 `invocation` 元数据、active
command/alias 冲突校验和仅包含触发摘要的 Assistant catalog。四个业务 Skill 各发布 `0.2.0` v2
包，原 `0.1.0` 包保持可读、可固定和可恢复；legacy `/api/v1/skills` 与 QA pointer 继续保持 v1
兼容，Assistant 使用独立 v2 路由目录。

`invoke_skill` 只能选择 active catalog 条目，服务端重新 pin `(name, version, content_sha256)`，
拒绝 hidden/inactive/未授权 Skill 和模型提供的资源/Space/版本 ID。成功选择后复用同一
`ConversationRun` parent ID 建立 QA projection，并沿既有 Grounded QA Application Port、QA Worker、
`qa-sse-v1` 与 `agent-run-sse-v2` 生命周期执行。自然语言资源解析只读当前 Space 的已发布版本；唯一
匹配固定范围，歧义返回无 ID 的安全候选，缺失/冲突使用 `RESOURCE_NOT_FOUND`/
`RESOURCE_CONFLICT` 或 server-authored clarification。

本步未实现 slash command API 或上下文压缩；未运行 formal retrieval/answer/Skill holdout。实现和
验证均为 provisional，不改变 ADR-010/ADR-011 边界。

Step 3 工程复核：后端 `718 passed, 52 skipped`，Ruff 全量检查通过；Mypy 仅保留两个既有的
`Any` 返回告警（`packages/application/src/application/retrieval/dense.py`、
`apps/worker/src/worker/ingestion_tasks.py`）。Step 3 的 v2/legacy catalog、资源解析、Skill pin
和 parent Run promotion smoke 验证通过；未运行 formal holdout，也未读取私有正文或调用外部 Provider。

## 2026-08-06 Assistant Conversation Evolution Step 4

按 `agent-conversation-evolution-plan.md` 的 Step 4，新增版本化 `GET /api/v2/commands` 和服务端权威
parser。目录合并基础指令与 active Skill 指令，支持大小写不敏感、别名、未知命令候选和 `//` 转义；
turn 请求中的可选 `command` 仅作为不可信提示，服务端始终重新解析原始内容。

`/help`、`/skills`、`/new`、`/stop` 返回无业务 Run 的命令结果；显式 `/ask`、`/summarize`、
`/compare`、`/cards` 直接复用 Skill pin、资源校验、parent Run 和 QA Worker/SSE 路径，并固定
`selection_source=command`。命令重放复用原 idempotency identity；模型不会参与显式 Skill 选择。
`/compact` 目前只确认请求并明确延后到 Step 5 的上下文摘要 Worker。

Step 4 验证为 provisional，未运行 formal retrieval/answer/Skill holdout，不改变 ADR-010/ADR-011 边界。

> 对应收尾计划：[`post-stage-3-stage-4-5-completion-plan.md`](post-stage-3-stage-4-5-completion-plan.md)
>
> 看板版本：v1
>
> 建立日期：2026-08-03
>
> 状态规则：`已完成` 仅表示对应工程/契约已有证据；`provisional` 表示可用于后续开发或内部
> 验证，但不能写入正式质量结论；`待执行` 表示尚无实际验收证据；`阻塞` 表示存在前置门禁或
> 事实源缺口。

## 1. 当前阶段结论

| 阶段 | 工程状态 | 正式质量状态 | 当前决策依据 |
| --- | --- | --- | --- |
| 阶段 3 | 工程 Step 0-10 已完成；v1 development 复核已执行 | 仍未通过，未冻结，未运行 holdout | [ADR-010](adr/010-stage-3-termination-and-evaluation-boundary.md)；[v1 development 记录](stage-3-reopen-development-v1.md)：v1 修复标注但未 materially improve coverage/representativeness |
| 阶段 4 | QA Domain/Application、PostgreSQL、Worker、SSE、API、Web、Citation、反馈提交与反馈审核生命周期已实现 | 工程实现完成，质量 provisional | [stage-4-acceptance.md](stage-4-acceptance.md)：Playwright、真实回答质量、正式默认配置冻结和 answer holdout 未完成 |
| 阶段 5 | Runtime/Registry、active pointer、`knowledge_agent`、知识整理 Skill、审批/派生知识和 Skill 清理已实现 | 工程实现完成，质量 provisional | [stage-5-acceptance.md](stage-5-acceptance.md)：正式 Eval 和浏览器门禁未完成 |

本看板不批准任何正式 holdout，不改变 `retrieval-v1.yaml`、`qa-v1.yaml` 或现有 Skill 的状态，
也不改变阶段 0 的 `internal_team_only` 分发边界。

### 1.1 Step 1/2 结果与继续策略

Step 1/2 的工程复核不改变正式质量前置门未通过的事实。当前 GPU development 结果仅满足
[ADR-011](adr/011-provisional-stage-4-5-continuation-gate.md) 定义的 provisional continuation
gate；确认后才可进入 Step 3 的 Skill catalog 与自动调用工程工作。正式 retrieval/answer holdout、
正式质量结论和阶段退出仍被阻断。

### 1.2 Step 3 provisional E2E 结果

第 3 步的隔离 HTTP 旅程已执行并记录在 [`stage-3-4-5-step3-e2e.md`](stage-3-4-5-step3-e2e.md)：
健康检查、会话创建、异步 QA Run、回答、SSE 事件和反馈幂等均有证据。真实 fake 旅程的
Citation 解析为 `invalid`（无正文返回），所以 Citation 质量门禁仍未通过。期间发现并修复
SSE `id` 与 `Last-Event-ID` 类型不一致（改为严格递增 sequence）；12 个 API/SSE 单测通过。
隔离 API 镜像受 Docker buildx 权限限制尚未重建，网络层的 sequence 重验待后续正常构建完成。
该结果仅允许继续 provisional Step 4，不改变正式质量门禁、holdout 禁止或 internal-only 边界。

### 1.3 Step 4 provisional Web 结果

第 4 步已完成现有 Web 工程验证，记录见 [`stage-4-5-step4-web-e2e.md`](stage-4-5-step4-web-e2e.md)：
lint、typecheck、Vitest 27 项、production build，以及隔离 Web 首页、静态资源和同源 health 代理
均通过。组件测试覆盖导航、健康/错误状态、摄入取消/重试、QA 取消/恢复、拒答、Citation 失效重试、
键盘提交和审批预览。仓库没有 Playwright 配置，环境没有浏览器可执行文件，因此桌面/移动截图、真实
键盘遍历和浏览器级 SSE 重连仍未验收；该结果不构成阶段 4 正式退出。

### 1.4 Step 5 provisional Feedback 结果

第 5 步反馈闭环证据见 [`stage-4-5-step5-feedback.md`](stage-4-5-step5-feedback.md)：反馈领域/API/
导出定向测试 `27 passed`，隔离 PostgreSQL QA/Runtime/Skill persistence `6 passed`；HTTP 旅程证明回答绑定、
`pending_review`、重复幂等、冲突 409 和说明不回显。候选导出仅生成 metadata-only、版本可追溯的
`feedback-candidate-v1`，不会修改 frozen dataset/holdout。人工审核队列、Space 隔离和审核者身份
记录已经实现；独立认证系统仍不在当前本地边界内。候选写盘命令为
`scripts/export_feedback_candidates.py`。正式质量门禁仍未关闭。

## 2. 版本矩阵

所有“目标版本”均为待创建或待冻结的版本；完成前不得将候选版本标记为正式。

| 工作线 | 当前候选/事实 | 当前状态 | 下一步正式版本要求 | 数据与外发边界 | 负责人 |
| --- | --- | --- | --- | --- | --- |
| 阶段 3 检索 | `retrieval-v1.yaml` + dataset `knowledge-qa-v0`；另有 `retrieval-v1-knowledge-qa-v1.yaml` + dataset `knowledge-qa-v1` | v1 schema/locator/hash 校验和 GPU development 消融已通过；两者仍 `provisional`，formal runs disabled；正式门未通过但满足 ADR-011 continuation gate | 可在 v1 上继续阶段 4/5 provisional 工程；正式线仍需新 dataset/config、代表性覆盖、claim-aware evaluator、development 达标、配置 hash 冻结后才可一次性运行 retrieval holdout | 仅使用 manifest 允许来源；默认本地；私有语料不得外发 | 待认领 |
| 阶段 4 QA 评测 | `qa-continuation-v1.yaml` + `qa-profile-continuation-v1.yaml`；dataset `knowledge-qa-v0`；prompt `grounded-qa-v1-provisional` | provisional continuation 配置已 pin `retrieval-v1-knowledge-qa-v1`，validate-only 和受影响单测通过；正式配置未冻结 | 在 continuation gate 下继续 QA/E2E 工程；正式线仍需代表性 QA dataset、真实模型 development、answer config hash 和一次性 holdout | 题目、回答、引用原文和 Provider 响应不得写日志/报告；外部 Chat 需显式策略和同意 | 待认领 |
| 阶段 5 Skill 评测 | 新知识入口 `knowledge_agent 0.3.0`（保留 0.1/0.2 旧包）；`knowledge_qa` 仅历史 Run 恢复；`summarize_document 0.1.0`、`compare_sources 0.1.0`、`create_review_cards 0.1.0` | 工程实现完成、质量 provisional；整理 Skill 已支持预览/审批/派生写入，正式 Eval 未关闭 | 为每个 Skill 固定 workflow/manifest/prompt/schema/eval 版本和摘要；现有 Runtime 恢复、审批、派生写入、回滚/清理引用检查已实现，之后再做正式 Skill Eval | 受信根加载；运行固定 Skill identity；派生写入前必须持久审批，所有输入继承来源敏感度 | 待认领 |

### 2.1 版本冻结顺序

```text
新 Stage 3 dataset/config
  -> retrieval development 与默认配置冻结
  -> retrieval holdout
  -> Stage 4 QA dataset/profile/prompt/model 冻结
  -> answer development 与配置冻结
  -> answer holdout
  -> Stage 5 Skill workflow/schema/eval 固定
  -> Skill Runtime/写入/生命周期正式验收
```

任何一项失败都要创建新的版本回到 development，不能修改已冻结输入，也不能用上一阶段 provisional
结果代替本阶段门禁。

## 3. 退出项对照

### 3.1 阶段 4

| 退出项 | 当前状态 | 证据/缺口 | 看板动作 |
| --- | --- | --- | --- |
| 阶段 0/2 记录和 Stage 3 termination boundary | 已完成 | Stage 0/2 acceptance、ADR-010 已存在 | 保持引用，不重写为质量通过 |
| 导入 -> 问答 -> Citation -> 原文 | provisional 已执行 | Compose 最小链路已验证；完整用户旅程未验收 | Step 3/4 补充隔离 E2E 和 Playwright |
| Citation target resolution 100% 与隔离违规 0 | provisional 部分通过 | 解析和版本校验有证据；需正式报告与全量安全切片 | Step 2/3/9 复核 |
| 默认 QA 配置、development 消融、answer holdout | 待执行 | 当前为 fake/validate-only，`formal_runs_enabled=false` | Step 2，依赖新 Stage 3 质量输入 |
| SSE、取消、重试、重连、唯一终态 | provisional 已执行 | 契约和隔离集成测试已有；需真实旅程回归 | Step 3/4 |
| Conversation/Run/Evidence/Feedback 持久化与恢复 | 工程实现完成 | PostgreSQL/Worker/重启、反馈审核和幂等回归已执行；真实浏览器旅程未执行 | Step 3/5 |
| Web 桌面/移动/键盘/失败状态 | 待执行 | Playwright 未执行 | Step 4 |
| 版本可追溯、预算、日志与隐私 | 部分完成 | identity/稳定错误已存在；正式配置和最终扫描未完成 | Step 2/9/10 |

### 3.2 阶段 5

| 退出项 | 当前状态 | 证据/缺口 | 看板动作 |
| --- | --- | --- | --- |
| Runtime/Registry/manifest/权限/预算通用契约 | 已完成（工程） | 阶段 5 review 已审查通过 | 作为基础，不重复实现 |
| 通用 Runtime Checkpoint 跨进程恢复 | 工程实现完成、质量 provisional | PostgreSQL 快照/adapter、checkpoint resume、lease-loss 取消和恢复回归已实现；正式跨进程故障注入仍待执行 | Step 6 |
| 持久审批 | 工程实现完成、质量 provisional | Adapter/API、竞态幂等和审批状态已实现；独立身份认证与正式跨进程验收未关闭 | Step 6 |
| 派生知识写入 | 工程实现完成、质量 provisional | `create_review_cards` 审批前零副作用，批准后 Derived Knowledge exactly-once 已实现 | Step 7 |
| Skill Catalog/active pointer/回滚 | 工程实现完成、质量 provisional | CAS、查询、引用保护和 cleanup 已实现；最终发布安全验收仍待执行 | Step 8 |
| 三个知识整理 Skill | 工程实现完成、质量 provisional | 固定来源、引用预览、复用 QA Run/SSE 和审批写入已实现；正式 Eval 未执行 | Step 7/8 |
| 正式 Skill Eval 与阶段退出 | 未执行 | 不得使用 fake/provisional 结果替代 | Step 9/10 |

## 4. 风险、阻塞和决策

| 编号 | 类型 | 项目 | 解除条件 | 责任步骤 |
| --- | --- | --- | --- | --- |
| B-01 | 正式阻塞 | 当前 Stage 3 评测集代表性不足，旧 development/holdout 不能支持正式结论；不再阻塞 provisional Stage 4/5 工程 | 新 dataset version 完成覆盖、标注、locator、split 和隐私审查 | 正式质量线 |
| B-02 | 正式缺口 | Stage 4 provisional QA continuation config 已完成；正式 retrieval/QA profile、prompt、Chat model 仍未冻结 | 正式冻结仍需 Stage 3 正式输入和 answer development | 正式质量线 |
| B-03 | 阻塞 | 正式 retrieval/answer holdout 尚未运行 | 冻结配置后各运行一次；失败时创建新版本，不回写 holdout | Step 1/2 |
| B-04 | 缺口 | 完整导入到反馈的真实旅程和 Playwright 尚未执行 | 隔离 Compose E2E、桌面/移动截图、键盘和失败状态回归通过 | Step 3/4/5 |
| B-05 | 正式验收缺口 | Runtime Checkpoint、lease-loss、审批跨进程工程实现已完成；正式环境故障注入尚未执行 | 在隔离正式环境补充故障注入、租约竞态和审批生命周期报告 | Step 6 |
| B-06 | 正式验收缺口 | 派生知识写入和 `create_review_cards` 工程实现已完成 | 在隔离正式环境补充 exactly-once 副作用报告 | Step 7 |
| B-07 | 正式验收缺口 | Skill 旧版本清理、引用保护和生命周期工程实现已完成 | 补充最终发布安全、cleanup dry-run 和回滚报告 | Step 8 |
| B-08 | 约束 | 阶段 0 语料为 `internal_team_only`，私有内容和 Provider 外发受限 | 保持隔离环境、manifest 校验、授权记录和隐私扫描 | 全步骤 |
| B-09 | 约束 | Windows 沙箱可能产生 `.pytest_cache` 写权限警告 | 记录为环境限制，不修改权限或测试语义 | 全步骤 |

本步新增 ADR-012，固定反馈审核生命周期、Space 隔离和 metadata-only 候选导出边界。若 Step
6/7/8 后续改变模块边界、数据模型、公开 API、事件协议或 Skill 信任模型，仍必须新增或更新 ADR。

## 5. 第 0 步完成检查

- [x] 记录阶段 3 终止事实和禁止事项。
- [x] 记录阶段 4/5 provisional 工程现状与正式缺口。
- [x] 建立 Stage 3/4/5 dataset/config/model/prompt/Skill 版本矩阵。
- [x] 为每个主要缺口指定后续步骤和解除条件。
- [x] 明确 owner 尚未认领，不虚构负责人或时间承诺。
- [x] 确认本步未运行 holdout、未读取私有正文、未调用外部 Provider、未变更数据库或 API。

## 6. 下一步进入条件

Step 2 已完成 development 工程复核。确认本看板后可进入 Step 3 的 provisional Skill catalog
工程路径；不得
直接运行 `cases/evals/configs/retrieval-v1.yaml` 的当前 holdout，也不得仅通过修改
`formal_runs_enabled` 开启正式评测。正式质量线仍需按 ADR-010/011 的后续触发条件重新建立。
