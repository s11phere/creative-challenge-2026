# Agent 驱动的个人知识仓库

## 2026-08-03 Implementation Status

Stages 0-5 engineering capabilities are implemented under the current contracts. Stage 4 includes
the complete QA/Citation/API/Web/Worker path and a Space-scoped feedback review queue. Stage 5
includes durable Runtime/approval/derived-knowledge/Skill lifecycle behavior and the controlled
`scripts/export_feedback_candidates.py` command. These implementation results do not change the
Stage 3 termination record or claim formal retrieval, answer, or Skill holdout acceptance.

本项目面向个人学习、科研和开发资料，目标是构建一个本地优先、来源可追溯的知识工作台。
规划中的完整闭环包括文档摄入、增量索引、混合检索、带引用问答和可版本化 Agent Skill。

## 当前状态

**阶段 0、阶段 1 和阶段 2 已正式完成；阶段 3 Step 0-10 的工程实现已完成，但正式质量门禁未通过。PR #3/v1 GPU development 复现出 provisional Claim Recall@10=69.7548%，因当前评测集代表性局限，阶段 3 已按 [ADR-010](docs/adr/010-stage-3-termination-and-evaluation-boundary.md) 保持正式未通过；正式 holdout 未执行，配置仍未冻结。根据 [ADR-011](docs/adr/011-provisional-stage-4-5-continuation-gate.md)，当前结果只允许阶段 4/5 继续 provisional 工程，不构成正式质量接受；阶段 4/5 正式质量门禁仍未关闭。**

已交付的核心能力：

| 层级 | 内容 |
|------|------|
| 阶段 1 ✅ | 工程骨架：FastAPI、Worker、Web 工作台、PostgreSQL/pgvector、Redis、Alembic、模型网关、结构化日志、OpenTelemetry、Compose、CI |
| 阶段 2 ✅ | 摄入工程 Step 0-8 与正式 Step 9 验收完成；冻结 manifest 中 74 个 P0 来源解析/定位/分块成功率 100%，幂等、原子发布、删除恢复、API/Web 和 Compose E2E 通过 |
| 阶段 3 ⏹️ 已终止（工程完成，质量门禁未通过） | PostgreSQL FTS/pgvector 检索、加权 RRF、上下文扩展、可选 Reranker、Space/版本安全边界、检索 API、版本化离线评测与集成验收已完成；PR3 GPU development Claim Recall@10 为 69.7548%，当前评测集代表性不足，正式 holdout 未执行，配置保持 provisional |
| 阶段 4 🟡 provisional Step 0-10 | 在 ADR-011 continuation gate 下继续；ADR-007、唯一 provisional QA Application Port、Grounded QA/Evidence/Citation、PostgreSQL Repository/SSE、问答 API、Web、Worker 重启恢复、按需原文解析和回答评测 validate-only 已完成；默认配置与正式 holdout 未完成 |
| 阶段 5 🟡 provisional Skills | Step 0-10 工程功能已完成；当前 active 为 `knowledge_agent 0.2.0`（保留 `0.1.0` 回滚包）、`knowledge_qa 0.1.0` 与三个知识整理 Skill `0.1.0`，统一复用持久 QA Run/Worker/SSE；正式质量仍 provisional |

当前 Web 展示系统健康、数据来源和 provisional 知识问答工作区；HTTP API 可创建持久会话、提交
问题，由 API 仅向 Redis 投递 Run ID，再由独立 Worker 调用唯一 `GroundedQAApplicationPort`、
真实 PostgreSQL `SearchService` 和
Citation target adapter 生成回答或拒答。默认 fake 模型提供确定性抽取式回答；配置允许的外部
Chat Provider 仍走相同结构化生成与引用校验路径。Web 会展示终态回答、限制和文档版本/locator
引用身份；点击 Citation 后按 `run_id + evidence_id` 解析固定版本的最小原文片段并高亮。

