# 易知官网适配交付手册

本文档面向通班官网维护人员，说明如何把易知服务接入官网首期页面。它描述的是
项目服务边界和可执行的联调流程，不替代官网仓库中的 Next.js/Convex 实现。

## 1. 交付版本

- 项目：易知（`creative-challenge-2026`）
- 适配分支：`codex/cc2026-delivery-compliance`
- 适配提交：交付 tag `cc2026-yizhi-v1`（等价于 `git rev-list -n1 cc2026-yizhi-v1`，请勿使用分支尖端）
- 服务容器：`api`、`worker`、`postgres`、`redis`，可选 `tei`、`tei-reranker`
- API 监听：容器内 `api:8000`
- 健康检查：`GET /api/v1/health/live`、`GET /api/v1/health/ready`

官网接入应固定到该 tag 指向的 commit 或对应镜像 digest；不要直接跟随分支尖端部署。
该 tag 已通过 CI 的 `Backend quality`、`Backend tests`、`Migrations and integration`、
`Frontend`、`Compose smoke + web E2E` 和 `Website profile smoke` 六个作业。

## 2. 网络和身份边界

```text
浏览器
  -> 官网 Next.js Route Handler
       -> Convex 验证 session、成员和角色
       -> 注入内部 Header
            -> yizhi api:8000
                 -> PostgreSQL（Space、文档、会话、Run）
                 -> Redis（任务队列）
                 -> Worker（解析、Embedding、索引、问答）
                 -> 可选 TEI/Reranker
```

浏览器不得直连 API、PostgreSQL、Redis、TEI 或 Worker。官网 Route Handler 每次请求都要
验证 session，不能依赖页面 layout 或客户端 guard 已经执行。

### 2.1 必需 Header

| Header | 由谁生成 | 约束 |
| --- | --- | --- |
| `X-Internal-Service-Token` | 官网服务端 | 与 API 的 `INTERNAL_SERVICE_TOKEN` 完全一致；不得进入浏览器或日志 |
| `X-App-Scoped-User-Id` | 官网服务端 | 不含姓名、学号等个人信息；匹配 `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` |
| `X-Request-ID` | 官网服务端 | 每次请求生成或透传一个可关联的请求 ID |

API 不接受浏览器 session token。健康检查不要求前两个 Header，其他 API 请求在生产模式
缺少或不匹配时返回 `401`，响应 code 为 `SERVICE_AUTH_REQUIRED`。

示例（只在服务端执行，令牌使用 secret manager 注入）：

```bash
curl -H 'X-Internal-Service-Token: <secret>' \
  -H 'X-App-Scoped-User-Id: member:opaque-123' \
  -H 'X-Request-ID: req-20260912-0001' \
  http://yizhi-api:8000/api/v1/spaces
```

## 3. 首期官网接口

完整机器契约位于 [`docs/openapi.json`](openapi.json)。官网首期只使用下表接口；不要调用
OpenAPI 中的 Agent、考试、个人 Skill、审核、反馈或派生知识接口。

| 用途 | 方法和路径 |
| --- | --- |
| Space | `POST /api/v1/spaces`、`GET /api/v1/spaces`、`DELETE /api/v1/spaces/{space_id}` |
| 来源 | `POST /api/v1/spaces/{space_id}/sources`、`GET /api/v1/spaces/{space_id}/sources` |
| 上传 | `POST /api/v1/spaces/{space_id}/sources/{source_id}/upload`，multipart 字段名为 `file` |
| 来源详情 | `GET /api/v1/spaces/{space_id}/sources/{source_id}/detail` |
| 摄入 | `POST /api/v1/spaces/{space_id}/sources/{source_id}/ingest` |
| 文档删除 | `DELETE /api/v1/spaces/{space_id}/sources/{source_id}/documents/{document_id}` |
| 任务 | `GET /api/v1/tasks/{task_id}`、`POST /api/v1/tasks/{task_id}/cancel`、`POST /api/v1/tasks/{task_id}/retry` |
| 会话 | `POST/GET /api/v1/spaces/{space_id}/conversations`、`DELETE /api/v1/spaces/{space_id}/conversations/{conversation_id}` |
| 问答 | `POST /api/v1/conversations/{conversation_id}/questions` |
| Run | `GET /api/v1/qa/runs/{run_id}`、`POST /api/v1/qa/runs/{run_id}/cancel`、`POST /api/v1/qa/runs/{run_id}/retry` |
| 引用 | `GET /api/v1/qa/runs/{run_id}/citations/{evidence_id}` |
| 事件 | `GET /api/v1/qa/runs/{run_id}/events` |
| 配置 | `GET /api/v1/config/limits` |

