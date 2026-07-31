# 故障排查与已知限制

本文档适用于当前本地工程底座。排查时只记录状态码、机器码、trace ID 和任务 ID，不要把
密钥、连接串、文档正文、完整 prompt 或 Provider 响应粘贴到日志和 Issue。

## 快速诊断

```powershell
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs --tail 100 api worker web migrate postgres redis
curl.exe --fail http://127.0.0.1:8000/api/v1/health/live
curl.exe http://127.0.0.1:8000/api/v1/health/ready
```

`live` 只判断 API 进程能否响应；`ready` 会并发检查 PostgreSQL 和 Redis。模型状态单独报告，
默认不是本地 API readiness 的硬依赖。

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

默认 `MODEL_PROVIDER=fake` 不需要凭据。接入 OpenAI-compatible Chat Provider 时，只在被 Git 忽略的
`.env` 中配置 `MODEL_PROVIDER=openai-compatible`、`MODEL_ALLOW_EXTERNAL=true`、
`FAST_CHAT_ENDPOINT`、`FAST_CHAT_MODEL` 和 `FAST_CHAT_API_KEY`；API 与 Worker 必须使用相同配置。
不要把密钥、问题、模型原始响应、Tool 输出或引用原文写入日志或 Issue。

`RUN_LLM_DECISION_INVALID` 表示 Provider 没有返回严格的单个 JSON 决策；检查模型是否遵循
`call_tool/complete/refuse` schema。`RUN_LLM_MAX_ITERATIONS` 表示模型在两轮内未终止；
`TOOL_NOT_ALLOWED`、`TOOL_MODEL_OUTPUT_DENIED` 或 `TOOL_APPROVAL_REQUIRED` 表示服务端安全边界拒绝
模型选择。当前唯一允许的 Agent Tool 是只读 `grounded_qa 1.0.0`，写 Tool 不可通过 prompt 开启。

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
- 阶段 3 的 Keyword/Dense/Hybrid/Hybrid+Reranker 检索 API 已可用。provisional 知识问答 Web/API
  可以创建持久会话、提交问题，由独立 Worker 调用真实 PostgreSQL SearchService 产出回答或拒答；
  终态响应和 Web 证据区展示经过当前 Space/版本/Chunk 再校验的 Citation 身份。
- provisional QA 的会话、Message、Run/Attempt、Evidence、Citation、Feedback 与 SSE 事件均保存到
  PostgreSQL。API 启动时会重排队安全的非终态 attempt，保留终态并清理中断时尚未发布的 Evidence；
  断开 SSE 不会取消 Run，只有显式取消请求才会记录取消意图。Worker 使用 attempt lease/heartbeat，
  启动时接管 queued 或租约过期运行，重复投递不会重复发布终态。Citation 可按需解析固定版本的
  最小原文片段；用户重试和反馈审核尚未实现。
- 默认 `FakeModelGateway` 使用确定性抽取式回答，返回相关证据片段而不是高质量综合回答；这是当前
  流程验证基线。Stage 3 达标并冻结检索配置后再调整召回、重排和回答表现，不得把当前结果用于 holdout。
- 阶段 3 评测配置仍为 provisional：阶段 0 和阶段 2 已正式关闭，但 2026-07-29 冻结语料
  development 的最佳 Dense Recall@5 只有 51.90%，BGE Reranker 没有净收益且 P95 为
  3523.9 ms，因此 holdout 仍被配置门禁拒绝。不要手工打开 `formal_runs_enabled`。
- P0 检索评测只纳入 Markdown/TXT/PDF 证据来源；Code/Notebook 属于 P1。validation 会同时记录
  原始与纳入 case 数，无证据安全 case 不得因格式过滤而跳过。
- Qwen3 Embedding 与 BGE Reranker 同时运行时，
  两者同时常驻约需 11 GiB 以上内存。内存不足时先停止 Qwen3 再运行离线 Reranker，或反向串行；
  不要降低模型 revision、混用旧向量或用 fake 结果替代。扩大 Qwen3 batch/并发在当前 CPU 上不会
加速，Compose 已保留实测较快的限制。
- 需要检索时先确认 Space 存在、Document 有当前 published version，且查询模式所需的 Embedding/Reranker 能力已配置；无命中是成功的空列表，不是系统故障。
- 模型服务不可用不会阻断 PostgreSQL/Redis 管理面 ready；Dense 会返回明确 Provider 错误，Hybrid 只有 profile 明确允许时才可降级为 Keyword。
- 已有离线 Agent Runtime、Tool/Skill Registry、声明式执行器、内存检查点恢复和 Skill 模板；
  `knowledge_qa 0.1.0` 首次由配置初始化，随后以 PostgreSQL active pointer 为准，现有 QA HTTP/Web 入口创建的每个 Run 都固定包摘要，
  Worker 校验后才调用唯一 QA Application Port。可用 `GET /api/v1/skills` 和
  `GET /api/v1/skills/knowledge_qa/versions` 检查安装摘要、active 版本和 manifest 预算。
- `knowledge_agent 0.1.0` 通过 `fast_chat` 执行受约束 LLM 决策，并在同一持久 QA Run 中调用一次
  `grounded_qa`。外层模型只看到 Tool 状态/计数，不看到回答或引用原文；通用 Runtime 决策历史仍未
  单独持久化，恢复和最终结果以 QA PostgreSQL 状态为准。
- 若 Run 以 `QA_SKILL_INVALID` 失败，检查 API 与 Worker 的 `SKILL_ROOT_PATH`、
  `KNOWLEDGE_QA_SKILL_VERSION` 和镜像内 `skills/knowledge_qa` 内容是否一致。不要就地修改已被 Run
  引用的同名版本；发布新 semver 并保留旧包供排队/恢复 Run 校验。
- Registry active pointer 已持久化到 `skill_activations`；激活或回滚出现
  `SKILL_ACTIVATION_CONFLICT` 时，应刷新 Catalog 的 `active_revision` 后重试，不能绕过 CAS。
  QA 的 PostgreSQL Run/Attempt/Event 是当前执行恢复事实源。通用 Runtime Checkpoint、持久生命周期
  事件和旧版本引用清理仍待阶段 5 后续实现。
- 知识整理入口会把选中的 Source/Document/DocumentVersion 固定到 QA Run。若排队期间来源撤下、
  文档发布新版本或 selector 不再匹配，运行会以稳定范围错误失败，不会自动跟随新版本；重新确认
  当前版本后创建新 Run。`compare_sources` 缺少两个来源的 Citation 时会拒答。
- `create_review_cards` 当前只生成预览。`write.code=SKILL_WRITE_PORT_UNAVAILABLE` 且
  `side_effects=0` 是预期结果；在派生知识 Application Port 和持久确认协议落地前不要绕过该标记
  直接写表或文件。
- Web 分别展示真实健康状态、真实数据来源/摄入任务和 provisional QA 状态；QA 证据面板只对
  服务端已发布的 Citation 按需请求原文，不接受客户端提供的 locator 或版本。若返回 `invalid`，
  先检查 Blob hash、parser 版本和 locator 是否仍与固定 DocumentVersion 一致，不要回退到相似文本。
- 阶段 0 语料已按 `docs/stage-0-acceptance.md` 冻结为 `internal_team_only`；真实语料只可在
  manifest 允许列表内用于本地/组内评测，禁止 Git 分发、公开演示和未经策略允许的外部 Provider
  外发。阶段 2 Step 9 已关闭，但阶段 3 正式质量门禁仍未关闭；阶段 4 因此仍未正式启动。
