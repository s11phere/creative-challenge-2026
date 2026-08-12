# Agent Harness v2 上下文与工具编排实施计划

> 状态：进行中（Step 1 已完成；等待确认后进入 Step 2）
>
> 日期：2026-08-12
>
> 范围：Assistant Loop、Skill runtime、ModelGateway、Conversation Context、知识 Tool、checkpoint/SSE、评测与运维文档
>
> 质量边界：本计划只规划 provisional 工程改进；不运行当前 retrieval、answer 或 Skill formal holdout，也不改变 ADR-010、ADR-011 的正式质量结论。

## 1. 已确认的问题

已核对现有实现，以下问题成立：

- AutonomousAssistantLoopService 会同时注入 active Skill catalog 与每个 active Skill 的完整 instructions；未被选择的 Skill（包含 skill_creator）仍在每轮进入上下文。
- LLMDecisionNode 把全部 Tool schema 串成文本 system prompt，并要求模型生成 call_tool、complete、clarify 或 refuse 的 JSON。
- complete.final_response 与 Tool 决策共享默认 512 output tokens；截断会触发脱离已观察上下文的 long-answer 二次生成。
- KnowledgeLoopDecisionPolicy 对 grounded_answer、verify_answer、finalize_answer 强制改写模型决策，说明这些连续 gate 不应该占用模型的 Tool 选择轮次。
- 当前 checkpoint 保存模型原始观察；ConversationContextSnapshot 虽含 redacted Tool history，却没有供下一轮自省的轻量决策历史。audit output_summary/digest 也不是为模型上下文压缩设计。
- active_skill_catalog 与 active_skill 块重复相同的 name、version、description。

本计划把模型职责收敛为“选择下一项用户可解释的能力，或生成最终文本”，把权限、连续 gate、发布、恢复和摘要留在服务端。

## 2. v2 目标模型

~~~text
base prompt + thin Skill route catalog + bootstrap native Tools
  -> model
  -> native Tool call? -- no --> terminal direct text -> direct finalizer -> completed
       |
       v
  server validates / pins / invokes exactly one Tool
       |
       +-- invoke_skill --> selected Skill instructions + its Tool allowlist enter next context
       +-- knowledge_retrieve --> search + deterministic coverage inspection -> compact observation
       +-- knowledge_answer --> QA + verify + publication gate -> finalizer -> completed/refused
       +-- other Tool --> compact observation
       |
       v
  append decision summary + checkpoint -> next model round
~~~

初版只允许一个 native Tool call per model response。它保持当前顺序、审批、幂等键、取消与 checkpoint 恢复语义；并行 Tool call 留待有明确的冲突、预算和恢复模型后另行版本化。

### 2.1 模型输出与终止

- 新建 provider-neutral native-tool-use-v2：ChatRequest 携带 typed Tool definitions；ChatResponse 携带结构化 Tool calls、finish reason 和可选 text。每次调用有稳定 call ID，结果只能回送给对应 call ID。
- response 含 Tool call 时，Loop 执行这一个调用，忽略同一 response 的可选文本，写入决策摘要后继续下一轮。
- response 不含 Tool call 时，非空 text 由 DirectResponseFinalizer 原样发布为一次直接回复；空 text 是稳定 schema/provider 错误。
- v2 不再解析 LLMDecisionAction 文本 JSON，也没有 complete.final_response 或 escalate_long_answer。Tool-use 和最终文本采用互不混淆的输出路径，直接回复不再因 512-token 决策限制触发二次生成。
- clarify/refuse 的确定性服务端场景仍由服务端输出稳定消息和终态。正常无 Tool 文本统一通过 direct finalizer 发布，模型不能借此改变权限、Space、预算或终止资格。
- v1 JSON 决策专供已 pin 的旧 Run 恢复。新 v2 Run 不静默回退；Provider 未声明 native Tool-use 能力时 v2 fail closed，现有 v1 路径由显式兼容开关保留。

### 2.2 Skill 按需加载

启动上下文只含一份薄目录：稳定 name、version、短 trigger summary、是否存在 Runtime adapter 与显式 command。薄目录不含 instructions、示例、workflow、完整 schema 或 Tool 说明；同一 Skill 不再同时出现在 catalog 与 active_skill 块。

