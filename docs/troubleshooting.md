# 故障排查与已知限制

本文档适用于当前本地工程底座。排查时只记录状态码、机器码、trace ID 和任务 ID，不要把
密钥、连接串、文档正文、完整 prompt 或 Provider 响应粘贴到日志和 Issue。

## Current Assistant Contract (2026-08-11)

新 Run 只使用 `knowledge_agent 1.0.0` 和当前五个受信 Skill。不要设置
`AGENT_LOOP_V5_ENABLED`、`VITE_ASSISTANT_*`，也不要使用 `start-local.ps1 -LegacyKnowledgeAgent`：
这些回退兼容入口均已删除。Web 只提供 Assistant 主路径；`GET /api/v1/skills` 也只返回固定的只读清单。

## 快速诊断

### Current Retrieval Boundary (2026-08-04)

The Search API and QA workflow now default to `dense_rerank`: dense-exact candidates go directly to
the configured reranker. `hybrid_rerank` is retained only for explicit compatibility requests. PR #4
corrected the GPU development reproduction, but the result remains provisional and Stage 3 stays
terminated under ADR-010. Do not enable or run the existing formal holdout.

```powershell
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 100 api worker web migrate postgres redis
curl.exe --fail http://127.0.0.1:8000/api/v1/health/live
curl.exe http://127.0.0.1:8000/api/v1/health/ready
```

`live` 只判断 API 进程能否响应；`ready` 会并发检查 PostgreSQL 和 Redis。模型状态单独报告，
默认不是本地 API readiness 的硬依赖。

## Assistant 路由开发报告被拒绝

先只校验固定的 synthetic development 数据集：

```powershell
uv run --frozen python scripts/evaluate_assistant_routing.py --validate-only
```

`routing dataset cannot enable formal evaluation`、`routing dataset must remain synthetic only` 或
SHA-256 mismatch 表示 manifest、schema 或 case 文件不符合安全协议。不要修改
`formal_runs_enabled`、改用受控语料或把当前 development 报告当作正式质量结果；修复固定文件的
摘要或从干净工作树恢复预期版本。无 `--predictions` 时退出码 `4` 是刻意的阻断，而不是模型错误。
预测文件只可包含动作、允许的 Skill 名称、计数、用量、延迟和终止原因，不能包含消息、prompt、文档
正文、Provider 响应或内部资源 ID。

## Compose 提示缺少变量

现象：配置阶段提示 `APP_SECRET_KEY must be set` 或 `POSTGRES_PASSWORD must be set`。

处理：

```powershell
Copy-Item .env.example .env
```

编辑 `.env` 并替换两个占位值。不要把 `.env` 提交到 Git，也不要在聊天、日志或截图中暴露值。

## 端口被占用

现象：容器启动失败并提示 5173、8000、5432 或 6379 已被使用。

处理方式二选一：停止占用端口的本项目旧进程，或在 `.env` 中修改对应的 `WEB_PORT`、
`API_PORT`、`POSTGRES_PORT`、`REDIS_PORT`。修改 Web/API 端口后，smoke test 地址也要同步调整。

## 迁移失败

```powershell
docker compose -f deploy/compose.yaml logs migrate postgres
docker compose -f deploy/compose.yaml run --rm migrate
```

先确认 PostgreSQL healthy、账号与数据库名一致，再重新运行一次性迁移。不要在包含数据的环境
直接执行 `downgrade`。只有确认测试数据可以永久删除时，才使用 `down --volumes` 重建空库。

## `.env` 配置陷阱

以下三条来自 2026-08-04 本地重建时实际遇到的问题，排查 `.env` 相关故障时优先核对。

### 1. `POSTGRES_DB` 必须与实际数据所在的库一致

`POSTGRES_DB` 指错库（例如指向一个只有 `alembic_version` 的空库）时，表现是"看似莫名"的
故障：

- 迁移失败：`relation "documents" does not exist`，`ALTER TABLE` 找不到前置表；
- dense 检索 0 命中：查询打到空库，实际语料在另一个库。