该链路是可真实使用的 provisional 版本，不是阶段 4/5 正式完成：QA 会话、Message、Run/Attempt、
Evidence、Citation、Feedback、SSE 事件和 Worker lease 已写入 PostgreSQL；API/Worker 重启可恢复
未完成运行，重复投递不会重复发布终态；Citation 原文解析不会接受客户端伪造的版本、locator 或 Blob 路径；
阶段 3 默认检索配置和质量门禁也尚未冻结。
阶段 5 的 `knowledge_qa 0.1.0` 已从本地受信根显式激活：API 在新 QA Run 中固定 Skill 名称、
版本和内容摘要，Worker 恢复时按该固定身份校验声明式 workflow，再调用唯一 QA Application Port。
现有 QA Web/API 因此已是该 provisional Skill 的真实入口；统一 `/api/v1/runs` facade、PostgreSQL
Runtime Checkpoint、Skill 管理 Web 和受控旧版本清理均已提供。`/api/v1/skills` 可查询 active/已安装版本、
摘要、预算和 pointer revision，并通过受控 CAS 接口激活或回滚到受信根中已安装的版本；pointer
保存在 PostgreSQL，API 重启后恢复。阶段 5 工程功能已完成；正式 Skill Eval 和阶段退出仍受
Stage 3/4 质量门禁约束，不能把 provisional 结果写成正式质量通过。
`summarize_document`、`compare_sources` 和 `create_review_cards` 也已提供 provisional HTTP
入口并固定提交时的 Source/Document/DocumentVersion 范围；版本变更、撤下或跨 Space 选择不会
扩大检索范围。比较结果必须引用至少两个来源，否则按证据不足拒答。复习卡当前只返回带引用预览，
并以 `SKILL_WRITE_REQUIRES_APPROVAL` 明确报告审批前 `side_effects=0`；批准后通过持久化
Derived Knowledge Port 幂等写入，并可查询或撤销。
`knowledge_agent 0.2.0` 提供真实的受约束 LLM/Tool 循环：模型可先调用只读
`inspect_retrieval 1.0.0` 调整多查询、候选数和上下文预算，再调用一次 `grounded_qa 1.0.0`，
由现有 QA Run、Worker、SSE、Grounded QA Port 和 Citation 链路完成问答。动态数值由服务端
profile 封顶，Space/版本边界不能由模型扩大；规划失败会降级到原问题的 Grounded QA，而不是
把 Run 变成基础设施失败。Tool 仅向外层模型返回状态和覆盖计数，不返回回答或原文。旧
`0.1.0` 保留用于固定 Run 恢复和回滚。默认 fake 可跑通流程，配置允许的
OpenAI-compatible `fast_chat` Provider 会执行真实模型决策。写 Tool 仍被明确拒绝。
真实本地组合使用外部 OpenAI-compatible `fast_chat`、
`EMBEDDING_PROVIDER=text-embeddings-inference` 和本地 Qwen3 TEI Embedding；当前
`RERANKER_PROVIDER=fake`。三项能力独立路由，Embedding 不会随外部 Chat 回退为 fake。
阶段 0 已冻结为 `internal_team_only`，原始语料和评测 JSONL 仍只在组员本地保留；退出证据见
[Stage 0 验收记录](docs/stage-0-acceptance.md)，摄入退出证据见
[Stage 2 验收记录](docs/stage-2-acceptance.md)。不要直接运行 holdout；历史 `90.48%` Recall@5 结果已判定为虚假，当前 PR3 GPU development Claim Recall@10=69.7548%，仍未达到正式门禁。阶段 3 已终止；若未来重新开启，必须发布新的 dataset/config version 并重新走评测流程。

## 快速启动

前置条件：Docker Engine 29+ 和 Docker Compose 5+。本机不需要单独安装 PostgreSQL 或 Redis。