- invoke_skill(name) 是 bootstrap native Tool。服务端只接受当前 active 薄目录内的名称，再次 pin name、version、content_sha256，并校验 Space、权限和 command source。
- 选择成功仅返回 body-free 结果；下一轮才把该 Skill 的完整 instructions 和该 Skill 可用的业务 Tool schema 加入受信 system layer。
- list_skills 返回安全目录，用于“有哪些 Skill”或路由不明确的请求。没有 Runtime adapter 的 Skill 只能由显式入口启动，不能被 invoke_skill 伪装为可调用能力。
- 一次 Run 的 selected Skill stack 上限为两个，总 instruction bytes 受 versioned context budget 限制；每项均 checkpoint pin。超过限制时稳定报错或澄清，不能静默裁掉已选 Skill 的安全约束。
- 基础 prompt 只保留通用信任、权限、终止和 Tool-use 规则。具体检索示例下放到相应 Skill，并标注“仅为示意，不是固定工作流或答案模板”。

### 2.3 Tool 分层与知识编排

Tool Registry 仍是权限、Space、审批、幂等、取消与审计的权威；分层仅控制模型可见 surface，不改变 Registry 的安全校验。

| 模型可见层 | v2 Tool | 服务端职责 | 不再暴露的细节 |
| --- | --- | --- | --- |
| Bootstrap | list_skills、invoke_skill | 安全目录、active 检查、Skill pin、加载上下文 | 未选 Skill 正文 |
| Knowledge（选择后） | knowledge_retrieve(query) | SearchService.search 后立即做 coverage inspection，返回计数、缺口和安全推荐 | 检索 ORM、原文、RRF/精排细节 |
| Knowledge（选择后） | knowledge_answer() | Grounded QA、claim/citation 验证、conflict、唯一 finalizer 发布 | grounded_answer、verify_answer、finalize_answer 三个模型 gate |
| Workspace / Creator | 既有用户可解释的读、写、草稿、校验、eval、激活 Tool | 原有审批、路径、权限与副作用检查 | 内部 workflow transition |

knowledge_answer 只有在已有覆盖充分的 retrieval observation 时才继续；否则返回短的 needs_retrieval observation。进入 QA 后，Grounded QA -> verify -> finalization 是服务端事务性编排，模型不再为中间 gate 作决定，也不会在成功后再被要求发出 complete。

知识能力继续只通过 SearchService.search(SearchRequest, RetrievalProfileV1) 和现有 Grounded QA Application Port 工作，保留 QA Run、Worker、SSE、取消和 Citation 所有权。

### 2.4 模型可见上下文与决策历史

新增 hash-pinned agent-model-context-v2，严格区分“审计记录”与“下一轮模型上下文”。

- 每轮写一条 DecisionHistoryItem：iteration、tool 或 terminal、Tool 名称、短目的、结果状态、stable error code、下一项未解决问题。不得写原始 arguments、推理文本、Prompt、回答、原文或完整 Tool 输出。
- 每个 Tool 声明独立 model_observation 投影 schema；Registry 在 handler 返回后验证、裁剪和脱敏。output_summary/digest 保持 audit 字段，不会自动进入模型上下文。
- 默认模型视图最多保留最近 8 条 observation（每条最多 1,200 UTF-8 bytes）、最近 12 条决策（每条最多 320 bytes）和最多 4,000 bytes 的结构化 progress summary。过期内容按 resolved、pending、evidence、approval 聚合，不以 LLM 重述不可信内容。
- checkpoint 保存 v2 task state、selected Skill pins、上述摘要和 schema digest；恢复后沿用同一决策轨迹。v1 checkpoint 不做原地转换，继续由 v1 executor 恢复。
- ConversationContextSnapshot 复用同一摘要字段，避免 Tool history 在 loop、conversation 与 trace 三处重复序列化。

### 2.5 Prompt caching、观测与安全