处置：先用
`docker exec <postgres-container> psql -U <user> -d <库名> -c '\dt'`
确认业务表实际存在哪个库，再让 `.env` 的 `POSTGRES_DB` 与之对齐。注意 `.env` 当前指向的库
可能与运行中旧容器（创建时写入的配置）不一致，重建容器前先核对当前 `.env` 值。

### 2. `MODEL_PROVIDER=fake` 会让 embedding/reranker 身份一起 fake 化

`MODEL_PROVIDER=fake` 不只影响回答：它会把查询端 embedding identity 的 model_revision
强制改为 `fake-sha256-v1`（见 `packages/infrastructure/src/infrastructure/config.py`），
导致查询 embedding_version 与真实模型摄入的语料版本不一致，dense 检索按版本过滤后
0 命中（`reranker_version` 也会显示为 empty）。

所以 `MODEL_PROVIDER=fake` 只适合"空库 + 纯流程验证"；语料一旦用真实 embedding 摄入，
切回 fake 会让 dense_rerank 检索失效。要跑真实检索，`MODEL_PROVIDER` 必须是真实
（如 `openai-compatible`）且 `EMBEDDING_PROVIDER=text-embeddings-inference`。

### 3. 外部 Chat 的数据边界

`MODEL_ALLOW_EXTERNAL=true` 开启后，QA 会把问题以及检索到的文档片段发送给外部 Provider
（例如 DeepSeek）。阶段 0 语料为 `internal_team_only`，含版权资料，接入外部 API 前必须确认
来源授权和团队策略，禁止把未授权内容公开演示或外发。密钥只写在被 Git 忽略的 `.env`，不得
进入仓库、日志或截图。

## Readiness 返回 503

| 机器码 | 含义 | 检查方向 |
| --- | --- | --- |
| `POSTGRESQL_UNREACHABLE` | API 无法在时限内查询数据库 | PostgreSQL health、迁移日志、数据库环境变量 |
| `REDIS_UNREACHABLE` | API 无法在时限内 ping Redis | Redis health、端口和 `REDIS_HOST` |
| `MODEL_DISABLED` | 模型能力被显式禁用 | 本地管理功能仍可 ready；按需检查模型配置 |
| `MODEL_CONFIGURATION_MISSING` | Provider 配置不完整 | endpoint、能力别名和模型部署名 |
| `MODEL_POLICY_DENIED` | 外发策略拒绝公网 endpoint | 保持本地 endpoint，或明确评审后允许外发 |

健康响应不会返回主机、密码或底层异常。进一步定位使用响应头中的 `X-Trace-ID` 和
`X-Request-ID` 关联结构化日志。

## knowledge_agent 模型决策失败

### 先区分 Chat、Embedding 和 Reranker

`MODEL_PROVIDER=text-embeddings-inference` 只提供 `embedding_zh`（以及配置好的
reranker），不会提供 `fast_chat`。如果 Web/API 的 `/health/ready` 正常，但
`knowledge_agent` 直接失败，请先检查是否把 TEI 误配置成了唯一的 `MODEL_PROVIDER`，或者
是否忘记启动 `embedding` profile。使用真实 Chat 时，API 和 Worker 都需要同时加载以下
配置，并在修改后重新创建两个容器：

