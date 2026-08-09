# 通用 Agent Loop、基础 Tool 与 `/effort` 实施计划

> 文档状态：Draft v1（仅计划，尚未实施）  
> 制定日期：2026-08-09  
> 适用范围：`agent-runtime`、Assistant conversation、知识问答 Skill、ModelGateway、API/SSE、Web  
> 质量边界：本计划只规划 provisional 工程演进，不改变 ADR-010/ADR-011，也不构成检索、回答或 Skill 的正式质量接受。

## 1. 目标与约束

本计划把现有“固定 Skill workflow”演进为“Skill 提供指导和 Tool allowlist，Agent 在循环中自主决策”的通用执行模型：

1. Agent 可在一次 Run 中反复规划、调用 Tool、观察结果并继续决策；正常流程不向用户展示固定调用次数。
2. 每个 Run 必须以唯一的 `finalizing -> completed` 路径结束，最终一步只能发布 Assistant final message。
3. 基础文件、写入文件和命令执行能力通过受信 Tool Registry 提供，所有副作用经过服务端权限和必要的用户审批。
4. 用户通过 `/effort` 决定推理强度；Gateway 根据 Provider/model 能力自动映射，并记录请求值与实际生效值。
5. 知识检索、证据检查和 grounded answer 成为 Tool，继续复用 `SearchService.search(...)` 与现有 Grounded QA Application Port。
6. Web 通过 SSE 实时展示脱敏的完整 Tool 调用历史，最终回答固定呈现在时间线末尾。
7. Agent 只有在确认目标完成、证据充分，或已确认证据不足/存在冲突时才允许最终回答。

必须保持的边界：Domain 不依赖 Provider SDK；QA 不绕过 Application Port；Skill 版本、Space、敏感度、审批、取消、租约、checkpoint 和 Worker 语义继续由服务端决定；`private_local`/`restricted` 内容默认不得外发；不得递归摄入 `cases/`，不得运行当前 formal holdout。

## 2. 当前实现差距

- `BoundedLLMAgentNode` 目前有 `max_iterations=4`，且 `knowledge_agent` prompt 写死检索/回答次数。
- `ToolDefinition`/`InMemoryToolRegistry` 已有 schema、权限、Space、幂等、审批和重试基础，但基础文件/shell Tool 尚未成为通用 Agent 能力。
- `ConversationRun`、checkpoint、`agent-run-sse-v2` 和 `ConversationFinalizer` 已存在；SSE 尚不能表达逐次 Tool 请求、开始、结果和审批。
- `ChatRequest` 没有请求级 reasoning 参数；配置只有 `fast_chat_reasoning_enabled` 布尔开关。
- `knowledge_agent` 已有 `inspect_retrieval` 和 `grounded_qa`，但调用约束仍由 Skill workflow/prompt 固定，而非由通用 Loop 状态决定。

## 3. 目标运行模型

```text
accepted
  -> planning
  -> tool_requested
  -> (waiting_approval)?
  -> tool_running
  -> observing
  -> planning ...
  -> finalizing
  -> completed
```

其他合法终态为 `clarifying`、`refused`、`failed`、`cancelled`、`timed_out`。模型输出只是经过 JSON Schema 校验的意图，不能直接授予权限、指定 Space/版本/内部 UUID 或发布消息。

`finalize` 是唯一的最终动作。进入 `finalizing` 后不得再调用 Tool；finalizer 接收结构化任务状态和已验证结果，原子发布一条 Assistant Message，再写入唯一终态事件。重复投递、Worker 重启和 SSE 重连都必须保持 exactly-once 语义。

## 4. 分阶段实施

### Step 0：契约、ADR 与评测基线

- 新增/更新 ADR，覆盖通用 Loop、Tool 信任模型、reasoning profile、事件协议和 Skill 指导模型。
- 冻结 `agent-loop-v1`、`tool-invocation-v1`、`reasoning-profile-v1`、`agent-run-sse-v3`、`assistant-final-answer-v2` schema。
- 建立合成 development 集：普通聊天、需多轮检索、证据不足/冲突、Prompt injection、跨 Space、写审批和命令越权。
- 记录现有 v1/v2 API、Run 恢复、finalizer 和 Web 状态基线；不运行当前 formal holdout。

退出条件：schema contract tests、隐私审查、版本兼容策略和回滚开关确定。

### Step 1：通用 Loop Domain/Application Port