- 静态前缀以 assistant-base-prompt-v8、thin catalog digest、selected Skill pin、Tool schema digest、Provider/model 建立确定性 cache key。动态用户消息、Tool observation 和 private 内容不得进入共享 cache key 或日志。
- ModelCapabilityRegistry 增加 native_tool_use 与 prompt_caching 能力。Adapter 仅在 Provider 官方支持且配置允许时发送 cache hint；不支持则正常执行，并记录 cache_mode=unsupported。
- ChatUsage 扩展可选 cache-read/cache-write token 字段。指标仅含 token、延迟、命中状态、版本和 digest，禁止记录 prompt、答案或 Tool 原文。
- QA_DEBUG_TRACE_ENABLED 继续只限 development/local。trace exporter 可增加 Tool call、context digest、可见 observation size 和 cache 指标，但不得扩大 trace 的可分享范围。
- private_local/restricted 外发、用户可见同意与部署策略继续沿用现有门禁；prompt cache 是成本/延迟优化，不是安全边界或正确性前提。

## 3. 版本与兼容策略

这是 Agent Runtime、Skill 信任模型、Provider Tool-use 和终止契约的变更，不能原地替换 assistant-base-prompt-v7 或旧 JSON 决策协议。

- 新增 ADR-020，记录渐进式 Skill 上下文、native Tool-use、服务端 gate 编排、上下文摘要和 v1/v2 并行恢复策略；同步更新 ADR-013、ADR-014、ADR-015、ADR-017 中相冲突的描述。
- 新增版本化契约与 manifest：native-tool-use-v2、agent-model-context-v2、assistant-base-prompt-v8、agent-loop-v2，以及新版 Agent SSE projection。若 API schema 变化，重新生成 docs/openapi.json。
- 新 Run 仅在 AGENT_HARNESS_V2_ENABLED=true 且 Provider capability 已验证时 pin v2。已开始的 v1 Run 继续使用原 prompt、JSON executor 和 checkpoint 直至自然终态，不得被升级为 v2。
- selected Skill pin 写入现有 versioned checkpoint/event payload。若实现核对确认需要变更关系表或生命周期字段，先增加 Alembic revision，并验证 upgrade、downgrade 与单一 head；不把数据库 schema 改动藏在 JSON 兼容分支中。
- 发布顺序：fake/local -> 开发环境已批准的 native Tool-use Provider -> 小范围、受策略允许的外部 Provider。feature flag 关闭时不得影响 v1。

## 4. 分步实施与停点

每一步完成后必须：总结实际变更、列出已运行验证和未运行项、标记 provisional 边界，然后暂停等待确认。没有下一步确认，不得自动继续。

### Step 1：冻结 v2 决策与评测基线

- 新建 ADR-020、增补冲突 ADR，添加 v2 contracts/manifest 的空实现与 hash-pinned schema tests。
- 新建 synthetic-only agent-loop-v2 用例，不含用户文本、Prompt、答案、citation 或 Tool body：direct terminal、Skill 按需加载、连续 retrieval、知识拒答、重复 Tool、审批、取消、恢复、prompt injection 与 Provider capability 缺失。
- 为 development-local trace 增加聚合指标：每轮 static/dynamic/context token 估计、未选 Skill instruction bytes、Tool 数、终止次数、二次生成次数、cache usage。不得写入 PostgreSQL、SSE 或常规日志。
- 记录匿名 v1 trace 基线与回归断言，不读取或提交私有正文。

验收：新契约和合成基线可离线验证；不改变 v1 默认行为、API 或 feature flag。

完成记录（2026-08-12）：已新增 ADR-020、hash-pinned `native-tool-use-v2`、
`agent-model-context-v2` 与 `agent-loop-v2` contracts，以及独立、body-free 的
`agent-harness-v2` synthetic metadata fixture（10 个 development cases，
`formal_runs_enabled=false`）。开发本地 trace 可通过
`scripts/summarize_agent_harness_baseline.py` 输出逐轮静态/动态上下文、eager Skill 指令、
Tool、终止/long-answer 与 cache 计数，且不输出或持久化任何内容正文。已运行：定向 pytest
`20 passed`、两套 fixture validate-only、Ruff format/check、定向 mypy、复合 JSON Schema
`$ref` 校验和 `git diff --check`。未运行 formal holdout、外部 Provider、数据库迁移或浏览器测试。

### Step 2：ModelGateway native Tool-use v2

- 扩展 provider-neutral Chat contracts、fake Gateway、capability registry 和 OpenAI-compatible adapter，支持严格 Tool schema、call ID、Tool result replay、finish reason 与 cache usage。
- 实现 v2 AgentLoopExecutor：有一个 Tool call 就执行并继续；没有 Tool call 就发布一次直接 terminal text；移除 v2 的 long-answer escalation 与文本 JSON parser。
- 保留 v1 executor/trace parser 专供已 pin 的旧 Run；覆盖 malformed call、多个 Tool call、能力缺失、空 terminal text、timeout 和恢复。