```dotenv
MODEL_PROVIDER=openai-compatible
MODEL_ALLOW_EXTERNAL=true
FAST_CHAT_ENDPOINT=https://api.example.com/v1
FAST_CHAT_API_KEY=<ignored-local-secret>
FAST_CHAT_MODEL=<chat-model>
EMBEDDING_PROVIDER=text-embeddings-inference
EMBEDDING_ENDPOINT=http://tei:80
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RERANKER_PROVIDER=inherit
RERANKER_ENDPOINT=http://tei-reranker:80
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

`RERANKER_PROVIDER=fake` 只适用于可选 GPU `reranker` profile 没有运行的本地体验；完整 GPU
路径使用上面的 `inherit` 配置并同时启动 `--profile reranker`。外部 Chat 开启后，问题和检索到
的文档片段可能发送到该 Provider，私有或 restricted 来源必须先通过数据策略检查。

启动/重建和检查命令：

```powershell
docker compose -f deploy/compose.yaml --env-file .env `
  --profile embedding --profile reranker up --build --detach --wait
docker compose -f deploy/compose.yaml --env-file .env `
  --profile embedding --profile reranker up --detach --force-recreate api worker
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready | ConvertTo-Json -Depth 8
```

健康响应中 `fast_chat`、`embedding_zh` 必须为 `MODEL_CAPABILITY_CONFIGURED`；没有 GPU
reranker 时，`reranker_multilingual` 应显示 `MODEL_FAKE_READY`。如果只启动基础 Compose
服务而没有 `--profile embedding`，`http://tei:80` 不存在，上传会在向量化阶段重试后失败；
这不会被 API 的 PostgreSQL/Redis readiness 单独检查发现。

默认 `MODEL_PROVIDER=fake` 不需要凭据。接入 OpenAI-compatible Chat Provider 时，只在被 Git 忽略的
`.env` 中配置 `MODEL_PROVIDER=openai-compatible`、`MODEL_ALLOW_EXTERNAL=true`、
`FAST_CHAT_ENDPOINT`、`FAST_CHAT_MODEL` 和 `FAST_CHAT_API_KEY`；API 与 Worker 必须使用相同配置。
若只接入 Chat，额外设置 `EMBEDDING_PROVIDER=fake` 和 `RERANKER_PROVIDER=fake`，避免把已有
fake/local 索引误判为缺少外部 Embedding revision。
不要把密钥、问题、模型原始响应、Tool 输出或引用原文写入日志或 Issue。

`RUN_LLM_DECISION_INVALID` 表示 Provider 没有返回严格的单个 JSON 决策；检查模型是否遵循
`call_tool/complete/refuse` schema。`RUN_LLM_MAX_ITERATIONS` 表示模型在五轮内未终止；
`TOOL_NOT_ALLOWED`、`TOOL_MODEL_OUTPUT_DENIED` 或 `TOOL_APPROVAL_REQUIRED` 表示服务端安全边界拒绝
模型选择。默认 `knowledge_agent 1.0.0` 只允许受信的知识 Tool：`knowledge_search`、
`knowledge_inspect`、`summarize_document`、`grounded_answer`、`verify_answer` 和 `finalize_answer`；文档 Tool 只有在资源
解析器注册时可用，写 Tool 不可通过 prompt
开启。旧版本和回退路径已删除；应直接修正当前 `1.0.0` Skill 的配置或实现。

### 开发环境记录完整 QA 运行轨迹

普通 Worker 日志会为模型失败记录 `error_code`、`error_type`、`capability` 和 `retryable`，但不会
记录问题、prompt、文档正文、模型响应或 Tool 内容。需要定位 `QA_MODEL_FAILED` 或优化 Agent 时，
可在被 Git 忽略的本地环境文件中显式启用：

```text
QA_DEBUG_TRACE_ENABLED=true
QA_DEBUG_TRACE_MAX_BYTES=10000000
```

Compose 将每个 Run 写入宿主机 `tmp/qa-debug/<run_id>.jsonl`，事件包括 `llm_request`、
`llm_response`、`llm_error`、`tool_call`、`tool_result`、`runtime_result` 和 `run_result`。单文件达到
上限后轮转为 `.jsonl.1`。该文件包含完整问题、证据上下文、模型结果和 Tool payload，只能用于本地
开发排查，不得提交、上传或粘贴到 Issue；使用完毕后关闭开关并删除相应文件。`APP_ENV=production`
时该能力强制禁用，即使误设开关也不会写入内容。

若轨迹显示 Chat 请求已收到 HTTP 200，随后以 `MODEL_TIMEOUT` 或 `QA_TIMED_OUT` 结束，通常是
Provider 在非流式响应体生成阶段超过 read timeout，而不是连接失败。Chat 使用独立的
`FAST_CHAT_TIMEOUT_SECONDS`（开发默认 120 秒）；Embedding/Reranker 继续使用
`MODEL_TIMEOUT_SECONDS`。为避免重复计费和重复生成，Chat 在收到响应头后的 read timeout 不会整单
重发，连接错误和限流仍遵循有限重试。最终 Grounded QA 生成预算为 150 秒，Worker 外层任务预算为
300 秒；三者应保持 `FAST_CHAT_TIMEOUT_SECONDS < generation timeout < QA_TASK_TIMEOUT_MS`。