- 增加 Loop iteration、任务目标、完成检查、Tool observation、停止原因和 finalization 状态。
- 将现有 `BoundedLLMAgentNode` 重构为可恢复的循环执行器；移除 Skill prompt 中的固定调用次数约束。
- 保留服务端总超时、上下文上限、重复参数/无进展检测、取消和 emergency ceiling。
- 每次迭代保存 checkpoint；恢复时校验 caller、Space、Skill digest、schema、approval、lease 和幂等键。

退出条件：fake Provider 可执行“多次 Tool -> 观察 -> finalizing -> final message”；任一运行路径不能绕过最终输出状态。

### Step 2：上下文快照与循环上下文

- 扩展 `ConversationContextService`，统一生成 router、Loop、资源解析和 Skill standalone request 的快照。
- 快照包含当前目标、子问题、最近对话、滚动摘要、Tool 历史摘要、证据覆盖、未解决项和取消/审批状态。
- 对支持 Responses continuation 的 Provider 保存 continuation ID/必要 items；其他 Provider 使用结构化 transcript 重放。
- 原始 Message append-only；摘要版本化、幂等、继承 sensitivity，不能覆盖 system instruction。

退出条件：长会话压缩后仍保留当前意图、明确指代和未完成任务；上下文不会跨 Conversation/Space 泄漏。

### Step 3：基础只读 Tool

- 注册 `fs_list`、`fs_read`，仅允许配置的 workspace/trusted roots 和 manifest 允许路径。
- canonicalize 路径并拒绝 `..`、符号链接/junction 越界、设备路径、超大文件、过深遍历和编码异常。
- 返回结构化元数据和截断内容；Tool 输出标记为不可信，日志只保留摘要/哈希。
- 为路径隔离、大小限制、取消、超时、幂等和跨 Space 编写单元/契约测试。

退出条件：Agent 可在不修改文件、不执行命令的情况下完成安全读取任务；workspace 外读取始终被拒绝。

### Step 4：写文件、命令执行与审批

- 注册 `fs_write` 和 `shell_exec`，默认拒绝，必须经过 durable approval、目标复核和 idempotency key。
- `fs_write` 使用原子临时文件/替换、大小限制、目标白名单和失败清理；不得覆盖 `.env`、凭据、系统提示词或 Skill 包。
- `shell_exec` 使用受控 executable allowlist、固定 cwd、最小环境、无默认网络、超时、输出截断和取消。
- 将批准/拒绝/过期/竞态/重试/零副作用写入测试和 Web 状态。

退出条件：未经批准的写入、删除、安装、网络和进程控制均无副作用；批准后执行 exactly once，可恢复。

### Step 5：知识能力 Tool 化

- 将现有能力拆为 `knowledge_search`、`knowledge_inspect`、`grounded_answer`、`verify_answer` 和 `finalize_answer`。
- 所有检索只能调用 `SearchService.search(SearchRequest, RetrievalProfileV1)`，不得读取检索 ORM 表或复制过滤/RRF/精排逻辑。
- `knowledge_search`/`knowledge_inspect` 默认只返回覆盖计数、证据 ID、来源/版本元数据和缺口信号；正文只进入现有 QA ContextBuilder/Generator。
- `grounded_answer` 复用 Grounded QA Application Port；`verify_answer` 检查 claims、citations、冲突和证据充分性；证据不足是正常拒答，不是基础设施失败。
- 更新 `knowledge_agent` prompt：先定义目标和子问题，全面检索，检查覆盖，再决定补充检索、拒答或最终回答。

退出条件：多文档问题不再被“一次 grounded QA”截断；答案只能引用当前 Run 已验证证据。

### Step 6：`/effort` 与能力自动映射

- 增加用户层枚举：`auto | none | minimal | low | medium | high | xhigh | max`。
- 支持 `/effort` 查看当前设置，`/effort auto|low|high` 修改 Conversation 默认偏好。
- 扩展 `ChatRequest`/Gateway Port 为 provider-neutral reasoning profile，不把 OpenAI 字段放入 Domain。
- 建立 model capability registry：记录支持的 effort、默认值、是否支持 reasoning mode、是否支持 continuation。
- 映射规则：Responses reasoning model 传原生 effort；只有布尔 thinking 的 Provider 将 `none` 映射为 disabled、其余映射为 enabled 并标记 coarse；不支持 reasoning 的 Provider 对显式高强度请求 fail-closed，`auto` 才允许降级。
- 每个 Run 持久化 requested/effective effort、Provider/model、映射版本和降级原因。

退出条件：用户选择可审计、映射可测试、显式请求不会静默降低；旧 Provider 仍兼容。