请求体、响应字段、UUID 格式和 422 校验以 OpenAPI 为准。`owner_id`、`caller_id` 不由官网
填写或覆盖；生产 API 使用 `X-App-Scoped-User-Id` 作为唯一租户身份。

## 4. 推荐联调顺序

1. `POST /api/v1/spaces` 创建 Space，保存返回的 `id`。
2. `POST /api/v1/spaces/{space_id}/sources` 创建 `source_type=upload` 的来源。
3. 对返回的 `source_id` 调用 multipart upload；当前默认单文件上限由
   `GET /api/v1/config/limits` 返回，默认值为 50 MiB。
4. 保存 `task_id`，轮询 `GET /api/v1/tasks/{task_id}`。页面刷新后应继续从服务读取状态。
5. 只有任务为 `succeeded` 且来源存在已发布文档时，才允许进入问答页面。
6. 创建会话，调用 `POST /api/v1/conversations/{conversation_id}/questions`，保存返回的
   `run_id`。
7. 轮询 Run 或读取事件；终态包括 `succeeded`、`failed`、`cancelled`。回答中的
   `evidence_id` 通过引用接口取得最小原文片段和 locator。
8. 删除资料或 Space 前先刷新服务状态；删除后的对象对当前用户返回 404，不能继续展示旧缓存。

上传和问答都必须携带新的 `idempotency_key`。网络重试使用同一个 key，避免重复消息或重复
任务；不能用 React state 或 `localStorage` 作为唯一任务状态。

## 5. 错误和恢复约定

| HTTP | code/场景 | 官网处理 |
| --- | --- | --- |
| `401` | `SERVICE_AUTH_REQUIRED` | 网关配置或 secret 失效；不要让浏览器重试内部令牌 |
| `404` | Space、来源、任务、Run 不属于当前用户或已删除；公开模式下被隐藏的能力（`CAPABILITY_NOT_EXPOSED`） | 清除本地对象缓存并回到列表；不要显示“无权限”以外的对象信息 |
| `409` | 幂等冲突或状态不允许重试 | 重新读取对象状态，避免盲目重复提交 |
| `413` | 上传超过当前限制（先按 `Content-Length` 拒绝，再校验实际字节） | 重新读取 `/config/limits`，在页面提示大小限制 |
| `422` | 请求字段、UUID、查询或文件校验失败（含未知 `source_type`） | 展示字段级错误，不重试原请求 |
| `503` | 就绪探针 `GET /api/v1/health/ready` 报告依赖不可用 | 暂停新建任务并按退避重试，恢复后再放量 |
| 任务/Run 的 `error_code` | 超时、上游模型失败、结构化输出非法等（`MODEL_TIMEOUT`、`MODEL_UNAVAILABLE`、`QA_STRUCTURED_RESPONSE_INVALID`） | 以任务或 Run 状态为准，保留 ID 后重试；不要创建新任务替代原任务 |

异步链路不用 HTTP 状态码表达上游失败：提交类接口先返回 `202`，最终结果落在
`GET /api/v1/tasks/{task_id}` 与 `GET /api/v1/qa/runs/{run_id}` 的 `status` / `error_code` 上；
服务本身不返回 `502/504`。

标准错误响应包含 `code`、`message`、`trace_id`；将 `X-Trace-ID` 和 `X-Request-ID` 记录到
官网服务日志中，但不得记录文件正文、问题、回答、凭据或 Provider 响应。

## 6. 服务部署

### 6.1 生产环境变量

至少通过 secret manager 注入：

