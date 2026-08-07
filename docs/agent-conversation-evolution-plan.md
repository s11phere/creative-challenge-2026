# 通用 Agent 对话、自动 Skill 路由与指令系统实施计划

> 文档状态：Draft v1  
> 制定日期：2026-08-06  
> 适用范围：现有 Web 对话入口、Agent Runtime、Skill Registry、QA Run/Worker/SSE 及相关公开契约  
> 质量边界：本文只规划 provisional 工程演进，不改变 ADR-010/ADR-011，也不构成检索、回答或 Skill 的正式质量接受

## 2026-08-07 Implementation Status

Steps 0-8 in this plan are implemented for the current provisional engineering boundary. The v2
conversation workspace is the default Web entry, and the server-authoritative command catalog
supports automatic routing plus explicit slash commands. New knowledge requests use only
`knowledge_agent 0.3.0`; `knowledge_qa` is retained for historical Run recovery and validation.

The closeout includes bounded conversation context, operational metrics, exactly-once finalization,
durable Skill invocation trace cards, mutually exclusive and closable citation display, original
command preservation with metric-neutral prefix highlighting, and Markdown/GFM/LaTeX rendering.
The finalizer receives the user question and the Skill result as separate inputs and publishes a
separate Assistant message, so a Skill result is never treated as the final answer. Formal
retrieval, answer, and Skill quality gates remain provisional under ADR-010/ADR-011; browser
Playwright evidence is still not claimed.

## 1. 目标与非目标

本次演进的目标是把当前“先手动选择 Skill，再提交问题”的知识问答工作区，改造成一个默认可直接
对话、能够自主判断是否调用 Skill、也允许用户用 `/` 指令确定性指定行为的通用 Agent。

完成后应满足：

1. 普通消息默认进入通用 Assistant Agent；日常对话无需调用知识问答或任何 Skill。
2. Agent 根据当前请求、必要的会话上下文以及已激活 Skill 的触发说明，自主选择直接回答、追问，
   或调用一个受信 Skill。
3. 输入框以 `/` 开头时展示可搜索指令面板；Skill 可声明一个主指令及别名，基础指令与 Skill 指令
   使用同一交互。
4. `/skill-name 问题` 确定性选择 Skill，不再经过模型路由，但仍经过服务端版本固定、权限、Space、
   schema、审批和数据外发检查。
5. 会话保留完整消息事实；模型上下文使用最近窗口、滚动摘要和当前任务状态，长会话不会因为到达
   前端可见的 token “额度”而停止。
6. Token 用量可作为折叠的运行信息展示；不显示剩余额度、token 进度条或 Tool 调用次数上限。

以下内容不在本次范围内：多 Agent 编排、开放式任意插件、远程 Skill 安装、跨 Space 记忆、绕过审批
的写操作，以及重新开启当前 formal holdout。通用对话也不是“联网事实搜索”；没有适用 Skill 或
Tool 时，Agent 应直接回答、说明不确定性或请求澄清。

## 2. 当前实现差距

| 目标 | 当前行为 | 需要的变化 |
| --- | --- | --- |
| 默认通用对话 | Web 固定显示五个模式，默认 `knowledge_agent` | 移除提交前的模式选择，新增通用 Agent 入口 |
| 自动 Skill 调用 | `skill_name` 由前端选择并写入请求 | 服务端向模型提供 active Skill catalog，由模型输出受校验的调用意图 |
| `/` 指令 | 无指令解析和候选面板 | 新增版本化 Command catalog、服务端解析和 Web combobox |
| 日常聊天 | `knowledge_agent` 最终总会回退到 `grounded_qa` | 新增直接回答终态；知识 QA 只作为可选 Skill |
| 多轮路由 | QA 生成会读取历史；Agent 决策只看到当前问题 | 通用上下文服务同时服务路由、直接回答和 Skill 入参解析 |
| 长会话 | QA 按消息数/token 上限截断，超限可成为契约错误 | 自动压缩旧历史并保留最近轮次，不向用户暴露“剩余额度” |
| 运行信息 | Skill 管理页展示步数、Tool 数和超时预算 | 产品界面仅可选展示实际 token/耗时；保护阈值留在服务端 |