验收：fake Provider 完成 Tool -> observation -> terminal text，且一次终止回复仅有一次模型生成；v1 回归不变。

完成记录（2026-08-12）：已增加 provider-neutral native Tool-use Chat contract（严格的 Tool
schema、稳定 call ID、server-owned Tool result replay、finish reason 和 cache usage），并在
Fake 与 opt-in 的 OpenAI-compatible Gateway 中实现。新增独立的
`NativeToolUseAgentLoopExecutor`：一轮最多接受一个 native Tool call；调用后由 Registry
校验、执行、checkpoint 并回放观察；无 Tool call 的非空文本经一次 server finalizer 直接完成。
v2 不使用 v1 的文本 JSON decision parser 或 long-answer escalation。恢复路径覆盖了审批等待后的
pending Tool，并验证同一调用只执行一次。`FAST_CHAT_NATIVE_TOOL_USE` 对应的 Settings/Gateway
开关默认关闭；当前 v2 executor 尚未接入既有 Autonomous Assistant Loop，所以已 pin 的 v1 Run、
checkpoint、trace 和 API 行为不变。

已运行：`pytest`（native Tool-use、ModelGateway、config、v1 loop/autonomous loop 与 Gateway
contract）`86 passed`；定向 `ruff format --check`、`ruff check`、`mypy` 和 `git diff --check`。
pytest 仅报告既有 `.pytest_cache` 无写权限警告。未运行 formal holdout、外部 Provider、数据库迁移、
Worker/Compose 或浏览器测试；本步骤仍是 provisional 工程实现。

### Step 3：按需 Skill 选择与动态 Tool surface

- 实现 thin catalog、list_skills、invoke_skill、selected Skill checkpoint pins 与受信 prompt layer；删除 v2 中重复的 active_skill_catalog/全量 active_skill 拼接。
- 动态注册 selected Skill 的业务 Tool schema，限制 Skill stack/context budget，并保留显式 command 的服务端 pin 与访问控制。
- 将基础 prompt 示例下放到对应 Skill；覆盖未选 Skill 零注入、无 adapter Skill 不可调用、失效 pin、多 Skill 上限、重启恢复和列出 Skills。

验收：未选 skill_creator 等 instructions 不会出现在任意 v2 请求；选择后仅该 Skill 的 instructions 和 Tool 可见。

> 完成记录（2026-08-12）：已实现 `NativeSkillCatalog`、`NativeSkillPin`、薄路由条目及
> `FileSystemNativeSkillCatalog`。配置该目录的 v2 executor 首轮只暴露 `list_skills` 和
> `invoke_skill`；前者只返回 name/version/description/command/adapter 状态，后者由服务端验证
> active route 和 hash pin。选择成功仅写入 body-free observation 与 checkpoint pin，下一轮才从
> 受信 Registry 读取相应 prompt instructions，并暴露该 Skill 显式 adapter 的 Tool allowlist。
> 未选择的 Skill（包括 `skill_creator`）不进入任何 v2 request；无 adapter、重复/第三个选择、
> bootstrap 名称冲突、adapter 试图扩展服务器 Tool allowlist、pin 篡改和指令字节预算超限均 fail closed。恢复会重新解析已选 pin 并验证
> Registry 内容哈希，checkpoint 不保存 instructions。v1 Assistant Loop 仍未接入此路径，故原有
> eager prompt、API、checkpoint 和运行行为保持不变。
>
> 已运行：定向 pytest（native Tool-use、native Skill catalog、Skill Registry、Assistant command
> 和 workflow Skill contract）`49 passed, 1 skipped`；`ruff format --check`、`ruff check`、
> `mypy apps packages` 与 `git diff --check`。pytest 仅报告既有 `.pytest_cache` 无写权限警告。
> 未运行 formal holdout、外部 Provider、数据库迁移、Worker/Compose 或浏览器测试。本步骤仍为
> provisional 工程实现。

### Step 4：知识 Tool 收敛与服务端 gate 编排