若 Provider 返回 HTTP 200，但 `message.content` 为空、`reasoning_content` 占满 completion token
且 `finish_reason=length`，则 `QA_MODEL_FAILED` 的直接原因是隐藏推理耗尽了结构化回答预算。默认
`FAST_CHAT_REASONING_ENABLED=false` 会为没有会话 reasoning profile 的兼容 Chat 调用显式发送
`thinking.type=disabled`。正常 Conversation Run 会先按会话默认和 Provider 能力表解析 profile；只有确实需要
profile-less 推理模型且已单独配置足够的推理与回答预算时才应开启。

`/effort` 仅修改当前 Conversation 后续 Run 的默认偏好，不会重写已接受 Run。`deepseek-v4-flash`
使用 `reasoning-mapping-v2` 和原生 `reasoning_effort`，Run 应显示 `mode=native`；`xhigh` 的
`effective_effort` 为 `high` 是 DeepSeek 的公开映射。其他 OpenAI-compatible Chat 仍只能将
`none` 映射为 disabled、其他强度映射为 enabled 并记录 `coarse`。对不支持 reasoning 的 Provider，
显式强度会以 `MODEL_REASONING_UNSUPPORTED` 拒绝，只有 `auto` 可保存为 disabled 的降级 profile。
`reasoning_content` 不会被应用持久化或显示，排查时应只检查 Run profile 和安全的 usage 元数据。

`QA_STRUCTURED_RESPONSE_INVALID` 表示模型响应不是可验证的 `grounded-answer-v1`。回答正文由服务端
根据已校验、带 Evidence ID 的 `claims` 规范化生成；模型返回的冗余 `answer` 字段不会再因排版或
连接文本差异导致整个 Run 失败。JSON schema、Claim ID 唯一性、Evidence ID 白名单和 Citation 完整性
仍严格校验。

若模型已经生成多条带引用 claim，最终却显示证据不足，应检查引用是否仅指向 `context_only`
上下文扩展块。`context_only` 可共同支撑 claim，但按 ADR-007 不能成为唯一证据。系统会剔除这类
候选 claim，并发布其余至少含一个直接 `matched` 证据的 claims；只有没有可发布 claim 时才拒答。

## Web 显示 API 连接失败

先直连 `http://127.0.0.1:8000/api/v1/health/live`。直连成功但 Web 同源 `/api` 失败时，检查
`web` 容器和 `deploy/nginx.conf`；两者都失败时检查 `api` 日志。页面请求有 8 秒上限，修复后
可使用页面上的刷新按钮重试。

## Worker 不消费任务

```powershell
docker compose -f deploy/compose.yaml ps worker redis
docker compose -f deploy/compose.yaml logs --tail 100 worker redis
docker compose -f deploy/compose.yaml exec -T redis redis-cli ping
```

阶段 1 诊断任务只接受 ID、版本、计数和时间，不接受正文。超过有限重试的消息进入 Dramatiq
死信队列。Step 7 容器验收确认消息可被消费、确认和移入死信队列，但未在 `docker logs` 中稳定
复现 actor 的 `diagnostic_task_started/completed` 事件；排查时同时检查队列状态，不要仅凭缺少
这两条日志判断任务未执行。该日志差异是进入阶段 2 前需要关闭的已知问题。

## OTel Collector 不可用

Collector 默认不启动，且不是 API/Worker 的启动依赖。需要本地 trace 输出时：

```powershell
docker compose -f deploy/compose.yaml --profile otel up --detach
```

Collector 不可达时 exporter 会有界失败，API/Worker 应继续运行。检查 `OTLP_ENDPOINT` 是否为
容器网络可达地址；Compose 内通常使用 `http://otel-collector:4318`。