现有 `Conversation`/`Message` 已持久化完整对话，QA 生成也已读取先前消息，因此无需重建第二套会话
系统。主要结构性缺口是：当前持久 Run 和 Runtime 都要求在创建时已有固定 Skill 身份，无法先创建
一次通用对话运行，再由 Agent 选择是否调用 Skill。

## 3. 目标交互

### 3.1 普通输入

用户直接输入消息。通用 Agent 可返回三类决策：

- `respond`：直接聊天，不创建知识 QA 子流程，也不伪造引用。
- `clarify`：缺少资源、范围或意图不明确时追问；可附带服务端授权的资源候选。
- `invoke_skill`：选择一个 active Skill，并提交经过 schema 校验的参数。

模型只能从本次请求的服务端 allowlist 中选择 Skill。模型输出只是意图；Application 层负责固定
`(name, version, content_sha256)`、收窄 Space/资源范围并真正执行。

### 3.2 斜杠指令

首期基础指令：

| 指令 | 行为 | 是否创建 Agent Run |
| --- | --- | --- |
| `/help` | 展示当前可用基础指令和 active Skill 指令 | 否 |
| `/skills` | 展示 active Skill、用途和可用状态，不展示运行预算 | 否 |
| `/new` | 创建并切换到新会话 | 否 |
| `/compact` | 请求为当前会话生成新的滚动摘要 | 是，后台运行 |
| `/stop` | 取消当前活动 Run | 否，复用现有取消协议 |

首期 Skill 指令为 `/ask`、`/summarize`、`/compare`、`/cards`，分别映射
`knowledge_agent`、`summarize_document`、`compare_sources` 和 `create_review_cards`。
`knowledge_agent 0.3.0` 是唯一的新知识问答入口；`knowledge_qa` 的旧包只保留给固定历史 Run
恢复，不能由命令、自动路由或新的 v1 Run 请求选择。

客户端负责即时匹配和键盘交互，服务端负责最终解析。规则固定为：仅消息首个非空字符为 `/` 时
解释为指令；指令名大小写不敏感；`//` 转义为普通文本；未知指令不猜测执行，而是返回候选；Skill
指令缺少问题或必要资源时进入 `clarify`。显式指令优先于自动路由，但不能提升权限或扩大 Space。

### 3.3 需要资源的 Skill

摘要、比较和复习卡目前依赖固定 Source/Document/DocumentVersion。新入口不能让模型直接生成这些
ID。通用 Agent 只传自然语言资源提示，Application 层通过只读资源解析 Port 在当前 Space 中查找：

- 唯一匹配：固定当前已发布版本后执行 Skill。
- 多个匹配：返回 `clarify` 和仅含安全显示元数据的候选，Web 渲染选择器。
- 无匹配或版本已变更：返回稳定的 `RESOURCE_NOT_FOUND`/`RESOURCE_CONFLICT`，不扩大范围。

用户选择候选后以原 Agent Run 的 idempotency key 派生确认请求，避免重复创建或执行。

## 4. 核心架构决策

### 4.1 通用 Agent 与业务 Skill 分层

新增 Application 层 `AssistantAgentService` 作为会话 turn 的唯一入口。它不是一个可安装 Skill，
而是负责基础系统提示词、上下文选择、自动路由、直接回答和 Skill 调度的产品级编排器。

基础系统提示词至少包含：角色与能力边界、直接回答条件、调用 Skill 的决策原则、active Skill
触发目录、Tool/Skill 输出的不可信边界、权限与审批规则、澄清策略、会话上下文规则、语言和回答风格。
提示词不得把 Agent 定义成“知识问答助手”，也不得要求每轮都调用 Tool。

