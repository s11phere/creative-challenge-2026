# 故障排查与已知限制

本文档适用于当前本地工程底座。排查时只记录状态码、机器码、trace ID 和任务 ID，不要把
密钥、连接串、文档正文、完整 prompt 或 Provider 响应粘贴到日志和 Issue。

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
模型选择。当前允许的 Agent Tool 是只读 `inspect_retrieval 1.0.0` 和 `grounded_qa 1.0.0`，写 Tool
不可通过 prompt 开启。

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
`FAST_CHAT_REASONING_ENABLED=false` 会为 OpenAI-compatible Chat 显式发送
`thinking.type=disabled`；只有确实需要推理模型且已单独配置足够的推理与回答预算时才应开启。

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
  新建 QA HTTP/Web Run 首次由 `knowledge_agent 0.3.0` 初始化，随后以 PostgreSQL active pointer 为准，
  每个 Run 都固定包摘要，Worker 校验后才调用唯一 QA Application Port。可用 `GET /api/v1/skills` 和
  `GET /api/v1/skills/knowledge_agent/versions` 检查安装摘要、active 版本和 manifest 预算。
- `knowledge_agent 0.3.0` 通过 `fast_chat` 执行受约束 LLM 决策，可在同一持久 QA Run 中调用
  `inspect_retrieval` 后调用一次 `grounded_qa`；旧 Agent 与 `knowledge_qa` 包仅保留用于固定 Run 恢复。外层模型只看到
  Tool 状态/计数，不看到回答或引用原文；Runtime checkpoint 快照与 append-only checkpoint 已持久化，
  当前恢复和最终结果仍以 QA PostgreSQL 状态为准。
- 若 Run 以 `QA_SKILL_INVALID` 失败，检查 API 与 Worker 的 `SKILL_ROOT_PATH`、
  `KNOWLEDGE_AGENT_SKILL_VERSION` 和镜像内 `skills/knowledge_agent_v3` 内容是否一致。不要就地修改已被 Run
  引用的同名版本；发布新 semver 并保留旧包供排队/恢复 Run 校验。
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

## Assistant Web release rollback

The normal Web entry is API v2. During the compatibility window, the header's `兼容问答` selector
uses the existing `/api/v1` conversation and QA Run endpoints. If a v2 regression is observed, set
`VITE_ASSISTANT_DEFAULT_API_MODE=v1` in the ignored `.env` and rebuild/recreate only `web`:

```powershell
docker compose -f deploy/compose.yaml --env-file .env build web
docker compose -f deploy/compose.yaml --env-file .env up --detach --no-deps web
```

This rollback preserves v2 records, historical Runs, active Skill pointers, and installed packages.
It does not bypass external-provider policy. Keep `MODEL_ALLOW_EXTERNAL`, source policy, deployment
policy, and user consent unchanged. If the compatibility deadline has passed, v1 is intentionally
fail-closed in the Web and requires a separately reviewed release decision; do not delete data to
force a rollback. For rollout diagnosis, use the Step 7 aggregate counters and inspect only safe
labels for routing misfires, clarification loops, cancellation, recovery, token usage, and latency.

## `start-local.ps1` Count error

If PowerShell reports that the `Count` property is missing, use the current
`scripts/start-local.ps1`. The script normalizes Skill files and managed Compose projects to arrays
before checking `.Count`, so both a single result and an empty result are supported. This check runs
before Docker startup and does not remove volumes or application data.