```dotenv
APP_ENV=production
APP_DEBUG=false
APP_SECRET_KEY=<random-secret>
POSTGRES_PASSWORD=<database-secret>
INTERNAL_SERVICE_TOKEN=<gateway-api-secret>
SERVICE_AUTH_REQUIRED=true
PUBLIC_MODE=true
MODEL_ALLOW_EXTERNAL=false
# PUBLIC_MODE_ALLOW_EXTERNAL_MODEL=true   # 仅在完成数据使用评审后设置
```

如果要启用外部 Chat Provider，必须经过官网维护侧的数据使用、预算和来源策略评审，再
显式设置 Provider 相关变量；`private_local` 和 `restricted` 内容不得因此自动外发。

### 6.2 CPU Compose

无 GPU 时使用：

```bash
docker compose -f deploy/compose.yaml \
  -f deploy/compose.cpu.yaml \
  -f deploy/compose.intranet.yaml \
  --env-file .env \
  --profile embedding --profile reranker \
  up --build --detach --wait
```

只使用 fake Embedding/Reranker 做流程联调时可以不启用两个 profile。已实测的 CPU 模式
内存、磁盘、时延和并发基线见 [CPU 资源基线](cc2026-cpu-baseline.md)；启用 TEI 后必须按该
文档第 6 节在目标机复测。`migrate` 服务负责
执行 `alembic upgrade head`；`pgdata`、`redisdata`、`blobdata` 和模型卷必须保留，不能用
`docker compose down --volumes` 作为普通发布步骤。

### 6.3 持久化、升级和回滚

- PostgreSQL 是 Space、文档版本、任务、会话、Run、引用的权威存储。
- Redis 是可恢复任务队列；Worker 启动会恢复符合条件的未完成任务。
- `blobdata` 保存当前 Local BlobStore 的原始文件；迁移到官网 R2 前不能删除该卷。
- 发布前固定镜像 digest 和数据库备份；升级先运行 migration，再启动 API/Worker。
- 回滚必须回到上一份镜像和对应兼容 migration，禁止直接删除卷或手工修改业务表。
- 生产备份路径、保留周期和恢复演练由官网/运维负责人补入部署系统，不能由项目默认值推断。

## 7. CI 和本地复核

远程工作流见 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)，包括后端质量、单元/契约
测试、PostgreSQL/Redis migration integration、前端 lint/typecheck/test/build 和 Compose
smoke/E2E。提交前按以下顺序执行：

```bash
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest tests/unit tests/contract
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web typecheck:e2e
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
```

生产 profile 的可执行验收脚本是 [`examples/first_phase_smoke.py`](../examples/first_phase_smoke.py)，
CI 的 `Website profile smoke` 作业在干净卷上运行它（含上传、摄入、问答、引用、租户隔离与
能力边界）：

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.intranet.yaml --env-file .env \
  up --build --detach --wait postgres redis migrate api worker
docker compose -f deploy/compose.yaml -f deploy/compose.intranet.yaml --env-file .env \
  exec -T -e SMOKE_TOKEN="$INTERNAL_SERVICE_TOKEN" api \
  python - < examples/first_phase_smoke.py
```

真实 PostgreSQL/Redis 集成测试需要隔离依赖并设置 `RUN_INTEGRATION=1`。当前适配提交已在
本地通过后端 format/lint/mypy、定向服务认证/OpenAPI 测试和 OpenAPI 确定性检查；完整 CI
还必须在 GitHub Actions 的 Linux/Docker 环境跑完，不能把本地定向测试当作 CI 通过证据。

## 8. 官网侧待办和验收证据

项目服务交付不包含以下官网代码或生产资源，负责人需要另行完成并留存证据：

- Next.js `/intranet/apps/yizhi` 页面和同源 Route Handler；
- Convex session、成员角色、配额和审计查询；
- R2 输入/输出对象用途、预签名上传、HEAD 校验、生命周期和受控下载；
- 官网服务到 `api:8000` 的内部 DNS/网络连通和令牌 secret rotation；
- CPU Compose 目标机器的内存、磁盘、首次模型下载、索引时延和并发记录（仓库已提供 fake
  provider 基线：`docs/cc2026-cpu-baseline.md`；启用 TEI 后仍需目标机数据）；
- 页面刷新、服务重启、任务失败/重试、跨用户访问和未登录访问的端到端报告。

完成上述事项后，才能把本项目从“服务适配完成”标记为“官网上线验收通过”。