基础提示词只注入每个 Skill 的短触发元数据和输入摘要，不拼接完整 Skill prompt。选中 Skill 后，
Registry 固定 active 版本并按摘要校验，再加载该版本自己的 prompt、workflow 和 Tool allowlist。
这形成“目录发现 -> 固定版本 -> 按需加载”的渐进披露，避免提示词膨胀和多个 Skill 指令冲突。

### 4.2 Skill manifest v2

新增 manifest v2，并继续只读支持 v1 以恢复历史 Run。v2 在既有字段上增加：

```yaml
invocation:
  command: summarize
  aliases: [summary]
  argument_hint: "<文档或主题> [重点]"
  trigger:
    summary: "对当前 Space 中的一个固定文档版本生成带引用摘要"
    when:
      - "用户明确要求总结或提炼某个库内文档"
    avoid_when:
      - "用户只是日常聊天或总结其刚输入的一小段文字"
    examples:
      - "总结操作系统课程笔记中的进程调度章节"
  input_mode: document
```

Registry 在激活时验证：主指令和别名全局唯一、触发文本长度受控、示例不包含敏感内容、输入模式与
schema 相容。Catalog API 只返回 active 版本的安全调用元数据；完整 prompt、内部预算和未激活版本
不会进入模型路由目录。任何命令或触发元数据变化都产生新 Skill 版本和内容摘要。

### 4.3 Run 与持久化

引入通用 `ConversationRun` 作为持久运行父实体，字段包含 conversation/space/caller、当前用户消息、
`run_kind`、router/core-prompt/model 版本、状态、实际 usage、选择来源（`auto`/`command`/`none`）和
可选的固定 Skill 身份。现有 QA Run 成为同一 run identity 的 grounded-QA 投影，而不是平行系统。

迁移采用前向 Alembic revision：先创建父记录并回填现有 `qa_runs`，再让 `runtime_runs`、审批、
checkpoint 和 QA 投影引用同一父 ID。旧 `/api/v1` 继续通过映射读取；新接口使用版本化 v2 契约。
迁移必须验证空库、既有数据升级、downgrade 和单一 head。

Worker 仍只接收 `run_id/trace_id/event_version`，由 Application dispatcher 根据持久 `run_kind` 执行
直接回答、上下文压缩或固定 Skill。不得在 API 请求协程中执行长模型调用，也不得创建第二套队列、
取消、审批或重试协议。

### 4.4 对话上下文策略

原始 `Message` 始终 append-only，是可恢复事实源。新增版本化 `ConversationContextService`：

1. 固定包含当前用户消息、最近连续对话轮次、待审批/待澄清状态。
2. 在上下文软水位以上，将更早消息压缩为滚动摘要；摘要记录覆盖的消息范围、模型/提示词版本、
   内容摘要和 sensitivity，绝不覆盖或删除原消息。
3. 自动压缩在后台、幂等执行；`/compact` 只是显式触发同一用例。
4. 摘要与历史都作为有边界的用户上下文，不能覆盖 system instruction；Tool 结果和文档继续标记为
   不可信数据。
5. Skill 调度生成一个可追溯的 standalone request，解决“它/上一个文档”等指代；原始用户消息仍
   保留，QA 子流程只接收完成任务所需的历史和固定资源范围。
6. 不跨 Conversation 或 Space 自动召回记忆；长期个人记忆另行决策，不在本计划内。

上下文窗口、Provider 最大输入和超时仍是服务端事实。达到软水位时应压缩或缩减旧历史，而不是向
用户显示“token 不足”。无法在 Provider 硬上限内构造安全请求时返回可恢复错误，并建议新建会话，
不能静默丢弃当前任务所需信息。

### 4.5 预算与运行保护

用户目标是移除产品层的人工配额感知，不是取消服务端安全边界。实现时应：

