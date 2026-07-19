# Agent 驱动的个人知识仓库

本项目面向个人学习、科研和开发资料，目标是构建一个本地优先、来源可追溯的知识工作台。
规划中的完整闭环包括文档摄入、增量索引、混合检索、带引用问答和可版本化 Agent Skill。

## 当前状态

**阶段 2 进行中：解析器基础已完成。**

已交付的核心能力：

| 层级 | 内容 |
|------|------|
| 阶段 1 ✅ | 工程骨架：FastAPI、Worker、Web 工作台、PostgreSQL/pgvector、Redis、Alembic、模型网关、结构化日志、OpenTelemetry、Compose、CI |
| 阶段 2 ✅ Step 0/1 | ADR-005 + 6 张数据模型表 + ORM/仓库 + 身份、版本、任务约束迁移；R2-01～03 已关闭 |
| 阶段 2 ✅ Step 2 | Markdown/TXT/PDF 解析器 + `ParsedDocument` schema + Parser Port + `ParserFactory` + 统一错误分类 |
| 阶段 2 ⏳ 进行中 | 分块器、嵌入、摄入 API/Worker、Web 数据源页面 |

当前 Web 只展示真实系统健康状态，尚未实现文档摄入、搜索、会话、引用或问答功能。
阶段 0 的语料授权复核、人工标注复核和版本冻结仍未关闭，项目状态为”工程进行中，等待阶段 0 数据门禁”。

## 快速启动

前置条件：Docker Engine 29+ 和 Docker Compose 5+。本机不需要单独安装 PostgreSQL 或 Redis。

1. 创建本地环境文件，并修改其中的 `APP_SECRET_KEY` 和 `POSTGRES_PASSWORD`：

```powershell
Copy-Item .env.example .env
```

2. 构建并启动完整本地栈：

```powershell
docker compose -f deploy/compose.yaml up --build --detach --wait
```

3. 打开工作台：<http://127.0.0.1:5173>

首次构建需要下载锁定 digest 的基础镜像和依赖。Compose 会依次等待 PostgreSQL、迁移、Redis、
API、Worker 和 Web 达到各自完成或健康条件。

## Smoke Test

```powershell
curl.exe --fail http://127.0.0.1:8000/api/v1/health/live
curl.exe --fail http://127.0.0.1:8000/api/v1/health/ready
curl.exe --fail http://127.0.0.1:5173/api/v1/health/ready
docker compose -f deploy/compose.yaml ps
```

预期 `live` 返回 `alive`，`ready` 返回 `ready`，PostgreSQL 和 Redis 分别报告
`POSTGRESQL_OK`、`REDIS_OK`。默认确定性模型替身报告 `MODEL_FAKE_READY`。

## 停止与清理

停止容器但保留 PostgreSQL 和 Redis 数据：

```powershell
docker compose -f deploy/compose.yaml down
```

仅在确认要永久删除当前项目数据时执行：

```powershell
docker compose -f deploy/compose.yaml down --volumes --remove-orphans
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
- [故障排查与已知限制](docs/troubleshooting.md)
- [阶段 1 实施计划](docs/stage-1-implementation-plan.md)
- [OpenAPI](docs/openapi.json)
- [架构决策记录](docs/adr/README.md)

## 安全与数据边界

- 密钥只从环境变量或被 Git 忽略的 `.env` 读取，不得提交或写入日志。
- 默认模型为确定性 fake；外部 Provider 必须显式配置并允许外发。
- 只允许处理 `cases/evals/corpus/v0/manifest.yaml` 明确列出的来源，并在读取前校验 SHA-256。
- 阶段 0 关闭前不得使用真实个人资料、私有笔记或未确认授权的语料。

问题恢复步骤和当前限制见[故障排查文档](docs/troubleshooting.md)。