> **GPU 加速（默认）**：Embedding 服务默认使用 GPU 加速，需要：
> - NVIDIA 驱动（支持 CUDA 12.2+）
> - [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
> - 安装后验证：`docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`
>
> **无 GPU？使用 CPU 回退**：添加 `-f deploy/compose.cpu.yaml` 即可切换到 CPU 版本：
> ```bash
> docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml --env-file .env \
>   --profile embedding up --build --detach --wait
> ```

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

真实模型组合需要在被 Git 忽略的 `.env` 中同时配置外部 Chat、
`EMBEDDING_PROVIDER=text-embeddings-inference`、`EMBEDDING_ENDPOINT=http://tei:80`、固定的
Qwen3 Embedding 模型/revision，以及适配 TEI 限制的 `EMBEDDING_BATCH_SIZE`。完整字段见
`.env.example`，凭据不得提交仓库。

> **注意**：Docker Compose v5 可能需要显式指定 `--env-file .env`；若不加也能正常运行则无需此参数。

3. 打开工作台：<http://127.0.0.1:5173>

首次构建需要下载锁定 digest 的基础镜像和依赖。Compose 会依次等待 PostgreSQL、迁移、Redis、
API、Worker 和 Web 达到各自完成或健康条件。

> 首次启动 Embedding 模型服务（`--profile embedding`）时，TEI 会从 HuggingFace Hub
> 自动下载 Qwen3-Embedding-0.6B（约 400 MB）。模型文件会缓存在 Docker 层面，后续启动
> 无需重下载。中国用户可参考 [模型下载文档](docs/model-setup.md) 使用镜像源加速。
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
| Embedding | `127.0.0.1:8080` | Qwen3-Embedding-0.6B（TEI），需 `--profile embedding` |

端口可通过 `.env` 中的 `WEB_PORT`、`API_PORT`、`POSTGRES_PORT`、`REDIS_PORT` 和 `EMBEDDING_PORT` 覆盖。

Embedding（Qwen3-Embedding-0.6B）服务需通过 `--profile embedding` 显式启动。首次启动时，TEI 会自动从 HuggingFace Hub 下载模型（缓存至 Docker 层面，后续启动无需重下载）。

**GPU 模式（默认）：**
```bash
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding up --build --detach --wait
```

**CPU 模式（无 GPU 时）：**
```bash
docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml --env-file .env \
  --profile embedding up --build --detach --wait
```

模型启动后，健康检查输出应显示 `embedding` 处于 `healthy` 状态。可以通过 `docker compose ps` 确认。

可通过 `.env` 中的 `EMBEDDING_QUERY_INSTRUCTION_VERSION` 和 `EMBEDDING_DOCUMENT_INSTRUCTION_VERSION` 切换 embedding 指令前缀版本（详见 `.env.example` 注释）。

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
- [阶段 5 provisional 验收记录](docs/stage-5-acceptance.md)
- [阶段 4/5 收尾看板](docs/stage-4-5-completion-tracker.md)
- [阶段 3 v1 development 复核记录](docs/stage-3-reopen-development-v1.md)
- [阶段 3 终止后的阶段 4/5 收尾计划](docs/post-stage-3-stage-4-5-completion-plan.md)
- [OpenAPI](docs/openapi.json)
- [架构决策记录](docs/adr/README.md)

## 安全与数据边界

- 密钥只从环境变量或被 Git 忽略的 `.env` 读取，不得提交或写入日志。
- 默认模型为确定性 fake；外部 Provider 必须显式配置并允许外发。
- 只允许处理 `cases/evals/corpus/v0/manifest.yaml` 明确列出的来源，并在读取前校验 SHA-256。
- 阶段 0 语料仅在 manifest 允许列表内使用；未确认外部授权的来源仍限于组员本地，禁止 Git 上传、公开演示和外部 Provider 外发。

问题恢复步骤和当前限制见[故障排查文档](docs/troubleshooting.md)。