## Harness v2 SSE 或 prompt cache 不生效

`agent-run-sse-v4` 只由启用 native Tool-use 的 v2 executor 写入；旧 Run 仍读取
`agent-run-sse-v3`，不要在 Web timeline 中把两种 schema 混为同一历史。事件 payload 只包含安全
字符串和计数，禁止 prompt、回答、文档正文、Tool body 和密钥；如果看到 v4 字段缺失，先检查
`FAST_CHAT_NATIVE_TOOL_USE` 是否被部署显式设为 `false`，或 Provider capability 是否未声明
native Tool-use；默认配置启用 v2，能力不可用时会回退到保留的 v1 路径。

Prompt cache 仅在 `FAST_CHAT_PROMPT_CACHING=true`、ModelGateway capability 声明支持且部署策略
允许时发送 `extra_body.cache_key`。cache key 只由静态 prompt/schema/Skill/provider 摘要组成，
不包含用户消息、Space、Tool 观察或私有内容；隐私策略或 Provider 不支持时仍会正常执行并显示
`cache_mode=unsupported`。不要把 cache key 当作安全边界。

如果 `agent_harness_trace` 的最后事件是 `llm_error=MODEL_INVALID_RESPONSE`，且 Provider 原始响应
中 `reasoning_content` 充满重复的“如何保存文件”推理并出现 `finish_reason=length`，检查请求是否要求
“知识问答并保存为 md 文件”。v2 现在会让 `knowledge_answer` 返回非终态观察，agent 随后自行决定
`fs_list`、`fs_write` 顺序，并使用 `{{current_grounded_qa_answer}}` 由服务端解析和 finalize。

## Windows 下 pnpm 脚本被阻止

不要修改系统执行策略。使用仓库固定命令：

```powershell
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
```

## Docker Hub 网络不可达

API、Worker 和 Web 的 Dockerfile 使用 AWS 公共只读缓存中的 Docker Official Images，并以
与 Docker Hub 官方 API 一致的 digest 固定内容。PostgreSQL、Redis 和 OTel 镜像仍需要本机
已有缓存或可访问的 registry。不要为了绕过网络问题移除 digest。

## 安全清理

`docker compose down` 保留命名卷；`down --volumes` 永久删除 PostgreSQL 和 Redis 数据。
执行后者前先用 `docker compose ls` 和 `docker volume ls` 确认项目名，避免清理其他项目。

## 当前功能限制

- 已有阶段 2 的 6 张核心业务表、摄入流水线和 Worker 消费者；当前支持上传文件，尚无目录监听。
- 阶段 3 的 Keyword/Dense/Dense+Reranker/Hybrid/Hybrid+Reranker 检索 API 已可用；默认模式为
  `dense_rerank`，即纯 dense 候选直接精排。provisional 知识问答 Web/API 可以创建持久会话、
  提交问题，由独立 Worker 调用真实 PostgreSQL SearchService 产出回答或拒答；终态响应和 Web
  证据区展示经过当前 Space/版本/Chunk 再校验的 Citation 身份。
- provisional QA 的会话、Message、Run/Attempt、Evidence、Citation、Feedback 与 SSE 事件均保存到
  PostgreSQL。API 启动时会重排队安全的非终态 attempt，保留终态并清理中断时尚未发布的 Evidence；
  断开 SSE 不会取消 Run，只有显式取消请求才会记录取消意图。Worker 使用 attempt lease/heartbeat，
  启动时接管 queued 或租约过期运行，重复投递不会重复发布终态。Citation 可按需解析固定版本的
  最小原文片段；用户重试、反馈提交及 `pending_review -> accepted/rejected` 的持久审核 API 已实现，
  但 Playwright 浏览器级完整旅程仍未执行。
- 默认 `FakeModelGateway` 使用确定性抽取式回答，返回相关证据片段而不是高质量综合回答；这是当前
  流程验证基线。Stage 3 达标并冻结检索配置后再调整召回、重排和回答表现，不得把当前结果用于 holdout。
