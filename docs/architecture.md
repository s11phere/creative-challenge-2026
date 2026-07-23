# 项目架构概览

> 本文档描述 "Agent 驱动的个人知识仓库" 项目的整体架构、各组件职责与协作关系。
> 更新于阶段 3 Step 10 工程验收、阶段 4 实施计划和阶段 5 通用 Runtime/Registry 审查完成时
>（2026-07-23）。

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
├──────────────────┬──────────────────┬────────────────┤
│ application      │ agent_runtime    │ infrastructure │  ← 应用/Runtime/基础设施层
│ (用例编排)       │ (离线通用基础)   │ (DB/队列/OTel) │
├──────────────────┴──────────────────┴────────────────┤
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
Agent Runtime → Domain + ModelGateway
```

关键约束：
- `domain` 不依赖 FastAPI、SQLAlchemy、Redis、Dramatiq、OpenTelemetry 或具体模型 SDK
- `application` 编排用例，不承载供应商实现细节
- `agent_runtime` 实现声明式、确定性的通用执行与 Registry；当前尚未接入 API/Application
- 传输层 (`api`) 只做协议、校验和响应映射，不直接实现领域规则

---

## 3. 目录结构

```
/
├── .env.example                    # 环境变量模板
├── .gitignore                      # Git 忽略规则
├── .python-version                 # Python 版本锁定 (3.12)
├── AGENTS.md                       # AI 代理开发指南与仓库级约束
├── README.md                       # 项目简介
├── alembic.ini                     # Alembic 迁移配置
├── pyproject.toml                  # Python 项目配置（uv workspace + 质量工具）
├── uv.lock                         # 依赖锁文件
│
├── apps/
│   ├── api/                        # FastAPI HTTP 服务
│   ├── worker/                     # Dramatiq 后台 Worker
│   └── web/                        # React 前端工作台
│
├── packages/
│   ├── domain/                     # 纯类型与接口
│   ├── application/                # 用例编排
│   ├── agent_runtime/              # Tool/Skill Registry 与确定性执行器
│   ├── infrastructure/             # 基础设施适配器
│   └── model_gateway/              # 模型网关
│
├── cases/                          # 阶段 0 基线、允许语料、评测集与公开 fixture
│   ├── docs/
│   └── evals/
│
├── skills/
│   └── _template/                  # 声明式 Skill 开发模板（不参与批量注册）
│
├── scripts/                        # OpenAPI 导出、Embedding 重建和检索评测 CLI
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
    ├── integration/                # 隔离 PostgreSQL/Redis/API 集成测试
    ├── contract/                   # ModelGateway/Tool Registry 契约测试
    └── fixtures/                   # 确定性 parser fixture
