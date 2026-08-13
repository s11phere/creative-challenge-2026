# Agent 驱动的个人知识仓库

本地优先、来源可追溯的个人知识工作台：摄入个人文档 → 增量索引 → 混合检索 → 带引用问答 →
可版本化 Agent Skill。当前工程能力在临时质量边界内可真实使用；正式质量门禁状态见
[当前状态](#当前状态)。

## 目录

- [当前状态](#当前状态)
- [快速启动](#快速启动)
- [使用](#使用)
  - [知识问答与 Assistant 对话](#知识问答与-assistant-对话)
  - [命令与 Skill](#命令与-skill)
  - [个性化：记忆、个人 Skill 与自动提取](#个性化记忆个人-skill-与自动提取)
  - [本地 Agent 工作区](#本地-agent-工作区)
  - [评测与诊断脚本](#评测与诊断脚本)
- [Smoke Test](#smoke-test)
- [停止与清理](#停止与清理)
- [开发命令](#开发命令)
- [服务与端口](#服务与端口)
- [文档](#文档)
- [安全与数据边界](#安全与数据边界)
- [附录：实现历史](#附录实现历史)

## 当前状态

**阶段 0、1、2 已正式完成；阶段 3 工程实现完成但按 [ADR-010](docs/adr/010-stage-3-termination-and-evaluation-boundary.md)
终止（正式质量门禁未通过）；阶段 4/5 在 [ADR-011](docs/adr/011-provisional-stage-4-5-continuation-gate.md)
继续门禁下为临时工程能力。正式 retrieval/answer/Skill 留出集均未执行，**不构成正式质量接受**。
**

| 层级 | 内容 |
|------|------|
| 阶段 1 ✅ | 工程骨架：FastAPI、Worker、Web 工作台、PostgreSQL/pgvector、Redis、Alembic、模型网关、结构化日志、OpenTelemetry、Compose、CI |
| 阶段 2 ✅ | 摄入 Step 0-8 与正式 Step 9 验收完成；冻结 manifest 中 74 个 P0 来源解析/定位/分块成功率 100%，幂等、原子发布、删除恢复、API/Web/Compose E2E 通过 |
| 阶段 3 ⏹️ 已终止 | PostgreSQL FTS/pgvector 检索、加权 RRF、上下文扩展、可选 Reranker、Space/版本安全边界、检索 API、版本化离线评测已完成；默认 `dense_rerank`，PR #4 GPU 开发集 Claim Recall@10 为 82.37%/78.75%，但评测集代表性不足，正式留出集未执行 |
| 阶段 4 🟡 临时 | Grounded QA/Evidence/Citation、PostgreSQL Repository/SSE、问答 API、Web、Worker 重启恢复、按 Space 隔离的反馈审核队列已完成；默认配置与正式留出集未完成 |
| 阶段 5 🟡 临时 | Assistant 对话演进、自主可恢复 Skill/Tool Loop、有界上下文、指标、最终回答生成器、调用记录卡、持久化审批与派生知识已完成；知识请求固定 `knowledge_agent 1.0.0`，正式 Skill 评测未关闭 |

新 Run 统一走 **Agent Harness v2 native Tool-use**（`agent-run-sse-v4` 时间线），是当前唯一
Assistant 执行路径。旧 v1 记录不会被当前执行或恢复逻辑解释。这不改变 ADR-010/011 正式质量边界，
也不授权任何 formal holdout。

旧 Skill 激活/回滚/清理接口、旧 Prompt 与 `knowledge_qa` 恢复适配器均已移除；`GET /api/v1/skills`
只返回当前安装的只读 Skill 清单。

## 快速启动

前置条件：Docker Engine 29+ 和 Docker Compose 5+。本机不需要单独安装 PostgreSQL 或 Redis。

> **GPU 加速（默认）**：Embedding 服务默认使用 GPU 加速，需要 NVIDIA 驱动（CUDA 12.2+）和
> [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)。
> 无 GPU 时加 `-f deploy/compose.cpu.yaml` 切换到 CPU 版本。

1. 创建本地环境文件并填入两个必填项：

```bash
cp .env.example .env
# 编辑 .env：设置 APP_SECRET_KEY 与 POSTGRES_PASSWORD（其他保持默认即可快速体验）
```

2. 构建并启动完整本地栈（默认使用确定性的 fake provider）：

```bash
docker compose -f deploy/compose.yaml --env-file .env up --build --detach --wait
```

要在 Web QA / `knowledge_agent` 中使用真实 Chat provider 和完整本地 GPU 检索路径，显式启动两个
模型 profile：

```bash
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding --profile reranker up --build --detach --wait
```

> `text-embeddings-inference` 不是 Chat provider。外部 Chat 需设置 `MODEL_PROVIDER=openai-compatible`、
> `FAST_CHAT_ENDPOINT`、`FAST_CHAT_MODEL`、`MODEL_ALLOW_EXTERNAL=true`，同时保持
> `EMBEDDING_PROVIDER=text-embeddings-inference`。默认 `dense_rerank` 路径需设置
> `RERANKER_PROVIDER=inherit`、`RERANKER_ENDPOINT=http://tei-reranker:80`、
> `RERANKER_MODEL=BAAI/bge-reranker-v2-m3`。修改 `.env` 后重新创建 `api` 和 `worker` 服务。

3. 打开工作台：<http://127.0.0.1:5173>

首次构建需下载锁定 digest 的基础镜像；首次启动 Embedding 服务时 TEI 会从 HuggingFace Hub 下载
Qwen3-Embedding-0.6B（约 400 MB，缓存到命名卷，中国用户可参考
[模型下载文档](docs/model-setup.md) 用镜像源加速）。在「数据来源」上传并等待文档发布后，进入
「知识问答」即可使用当前 Space 的真实索引提问。

## 使用

### 知识问答与 Assistant 对话

- 新知识请求统一固定为 `knowledge_agent 1.0.0`：API 与 Assistant 主路径只会创建该 Skill 的 Run，
  提交时固定名称/版本/内容摘要，Worker 恢复时按固定身份校验。
- 模型仅可调用服务端注册的工具：`knowledge_search`、`knowledge_inspect`、`summarize_document`、
  `grounded_answer`、`verify_answer`、`finalize_answer`。规划失败会降级到原问题的 Grounded QA，
  而不是把 Run 变成基础设施失败；Tool 只返回状态与覆盖计数，不返回回答或原文。
- 服务端 `research-grounded-answer-v2` 契约：单篇精读和多篇综述不再只依靠格式提示，多篇综述必须
  包含逐篇摘要、带引用证据矩阵、主题综合段，以及共识、条件差异、冲突、证据空白与局限。
- Web 为每次 Skill 调用保留默认折叠的调用记录卡；存在证据时提供引用操作，`run_id + evidence_id`
  解析固定版本的最小原文片段并高亮。默认 fake 模型返回确定性抽取式回答，可复现、适合先跑通流程。

### 命令与 Skill

输入 `/` 或点击「指令与技能」打开按「指令/技能」分组的可视化选择面板（可搜索、键盘可操作）。

| 命令 | 作用 |
| --- | --- |
| `/ask`、`/summarize`、`/compare`、`/cards` | 显式调用对应 v2 Skill（固定当前 Space 资源、版本 pin、权限） |
| `/research` | `research_reading_workflow 1.1.0`（provisional）论文精读/文献综述 |
| `/create-skill`（别名 `/skill`） | 进入 Skill Creator 引导流程，经草稿→校验→eval 门禁→审批→激活 |
| `/effort` | 读取/设置后续 Run 的 reasoning effort（`low|medium|high|xhigh|max`，菜单只提供这五个值） |
| `/compact` | 通过 Worker 队列创建可持久化的上下文压缩 Run |
| `/workspace`、`/ws` | 选择本地 Agent 工作区（见下） |
| `/help`、`/skills`、`/new`、`/stop` | 无业务 Run 的命令结果 |

Assistant 输出支持 GFM Markdown 与 LaTeX 渲染；每个 Run 保存 provider-neutral 的
requested/effective effort、Provider/model 与降级原因。

### 个性化：记忆、个人 Skill 与自动提取

- **跨会话长期记忆（Phase 5）**：Worker 从会话摘要 + 使用模式用 LLM 蒸馏持久
  fact/preference/pattern 写入 `memory_entries`，新回合按当前问题做向量 + 近因加权检索注入有界
  `<long-term-memory>` 块（top-K 5）；restricted 内容不注入、不向更低敏感度泄漏。蒸馏显式触发：

  ```powershell
  uv run python scripts/distill_memories.py --json        # 本地
  uv run python scripts/distill_memories.py --enqueue     # 调度到 Worker
  ```

- **个人 Skill（Phase 3/4）**：用户可写自己的 Skill（`PERSONAL_SKILLS_DIR`，默认
  `./data/personal_skills`），只组合既有 handler/tool、不得覆盖内置 Skill 名（ADR-018）；
  Skill Creator 走 `draft → 校验 → eval 门禁 → 用户审批 → active` 生命周期，
  `/api/v1/skills/personal/drafts` 提供 CRUD/validate/eval/activate。
- **自动提取工作模式（Phase 6，Path B）**：从使用痕迹自动提议个人 skill，**绝不自动激活**——
  每次回合结束 Worker 自动调度挖掘（Redis 节流），经双闸验证（结构化门禁 + 历史锚定）后进入
  SkillsPanel 草稿区并附 `evidence.json`，激活仍需用户审批：

  ```powershell
  uv run python scripts/extract_skill_candidates.py --json
  ```

- **使用痕迹记录（Phase 2）**：每次 Skill/Assistant 回合落一条脱敏 `usage_traces`，按需蒸馏
  `usage_patterns`，为记忆与自动提取备料（只记录、不注入）：

  ```powershell
  uv run python scripts/query_usage_traces.py --limit 20
  uv run python scripts/distill_usage_patterns.py --json
  ```

### 本地 Agent 工作区

对话可通过 `/workspace <folder>` 选择 `AGENT_WORKSPACE_ROOT_PATH` 下的现有目录。选中后 `fs_list`、
`fs_read` 可用；`fs_write`、`shell_exec` 需持久审批。工作区独立于上传的知识；`MODEL_PROVIDER=fake`
默认注册工作区工具，非 fake Provider 需显式 `AGENT_WORKSPACE_MODEL_VISIBILITY_CONSENT=true`（或用
`.\scripts\start-local.ps1 -AllowExternalWorkspaceTools` 一次性开启）。工具路径/cwd 以工作区为根，
展示相对路径；审批可一次允许、拒绝或「本次会话始终允许」某工具类型。详见
[开发环境文档](docs/development-environment.md#local-agent-workspaces) 与
[ADR-016](docs/adr/016-conversation-workspace-tools.md)。

### 评测与诊断脚本

以下命令只读取 hash 固定的合成 fixture 或当前库，不调用 Provider、不读取受控语料、不启用 formal
holdout；报告均标记为 development / provisional。

```powershell
uv run --frozen python scripts/evaluate_agent_harness_v2.py --validate-only   # Agent Loop 合成校验
uv run python scripts/evaluate_skills.py --all --output tmp/skill-eval.json   # Skill eval 门禁
uv run python scripts/evaluate_assistant_routing.py --validate-only           # Assistant 路由开发集
uv run python scripts/evaluate_query_rewrite.py --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml   # LLM 查询改写 A/B
```

## Smoke Test

### 一键启动 GPU

当 `.env` 中包含已批准的 Chat 端点和密钥时，可执行 `.\scripts\start-local.ps1` 启动完整 GPU 栈：
脚本检查 Docker GPU 透传、启动 `embedding` 与 `reranker` profile、强制 reranker 用
`BAAI/bge-reranker-v2-m3` 的 `inherit`，并将新 Run 固定到 `knowledge_agent 1.0.0`。脚本不修改
`.env`、不打印密钥；外部 Chat 端点可能接收问题和检索片段，未完成策略审批前不要将私有/受限来源
用于该路径。

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:5173/api/v1/health/ready
docker compose -f deploy/compose.yaml --env-file .env ps
```

预期 `live` 返回 `alive`，`ready` 返回 `ready`；模型能力按实际配置报告 `MODEL_FAKE_READY` 或
`MODEL_CAPABILITY_CONFIGURED`。

### 浏览器 E2E（Playwright）

Web 核心旅程的浏览器级 E2E 跑在**真实 Compose 栈**上（默认 `MODEL_PROVIDER=fake`，断言全部是
确定性的 fake-provider 输出）。先起栈再跑：

```powershell
docker compose -f deploy/compose.yaml --env-file .env up --build --detach --wait
corepack pnpm@10.20.0 --dir apps/web exec playwright install chromium
corepack pnpm@10.20.0 --dir apps/web test:e2e
```

国内网络若 Playwright 官方 CDN 下载 Chromium 卡住，可用 npmmirror 镜像：
`$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"` 后重装。

- 目标地址 `http://127.0.0.1:5173`，可用 `PLAYWRIGHT_BASE_URL` 覆盖；不要与 Vite dev（也占 5173）
  同时运行。
- 覆盖健康面板、问候终态回答与运行卡片、空会话/错误状态、键盘提交与命令面板、移动视口冒烟。
- 隐私：默认关闭截图/录屏，只保留失败 trace；prompt 与断言均为合成内容。
- 已知限制：空 Space 知识问题的 fake 路径目前不终止（既有缺陷，见
  `docs/stage-4-5-step4-web-e2e.md` 的 Known defect），核心套件暂不断言该路径。

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
corepack pnpm@10.20.0 --dir apps/web typecheck:e2e
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web test:e2e
corepack pnpm@10.20.0 --dir apps/web build
```

迁移与 OpenAPI：

```powershell
uv run alembic upgrade head
uv run alembic current
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
```

提交前质量门禁（每克隆执行一次）：

```bash
git config core.hooksPath .githooks
```

启用后，`git commit` 前自动复现 CI 的快速门禁：`ruff format --check .`、`ruff check .`、
`mypy apps packages`、OpenAPI 导出无差异；改动 `apps/web/` 时另跑 `pnpm lint`、`pnpm typecheck`
与 `pnpm typecheck:e2e`。完整 pytest、集成与 Compose smoke 仍由 CI 执行。临时跳过可用
`git commit --no-verify`（不推荐）。

真实依赖集成测试由 CI 在隔离 PostgreSQL/Redis 中运行。手动运行前必须配置隔离依赖并设置
`RUN_INTEGRATION=1`，不要指向包含业务数据的数据库。

## 服务与端口

| 服务 | 默认地址/端口 | 说明 |
| --- | --- | --- |
| Web | `http://127.0.0.1:5173` | nginx 静态托管并代理同源 `/api` |
| API | `http://127.0.0.1:8000` | `/api/v1` 兼容接口；`/api/v2` 提供 Assistant 对话、`/commands`、Skill 路由、取消和无正文 SSE |
| PostgreSQL | `127.0.0.1:5432` | PostgreSQL 16 + pgvector |
| Redis | `127.0.0.1:6379` | Dramatiq broker，启用 AOF |
| OTel Collector | `4317`、`4318` | 仅 `--profile otel` 启动 |
| Embedding | `127.0.0.1:8080` | Qwen3-Embedding-0.6B（TEI），需 `--profile embedding` |
| Reranker | `127.0.0.1:8081` | BAAI/bge-reranker-v2-m3（TEI），需 `--profile reranker` |

端口可通过 `.env` 中的 `WEB_PORT`、`API_PORT`、`POSTGRES_PORT`、`REDIS_PORT`、`EMBEDDING_PORT`、
`RERANKER_PORT` 覆盖。Embedding 服务需 `--profile embedding` 显式启动（首次从 HuggingFace Hub
下载模型，缓存到命名卷）：

```bash
# GPU 模式（默认）
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding up --build --detach --wait
# CPU 模式（无 GPU）
docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml --env-file .env \
  --profile embedding up --build --detach --wait
```

## 文档

- [架构概览](docs/architecture.md)
- [开发环境与 Provider 边界](docs/development-environment.md)
- [故障排查与已知限制](docs/troubleshooting.md)
- [知识问答：Grounded QA 持久化、引用、执行与 SSE（ADR-007）](docs/adr/007-grounded-qa-persistence-and-sse.md)
- [Conversation Workspace Tools（ADR-016）](docs/adr/016-conversation-workspace-tools.md)
- [knowledge_agent 证据扩池实验报告](docs/knowledge-agent-evidence-expansion-report.md)
- [阶段 4/5 收尾看板](docs/stage-4-5-completion-tracker.md)
- [阶段 3 终止后的阶段 4/5 收尾计划](docs/post-stage-3-stage-4-5-completion-plan.md)
- [实现历史与阶段记录（附录）](#附录实现历史)
- [架构决策记录](docs/adr/README.md)
- [OpenAPI](docs/openapi.json)

各阶段验收记录与实施计划见 `docs/stage-*-acceptance.md`、`docs/stage-*-implementation-plan.md`。

## 安全与数据边界

- 密钥只从环境变量或被 Git 忽略的 `.env` 读取，不得提交或写入日志、trace、评测报告。
- 默认模型为确定性 fake；外部 Provider 必须显式配置并允许外发（`MODEL_ALLOW_EXTERNAL`）。
- 只允许处理 `cases/evals/corpus/v0/manifest.yaml` 明确列出的来源，并在读取前校验 SHA-256 与
  `content_sha256` 一致；文档内容不可信，不能借由内容提升工具权限或覆盖系统指令。
- 阶段 0 语料为 `internal_team_only`：原始语料和评测 JSONL 只在组员本地保留，禁止 Git 上传、
  公开演示和外部 Provider 外发；`private_local`/`restricted` 内容默认不得离开本地。
- 结构化指标日志只含指标名称、安全标签和聚合值，不包含对话内容、prompt、文档正文、Provider
  响应或内部资源 ID。

问题恢复步骤见[故障排查文档](docs/troubleshooting.md)。

## 附录：实现历史

> 以下是 2026-08 各阶段的工程实现记录，保留为历史背景；其中出现的旧版本、回滚开关和兼容入口
> 已不再是当前可用配置。当前能力以本文档正文为准，详细证据见
> [阶段 4/5 收尾看板](docs/stage-4-5-completion-tracker.md) 和各阶段验收记录。

- **2026-08-13 · Playwright web E2E core journey**：新增 `apps/web/e2e/` 浏览器级 E2E（真实
  Compose 栈 + fake provider），覆盖健康面板、问候终态、键盘/命令面板、移动视口；CI
  `compose-smoke` 扩展为 "Compose smoke + web E2E"。实跑发现并修复 fake 知识循环的空 Space 无限
  重发缺陷（有界检索），`knowledge_answer` 终态 schema 校验仍待专项修复（见
  `docs/stage-4-5-step4-web-e2e.md`）。
- **2026-08-13 · Native Tool-use v2 only**：移除旧 v1 文本-JSON 执行器；新 Run 统一
  `assistant-native-tool-use-v2` + `agent-run-sse-v4`。
- **2026-08-11 · Conversation-scoped workspace tools（ADR-016）**：`/workspace` 本地工作区、
  文件/命令工具、持久审批。
- **2026-08-10 · Autonomous assistant loop**：可恢复的顶层 Agent Loop，串行组合 Skill/Tool。
- **2026-08-06 · LLM 查询改写**：`rewrite_enabled=true`、`max_subqueries=4`（dev 上 context
  coverage@32 +20.5pp、0 回退），A/B harness 见 `scripts/evaluate_query_rewrite.py`。
- **2026-08-03 · 阶段 4/5 收尾计划**：按 ADR-010/011 继续临时工程。
