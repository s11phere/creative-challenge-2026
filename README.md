# Agent 驱动的个人知识仓库

本项目面向个人学习、科研和开发资料，目标是构建一个本地优先、来源可追溯的知识工作台。
规划中的完整闭环包括文档摄入、增量索引、混合检索、带引用问答和可版本化 Agent Skill。

## 当前状态

**阶段 0、阶段 1 和阶段 2 已正式完成；阶段 3 Step 0-10 的工程实现已完成，冻结语料 development 已复核但未达到 Recall/Reranker/P95 门禁，因此默认配置未冻结且正式 holdout 未执行。阶段 4 仍未正式启动，但 provisional Step 0-10 的领域/Application、PostgreSQL QA 持久化、API/SSE、Web、反馈和回答评测门禁已跑通；阶段 5 通用 Agent Runtime/Skill 基础已并行通过审查。**

已交付的核心能力：

| 层级 | 内容 |
|------|------|
| 阶段 1 ✅ | 工程骨架：FastAPI、Worker、Web 工作台、PostgreSQL/pgvector、Redis、Alembic、模型网关、结构化日志、OpenTelemetry、Compose、CI |
| 阶段 2 ✅ | 摄入工程 Step 0-8 与正式 Step 9 验收完成；冻结 manifest 中 74 个 P0 来源解析/定位/分块成功率 100%，幂等、原子发布、删除恢复、API/Web 和 Compose E2E 通过 |
| 阶段 3 🟡 工程 Step 0-10 | PostgreSQL FTS/pgvector 检索、加权 RRF、上下文扩展、可选 Reranker、Space/版本安全边界、检索 API、版本化离线评测与集成验收已完成；真实模型 development 最佳 Dense Recall@5 为 51.90%，未达到 85%，默认配置未冻结且正式 holdout 未执行 |
| 阶段 4 🟡 provisional Step 0-10 | ADR-007、唯一 provisional QA Application Port、Grounded QA/Evidence/Citation、PostgreSQL Repository/SSE、问答 API、Web 和回答评测 validate-only 已完成；独立 Worker 通过 attempt lease 执行并支持重启恢复/重复投递；原文跳转、默认配置与 holdout 未完成 |
| 阶段 5 🟡 通用基础 + 可用 provisional 链路 | ADR-006、Runtime 领域契约、Tool/Skill Registry、确定性执行器、版本固定、预算/权限/审计、事务式 reload/回滚和 Skill 模板已通过审查；现有 QA Web/API 已作为后续业务 Skill 开发的临时可用链路 |

当前 Web 展示系统健康、数据来源和 provisional 知识问答工作区；HTTP API 可创建持久会话、提交
问题，由 API 仅向 Redis 投递 Run ID，再由独立 Worker 调用唯一 `GroundedQAApplicationPort`、
真实 PostgreSQL `SearchService` 和
Citation target adapter 生成回答或拒答。默认 fake 模型提供确定性抽取式回答；配置允许的外部
Chat Provider 仍走相同结构化生成与引用校验路径。Web 会展示终态回答、限制和文档版本/locator
引用身份。

该链路是可真实使用的 provisional 版本，不是阶段 4/5 正式完成：QA 会话、Message、Run/Attempt、
Evidence、Citation、Feedback、SSE 事件和 Worker lease 已写入 PostgreSQL；API/Worker 重启可恢复
未完成运行，重复投递不会重复发布终态；引用尚不能点击跳转原文；
阶段 3 默认检索配置和质量门禁也尚未冻结。
阶段 5 当前只有离线通用 Runtime/Registry、内存检查点恢复，以及不会被批量注册的 provisional
`knowledge_qa` QA Port 合成契约；没有活动业务 Skill、Runtime API、PostgreSQL 运行/检查点持久化
或 Web Skill 入口，不能据此宣称 `knowledge_qa` 可用或阶段 5 整体完成。
阶段 0 已冻结为 `internal_team_only`，原始语料和评测 JSONL 仍只在组员本地保留；退出证据见
[Stage 0 验收记录](docs/stage-0-acceptance.md)，摄入退出证据见
[Stage 2 验收记录](docs/stage-2-acceptance.md)。不要直接运行 holdout；必须先完成真实模型
development 改进并关闭 Recall/Reranker/P95 门禁、冻结默认配置，再按阶段 3 Runbook 执行一次性 holdout。

## 快速启动

前置条件：Docker Engine 29+ 和 Docker Compose 5+。本机不需要单独安装 PostgreSQL 或 Redis。

1. 创建本地环境文件，必须设置 `APP_SECRET_KEY` 和 `POSTGRES_PASSWORD`：

```bash
cp .env.example .env
```

编辑 `.env`，填入两个必填项（其他保持默认即可快速体验）：