- 将固定的低 `max_tool_calls` 从模型可见 Skill 目录和对话 UI 移除。
- Agent 以任务完成、澄清、拒绝、取消或无进展检测为正常停止条件。
- 服务端保留 Provider 上下文上限、总超时、取消、重复调用检测、相同参数循环检测、审批和部署级
  emergency ceiling；这些是防失控保险丝，不作为用户工作流配额宣传。
- Token 记录改为实际 `input/output/total` usage；可在折叠的“运行信息”中展示模型、耗时和实际
  token，不显示剩余 token、预算进度或 Tool 次数上限。
- Tool 调用明细仅进入脱敏审计和开发诊断；默认对话界面不展示调用次数。

此变化需要更新 ADR-003 和 ADR-006；不能简单删除 `RunBudget` 而失去恢复前的剩余资源校验。

### 4.6 隐私与信任

自动路由不得削弱现有边界：

- 路由模型看到用户消息和安全 Skill 目录，不默认看到文档正文。
- Skill/Tool 权限由服务端 Skill pin 与 caller policy 交集决定，模型选择不授予权限。
- `private_local`/`restricted` 的消息、摘要和文档片段不得发送给未获批准的外部 Provider。
- 会话摘要、standalone request 和模型响应继承输入 sensitivity；日志、trace、队列和 committed
  fixture 只保留 ID、摘要、计数和安全错误。
- 文档中的 `/command`、Skill 名称或“覆盖系统提示词”均视为内容，不触发命令或权限变化。

## 5. 公开契约草案

新增 API v2，保留现有 v1 作为兼容层：

```text
GET  /api/v2/commands
POST /api/v2/conversations/{conversation_id}/turns
GET  /api/v2/runs/{run_id}
GET  /api/v2/runs/{run_id}/events
POST /api/v2/runs/{run_id}/cancel
POST /api/v2/runs/{run_id}/clarifications/{clarification_id}
```

提交 turn 的最小请求为 `content + idempotency_key`；客户端可附带从 `/api/v2/commands` 解析出的
`command`，但服务端必须根据原始首行重新校验。响应统一包含：

- `run_id/status/run_kind`；
- `selection.source` 与可选的固定 Skill identity；
- 最终 assistant message，或结构化 clarification；
- grounded Skill 才有 citations/write approval；
- 可选实际 token/耗时 usage，不返回 Tool 调用上限。

新事件协议命名为 `agent-run-sse-v2`，覆盖 `accepted/routing/clarification/skill_started/phase/
completed/failed/cancelled`。Grounded QA 事件投影到该协议，现有 `qa-sse-v1` 继续供 v1 客户端使用。
事件仍以 PostgreSQL 持久状态为权威，未知版本不得静默解释。

## 6. 分步实施计划

### Step 0：决策、基线与评测集

- 新增 ADR，更新 ADR-003/006/007：通用 Run 父身份、自动 Skill 路由、manifest v2、上下文压缩和
  `agent-run-sse-v2`。
- 冻结基础 system prompt、路由 decision schema、Command schema、clarification schema 和错误码。
- 建立仅含合成/允许 fixture 的 routing development 集，覆盖日常聊天、知识问题、显式命令、歧义
  资源、prompt injection、跨 Space、写审批和不应调用 Skill 的反例。
- 记录现有 Web/API/Run/历史恢复基线；不得运行当前 formal holdout。

退出条件：ADR 状态明确；schema 契约测试可运行；数据与 Provider 边界通过隐私审查。

### Step 1：通用 Run 与 API v2 骨架

- 在 Domain 定义 `ConversationRun`、选择来源、run kind、clarification 和通用 assistant result。
- 新增 Application Port、PostgreSQL repository、迁移和现有 QA Run backfill。
- Worker dispatcher、取消、租约、重试、checkpoint 和 approval 改为共享通用 run identity。
- 实现 API v2 读写与 v1 兼容映射，重新生成 `docs/openapi.json`。

退出条件：不调用模型也可持久创建/恢复一个 turn；历史 QA Run 在 upgrade/downgrade 后身份与引用不变；
重复提交不会产生两个终态。