- 将 knowledge_search + knowledge_inspect 收敛为 knowledge_retrieve；将 grounded_answer + verify_answer + finalize_answer 收敛为 knowledge_answer 的服务端编排。
- 删除 v2 KnowledgeLoopDecisionPolicy 对内部 gate 的模型决策改写；保留 QA Port、证据、refusal/conflict、Space、取消、lease、idempotency 与唯一 finalizer。
- 更新 knowledge_agent v2 instructions 和 Tool observation schema；覆盖多次 retrieval、覆盖不足、冲突、无答案、QA/验证失败、workspace artifact 与恢复。

验收：知识成功路径不再出现“模型 complete 后被服务端强制 finalize”的额外模型轮次；无引用直接回答仍不能绕过 Grounded QA。

### Step 5：上下文压缩、决策历史与 cache

- 实现 agent-model-context-v2、DecisionHistoryItem、按 Tool 许可的 observation 投影、滚动 progress summary 与 v2 checkpoint 恢复。
- 调整 ConversationContextService 和 trace exporter，确保模型只见 v2 上下文预算内的数据，audit summary/digest 不泄漏为 Tool 正文。
- 实现 cache capability、稳定 cache key、usage metrics 和故障降级；覆盖多轮大小上界、历史自省、redaction、跨 Space、private/restricted 与 cache-disabled Provider。

验收：长 Run 的模型可见上下文有固定上界且含决策历史；无未选 Skill 正文、全量 Tool result 或重复 static catalog；cache 不可用时行为和安全语义不变。

### Step 6：SSE/Web、文档与受控发布

- 新增以 Step 1 契约命名为准的安全事件投影，显示 Skill selected、Tool family、决策摘要、context/cache usage 和 stop reason，不显示 prompt、原文、答案或 Tool body。
- 更新 Web timeline 的 Tool 分组、selected Skill 状态、直接终止与 grounded terminal 展示；保留旧 Run 旧投影，完成可用的 Vitest/浏览器回归。
- 更新 README、architecture、troubleshooting、stage tracker、trace 文档和 OpenAPI；执行 feature flag 灰度与回滚演练，确认旧 Run 可读、v2 Run 可恢复。

验收：前后端按版本读取历史与新 Run；所有文档明确仍为 provisional，feature flag 关闭时 v1 不受影响。

## 5. 验证与最终完成门槛

每步按影响范围运行实际可用命令；Step 6 最终至少运行：

~~~powershell
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe apps packages
.venv\Scripts\python.exe -m pytest tests/unit
.venv\Scripts\python.exe scripts/evaluate_agent_loop.py --validate-only
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
.venv\Scripts\python.exe scripts/export_openapi.py
.venv\Scripts\python.exe -m pytest tests/unit/test_openapi.py
git diff --check
~~~

数据库或 API 变更另运行隔离 PostgreSQL/Redis 的 migration、integration 与 OpenAPI 一致性检查；真实依赖测试显式使用 RUN_INTEGRATION=1。不得运行当前 formal holdout，也不得把 synthetic、fake、development 或 trace 成果描述为正式质量接受。

v2 的量化完成门槛：

- 任一 v2 请求中，未 selected Skill 的 instruction bytes 必须为 0；完整 Skill 信息最多出现一次。
- Tool schema 通过 native Tool-use 传递，v2 system/user text 不得再出现序列化的全量 Tool JSON 或 action=call_tool 指令。
- 直接终止回复没有 long-answer 二次生成；知识完成后不再请求模型 complete，finalizer 仅发布一次。
- 模型可见 observation/decision history 符合 v2 字节上限，恢复后保持相同摘要顺序。
- synthetic 安全用例、v1 恢复回归、Space/approval/cancellation/重复调用负例全部通过；外部 Provider 的 cache/native Tool-use 始终受 capability 与现有隐私同意门禁约束。

## 6. 保留的边界

- 本计划不会开放新的文件、shell 或网络权限，也不会让模型决定 Space、版本、Tool 权限、预算、approval、citation 或最终发布资格。
- Skill instructions 是受信配置；文档、用户消息、Tool 输出、历史摘要都是不可信数据。按需加载不是把未受信 Skill 内容提升为 system instruction 的机制。
- Stage 3、4、5 的 formal quality gate 仍未关闭。所有后续交付、文档和验证报告必须保留这一边界。