- 阶段 3 评测配置仍为 provisional：阶段 0 和阶段 2 已正式关闭，但当前评测集代表性不足，
  因此阶段 3 已按 ADR-010 终止。PR #4 修正后的 GPU development 表明 `dense_rerank` 在 v0/v1
  上的 Claim Recall@10 为 82.37%/78.75%，高于 `hybrid_rerank`；这不构成正式质量结论，也不允许
  运行当前 holdout。不要手工打开 `formal_runs_enabled`；重新开启必须使用新的 dataset/config version。
- P0 检索评测只纳入 Markdown/TXT/PDF 证据来源；Code/Notebook 属于 P1。validation 会同时记录
  原始与纳入 case 数，无证据安全 case 不得因格式过滤而跳过。
- Qwen3 Embedding 与 BGE Reranker 同时运行时，
  两者同时常驻约需 11 GiB 以上内存。内存不足时先停止 Qwen3 再运行离线 Reranker，或反向串行；
  不要降低模型 revision、混用旧向量或用 fake 结果替代。扩大 Qwen3 batch/并发在当前 CPU 上不会
加速，Compose 已保留实测较快的限制。
- 需要检索时先确认 Space 存在、Document 有当前 published version，且查询模式所需的 Embedding/Reranker 能力已配置；无命中是成功的空列表，不是系统故障。
- 模型服务不可用不会阻断 PostgreSQL/Redis 管理面 ready；Dense 会返回明确 Provider 错误，Hybrid 只有 profile 明确允许时才可降级为 Keyword。
- 已有 Agent Runtime、Tool/Skill Registry、声明式执行器、内存与 PostgreSQL 检查点恢复和 Skill 模板；
  新建 QA HTTP/Web Run 默认由 `knowledge_agent 1.0.0` 初始化，每个 Run 都固定包摘要，Worker 校验后才调用唯一 QA Application Port。
  可用 `GET /api/v1/skills` 检查当前安装版本和 manifest 预算。
  当前 `assistant_agent 1.0.0` 的复合任务预算已提高到 256 steps、256 Tool calls、262144 input
  tokens、131072 output tokens 和 7200 秒；`knowledge_agent 1.0.0` 仍按固定 32/24/600 秒用于
  QA Runtime。若仍触发 `RUN_BUDGET_EXCEEDED`，
  Runtime error message 会列出具体超限项及实际值；优先检查 `input_tokens` 是否因长对话上下文累积。
- `knowledge_agent 1.0.0` 通过 `fast_chat` 执行当前默认的受约束 LLM 决策，可在同一顶层 Loop 中串行调用
  注册 Tool；可调用
  `knowledge_search`、`knowledge_inspect`、`summarize_document`、`grounded_answer`、`verify_answer` 和
  `finalize_answer`；
  外层模型只看到 Tool 状态/计数，不看到
  回答或引用原文；Runtime checkpoint 快照与 append-only checkpoint 已持久化，
  当前恢复和最终结果仍以 QA PostgreSQL 状态为准。首个重复 Tool 请求会回传 `already_observed`
  观察而不重复执行；第二次相同重复才会以 `RUN_LLM_NO_PROGRESS` 终止。
- 若 Run 以 `QA_SKILL_INVALID` 失败，检查 API 与 Worker 的 `SKILL_ROOT_PATH`、
  `KNOWLEDGE_AGENT_SKILL_VERSION=1.0.0` 和镜像内 `skills/knowledge_agent` 内容是否一致；不要就地修改
  已被 Run 引用的同名版本。
- Registry active pointer 已持久化到 `skill_activations`；激活或回滚出现
  `SKILL_ACTIVATION_CONFLICT` 时，应刷新 Catalog 的 `active_revision` 后重试，不能绕过 CAS。
  QA 的 PostgreSQL Run/Attempt/Event 是当前执行恢复事实源；通用 Runtime Checkpoint、持久生命周期
  事件、审批查询/撤销和旧版本引用清理均已提供。正式跨进程故障注入仍需独立环境验收。