官方协议复核：实现 Responses/Function Calling Adapter 前重新核对 [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning) 和 [Function calling](https://developers.openai.com/api/docs/guides/function-calling) 的模型支持矩阵。

### Step 7：SSE v3、调用历史与持久化

- 新增 Tool invocation、iteration、stop reason、reasoning profile、continuation/checkpoint 和 finalizer publication identity 的持久化记录。
- 新事件：`iteration_started`、`tool_requested`、`tool_started`、`tool_output`、`approval_required`、`checkpoint_saved`、`finalizing`，以及现有终态事件。
- payload 只含 Tool 名称/版本、状态、耗时、重试、输入/输出摘要、计数和稳定错误码，不含 prompt、回答、原文、密钥或完整 shell 输出。
- 保留 v1/v2 投影；新增分页/重连读取接口，未知事件版本不得静默解释。

退出条件：刷新、SSE 重连、Worker 重启后能重建完整脱敏调用历史，并保持单一终态。

### Step 8：Web Agent Timeline

- 将现有 Skill invocation card 扩展为 Agent Run Timeline，实时展示 iteration、Tool 状态、耗时、重试、审批和 stop reason。
- Tool 卡片默认折叠；展开仅显示经过脱敏的参数/结果摘要；读、检索、写、命令使用不同图标和状态。
- 写 Tool 在 timeline 中显示等待批准，批准后继续原 Run；拒绝后显示无副作用结果。
- 显示 requested/effective effort、模型、实际 token、耗时和最终停止原因，不显示剩余 token或 Tool 次数上限。
- final answer 始终渲染为时间线最后一个 Assistant frame；取消、失败、重连和移动端布局加入 Web 测试。

退出条件：桌面/移动 viewport 无重叠；键盘、ARIA combobox、审批、刷新恢复和 SSE 重连可用。

### Step 9：质量、安全与渐进发布

- 指标：目标覆盖率、子问题覆盖率、证据覆盖率、citation completeness、unsupported claim rate、正确拒答率、Tool 选择/重复调用率、终止原因、延迟、token 和恢复成功率。
- 测试：Domain 状态机、Tool 安全、Loop 恢复、API/SSE 顺序与脱敏、Web timeline、合成 Agent Eval，以及隔离 PostgreSQL/Redis integration。
- 运行 `ruff format --check .`、`ruff check .`、`mypy apps packages`、迁移 upgrade/downgrade、OpenAPI diff；涉及 Web 时运行 `pnpm lint`、`pnpm typecheck`、Vitest、build 和可用的浏览器 E2E。
- 以 fake/local Provider 开始，使用 feature flag 灰度；保留 v1/v2 回滚路径，不修改已有 Skill 版本和历史 Run 身份。
- 更新 README、architecture、troubleshooting、Skill README 和阶段 tracker；所有结果标记 provisional，直到独立正式质量线关闭。

## 5. 依赖与交付顺序

```text
契约/ADR/评测基线
  -> Loop 状态机 + finalizer
  -> 上下文快照 + checkpoint
  -> 只读文件 Tool
  -> /effort + Gateway 映射
  -> 知识检索 Tool 化
  -> 写文件/shell 审批
  -> SSE v3 + Web Timeline
  -> 安全、评测、故障注入与灰度发布
```

不建议先开放 `shell_exec` 或外部模型；它们依赖 Loop 恢复、审批、审计和隐私策略先稳定。

## 6. 主要风险与退出判定

| 风险 | 缓解 | 退出判定 |
| --- | --- | --- |
| 循环失控或重复调用 | 超时、无进展检测、参数去重、emergency ceiling | 循环可恢复终止，且无隐藏无限重试 |
| 最终回答过早 | 结构化 completion check、独立 verify、finalize-only gate | 未完成目标不能发布 final message |
| Tool 越权或路径穿越 | 服务端 allowlist、canonical path、Space/approval 校验 | 安全负例全部无副作用 |
| Provider effort 不兼容 | capability registry、版本化映射、显式请求 fail-closed | requested/effective 值可追溯 |
| 私有内容外发 | sensitivity 传播、部署策略、用户同意、脱敏日志 | private/restricted 默认留在本地 |
| 质量片面或幻觉 | 子问题覆盖、证据检查、冲突识别、正确拒答 | unsupported claim rate 和 citation completeness 达到新 provisional floor |

本计划完成后仍不能宣称正式检索、回答或 Skill 质量通过；正式质量线必须按 ADR-010/ADR-011 使用新版本 dataset/config 独立执行 development、冻结和 holdout。