```

---

## 4. 根配置文件说明

### `pyproject.toml`
Python 项目中央配置。包含：
- **uv workspace**：定义工作空间成员（`packages/*`、`apps/api`、`apps/worker`）
- **根项目依赖**：依赖 `agent-runtime`、`api`、`worker` 及 Runtime schema/YAML 工具，确保
  `uv sync --frozen` 安装当前已实现的 workspace 包
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
- `WORKER_*` / `DIAGNOSTIC_TASK_*` / `INGESTION_TASK_*`：Worker 并发、优雅停止、任务租约、
  心跳、超时与重试上限
- `MODEL_*` / `FAST_CHAT_*` / `EMBEDDING_*` / `RERANKER_*`：能力级模型端点、identity、协议
  和数据外发策略（默认 deterministic fake）
- `RETRIEVAL_*`：检索总超时和仅开发环境可用的安全诊断开关
- `OTLP_ENDPOINT`：OpenTelemetry 端点（可选）
- `LOG_LEVEL` / `LOG_FORMAT`：日志配置

### `alembic.ini`
数据库迁移配置文件。指定迁移脚本位置（`migrations/`）；实际连接 URL 由
`infrastructure.config.Settings` 从环境变量构造，避免迁移与应用使用两套配置。

### `AGENTS.md`
AI 开发代理的全局行为指南。定义了项目目标、优先级、架构不变量、技术基线、工作方式和 ADR 触发条件。

### `scripts/`

| 文件 | 职责 |
| --- | --- |
| `scripts/export_openapi.py` | 从应用工厂确定性导出 `docs/openapi.json` |
| `scripts/rebuild_embeddings.py` | 按固定 Embedding identity 创建受控重建任务，不绕过原子发布 |
| `scripts/evaluate_retrieval.py` | 校验/执行版本化检索评测、formal/holdout 门禁和机器可读报告 |

---

## 5. 后端核心包 (`packages/`)

### `packages/domain/` — 领域层

**职责**：最内层，包含纯领域实体、值对象和 Port（接口定义）。零外部依赖。

**文件**：

| 文件 | 职责 |
|------|------|
| `src/domain/__init__.py` | 稳定公开导出 |
| `src/domain/models.py` | 核心实体：`Space`、`Source`、`Document`、`DocumentVersion`、`Chunk`、`IngestionTask` 及其枚举、`RetrievalProfile` 值对象 |
| `src/domain/agent_runtime.py` | AgentRun 状态/步骤、预算、权限、调用记录、检查点、恢复校验及 Runtime/Registry/审批 Port |
| `src/domain/repositories.py` | 仓库接口定义（Protocol）：`SpaceRepository`、`SourceRepository`、`DocumentRepository`、`DocumentVersionRepository`、`ChunkRepository`、`IngestionTaskRepository` |
| `src/domain/parsing.py` | `ParsedDocument` / `StructNode` / `ParseError` 纯类型、`Parser` Protocol、`compute_blob_hash` 辅助函数 |
| `src/domain/fingerprinting.py` | 内容指纹：`normalize_stable_key`、`compute_content_hash`（含版本分隔符）、`compute_storage_key` |
| `src/domain/blob_store.py` | `BlobStore` Port（含 `store_and_verify`） |
| `src/domain/chunking.py` | 结构分块输入输出、`ChunkerConfig`、Chunk identity/hash 和 `Chunker` Port |
| `src/domain/embedding.py` | `EmbeddingIdentity`、处理配置摘要和 768 维版本边界 |
| `src/domain/retrieval.py` | SearchRequest/SearchResult、`RetrievalProfileV1`、候选/诊断/locator、`RetrievalStore`、QueryEmbedder 和 Reranker Port |
| `src/domain/grounded_qa.py` | provisional GroundedAnswer/Claim/Evidence/Citation/Refusal/Conflict 契约、稳定错误、引用身份校验和 QA 状态投影 |

**约束**：
- 零外部依赖（不依赖 FastAPI、SQLAlchemy、任何 SDK）
- 所有跨层接口（Port）在此定义
- 业务逻辑不包含基础设施细节
- 实体使用 `@dataclass(frozen=True)` 保证不可变性

---

### `packages/application/` — 应用层

**职责**：编排用例流程，协调 Domain Port 与 Infrastructure Adapter。

| 文件 | 职责 |
|------|------|
| `src/application/__init__.py` | 包标记 |
| `src/application/ingestion/__init__.py` | 摄入用例包 |
| `src/application/ingestion/source_registration.py` | 来源登记用例：`SourceRegistrationService`（创建 Source、FINGERPRINT 阶段、`(source_id, stable_key)` 查重、`blob_hash` 匹配）|
| `src/application/ingestion/embedding.py` | INDEX/VALIDATE/PUBLISH Embedding 流水线和原子发布编排 |
| `src/application/ingestion/orchestrator.py` | discover/parse/chunk/embed/publish 状态机、幂等重入、取消、删除与清理 |
| `src/application/ingestion/rebuild.py` | 固定 Embedding identity 的受控重建计划 |
| `src/application/retrieval/search.py` | `SearchService`：Space 校验、单路/混合召回、RRF、去重、扩展、精排和降级编排 |
| `src/application/retrieval/dense.py` | Query Embedding Adapter 边界、批处理、超时和顺序保持 |
| `src/application/retrieval/reranker.py` | ModelGateway Reranker Adapter 和响应映射 |
| `src/application/retrieval/profile.py` | 版本化 `RetrievalProfileV1` 配置解析 |
| `src/application/retrieval/evaluation.py` | Evidence/locator 映射、检索指标、失败分类和报告输入 |

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
| `src/infrastructure/blob_store.py` | 本地文件 BlobStore 适配器：写入/读取/删除/存在检测、`store_and_verify`（SHA-256 校验）、路径遍历防护 |
| `src/infrastructure/parsers/` | 文档解析器包：MarkdownParser（`markdown-it-py`）、TxtParser（编码回退）、PdfParser（`pypdf`，可复制文本/扫描件分类）、ParserFactory（扩展名+MIME校验+大小限制） |
| `src/infrastructure/chunkers/structure_chunker.py` | 结构感知分块、标题路径传播、父/邻接 metadata 和 locator 保留 |
| `src/infrastructure/retrieval/postgres_store.py` | 当前发布集合上的 PostgreSQL FTS、pgvector exact/IVFFlat、上下文候选和诊断 |

**`config.py` 详解**：

`Settings` 类从环境变量或 `.env` 文件加载配置：

- **`app_env`** / `app_debug` / `app_secret_key` — 应用基本配置
- **`postgres_*`** — PostgreSQL 连接参数，提供 `database_url` 属性
- **`redis_*`** — Redis 连接参数，提供 `redis_url` 属性
- **`max_upload_size_mb`** — 上传文件大小上限（默认 50 MB）
- **`blob_store_path`** — 本地 Blob 存储根目录（默认 `./data/blobs`）
- **`ingestion_task_*`** — 摄入任务超时、重试、心跳和租约
- **能力级模型配置** — Chat/Embedding/Reranker endpoint、模型 identity、Embedding 指令/
  归一化/精度和外发策略
- **`retrieval_*`** — SearchService 总超时和开发诊断策略
- **`otlp_endpoint`** / `otel_export_timeout_seconds` — 可选 Collector 与有界导出超时
- **`validate_secrets()`** — 生产环境（`app_env=production`）下校验必须密钥不为空，启动失败

全局实例 `settings = Settings()` 可在各模块中直接导入。

**依赖**：`domain`、`application`、`pydantic-settings`、`asyncpg`、`redis`、`dramatiq`、`opentelemetry`

---

### `packages/model_gateway/` — 模型网关层

**职责**：为上层提供统一模型调用接口，屏蔽具体 Provider 差异。

| 文件 | 职责 |
|------|------|
| `contracts.py` | Chat/Embedding/Reranker 类型、能力别名、Protocol、使用量与错误分类 |
| `fake.py` | 确定性 fake 和失败场景 |
| `factory.py` | Provider 选择、endpoint/data policy 校验 |
| `openai_compatible.py` | OpenAI-compatible HTTP Adapter、重试和响应解析 |
| `unavailable.py` | 禁用、配置缺失和策略拒绝实现 |
| `__init__.py` | 稳定公开导出 |

**设计要点**：
- 通过**能力别名**（`fast_chat`、`embedding_zh`、`reranker_multilingual`）引用模型，不散落具体模型名
- 默认使用**确定性 fake**，不需要 API key
- Provider Adapter 封装 SDK/HTTP 类型，不向 application 或 domain 泄漏
- 外部 endpoint 默认禁止，公网外发需要显式策略开关
- 模型状态加入 readiness，但不是 API 启动或本地管理功能的硬依赖

**依赖**：`httpx`、`opentelemetry-api`

阶段 3 已通过同一网关接入查询 Embedding 和可选 Reranker；CI 默认仍使用 fake。真实本地
Embedding/Reranker 仅通过固定镜像、revision 和显式 Compose profile 启动，私有内容不得
绕过 ADR-004 的数据策略发送到外部 Provider。`fast_chat` 当前返回完整响应，不提供阶段 4
所需的 SSE/流式协议。

---

### `packages/agent_runtime/` — Agent Runtime 通用基础

**职责**：实现 ADR-003/ADR-006 固定的单 Agent、声明式、有限状态 Runtime 与版本化 Registry。

| 文件 | 职责 |
| --- | --- |
| `tools.py` | Tool 定义、JSON Schema、显式 handler、权限/Space/预算/审批校验和脱敏调用记录 |
| `skills.py` | 受信目录 Skill manifest、包摘要、版本固定、事务式 reload、原子激活/回滚和恢复兼容检查 |
| `executor.py` | 声明式 workflow、状态迁移、预算预留、有限重试、取消/超时和审计事件 v1 |
| `__init__.py` | 稳定公开导出 |

**信任边界**：只读取配置的受信根目录；拒绝远程 schema、路径逃逸、symlink/junction 和
可执行 entrypoint；manifest 只能引用应用启动时注册的 handler。完整包、workflow、schema 和
prompt 摘要在运行开始时固定。

**当前边界**：该包只完成离线通用工程基础和 fake 契约。没有 AgentRun/Checkpoint ORM、
Runtime API、Web 入口或 `knowledge_qa` 等业务 Skill；活动版本和生命周期事件当前只在进程内。

**依赖**：`domain`、`model-gateway`、`jsonschema`、`packaging`、`pyyaml`

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
   - `/api/v1/spaces/{space_id}/sources*` — 来源创建/列表/详情、上传和触发摄入
   - `/api/v1/tasks/{task_id}*` — 摄入任务状态、取消和重试
   - `POST /api/v1/spaces/{space_id}/search` — Space-scoped Keyword/Dense/Hybrid 检索
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

**OpenAPI**：端点声明 `response_model`；`docs/openapi.json` 由运行时应用确定性导出，当前覆盖
健康、来源/摄入任务和检索 schema。新增或修改公开端点后必须重新导出并运行一致性检查。

**依赖**：`fastapi`、`uvicorn[standard]`、`python-multipart`、`alembic`、`infrastructure`、
`application`、`model-gateway`、`worker`

---

### `apps/worker/` — 后台 Worker

**技术栈**：Dramatiq + Redis

**文件**：

| 文件 | 职责 |
|------|------|
| `src/worker/main.py` | 独立 Worker CLI 入口与优雅停止参数 |
| `src/worker/__main__.py` | 支持 `python -m worker` 启动 |
| `src/worker/broker.py` | Worker 进程的 Redis/Dramatiq broker 初始化 |
| `src/worker/tasks.py` | 无正文诊断任务、有限重试和永久失败回调 |
| `src/worker/ingestion_tasks.py` | 持久摄入任务 actor、Orchestrator 组装、租约/心跳、取消、错误分类、有限重试和死信记录 |

**当前状态**：Redis/Dramatiq 同时承载无正文诊断任务和阶段 2 摄入任务。摄入 actor 执行解析、
分块、Embedding、索引验证和原子发布；PostgreSQL `IngestionTask` 是状态、幂等、取消、租约和
死信事实源，Redis 只投递 `task_id`/`trace_id` 等控制元数据。重复投递、Worker 丢失和取消均按
持久状态恢复，不以日志是否出现作为完成事实。

诊断 actor 的 started/completed 日志在容器中仍有已记录差异；排查应同时检查任务表、Redis
队列和 trace。问答/Agent 长任务尚未接入 Worker，等待阶段 4 ADR-007 和持久化协议。

**依赖**：`dramatiq`、`infrastructure`、`application`、`model-gateway`

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
| `src/App.tsx` | 系统状态/数据来源双视图导航与 TanStack Query 状态编排 |
| `src/SourcesPanel.tsx` | 来源、文档上传、摄入任务状态与操作界面 |
| `src/sources.ts` | 来源和摄入任务 API 客户端及错误映射 |
| `src/health.ts` | 健康接口类型、响应校验、超时和错误分类 |
| `src/App.css` | 根样式 |
| `src/index.css` | 全局样式与设计变量 |
| `src/test/setup.ts` | 测试初始化（@testing-library/jest-dom matchers） |

**当前状态**：Web 将“系统状态”和“数据来源”作为两个独立导航视图。系统状态读取
版本化的 live/ready 接口，展示 API、PostgreSQL、Redis 和模型网关的真实状态及
Trace/Request ID；数据来源读取真实 Source、Document 和 IngestionTask 数据，支持上传、
触发、取消、重试与轮询。两个视图均包含错误/空白/加载状态、键盘焦点和移动端布局，
不展示虚构的文档、会话或证据。

**后续将包含**：
- 阶段 3 检索结果界面（当前只有 HTTP Search API）
- 阶段 4 空间/会话导航、对话运行状态和取消/重试
- 阶段 4 证据与原文查看器、引用高亮和拒答/冲突状态

**规范命令**：
```bash
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
corepack pnpm@10.20.0 --dir apps/web dev
corepack pnpm@10.20.0 --dir apps/web build
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
```

---

## 7. 部署配置 (`deploy/`)

### `compose.yaml`

Docker Compose 编排，定义 5 个基础长期服务、1 个一次性迁移服务和 3 个按 profile 启动的
可选服务：

| 服务 | 镜像 | 关键配置 | 健康检查 |
|------|------|----------|---------|
| **postgres** | `pgvector/pgvector:pg16` | 命名卷持久化数据 | `pg_isready` |
| **redis** | `redis:7-alpine` | AOF + 命名卷 | `redis-cli ping` |
| **migrate** | API 镜像 | PostgreSQL 健康后执行 `upgrade head` | 成功退出 |
| **api** | 本地构建 | 依赖 migrate 成功、Redis 健康 | readiness |
| **worker** | 本地构建 | 依赖 migrate 成功、Redis 健康 | 进程检查 |
| **web** | 本地构建 (nginx) | 依赖 API 健康，同源代理 `/api` | `/healthz` |
| **otel-collector** (可选) | `otel/opentelemetry-collector-contrib` | 需 `--profile otel` 启动 | — |
| **tei** (可选) | 固定 digest 的 TEI CPU 镜像 | `--profile embedding`；固定 `bge-base-zh-v1.5` revision 和命名缓存卷 | `/health` |
| **reranker** (可选) | 固定 digest 的 TEI CPU 镜像 | `--profile reranker`；固定 `bge-reranker-base` revision 和命名缓存卷 | `/health` |

基础 API/Worker/Web 在模型 profile 未启用或模型故障时仍应保持管理面可用；Dense/Reranker
请求按 profile 返回明确错误或受控降级。全新模型卷首次下载仍需在可复现网络环境补证，不能
通过移除 digest、改用 `latest` 或开启外部 Provider 绕过。

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
| `versions/c3d4e5f6a7b8_seed_default_space.py` | **阶段 2 数据迁移**：幂等创建开发/API 使用的默认 Space |
| `versions/d4e5f6a7b8c9_add_chunk_fts.py` | **阶段 3 迁移**：增加持久生成的 Chunk FTS 文档列和 GIN 索引，并保留 pgvector 索引 |

当前只有上述 6 张业务表，没有 Conversation、Message、AgentRun、Evidence、Citation、Feedback
或 Checkpoint 表。阶段 4/5 新表必须等待阶段 0 门禁、ADR-007 和新的 Alembic revision；禁止
修改既有 revision 伪造历史。

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
| 006 | Skill Manifest Versioning And Trust Model | 固定 Skill manifest、摘要、受信目录、权限、恢复和回滚语义 |
| 007 | Grounded QA Persistence, Citation, Execution, And SSE Semantics | 固定唯一 QA Port、引用生命周期、运行/取消、Worker 和 SSE 语义；仅协议已接受 |
| 009 | Redis / Dramatiq Task Delivery | 队列选型 Redis + Dramatiq，状态存 DB |

ADR-007 已接受，但只固定 Stage 4 的 GroundedAnswer/Citation、Conversation/AgentRun/Evidence、
SSE/取消和后台执行协议。ADR-008 仍为保留编号；阶段 4 的持久化、SSE、API 和业务实现仍须等待
阶段 0、阶段 2 Step 9 和阶段 3 正式退出门禁。

### 其他文档

| 文件 | 内容 |
|------|------|
| `development-environment.md` | 开发环境基线：工具版本、Windows 配置、Provider 边界 |
| `project-implementation-plan.md` | 总实施计划 |
| `stage-1-implementation-plan.md` | 阶段 1 详细实施计划与任务清单 |
| `stage-1-acceptance.md` | 阶段 1 验收命令、结果、退出条件、外部确认和已知问题 |
| `stage-2-implementation-plan.md` | 阶段 2 Step 0～8 实现记录和 Step 9 正式质量门禁 |
| `stage-3-implementation-plan.md` | 阶段 3 检索、评测协议、分步实现和正式门禁 |
| `stage-3-acceptance.md` | 阶段 3 工程验收、正式完成清单、holdout Runbook 和阶段 4 移交 |
| `stage-4-implementation-plan.md` | 阶段 4 启动门禁、引用问答协议、分步执行与验收矩阵 |
| `stage-5-implementation-plan.md` | 阶段 5 依赖门禁、分步计划、完成与暂缓状态 |
| `stage-5-implementation-review.md` | 阶段 5 通用基础审查证据、未完成范围和审查决定 |
| `troubleshooting.md` | 本地运行故障恢复和已知限制 |
| `openapi.json` | 由应用确定性导出的公开 HTTP schema |
| `architecture.md` | **本文档** |

---

## 10. 测试 (`tests/`)

### 当前后端测试

```
tests/
├── __init__.py
├── fixtures/                       # Markdown/TXT/PDF 确定性 parser fixture
├── unit/
│   ├── test_parsers.py / test_chunkers.py / test_embedding_service.py / test_ingestion_orchestrator.py
│   ├── test_retrieval_domain.py / test_postgres_retrieval_store.py / test_retrieval_search.py
│   ├── test_gateway_*              # Query Embedding 与 Reranker Adapter
│   ├── test_source_api_helpers.py  # 来源 API 映射和安全边界
│   ├── test_agent_runtime_domain.py
│   ├── test_runtime_executor.py
│   ├── test_skill_registry.py
│   ├── test_skill_lifecycle.py
│   └── 其他阶段 1 配置、DB、错误、健康、OpenAPI、OTel 和 Worker 测试
├── integration/
│   ├── __init__.py
│   ├── test_data_model.py                 # 6 表 CRUD、身份、版本和任务约束
│   ├── test_local_dependencies.py         # pgvector/Alembic/Redis/readiness
│   ├── test_postgres_retrieval_store.py   # FTS、exact/IVFFlat、发布集合和上下文
│   ├── test_search_api.py                 # 四种检索模式、错误/降级和 HTTP schema
│   ├── test_source_api_isolation.py       # Space/Source/Task 越权边界
│   └── test_stage3_retrieval_lifecycle.py # 摄入、重建、原子切换、撤下和再次检索
└── contract/
    ├── test_model_gateway_contract.py  # fake/Adapter 共享契约
    └── test_tool_registry_contract.py  # Tool schema、权限、预算和审批契约
```

默认后端测试覆盖阶段 1 工程基线、阶段 2 摄入、阶段 3 检索和阶段 5 通用 Runtime；真实依赖
集成测试需
显式设置 `RUN_INTEGRATION=1`。覆盖：
- 配置：空密钥在 production 下拒绝启动，development 下跳过
- 错误：Pydantic model、404 统一格式、AppError 结构化响应、未知异常不泄露
- 健康：live 返回 alive、ready 返回 degraded + 机器码 + 不泄露主机信息
- OpenAPI：路径存在、schema 组件完整
- 数据库：结构化 URL、Engine 延迟连接、不可用语义和父 trace 延续
- 可观测性：关联 ID 校验、JSON schema、集中脱敏和错误体/响应头一致性
- Worker：消息无正文、输入校验、幂等执行、超时/重试、入队和 consumer trace
- 摄入：Parser、Chunker、Embedding identity、原子发布、状态机、取消、租约、删除和重建
- 检索：Keyword/Dense/Hybrid/Hybrid+Reranker、RRF、去重、扩展、Space/版本/tombstone、
  exact/IVFFlat、API、评测门禁和失败归因
- ModelGateway：共享 Chat/Embedding/Reranker 契约、能力别名、确定性 fake、有限重试、结构解析、
  endpoint 策略、显式不可用状态及输入/输出不进入日志或 span
- Agent Runtime：状态/步骤分离、终态、预算、Tool/Skill schema、受信路径、版本固定、
  声明式执行、权限、有限重试、取消/超时、审计脱敏和原子 reload/回滚

前端 Vitest 覆盖系统健康、数据来源、上传/触发、任务轮询/取消/重试、API 不可达、非法响应、
有界超时和键盘焦点。当前没有阶段 3 搜索 UI 或阶段 4 会话/引用 UI 测试。

所有真实依赖集成测试必须指向隔离 PostgreSQL/Redis 和 Blob 根，禁止复用含业务数据的本地卷；
CI 使用独立服务运行集成套件。测试数量不在本文档固定，以测试收集结果和阶段验收记录为准。

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

tests / future composition root ──→ agent-runtime ──┬──→ domain
                                                     └──→ model-gateway
```

`agent-runtime` 尚未被 API、Worker 或 Application 业务用例引用；阶段 2/3 已提供摄入与检索
Application Port，剩余接线等待阶段 4 Grounded QA、Conversation/AgentRun/Evidence 持久化及
SSE/取消协议，再由阶段 5 Step 5～7 封装业务 Skill。

依赖来源（通过各包的 `pyproject.toml`）：

| 包 | 依赖 |
|----|------|
| `api` | `application`、`infrastructure`、`model-gateway`、`worker`、`alembic`、`fastapi`、`python-multipart`、`uvicorn` |
| `worker` | `application`、`infrastructure`、`model-gateway`、`dramatiq` |
| `application` | `domain` |
| `infrastructure` | `domain`、`application`、PostgreSQL/pgvector、Redis/Dramatiq、Parser 和 OpenTelemetry Adapter 依赖 |
| `model-gateway` | `httpx`、`opentelemetry-api` |
| `agent-runtime` | `domain`、`model-gateway`、`jsonschema`、`packaging`、`pyyaml` |
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
uv run alembic upgrade head         # 升级真实数据库
uv run alembic upgrade --sql head   # 脱机生成迁移 SQL
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
```

### 前端

```bash
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
corepack pnpm@10.20.0 --dir apps/web dev
corepack pnpm@10.20.0 --dir apps/web build
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
```

### Docker

```bash
docker compose -f deploy/compose.yaml up --build --detach --wait  # 启动并等待健康
docker compose -f deploy/compose.yaml --profile otel up --detach  # 含 OTel
docker compose -f deploy/compose.yaml down                         # 停止并保留数据
docker compose -f deploy/compose.yaml down --volumes               # 仅确认后永久删除项目数据
```

---

## 附录：阶段状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| 阶段 0 | 🔶 进行中 | 语料授权、人工标注复核和版本冻结未完成；manifest 仍为 `draft_pending_license_review` |
| **阶段 1** | **✅ 完成** | **Step 0-8 验收完成；GitHub Actions 正常** |
| **阶段 2** | **🟡 工程 Step 0～8 完成** | **摄入闭环代码已落地；Step 9 正式质量验收和 `stage-2-acceptance.md` 尚未完成** |
| **阶段 3** | **🟡 工程 Step 0～10 验收完成** | **检索 API、离线评测和安全边界已落地；阶段 0、阶段 2 Step 9、真实模型定版及正式 holdout 未关闭，阶段未正式退出** |
| 阶段 4 | ❌ 未正式开始 | ADR-007、provisional 配置和纯领域契约已落地；Application 问答、持久化、SSE、Web 和回答评测未落地 |
| **阶段 5** | **🟡 通用基础已审查** | **Step 0～4 和 Step 9 通用部分通过；业务 Skill/API/持久化/验收仍阻塞** |

阶段 1 已完成本地验收：Step 0（启动决策）✅、Step 1（工具链）✅、Step 2（API 与错误协议）✅、Step 3（DB 迁移与 Worker）✅、Step 4（可观测性）✅、Step 5（ModelGateway）✅、Step 6（Web 工作台）✅、Step 7（Compose/CI）✅、Step 8（验收与移交）✅

阶段 2 Step 0～8 已完成工程实现与本地验证，尚不代表 Step 9 正式质量验收：

- **Step 0/1**：ADR-005 固定身份、版本、发布、任务和删除语义；Space、Source、Document、DocumentVersion、Chunk、IngestionTask 的领域实体、ORM 模型、仓库实现及迁移已完成，R2-01～03 已关闭。
- **Step 2**：`ParsedDocument` 纯类型 schema、`Parser` Port、Markdown/TXT/可复制文本 PDF
  ParserFactory 和统一错误分类。
- **Step 3**：内容指纹、`BlobStore`、`LocalFileBlobStore` 和 `SourceRegistrationService`。
- **Step 4**：结构感知 `StructureChunker`、统一 `ChunkOutput` 和父/邻接/locator metadata。
- **Step 5**：Embedding INDEX → VALIDATE → 原子 PUBLISH 流水线和版本 identity。
- **Step 6**：`IngestionOrchestrator` 状态机、Dramatiq actor、租约/心跳、幂等重入、取消和死信。
- **Step 7**：内容不变跳过、路径更新、原子撤下、异步删除和清理。
- **Step 8**：摄入 API（8 个端点：创建/列举来源、上传、触发摄入、状态查询、取消、重试）+ Web 数据源页面（来源列表、任务进度、上传/重试/取消 UI）。

阶段 3 已完成工程验收：

- **Step 0-1**：冻结评测协议和 `RetrievalStore`/`SearchResult`/诊断契约，固定 Space、当前发布版本、tombstone 和 768 维边界。
- **Step 2-7**：本地 Embedding 能力配置、FTS/pgvector 双路召回、加权 RRF、去重与相邻块扩展、可选 Reranker 及故障降级策略。
- **Step 8**：`POST /api/v1/spaces/{space_id}/search`、OpenAPI、日志和 trace 的隐私边界。
- **Step 9**：版本化离线评测 CLI、报告 schema、失败归因和 provisional 门禁；正式 holdout 被明确阻断。
- **Step 10**：隔离依赖、检索模式、安全边界和文档移交的验收记录见 `docs/stage-3-acceptance.md`。

阶段 5 通用基础审查见 `docs/stage-5-implementation-review.md`。该并行实现不改变主推进顺序：
仍应先完成阶段 4 引用问答，再接入阶段 5 业务 Skill。

阶段 0 关闭后不能直接运行 holdout；必须先按 `docs/stage-3-acceptance.md` 完成阶段 2 Step 9、
真实模型 development 消融、默认配置冻结和一次性正式 holdout。GitHub Actions 已由用户确认
运行正常，阶段 0 数据授权、人工标注复核和版本冻结仍为等待状态。