```bash
APP_SECRET_KEY=your-random-secret-key
POSTGRES_PASSWORD=your-database-password
```

2. 构建并启动完整本地栈：

```bash
docker compose -f deploy/compose.yaml --env-file .env up --build --detach --wait
```

> **注意**：Docker Compose v5 可能需要显式指定 `--env-file .env`；若不加也能正常运行则无需此参数。

3. 打开工作台：<http://127.0.0.1:5173>

首次构建需要下载锁定 digest 的基础镜像和依赖。Compose 会依次等待 PostgreSQL、迁移、Redis、
API、Worker 和 Web 达到各自完成或健康条件。

在“数据来源”中上传并等待文档状态发布后，进入“知识问答”即可使用当前 Space 的真实索引提问。
默认 fake 模型返回可复现的相关证据摘录，适合先跑通流程；回答质量将在阶段 3 达标后继续调整。

## Smoke Test

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:5173/api/v1/health/ready
docker compose -f deploy/compose.yaml --env-file .env ps
```

预期 `live` 返回 `alive`，`ready` 返回 `ready`，PostgreSQL 和 Redis 分别报告
`POSTGRESQL_OK`、`REDIS_OK`。默认确定性模型替身报告 `MODEL_FAKE_READY`。

## 停止与清理

停止容器但保留 PostgreSQL 和 Redis 数据：

```bash
docker compose -f deploy/compose.yaml --env-file .env down
```

仅在确认要永久删除当前项目数据时执行：

```bash
docker compose -f deploy/compose.yaml --env-file .env down --volumes --remove-orphans
```

## 开发命令

后端基线为 Python 3.12 和 uv 0.11.x：

```powershell
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest
```

前端基线为 Node 24 和 Corepack 管理的 pnpm 10.20.0：

```powershell
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
```

迁移与 OpenAPI：

```powershell
uv run alembic upgrade head
uv run alembic current
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
```

真实依赖集成测试由 CI 在隔离 PostgreSQL/Redis 中运行。手动运行前必须配置隔离依赖并设置
`RUN_INTEGRATION=1`，不要指向包含业务数据的数据库。

## 服务与端口

| 服务 | 默认地址/端口 | 说明 |
| --- | --- | --- |
| Web | `http://127.0.0.1:5173` | nginx 静态托管并代理同源 `/api` |
| API | `http://127.0.0.1:8000` | 公开接口前缀为 `/api/v1` |
| PostgreSQL | `127.0.0.1:5432` | PostgreSQL 16 + pgvector |
| Redis | `127.0.0.1:6379` | Dramatiq broker，启用 AOF |
| OTel Collector | `4317`、`4318` | 仅 `--profile otel` 启动 |

端口可通过 `.env` 中的 `WEB_PORT`、`API_PORT`、`POSTGRES_PORT` 和 `REDIS_PORT` 覆盖。

## 文档

- [架构概览](docs/architecture.md)
- [开发环境与 Provider 边界](docs/development-environment.md)
- [阶段 1 验收记录](docs/stage-1-acceptance.md)
- [阶段 2 验收记录](docs/stage-2-acceptance.md)
- [故障排查与已知限制](docs/troubleshooting.md)
- [阶段 1 实施计划](docs/stage-1-implementation-plan.md)
- [阶段 2 实施计划](docs/stage-2-implementation-plan.md)
- [阶段 3 实施计划](docs/stage-3-implementation-plan.md)
- [阶段 3 验收记录](docs/stage-3-acceptance.md)
- [阶段 4 实施计划](docs/stage-4-implementation-plan.md)
- [阶段 4 provisional 验收记录](docs/stage-4-acceptance.md)
- [阶段 4 持久化设计](docs/stage-4-persistence-design.md)
- [ADR-007：Grounded QA 持久化、引用、执行与 SSE](docs/adr/007-grounded-qa-persistence-and-sse.md)
- [阶段 5 实施计划](docs/stage-5-implementation-plan.md)
- [阶段 5 实现审查记录](docs/stage-5-implementation-review.md)
- [OpenAPI](docs/openapi.json)
- [架构决策记录](docs/adr/README.md)

## 安全与数据边界

- 密钥只从环境变量或被 Git 忽略的 `.env` 读取，不得提交或写入日志。
- 默认模型为确定性 fake；外部 Provider 必须显式配置并允许外发。
- 只允许处理 `cases/evals/corpus/v0/manifest.yaml` 明确列出的来源，并在读取前校验 SHA-256。
- 阶段 0 语料仅在 manifest 允许列表内使用；未确认外部授权的来源仍限于组员本地，禁止 Git 上传、公开演示和外部 Provider 外发。

问题恢复步骤和当前限制见[故障排查文档](docs/troubleshooting.md)。
