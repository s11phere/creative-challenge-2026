# 项目架构概览

> 本文档描述 "Agent 驱动的个人知识仓库" 项目的整体架构、各组件职责与协作关系。
> 更新于阶段 2 Step 1（数据模型基础）完成时。

---

## 目录

- [1. 项目目标](#1-项目目标)
- [2. 架构总览](#2-架构总览)
- [3. 目录结构](#3-目录结构)
- [4. 根配置文件说明](#4-根配置文件说明)
- [5. 后端核心包 (`packages/`)](#5-后端核心包-packages)
- [6. 应用入口 (`apps/`)](#6-应用入口-apps)
- [7. 部署配置 (`deploy/`)](#7-部署配置-deploy)
- [8. 数据库迁移 (`migrations/`)](#8-数据库迁移-migrations)
- [9. 文档 (`docs/`)](#9-文档-docs)
- [10. 测试 (`tests/`)](#10-测试-tests)
- [11. 组件依赖关系](#11-组件依赖关系)
- [12. 规范命令](#12-规范命令)

---

## 1. 项目目标

构建一个**本地优先、来源可追溯**的个人知识工作台。核心闭环：

1. 导入真实文档，展示解析、索引和失败状态
2. 针对单文档或跨文档问题返回**有依据的回答**
3. 每个结论能够定位到对应文档版本和原文位置
4. 文件修改、移动或删除后进行**幂等增量更新**
5. 同一知识能力可从 Web、HTTP API 和 Skill 调用
6. 检索和回答质量通过版本化评测集回归验证

---

## 2. 架构总览

### 分层架构

```
┌─────────────────────────────────────────────────────┐
│                    apps/web                          │  ← 用户界面层
│              React + TypeScript                      │     (Vite, TanStack Query)
├─────────────────────────────────────────────────────┤
│                 apps/api                             │  ← HTTP API 层
│               FastAPI + Uvicorn                      │     (协议、校验、响应映射)
├─────────────────────────────────────────────────────┤
│               apps/worker                            │  ← 后台任务层
│                Dramatiq + Redis                      │     (解析、Embedding、索引)
├──────────────────────┬──────────────────────────────┤
│ packages/application │ packages/infrastructure       │  ← 应用/基础设施层
│  (用例编排)          │  (DB/Redis/日志/配置/OTel)    │
├──────────────────────┴──────────────────────────────┤
│ packages/model_gateway                               │  ← 模型网关层
│     (Chat/Embedding/Reranker Provider 适配)          │
├─────────────────────────────────────────────────────┤
│ packages/domain                                      │  ← 领域层(最内层)
│     (纯类型、接口定义、无外部依赖)                    │
├─────────────────────────────────────────────────────┤
│ migrations/    deploy/    docs/                       │  ← 支撑层
│ (DB 迁移)     (容器化)   (文档/ADR)                  │
└─────────────────────────────────────────────────────┘
```

### 依赖方向

依赖方向从外向内，外层依赖内层，内层不感知外层：

```
API / Worker → Application → Domain (纯类型)
              ← Infrastructure (DB/队列/配置等适配器)
              ← ModelGateway (LLM 模型适配器)
```

关键约束：
- `domain` 不依赖 FastAPI、SQLAlchemy、Redis、Dramatiq、OpenTelemetry 或具体模型 SDK
- `application` 编排用例，不承载供应商实现细节
- 传输层 (`api`) 只做协议、校验和响应映射，不直接实现领域规则

---

## 3. 目录结构

```
/
├── .env.example                    # 环境变量模板
├── .gitignore                      # Git 忽略规则
├── .python-version                 # Python 版本锁定 (3.12)
├── AGENTS.md                       # AI 代理开发指南（精简版）
├── README.md                       # 项目简介
├── alembic.ini                     # Alembic 迁移配置
├── pyproject.toml                  # Python 项目配置（uv workspace + 质量工具）
├── uv.lock                         # 依赖锁文件
│
├── apps/
│   ├── api/                        # FastAPI HTTP 服务
│   └── worker/                     # Dramatiq 后台 Worker
│   └── web/                        # React 前端工作台
│
├── packages/
│   ├── domain/                     # 纯类型与接口
│   ├── application/                # 用例编排
│   ├── infrastructure/             # 基础设施适配器
│   └── model_gateway/             # 模型网关
│
├── deploy/                         # Docker 部署配置
│   ├── compose.yaml                # 服务编排
│   ├── Dockerfile.api              # API 容器构建
│   ├── Dockerfile.worker           # Worker 容器构建
│   ├── Dockerfile.web              # Web 容器构建
│   ├── nginx.conf                   # Web 静态托管与同源 API 代理
│   └── otel-collector.yaml         # OpenTelemetry 配置
│
├── migrations/                     # 数据库迁移
│   ├── env.py                      # Alembic 运行环境
│   ├── script.py.mako              # 迁移模板
│   └── versions/                   # 迁移版本
│
├── docs/                           # 项目文档
│   ├── adr/                        # 架构决策记录
│   ├── architecture.md             # 本文档
│   └── ...
│
└── tests/                          # 测试
    ├── unit/                       # 单元测试
    ├── integration/                # 集成测试（预留）
    └── contract/                   # 契约测试（预留）
```

---

## 4. 根配置文件说明

### `pyproject.toml`
Python 项目中央配置。包含：
- **uv workspace**：定义工作空间成员（`packages/*`、`apps/api`、`apps/worker`）
- **根项目依赖**：依赖 `api` 和 `worker`，确保 `uv sync --frozen` 安装全部 workspace 包
- **构建系统**：各子包均使用 `setuptools` + `src/` 布局
- **质量工具配置**：
  - `ruff`：代码格式 + lint（选用规则：E、F、I、N、W、UP、B、SIM、ARG）
  - `mypy`：严格类型检查
  - `pytest`：测试配置（asyncio 模式 auto）
  - `coverage`：覆盖率配置

### `uv.lock`
自动生成的依赖锁文件。`uv sync --frozen` 使用它确保所有环境依赖版本一致。

### `.python-version`
锁定项目 Python 版本为 3.12。`uv` 自动读取此文件。

### `.env.example`
环境变量模板。复制为 `.env` 后填写实际值。包含：
- `APP_ENV` / `APP_DEBUG` / `APP_SECRET_KEY`：应用配置
- `POSTGRES_*`：数据库连接
- `REDIS_*`：Redis 连接
- `WORKER_*` / `DIAGNOSTIC_TASK_*`：Worker 并发、优雅停止、任务超时与重试上限
- `MODEL_*`：模型端点（默认注释，使用 deterministic fake）
- `OTLP_ENDPOINT`：OpenTelemetry 端点（可选）
- `LOG_LEVEL` / `LOG_FORMAT`：日志配置

### `alembic.ini`
数据库迁移配置文件。指定迁移脚本位置（`migrations/`）；实际连接 URL 由
`infrastructure.config.Settings` 从环境变量构造，避免迁移与应用使用两套配置。

### `AGENTS.md`
AI 开发代理的全局行为指南。定义了项目目标、优先级、架构不变量、技术基线、工作方式和 ADR 触发条件。

---

## 5. 后端核心包 (`packages/`)

### `packages/domain/` — 领域层

**职责**：最内层，包含纯领域实体、值对象和 Port（接口定义）。零外部依赖。

**文件**：

| 文件 | 职责 |
|------|------|
| `src/domain/__init__.py` | 稳定公开导出 |
| `src/domain/models.py` | 核心实体：`Space`、`Source`、`Document`、`DocumentVersion`、`Chunk`、`IngestionTask` 及其枚举、`RetrievalProfile` 值对象 |
| `src/domain/repositories.py` | 仓库接口定义（Protocol）：`SpaceRepository`、`SourceRepository`、`DocumentRepository`、`DocumentVersionRepository`、`ChunkRepository`、`IngestionTaskRepository` |

**约束**：
- 零外部依赖（不依赖 FastAPI、SQLAlchemy、任何 SDK）
- 所有跨层接口（Port）在此定义
- 业务逻辑不包含基础设施细节
- 实体使用 `@dataclass(frozen=True)` 保证不可变性

---

### `packages/application/` — 应用层

**职责**：编排用例流程，协调 Domain Port 与 Infrastructure Adapter。

- **`src/application/__init__.py`** — 包标记

**依赖**：`domain`

**模式**：每个用例是一个独立函数或类，接收 Port 作为参数，不直接依赖具体实现。

---

### `packages/infrastructure/` — 基础设施层

**职责**：实现所有外部系统适配器。

**文件**：

| 文件 | 职责 |
|------|------|
| `src/infrastructure/__init__.py` | 稳定公开导出 |
| `src/infrastructure/config.py` | Pydantic Settings 配置加载；向量维度不是运行时配置 |
| `src/infrastructure/database.py` | 异步 Engine、会话工厂、事务边界和有界连接检查 |
| `src/infrastructure/logging_config.py` | JSON 日志 schema 与集中脱敏 |
| `src/infrastructure/queue.py` | RedisBroker 延迟构造 |
| `src/infrastructure/telemetry.py` | OTel Provider、OTLP exporter 与客户端自动插桩 |
| `src/infrastructure/telemetry_context.py` | trace/request/task 上下文绑定与 ID 校验 |
| `src/infrastructure/orm.py` | 6 个 SQLAlchemy ORM 模型；含双哈希、处理版本、任务恢复字段、重试安全约束及 pgvector `Vector(768)`/cosine IVFFlat 索引 |
| `src/infrastructure/repositories.py` | 仓库实现：6 个 repository 类的完整 CRUD，含 domain ↔ ORM 映射 |

**`config.py` 详解**：

`Settings` 类从环境变量或 `.env` 文件加载配置：

- **`app_env`** / `app_debug` / `app_secret_key` — 应用基本配置
- **`postgres_*`** — PostgreSQL 连接参数，提供 `database_url` 属性
- **`redis_*`** — Redis 连接参数，提供 `redis_url` 属性
- **`otlp_endpoint`** / `otel_export_timeout_seconds` — 可选 Collector 与有界导出超时
- **`validate_secrets()`** — 生产环境（`app_env=production`）下校验必须密钥不为空，启动失败

全局实例 `settings = Settings()` 可在各模块中直接导入。

**依赖**：`domain`、`application`、`pydantic-settings`、`asyncpg`、`redis`、`dramatiq`、`opentelemetry`

---

### `packages/model_gateway/` — 模型网关层

**职责**：为上层提供统一模型调用接口，屏蔽具体 Provider 差异。

| 文件 | 职责 |
|------|------|
| `contracts.py` | Chat/Embedding 类型、能力别名、Protocol 与错误分类 |
| `fake.py` | 确定性 fake 和失败场景 |
| `factory.py` | Provider 选择、endpoint/data policy 校验 |
| `openai_compatible.py` | OpenAI-compatible HTTP Adapter、重试和响应解析 |
| `unavailable.py` | 禁用、配置缺失和策略拒绝实现 |
| `__init__.py` | 稳定公开导出 |

**设计要点**：
- 通过**能力别名**（`fast_chat`、`embedding_zh`）引用模型，不散落具体模型名
- 默认使用**确定性 fake**，不需要 API key
- Provider Adapter 封装 SDK/HTTP 类型，不向 application 或 domain 泄漏
- 外部 endpoint 默认禁止，公网外发需要显式策略开关
- 模型状态加入 readiness，但不是 API 启动或本地管理功能的硬依赖

**依赖**：`httpx`、`opentelemetry-api`

本阶段不包含 Reranker，也不执行真实 Provider 调用。

---

## 6. 应用入口 (`apps/`)

### `apps/api/` — HTTP API 服务

**技术栈**：FastAPI + Uvicorn

**入口**：`src/api/main.py`

**`main.py` 详解**：

使用**应用工厂模式**（`create_app()`），包含：

1. **生命周期管理**：`lifespan` 上下文管理器，启动时调用 `settings.validate_secrets()`
2. **异常处理器注册**：3 层保护：
   - `AppError` → 自定义业务异常（code/message/status_code）
   - `HTTPException` → 统一返回 `HTTP_{status_code}` 格式
   - `Exception` → 兜底，返回 `INTERNAL_ERROR`，不泄露内部细节
3. **路由注册**：
   - `GET /api/v1/health/live` — **存活探测**：仅检查进程事件循环
   - `GET /api/v1/health/ready` — **就绪探测**：并发检查 PostgreSQL 和 Redis，返回稳定机器码
4. **请求可观测性**：`observability.py` 校验或生成 trace/request ID，返回
   `X-Trace-ID`、`X-Request-ID`，并创建 HTTP server span 与开始/完成 JSON 日志。

**错误协议 (`errors.py`)**：

| 异常类型 | code 示例 | status |
|----------|-----------|--------|
| `AppError` | `NOT_FOUND` | 自定义 |
| HTTP 404 | `HTTP_404` | 404 |
| 请求校验失败 | `VALIDATION_ERROR` | 422 |
| 未预期异常 | `INTERNAL_ERROR` | 500 |

所有错误响应格式：
```json
{
  "code": "HTTP_404",
  "message": "Not Found",
  "trace_id": "a9000dc8547a4c97b28694baa2c9864e",
  "details": null
}
```

**OpenAPI**：端点声明 `response_model`，OpenAPI schema 中包含 `LiveResponse`、`ReadyResponse`、`ErrorResponse` 的 JSON Schema。

**依赖**：`fastapi`、`uvicorn[standard]`、`infrastructure`、`application`、`model-gateway`

---

### `apps/worker/` — 后台 Worker

**技术栈**：Dramatiq + Redis

**文件**：

| 文件 | 职责 |
|------|------|
| `src/worker/main.py` | 独立 Worker CLI 入口与优雅停止参数 |
| `src/worker/__main__.py` | 支持 `python -m worker` 启动 |
| `src/worker/tasks.py` | 无正文诊断任务、有限重试和永久失败回调 |

**当前状态**：已实现 Redis/Dramatiq Worker 基线。诊断消息仅包含 `task_id`、
`trace_id`、`event_version`、计数和请求时间；任务有明确超时、有限重试、优雅停止和
永久失败日志，重复执行不写入业务状态。

后续阶段实现：
- 文档解析、分块、Embedding、索引等长任务
- 任务状态跟踪（DB 持久化，Redis 只负责投递）

当前诊断任务已经提供 producer/consumer span 和结构化日志，能够按 trace/task/message ID
从入队事件关联到完成或永久失败事件。

**依赖**：`dramatiq`、`infrastructure`、`application`

---

### `apps/web/` — 前端工作台

**技术栈**：React 19 + TypeScript + Vite + TanStack Query

**文件结构**：

| 文件/目录 | 职责 |
|-----------|------|
| `package.json` | 依赖与命令定义，`engines: { "node": ">=24.0.0" }` |
| `vite.config.ts` | Vite 构建配置与开发环境 `/api` 反向代理 |
| `vitest.config.ts` | vitest 测试配置（jsdom 环境） |
| `tsconfig*.json` | TypeScript 编译配置 |
| `index.html` | 入口 HTML |
| `src/main.tsx` | React 挂载入口 |
| `src/App.tsx` | 系统状态工作台与 TanStack Query 状态编排 |
| `src/health.ts` | 健康接口类型、响应校验、超时和错误分类 |
| `src/App.css` | 根样式 |
| `src/index.css` | 全局样式与设计变量 |
| `src/test/setup.ts` | 测试初始化（@testing-library/jest-dom matchers） |

**当前状态**：阶段 1 系统状态工作台已实现。页面只读取版本化的 live/ready 接口，展示
API、PostgreSQL、Redis 和模型网关的真实状态及 Trace/Request ID；包含 8 秒请求超时、
30 秒自动刷新、手动重试、响应结构校验、键盘焦点和移动端布局。当前不展示任何虚构的
文档、会话、证据或摄入状态。

**后续将包含**：
- 空间/会话导航
- 对话与任务工作区
- 证据与原文查看器
- 长任务进度展示
- 错误/空白/加载状态

**规范命令**：
```bash
corepack pnpm --dir apps/web install      # 安装依赖
corepack pnpm --dir apps/web dev          # 开发服务器
corepack pnpm --dir apps/web build        # 生产构建
corepack pnpm --dir apps/web test         # 运行测试
corepack pnpm --dir apps/web lint         # 代码检查
corepack pnpm --dir apps/web typecheck    # 类型检查
```

---

## 7. 部署配置 (`deploy/`)

### `compose.yaml`

Docker Compose 编排，定义 5 个长期服务、1 个一次性迁移服务和 1 个可选服务：

| 服务 | 镜像 | 关键配置 | 健康检查 |
|------|------|----------|---------|
| **postgres** | `pgvector/pgvector:pg16` | 命名卷持久化数据 | `pg_isready` |
| **redis** | `redis:7-alpine` | AOF + 命名卷 | `redis-cli ping` |
| **migrate** | API 镜像 | PostgreSQL 健康后执行 `upgrade head` | 成功退出 |
| **api** | 本地构建 | 依赖 migrate 成功、Redis 健康 | readiness |
| **worker** | 本地构建 | 依赖 migrate 成功、Redis 健康 | 进程检查 |
| **web** | 本地构建 (nginx) | 依赖 API 健康，同源代理 `/api` | `/healthz` |
| **otel-collector** (可选) | `otel/opentelemetry-collector-contrib` | 需 `--profile otel` 启动 | — |

### `Dockerfile.api`

多阶段构建：
- **Builder 阶段**：安装 uv，复制 `pyproject.toml` + `uv.lock` + 源码，执行 `uv sync --frozen --no-dev`
- **Runtime 阶段**：digest 锁定的 `python:3.12-slim-bookworm`，包含 Alembic migration，
  运行 `uvicorn api.main:app`

### `Dockerfile.worker`

同 API 的多阶段构建，CMD 为 `python -m worker.main`。

### `Dockerfile.web`

- Builder：digest 锁定的 Node 24 + pnpm，安装依赖并执行 `pnpm build`
- Runtime：digest 锁定的 nginx:alpine，托管 `dist/` 并代理 `/api`

### `otel-collector.yaml`

开发环境 OTel Collector 配置。接收 OTLP（gRPC + HTTP），处理后输出到 debug。

---

## 8. 数据库迁移 (`migrations/`)

| 文件 | 职责 |
|------|------|
| `env.py` | Alembic 异步运行环境，使用 `create_async_engine` 连接 PostgreSQL；`target_metadata` 指向 ORM 的 `Base.metadata`，支持 autogenerate |
| `script.py.mako` | 迁移脚本生成模板 |
| `versions/328a3caa2960_enable_pgvector.py` | **初始迁移**：启用 `vector` 扩展 |
| `versions/a1b2c3d4e5f6_create_core_tables.py` | **阶段 2 迁移**：创建 `spaces`、`sources`、`documents`、`document_versions`、`chunks`（含 IVFFlat 向量索引）、`ingestion_tasks` 6 张表 |
| `versions/b2c3d4e5f6a7_complete_ingestion_identity.py` | **阶段 2 修正迁移**：补齐双哈希、处理版本、tombstone、Chunk/Task 幂等与恢复字段、外键和约束，并兼容回填旧数据 |

---

## 9. 文档 (`docs/`)

### 架构决策记录 (`docs/adr/`)

| ADR | 标题 | 摘要 |
|-----|------|------|
| 001 | Modular Monolith With Independent Worker | 模块化单体 + 独立 Worker，不拆分微服务 |
| 002 | PostgreSQL / pgvector | 数据库选型 PostgreSQL 16+ + pgvector |
| 003 | Agent Runtime Boundary | 自有 Agent Runtime Port，LangGraph Adapter 延后 |
| 004 | Local-First Data Boundary | 本地优先，外部模型显式选择 |
| 005 | Ingestion Identity, Versioning, Publication, And Deletion | 固定摄入身份、双哈希、处理版本、原子发布、任务可靠性和删除语义 |
| 009 | Redis / Dramatiq Task Delivery | 队列选型 Redis + Dramatiq，状态存 DB |

### 其他文档

| 文件 | 内容 |
|------|------|
| `development-environment.md` | 开发环境基线：工具版本、Windows 配置、Provider 边界 |
| `project-implementation-plan.md` | 总实施计划 |
| `stage-1-implementation-plan.md` | 阶段 1 详细实施计划与任务清单 |
| `stage-1-acceptance.md` | 阶段 1 验收命令、结果、退出条件、外部确认和已知问题 |
| `troubleshooting.md` | 本地运行故障恢复和已知限制 |
| `openapi.json` | 由应用确定性导出的公开 HTTP schema |
| `architecture.md` | **本文档** |

---

## 10. 测试 (`tests/`)

### 当前后端测试

```
tests/
├── __init__.py
├── unit/
│   ├── __init__.py
│   ├── test_config.py              # 配置、密钥与固定向量 schema 边界（6 个）
│   ├── test_database.py            # Engine、失败语义和数据库 span（3 个）
│   ├── test_domain_models.py       # 领域实体、值对象与任务状态（23 个）
│   ├── test_errors.py              # 错误协议测试（6 个）
│   ├── test_health.py              # 本地依赖与模型状态测试（4 个）
│   ├── test_model_gateway.py       # Provider 策略、错误、重试、维度和隐私（21 个）
│   ├── test_observability.py       # 上下文、日志 schema 和脱敏（4 个）
│   ├── test_openapi.py             # OpenAPI schema 测试（1 个）
│   ├── test_orm_models.py          # ORM 映射、向量索引与唯一约束（20 个）
│   ├── test_trace_middleware.py    # API 关联头与错误 trace（3 个）
│   └── test_worker_tasks.py        # 诊断任务、重试、入队和 trace（9 个）
├── integration/
│   ├── __init__.py
│   ├── test_data_model.py          # CRUD 与重试安全约束（17 个，需 RUN_INTEGRATION=1）
│   └── test_local_dependencies.py  # pgvector/Alembic/ORM 索引/Redis/readiness（4 个，需 RUN_INTEGRATION=1）
└── contract/
    └── test_model_gateway_contract.py  # fake/Adapter 共享契约（4 个）
```

**默认后端共 104 个测试运行，21 个真实依赖集成测试需显式启用**，覆盖：
- 配置：空密钥在 production 下拒绝启动，development 下跳过
- 错误：Pydantic model、404 统一格式、AppError 结构化响应、未知异常不泄露
- 健康：live 返回 alive、ready 返回 degraded + 机器码 + 不泄露主机信息
- OpenAPI：路径存在、schema 组件完整
- 数据库：结构化 URL、Engine 延迟连接、不可用语义和父 trace 延续
- 可观测性：关联 ID 校验、JSON schema、集中脱敏和错误体/响应头一致性
- Worker：消息无正文、输入校验、幂等执行、超时/重试、入队和 consumer trace
- ModelGateway：共享 Chat/Embedding 契约、能力别名、确定性 fake、有限重试、结构解析、
  endpoint 策略、显式不可用状态及输入/输出不进入日志或 span

前端另有 6 个 Vitest 组件测试，覆盖健康、依赖降级、API 不可达与手动重试、非法响应、
有界超时和键盘焦点。

另有 21 个需要 `RUN_INTEGRATION=1` 显式启用的真实依赖集成测试，覆盖 6 表 CRUD、作用域
唯一约束、版本/Chunk/Task 幂等身份、pgvector cosine 索引、单一 Alembic head、Redis 往返和
API readiness。CI 在独立 PostgreSQL/Redis 服务中运行这些测试。

---

## 11. 组件依赖关系

```
api ──┬── application ──→ domain
      ├── infrastructure ──→ domain
      │                   ──→ application
      └── model-gateway

worker ──┬── application ──→ domain
         └── infrastructure ──→ domain
                            ──→ application
```

依赖来源（通过各包的 `pyproject.toml`）：

| 包 | 依赖 |
|----|------|
| `api` | `application`、`infrastructure`、`model-gateway`、`fastapi`、`uvicorn` |
| `worker` | `application`、`infrastructure`、`dramatiq` |
| `application` | `domain` |
| `infrastructure` | `domain`、`application`、`pydantic-settings`、`sqlalchemy`、`asyncpg`、`redis`、`dramatiq`、`opentelemetry-api`、`opentelemetry-sdk` |
| `model-gateway` | `httpx` |
| `domain` | （无外部依赖） |

---

## 12. 规范命令

### 后端

```bash
uv sync --frozen                    # 安装依赖（干净环境）
uv sync --frozen --no-dev           # 安装生产依赖
uv run ruff format --check .        # 格式检查
uv run ruff check .                 # Lint
uv run mypy apps packages           # 类型检查
uv run pytest                       # 运行测试
uv run pytest -q --tb=short         # 精简输出
uv run uvicorn api.main:app         # 启动 API 服务
uv run python -m worker             # 启动独立 Dramatiq Worker
uv run alembic upgrade --sql head   # 脱机生成迁移 SQL
```

### 前端

```bash
corepack pnpm --dir apps/web install     # 安装依赖
corepack pnpm --dir apps/web dev         # 开发服务器
corepack pnpm --dir apps/web build       # 生产构建
corepack pnpm --dir apps/web test        # 运行测试
corepack pnpm --dir apps/web lint        # 代码检查
corepack pnpm --dir apps/web typecheck   # 类型检查
```

### Docker

```bash
docker compose -f deploy/compose.yaml up --build --detach --wait  # 启动并等待健康
docker compose -f deploy/compose.yaml --profile otel up --detach  # 含 OTel
docker compose -f deploy/compose.yaml down                         # 停止并保留数据
docker compose -f deploy/compose.yaml down --volumes               # 永久删除项目数据
```

---

## 附录：阶段状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| 阶段 0 | 🔶 进行中 | 语料授权复核、标注复核未完成 |
| **阶段 1** | **✅ 完成** | **Step 0-8 验收完成；GitHub Actions 正常** |
| **阶段 2** | **🟡 进行中** | **Step 0/1 已完成（ADR-005 + 6 表 + ORM + 仓库 + 两个数据模型迁移）** |
| 阶段 3+ | ❌ 未开始 | 解析器、检索、引用、Skill 等工作 |

阶段 1 已完成本地验收：Step 0（启动决策）✅、Step 1（工具链）✅、Step 2（API 与错误协议）✅、Step 3（DB 迁移与 Worker）✅、Step 4（可观测性）✅、Step 5（ModelGateway）✅、Step 6（Web 工作台）✅、Step 7（Compose/CI）✅、Step 8（验收与移交）✅

阶段 2 Step 0/1 已完成：ADR-005 固定身份、版本、发布、任务和删除语义；Space、Source、Document、DocumentVersion、Chunk、IngestionTask 的领域实体、ORM 模型、仓库实现及迁移已完成，R2-01～03 已关闭。

GitHub Actions 已由用户确认运行正常。阶段 0 数据授权、人工标注复核和版本冻结仍为等待状态。