- 知识整理入口会把选中的 Source/Document/DocumentVersion 固定到 QA Run。若排队期间来源撤下、
  文档发布新版本或 selector 不再匹配，运行会以稳定范围错误失败，不会自动跟随新版本；重新确认
  当前版本后创建新 Run。`compare_sources` 缺少两个来源的 Citation 时会拒答。
- `create_review_cards` 默认先生成预览。`write.code=SKILL_WRITE_REQUIRES_APPROVAL` 且
  `side_effects=0` 表示尚未获得持久化审批；审批后可通过
  `/api/v1/runs/{run_id}/derived-knowledge` 查询写入状态，撤销使用对应 DELETE 端点。
- Web 展示真实健康状态、真实数据来源/摄入任务、五个 Skill 入口和 provisional QA 状态；QA 证据面板只对
  服务端已发布的 Citation 按需请求原文，不接受客户端提供的 locator 或版本。若返回 `invalid`，
  先检查 Blob hash、parser 版本和 locator 是否仍与固定 DocumentVersion 一致，不要回退到相似文本。
- 阶段 0 语料已按 `docs/stage-0-acceptance.md` 冻结为 `internal_team_only`；真实语料只可在
  manifest 允许列表内用于本地/组内评测，禁止 Git 分发、公开演示和未经策略允许的外部 Provider
  外发。阶段 2 Step 9 已关闭，阶段 3 正式质量门禁未通过且已终止；阶段 4/5 工程功能已完成，
  正式质量门禁仍保持 provisional。
# Feedback review and candidate export

Feedback submission creates a `pending_review` record. Reviewers use the Space-scoped API under
`/api/v1/spaces/{space_id}/feedback`; responses intentionally contain metadata only. Accepted reviews
must include authorization/redaction confirmation, expected behavior, and the required gold digest.
Rejected reviews must include a reason. Replaying the same review is idempotent; a conflicting second
review is rejected.

To export accepted candidates, provide an isolated database and an explicit Space ID:

```powershell
.venv\Scripts\python.exe scripts/export_feedback_candidates.py `
  --space-id <space-uuid> `
  --output tmp/feedback-candidates-development.jsonl
```

The command refuses existing files and frozen/holdout paths. It writes metadata-only JSONL and skips
missing runs or incomplete review records. It does not run a formal evaluation.

The older provisional notes above describe the pre-completion baseline; the current Runtime
checkpoint, approval, derived-knowledge, Skill cleanup, and feedback review implementations are
covered by the Stage 4/5 completion tracker and their regression tests.

## Assistant Web deployment

The Web has a single Assistant entry and no v1 rollback mode. For rollout diagnosis, use the safe
aggregate counters for routing misfires, clarification loops, cancellation, recovery, token usage,
and latency. Keep `MODEL_ALLOW_EXTERNAL`, source policy, deployment policy, and user consent
unchanged when rebuilding the current Web image.

## Native Tool-use multiple Tool calls

If a native Tool-use Provider returns more than one Tool call in one response,
the Runtime executes none of them. It records one bounded protocol retry and
asks the model to choose one Tool in the next turn. A repeated violation fails
with `RUN_NATIVE_TOOL_USE_MULTIPLE_CALLS`. The retry does not bypass approval,
execute a discarded call, or impose a Tool order or artifact path.

## Workspace Tool unavailable

The Assistant registers local filesystem and command Tools when a conversation has selected a
workspace and either `MODEL_PROVIDER=fake` or
`AGENT_WORKSPACE_MODEL_VISIBILITY_CONSENT=true`. With a non-fake Provider and consent disabled,
`workspace.tools_enabled=false` and `workspace.status=model_visibility_consent_required` are
expected; do not bypass this restriction by placing local files in a prompt or enabling the Tools
manually. `scripts/start-local.ps1 -AllowExternalWorkspaceTools` is the reviewed one-process opt-in
and warns about the external data boundary.

When a request includes a workspace artifact but the model nevertheless selects an unavailable
workspace Tool, the Assistant completes with an explicit server-configuration explanation. A user
reply such as `confirm` cannot change that policy, and the Run should not surface
`RUN_LLM_DECISION_INVALID` for this bounded recovery case.