### Step 2：通用直接对话纵向闭环

- 实现 `AssistantAgentService`、版本化基础 prompt 和严格 JSON decision parser。
- 先只开放 `respond/clarify`，通过 `ModelGateway.fast_chat` 生成直接回答。
- 直接回答与 assistant message 原子发布；接入 Worker、SSE、取消、失败和恢复。
- fake provider 提供确定性日常对话路径，测试/CI 不依赖外部模型。

退出条件：Web/API 可在不触发检索的情况下完成多轮普通对话；失败不产生伪 assistant 终态；日志不含
用户原文或模型原始响应。

### Step 3：Skill catalog v2 与自动调用

- 扩展 Registry 读取 manifest v2 `invocation`，验证命令唯一性并生成安全 active catalog。
- 为四个业务 Skill 发布新版本的触发说明；旧 Skill 版本保持可恢复。
- 将 active catalog 注入基础 prompt；实现 `invoke_skill` decision、固定版本和 child execution mapping。
- `knowledge_agent` 通过 `GroundedQAApplicationPort` 完成最终回答；组织 Skill 继续复用既有 QA Run/Worker/SSE。
- 增加资源解析 Port 和 `clarify` 候选，不允许模型提供可信 UUID/Space/版本。

退出条件：普通聊天不强制进入 QA；明确知识请求可自动调用正确 Skill；命令/模型均不能调用 hidden、
inactive 或未授权 Skill；历史 Run 不随 active pointer 改变。

### Step 4：服务端 Command 系统

- 实现 `/api/v2/commands` 和权威 parser，合并基础指令与 active Skill 指令。
- 实现 `/help`、`/skills`、`/new`、`/compact`、`/stop`，以及四个 Skill 指令。
- 命令直接映射到与自动路由相同的 Application 用例；增加未知命令、别名冲突、`//` 转义、空参数和
  idempotency 测试。

退出条件：显式 Skill 指令 100% 绕过模型选择但不绕过安全校验；基础指令不误创建业务 Run。

### Step 5：多轮上下文与自动压缩

- 实现 `ConversationContextService`、持久摘要 schema 和版本化 summary prompt。
- 路由、直接回答、资源指代解析和 Skill standalone request 使用同一上下文快照。
- 增加软水位自动压缩、`/compact`、并发消息、摘要失败回退、恢复和 sensitivity 传播。
- 保留 QA `ContextBuilder` 的证据隔离；只传任务需要的会话上下文，不把整个聊天历史复制到每个 Skill。

退出条件：长会话能持续提交；最近意图和明确指代不因压缩丢失；原始消息可完整查询；摘要不能注入
system instruction 或跨 Space 泄漏。

### Step 6：Web 对话与 `/` 指令面板

- 将 `QAWorkspace` 演进为通用 Chat Workspace，移除五个常驻模式按钮和“只能知识检索”的空状态文案。
- 输入首个 `/` 时打开 ARIA combobox/listbox；按名称、别名和描述实时匹配，支持上下键、Enter、Esc、
  鼠标、移动端和中文 IME，选择后插入 `/<command> ` 并继续输入问题。
- clarification 使用行内资源选择器；选择后回到原 Run，不重建会话。
- 消息按直接回答、Skill 回答、澄清、审批和错误渲染；引用侧栏只在有 grounded citations 时出现。
- 折叠的运行信息仅显示实际 token、模型和耗时；移除预算条、剩余额度和 Tool 调用上限展示。

退出条件：桌面与移动 viewport 无重叠；键盘和屏幕阅读器可完整操作；刷新后会话、clarification、
活动 Run 和 citations 均可恢复。

### Step 7：安全、质量与可观测性收口

- 增加路由精确率/召回率、日常聊天误触发率、clarification 成功率、命令命中率、上下文压缩率、
  实际 token、延迟和循环终止原因指标；报告保持 development/provisional 标签。