For `WORKSPACE_PATH_DENIED`, confirm that the folder already exists below
`AGENT_WORKSPACE_ROOT_PATH` and that no selected path component is a symlink or Windows junction.
Use paths relative to the selected workspace in Tool calls: `.` for the root and `src/main.py` for a
child. The selected workspace name itself is descriptive context, not a child cwd: for example,
use `cwd="."`, not `cwd="project-a"`, to run at the root of workspace `project-a`. Absolute paths,
backslashes, drive prefixes, and `..` are denied.

Uploaded documents and the selected local workspace are separate scopes. If a request asks to
retrieve or summarize uploaded knowledge then save the result, do not infer missing knowledge from
`fs_list`. The Agent should search the current Space when the subject may be Space-specific, then
choose any useful workspace inspection and a descriptive, non-conflicting Markdown path. There is
no fixed `knowledge-answer.md` fallback; `fs_write` still requires its normal approval.

Under Compose, API and Worker must both mount the same `AGENT_WORKSPACE_HOST_PATH` at
`/data/agent-workspaces`. Recreate both services after changing either mount or workspace settings.
If a saved logical path no longer resolves in Worker, the Run fails closed without workspace Tools;
restore the directory or select a new workspace after active Runs have completed or been cancelled.

`waiting_approval` is normal for `fs_write` and `shell_exec`. Query
`GET /api/v2/runs/{run_id}/approvals`, then make exactly one decision on the matching approval ID.
An `APPROVAL_NOT_FOUND` response means the ID does not belong to that Run; an
`APPROVAL_CONFLICT` response means it is no longer pending. Approval resumes the same checkpoint;
rejection cancels it without running the requested operation.

In the Web Agent timeline, open the pending Tool card and choose `approve`, `reject`, or
`always allow this Tool type`. The last action approves the displayed invocation and remembers only
its Tool name for the current Conversation. It does not permit arbitrary paths or executables, and
a new Conversation starts without that allowance. The card also shows filesystem paths, shell
command and cwd, plus a bounded expandable output preview for commands and directory listings. A
truncated preview indicates only that the display limit was reached.

Runs created by the short-lived native Tool-use approval wiring defect may have
`waiting_approval` without an approval record. Worker startup recovery recognizes only a verified
native v2 checkpoint with a pending Tool call and a missing approval ID, requeues that Run, and lets
the current executor create the ordinary approval request. It never executes the pending write or
command automatically. Normal pending approvals, non-native checkpoints, cancelled Runs, and
malformed checkpoints remain untouched.

For a recovered workspace write that uses the Grounded QA answer marker, the Worker first reloads
the current Run's persisted QA result and repeats the normal claim/citation verification before it
can create the approval card. It still does not write the file until that approval is accepted. A
missing, failed, or non-publishable QA result leaves the Run failed rather than substituting model
text or re-running the write.

## `start-local.ps1` Count error

If PowerShell reports that the `Count` property is missing, use the current
`scripts/start-local.ps1`. The script normalizes Skill files and managed Compose projects to arrays
before checking `.Count`, so both a single result and an empty result are supported. This check runs
before Docker startup and does not remove volumes or application data.

## `start-local.ps1` reports that a local port is already allocated

The startup script discovers all prior `creative-challenge-local-*` Compose projects that use
`deploy/compose.yaml`, stops them with `down --remove-orphans`, and preserves their named volumes
before starting the current trusted-Skill-fingerprint project. If an older script leaves a local
stack behind, update the script and run it again; do not use `down --volumes` to resolve the
conflict.
# 当前版本提示（2026-08-11）

当前只运行 Assistant 主路径和固定 Skill `1.0.0`。不要设置已删除的 `AGENT_LOOP_V5_ENABLED`、
`VITE_ASSISTANT_*` 或 `LegacyKnowledgeAgent` 参数，也不要调用 Skill 激活、回滚、清理接口。
出现旧版本身份的持久化 Run 需要重新提交，不再尝试旧版本恢复。