- 覆盖模型选择越权 Skill、文档伪造命令、重复 Tool 参数循环、外部 Provider policy、写审批、取消、
  Worker 重启、active Skill 切换和日志泄漏测试。
- 运行后端 format/lint/typecheck/test、隔离 PostgreSQL/Redis integration、迁移往返、OpenAPI 无差异、
  Web lint/typecheck/test/build 和 Playwright desktop/mobile E2E。
- 更新 README、architecture、stage tracker、Skill README、development environment 和 troubleshooting。

退出条件：显式命令契约与安全用例全通过；自动路由 development 报告可复现；跨 Space/未授权/未审批
副作用为 0；不把 provisional 结果表述为正式质量接受。

### Step 8：兼容迁移与受控发布

- 默认 Web 切换到 API v2；v1 模式选择入口进入一个有期限的兼容窗口。
- 先对 fake/local Provider 发布，再按现有外发策略小范围启用获批的外部 Chat Provider。
- 监控自动路由误触发、clarification 循环、取消率、恢复失败和 token/延迟回归；保留配置级回滚开关。
- 只有当真实使用证明无需 v1 且所有历史 Run 可读取时，另行版本化移除兼容接口。

退出条件：新入口为默认路径；旧 Run 和 v1 客户端继续工作；回滚不需要删除数据或 Skill 包。

## 7. 验收矩阵

| 场景 | 预期结果 |
| --- | --- |
| “今天状态怎么样” | 直接回答，不调用知识 Skill |
| “我的笔记里进程和线程有什么区别” | 自动调用 `knowledge_agent`，返回可验证引用或证据不足拒答 |
| `/ask 比较 TCP 和 UDP` | 确定性调用 `knowledge_agent`，不经过模型选择 |
| `/summarize 操作系统笔记中的内存章节` | 唯一文档则固定版本执行；歧义则请求选择 |
| `/compare CS229 笔记和数学笔记中的优化方法` | 固定至少两个合法来源后执行，否则澄清 |
| `/cards 量子力学常用公式` | 生成带引用预览；写入仍需持久审批 |
| “把上一个回答说得更简单” | 使用最近上下文直接回答或延续原 Skill 结果，不重新做无关检索 |
| 文档正文包含 `/cards` | 视为不可信内容，不触发命令 |
| active Skill 在排队后切换版本 | 已创建 Run 使用原固定版本，新 Run 使用新版本 |
| 长会话超过软水位 | 自动生成可追溯摘要并继续，不显示 token 配额阻断 |

## 8. 风险与取舍

| 风险 | 处理 |
| --- | --- |
| 所有 Skill prompt 注入导致冲突和成本增长 | 基础 prompt 只放触发目录，选中后按需加载完整 Skill |
| 自动路由误调用昂贵或写 Skill | 写操作永远审批；低置信度澄清；显式命令可覆盖路由 |
| 移除预算造成无限循环 | 隐藏产品配额，但保留超时、取消、循环检测和 emergency ceiling |
| 通用聊天破坏有引用问答 | 直接回答和 grounded answer 使用不同 result kind；只有后者展示 citations |
| 长摘要歪曲历史 | 原始消息永不覆盖；摘要有版本/范围/摘要；最近关键轮次始终保留 |
| 新通用 Run 形成第二套 Runtime | 使用共享父 run identity，QA/Runtime/审批只是投影 |
| 资源自然语言解析扩大检索范围 | Application Port 只在当前 Space、当前发布集合内解析并固定版本 |
| 自动能力被误写成正式质量完成 | 单独 routing development 报告，持续保留 ADR-010/011 边界 |

## 9. 推荐执行顺序

严格按 `Step 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8` 推进。先建立通用 Run 和直接聊天，
再接自动 Skill；先完成服务端 Command 契约，再做前端候选面板；上下文压缩必须在长会话默认开放前
完成。任何阶段都不得通过前端硬编码 Skill、在 API 内同步跑长任务、复制 QA 逻辑或放宽数据外发
策略来提前演示。
