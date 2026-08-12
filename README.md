# Agent 驱动的个人知识仓库

## 2026-08-10 实现状态

当前 Assistant 对话版本已完成临时工程契约。新的知识请求统一使用 `knowledge_agent 1.0.0`；
五个既有 Runtime Skill 保持 `1.0.0`；`research_reading_workflow 1.1.0` 已作为 provisional
Agent Loop Skill 激活，另外两个学习 Workflow 契约仍未激活。旧版 Skill、旧 Prompt 和
`knowledge_qa` 适配器已删除，不再提供回退恢复路径。Grounded Skill 结果只是参考材料，由一次独立的
`ConversationFinalizer` 生成面向用户的 Assistant 消息，并且只发布一次。

Research 的模型输出使用服务端 `research-grounded-answer-v2` 契约：单篇精读和多篇综述不再只依靠
格式提示。多篇综述必须包含逐篇摘要、至少三行带引用证据矩阵、至少两个主题综合段，以及共识、条件差异、
真实冲突、证据空白和局限；跨论文综合必须覆盖至少两个固定文档。不完整结构使用既有一次 repair 机会，
仍不合格则不会作为综述发布。

Web 会为每次 Skill 调用保留一个默认折叠的调用记录卡，即使调用完成、失败、取消或进入澄清状态也不会
隐藏。调用记录卡和最终回答框在存在证据时均提供引用操作，并共享一个可关闭、相互排他的证据栏。
显式斜杠指令支持按 Enter 提交，在用户消息中保留原始指令，并使用不改变文字尺寸的强调色突出合法前缀。
Assistant 输出支持 GFM Markdown 和 LaTeX 渲染。这些改动不改变阶段 3 的终止记录，也不改变阶段 4/5
的临时质量边界。

阶段 0-5 的工程能力均已在当前契约下实现。阶段 3 当前默认使用 `dense_rerank`，直接对 dense 候选
进行精排，`hybrid_rerank` 仅作为显式兼容模式保留。阶段 4 包含完整的 QA/Citation/API/Web/Worker
链路和按 Space 隔离的反馈审核队列。阶段 5 包含持久化 Runtime、审批、派生知识和 Skill 生命周期能力，
以及受控的 `scripts/export_feedback_candidates.py` 命令。这些实现结果不改变阶段 3 的终止记录，
也不代表正式检索、回答或 Skill 留出集验收通过。

本项目面向个人学习、科研和开发资料，目标是构建一个本地优先、来源可追溯的知识工作台。
规划中的完整闭环包括文档摄入、增量索引、混合检索、带引用问答和可版本化 Agent Skill。

## 当前状态

**阶段 0、阶段 1 和阶段 2 已正式完成；阶段 3 Step 0-10 的工程实现已完成，但正式质量门禁未通过。PR #4 已修正复现与在线默认路径为 `dense_rerank`（纯 dense 候选直接精排）；其 GPU 开发集结果在 v0/v1 上的 Claim Recall@10 分别为 82.37%/78.75%，高于 `hybrid_rerank`，但仍仅是临时工程证据。因当前评测集代表性局限，阶段 3 已按 [ADR-010](docs/adr/010-stage-3-termination-and-evaluation-boundary.md) 保持正式未通过；正式留出集尚未执行，配置仍未冻结。根据 [ADR-011](docs/adr/011-provisional-stage-4-5-continuation-gate.md)，当前结果只允许阶段 4/5 继续临时工程，不构成正式质量接受；阶段 4/5 正式质量门禁仍未关闭。**

已交付的核心能力：

| 层级 | 内容 |
|------|------|
| 阶段 1 ✅ | 工程骨架：FastAPI、Worker、Web 工作台、PostgreSQL/pgvector、Redis、Alembic、模型网关、结构化日志、OpenTelemetry、Compose、CI |
| 阶段 2 ✅ | 摄入工程 Step 0-8 与正式 Step 9 验收完成；冻结 manifest 中 74 个 P0 来源解析/定位/分块成功率 100%，幂等、原子发布、删除恢复、API/Web 和 Compose E2E 通过 |
| 阶段 3 ⏹️ 已终止（工程完成，质量门禁未通过） | PostgreSQL FTS/pgvector 检索、加权 RRF、上下文扩展、可选 Reranker、Space/版本安全边界、检索 API、版本化离线评测与集成验收已完成；API/QA 默认 `dense_rerank`，PR #4 GPU 开发集在 v0/v1 Claim Recall@10 为 82.37%/78.75%，但评测集代表性仍不足，正式留出集尚未执行，配置保持临时状态 |
| 阶段 4 🟡 临时 Step 0-10 | 在 ADR-011 继续门禁下继续；ADR-007、唯一临时 QA Application Port、Grounded QA/Evidence/Citation、PostgreSQL Repository/SSE、问答 API、Web、Worker 重启恢复、按需原文解析和回答评测仅校验流程均已完成；默认配置与正式留出集尚未完成 |
| 阶段 5 🟡 临时 Skills | Assistant 对话演进 Step 3-8 的当前调用目录、自主可恢复 Skill/Tool Loop、上下文、指标、最终回答生成器、调用记录卡和 Web 展示已完成；新知识请求固定 `knowledge_agent 1.0.0`；正式质量仍为临时状态 |

当前 Web 展示系统健康、数据来源和临时知识问答工作区；HTTP API 可创建持久会话、提交
问题，由 API 仅向 Redis 投递 Run ID，再由独立 Worker 调用唯一 `GroundedQAApplicationPort`、
真实 PostgreSQL `SearchService` 和
Citation 目标适配器生成回答或拒答。默认 fake 模型提供确定性抽取式回答；配置允许的外部
Chat Provider 仍走相同结构化生成与引用校验路径。Web 会展示终态回答、限制和文档版本/locator
引用身份；点击 Citation 后按 `run_id + evidence_id` 解析固定版本的最小原文片段并高亮。

该链路是可真实使用的临时版本，不是阶段 4/5 正式完成：QA 会话、Message、Run/Attempt、
Evidence、Citation、Feedback、SSE 事件和 Worker lease 已写入 PostgreSQL；API/Worker 重启可恢复
未完成运行，重复投递不会重复发布终态；Citation 原文解析不会接受客户端伪造的版本、locator 或 Blob 路径；
阶段 3 默认检索配置和质量门禁也尚未冻结。
新建知识问答统一固定为 `knowledge_agent 1.0.0`：API 和 Assistant 主路径只会创建该 Skill 的 Run。API 在提交时固定名称、版本和
内容摘要，Worker 恢复时按该固定身份校验声明式 workflow，再经唯一 QA Application Port 执行。
旧版本和 Skill 生命周期激活/回滚/清理接口均已移除；`GET /api/v1/skills` 只返回当前安装的只读清单。
阶段 5 工程功能已完成；正式 Skill 评测和阶段退出仍受阶段 3/4 质量门禁约束，不能把临时
结果写成正式质量通过。
`summarize_document`、`compare_sources` 和 `create_review_cards` 也已提供临时 HTTP
入口并固定提交时的 Source/Document/DocumentVersion 范围；版本变更、撤下或跨 Space 选择不会
扩大检索范围。比较结果必须引用至少两个来源，否则按证据不足拒答。复习卡当前只返回带引用预览，
并以 `SKILL_WRITE_REQUIRES_APPROVAL` 明确报告审批前 `side_effects=0`；批准后通过持久化
Derived Knowledge Port 幂等写入，并可查询或撤销。
`knowledge_agent 1.0.0` 提供当前默认的受约束 LLM/Tool 循环：顶层 Agent 会在每轮观察 Tool
结果后自主选择下一步；模型仅可调用服务端注册的
`knowledge_search`、`knowledge_inspect`、`summarize_document`、`grounded_answer`、`verify_answer` 和
`finalize_answer`。`summarize_document` 只在服务端资源解析器可用时注册，先固定当前 Space 的已发布
DocumentVersion，再回到同一 Grounded QA Run；复合请求可在一个 Loop 中串行组合这些 Tool。
`grounded_answer` 继续通过现有 QA Run、Worker、SSE、Grounded QA Port 和 Citation 链路完成问答。
动态数值由服务端 profile 封顶，Space/版本边界不能由模型扩大；规划失败会降级到原问题的
Grounded QA，而不是把 Run 变成基础设施失败。Tool 仅向外层模型返回状态和覆盖计数，不返回回答
或原文。首个意外重复的同一 Tool 调用不会重新执行，而是作为可恢复的模型可见观察返回；第二次相同重复才以
`RUN_LLM_NO_PROGRESS` 停止。旧 Agent 版本、`knowledge_qa` 适配器和回退开关已删除。默认 fake 可跑通流程，配置允许的
OpenAI-compatible `fast_chat` Provider 会执行真实模型决策。非 fake Provider 仍不会注册本地工作区
读写或命令 Tool；仅 fake Provider 的已选工作区可使用这些 Tool，且写入和命令必须经过持久审批。
Assistant v2 使用独立的活动调用目录，包含 `/ask`、`/summarize`、`/compare`、`/cards`
对应的 v2 Skill 元数据；模型只能返回 Skill 意图，服务端负责当前 Space 资源解析、版本 pin、
权限和 QA Worker 投影。历史固定 Skill 身份仍可恢复旧 Run。普通聊天不会强制进入

Assistant 对话演进 Step 5 增加有界多轮上下文。原始消息保持追加写入；版本化滚动摘要保留其覆盖范围、
摘要指纹、prompt/模型版本和敏感度。路由、直接回答和 Skill 交接共享一个有界快照，而 QA 仍保持证据隔离。
`/compact` 和软水位压缩通过现有 Worker 队列创建可持久化的后台 Run。这仍是 ADR-010/011 下的临时工程能力。

Agent Loop Step 6 增加 `/effort`：不带参数时读取当前 Conversation 默认值，带
`low|medium|high|xhigh|max` 时只影响后续 Run。菜单只提供这五个值；服务端会将旧的内部默认值
解析为这五个值之一（无法解析时为 `medium`），并标记为 `(default)`。每个新 Assistant/压缩 Run 都保存
provider-neutral 的 requested/effective effort、Provider/model、映射版本、模式和降级原因。显式不支持
的强度请求会拒绝；仅 `auto` 可降级。当前 OpenAI-compatible Chat 仍使用布尔 `thinking` 映射，且保留
`FAST_CHAT_REASONING_ENABLED=false` 仍只影响没有解析出会话 profile 的兼容调用；Conversation 默认 `auto`
会按当前 Provider 能力表解析为 medium。这不代表 Responses 原生 effort 已接入或任何质量门禁已关闭。

配置 `FAST_CHAT_MODEL=deepseek-v4-flash` 时，`reasoning-mapping-v2` 使用 DeepSeek Chat
Completion 原生 `reasoning_effort` 字段，Run 显示 `mode=native`。`xhigh` 依照 DeepSeek 的公开映射
审计为实际 `high`，但请求仍保留用户选择的 `xhigh`；`reasoning_content` 不写入 Conversation、Run、SSE、
日志或前端。DeepSeek 当前公开的 Chat Completion 参数表列出 `low`、`high` 和 `max`，映射表另列
`xhigh`；当前配置端点已实测接受 `medium` 并返回非空 `reasoning_content`，但应在 Provider 升级后重新
验证该兼容性。其他 OpenAI-compatible 模型继续使用布尔 `thinking` 兼容映射。

Web 中提交不带参数的 `/effort` 会在当前对话位置打开一次性选择面板。可用左右方向键切换、Enter
确认或点击选项；确认后面板移除，并在该位置留下 `Model: <model> | reasoning effort: <effort>` 的固定结果。
命令结果与消息按照发生顺序渲染。

Assistant 对话演进 Step 6 将原 QA 工作区演进为通用对话工作区：输入 `/` 时显示可搜索、
可键盘操作的命令面板，资源歧义在消息内显示安全候选。选择候选会重新校验当前 Space 并回到原 Run，
不会重建会话。`GET /api/v2/conversations/{conversation_id}/runs` 用于刷新后恢复对话 Run 和待澄清
状态；引用侧栏只在已完成的 grounded Run 有 Citation 时出现。折叠运行信息只显示实际模型、token
和耗时，不显示预算、剩余额度或 Tool 上限。

Assistant 对话演进 Step 7 增加隐私安全的运行计数器，用于记录路由、指令、澄清、上下文压缩、实际 token
用量、延迟和终止原因。`scripts/evaluate_assistant_routing.py --validate-only` 用于校验固定的仅合成数据路由
开发集；预测报告明确标记为“开发集/临时”，不会调用 Provider、读取受控语料或启用正式留出集。
结构化指标日志只包含指标名称、安全标签和聚合值，不包含对话内容、prompt、文档正文、Provider 响应或内部资源 ID。

Assistant 对话工作区现在只保留 Assistant 主路径；“兼容问答”模式、对应的 v1 Web 入口和兼容窗口配置已删除。

Agent Loop 是 fake/local 的默认 provisional 路径。新 Run 默认固定到
`knowledge_agent 1.0.0`。先执行只读取 hash 固定合成 fixture 的检查：

```powershell
uv run --frozen python scripts/evaluate_agent_loop.py --validate-only
```

该命令不调用 Provider、不读取受控语料，也不会启用 formal holdout；其所有报告均为 development / provisional。

Skill eval 门禁（个性化 Phase 1）让 `skills/*/evals/cases.jsonl` 可执行、可判定、可出报告。CLI 默认
fake 模型、报告不阻塞任何流程：

```powershell
uv run python scripts/evaluate_skills.py --all --output tmp/skill-eval.json
```

无 `checks` 的既有 case 走最低判定（schema 合规 + finalized）并如实标注 `case_too_thin`，绝不误报 pass；
行为标签编码为对 AgentRun trace 的结构化断言（`trace_tool_called` / `finalized`），不靠 LLM 判定。
`--model settings` 可切换诊断模型，`--database <url>` 启用 Grounded QA Skill 的生产适配器探针；
报告只序列化 body-free 证据，不落 output/正文。

个性化 Phase 2（使用痕迹记录与蒸馏）只记录、不注入 Agent：每次 Skill 调用 / Assistant turn 结束时在
Worker 落一条脱敏 `usage_traces`（`input_summary` 截断 + 长 hex 密钥打码，不存完整 Prompt / 私密正文），
并按需蒸馏出 (skill, 任务类别, 工具序列, 输入类型) 的 `usage_patterns` 聚合，为后续跨会话记忆与自动提取备料。
调试/按需查看：

```powershell
uv run python scripts/query_usage_traces.py --limit 20
uv run python scripts/distill_usage_patterns.py --json
```

`--enqueue` 可把蒸馏调度到 Dramatiq Worker（需 Redis）；Phase 6 前这些模式只产出、不消费。

发布 Assistant 时应先使用 `MODEL_PROVIDER=fake` 或获批准的本地 Chat stub。启用外部 Chat Provider 仍需满足现有的
`MODEL_ALLOW_EXTERNAL`、来源策略、部署策略和用户可见同意检查；Web 发布配置不会绕过这些边界。应监控路由误判、
澄清循环、取消率、恢复失败以及实际 token/延迟回归。

QA；资源歧义只显示服务端生成的候选，不暴露内部 UUID。该自动路由和 Step 4 Command API
均为临时能力；Step 5 才实现上下文压缩。
真实本地组合使用外部 OpenAI-compatible `fast_chat`、
`EMBEDDING_PROVIDER=text-embeddings-inference`、本地 Qwen3 TEI Embedding 和本地
BGE reranker；完整 GPU 路径使用 `RERANKER_PROVIDER=inherit`。`RERANKER_PROVIDER=fake`
只适用于不启动 reranker 服务时的确定性流程验证。三项能力独立路由，Embedding 不会随外部
Chat 回退为 fake。
阶段 0 已冻结为 `internal_team_only`，原始语料和评测 JSONL 仍只在组员本地保留；退出证据见
[阶段 0 验收记录](docs/stage-0-acceptance.md)，摄入退出证据见
[阶段 2 验收记录](docs/stage-2-acceptance.md)。不要直接运行留出集；历史 `90.48%` Recall@5 和
`75.8%` Claim Recall@10 均不能作为当前代码的质量结论。PR #4 的 `dense_rerank` 开发集
复现也仍是临时结果，阶段 3 已终止；若未来重新开启，必须发布新的数据集/配置版本
并重新走评测流程。

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

上面的命令使用确定性的 fake provider 路径。若要在 Web QA 或 `knowledge_agent` 中使用真实 Chat provider
和完整的本地 GPU 检索路径，请按照 `.env.example` 中的能力拆分进行配置，并显式启动两个模型 profile：

```bash
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding --profile reranker up --build --detach --wait
```

`text-embeddings-inference` 不是 Chat provider。外部 Chat 需要设置
`MODEL_PROVIDER=openai-compatible`、`FAST_CHAT_ENDPOINT`、`FAST_CHAT_MODEL` 和
`MODEL_ALLOW_EXTERNAL=true`，同时保持 `EMBEDDING_PROVIDER=text-embeddings-inference`。默认
`dense_rerank` 路径需要设置 `RERANKER_PROVIDER=inherit`、
`RERANKER_ENDPOINT=http://tei-reranker:80` 和 `RERANKER_MODEL=BAAI/bge-reranker-v2-m3`。
只有在有意不启动可选 GPU reranker、验证流程时，才设置 `RERANKER_PROVIDER=fake`。修改 `.env` 后，
请重新创建 `api` 和 `worker` 服务。

真实模型组合需要在被 Git 忽略的 `.env` 中同时配置外部 Chat、
`EMBEDDING_PROVIDER=text-embeddings-inference`、`EMBEDDING_ENDPOINT=http://tei:80`、固定的
Qwen3 Embedding 模型/revision，以及适配 TEI 限制的 `EMBEDDING_BATCH_SIZE`。完整字段见
`.env.example`，凭据不得提交仓库。

> **注意**：Docker Compose v5 可能需要显式指定 `--env-file .env`；若不加也能正常运行则无需此参数。

3. 打开工作台：<http://127.0.0.1:5173>

首次构建需要下载锁定 digest 的基础镜像和依赖。Compose 会依次等待 PostgreSQL、迁移、Redis、
API、Worker 和 Web 达到各自完成或健康条件。

> 首次启动 Embedding 模型服务（`--profile embedding`）时，TEI 会从 HuggingFace Hub
> 自动下载 Qwen3-Embedding-0.6B（约 400 MB）。模型文件会缓存到命名 Docker 卷，后续启动
> 无需重下载。中国用户可参考 [模型下载文档](docs/model-setup.md) 使用镜像源加速。
在“数据来源”中上传并等待文档状态发布后，进入“知识问答”即可使用当前 Space 的真实索引提问。
默认 fake 模型返回可复现的相关证据摘录，适合先跑通流程；回答质量将在阶段 3 达标后继续调整。

## Smoke Test

### 一键启动 GPU

当 `.env` 中包含已批准的 Chat 端点和密钥时，可以在 PowerShell 中执行以下命令启动完整 GPU 栈：

```powershell
.\scripts\start-local.ps1
```

脚本会检查 Docker GPU 透传，启动 `embedding` 和 `reranker` profile，将生效的 reranker 配置强制为
使用 `BAAI/bge-reranker-v2-m3` 的 `inherit`，并将新 Run 固定到 `knowledge_agent 1.0.0`，然后检查 Web、API、
Embedding 和 Reranker 健康状态。
脚本不会修改 `.env` 或打印密钥。配置的外部 Chat 端点可能接收问题和检索片段；没有完成必要的策略审批时，
不要将私有或受限来源用于该路径。

如果本地已有预热完成的 Reranker 模型缓存，请将 `.env` 中的 `RERANKER_VOLUME_NAME` 设置为现有 Docker
卷名。启动脚本会保留该设置，同时强制使用真实 Reranker 路径。首次 GPU 模型预热可能需要几分钟；
Compose 已为两个 TEI 服务提供更长的健康检查窗口，避免过早报告启动失败。

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:5173/api/v1/health/ready
docker compose -f deploy/compose.yaml --env-file .env ps
```

预期 `live` 返回 `alive`，`ready` 返回 `ready`，PostgreSQL 和 Redis 分别报告
`POSTGRESQL_OK`、`REDIS_OK`。模型能力会按实际配置报告 `MODEL_FAKE_READY` 或
`MODEL_CAPABILITY_CONFIGURED`；`ready` 只验证路由配置，仍应以一次真实检索请求验证 GPU 服务。

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

提交前质量门禁（每克隆执行一次）：

```bash
git config core.hooksPath .githooks
```

启用后，`git commit` 前自动复现 CI 的快速门禁：`ruff format --check .`、`ruff check .`、
`mypy apps packages`、OpenAPI 导出无差异；改动 `apps/web/` 时另跑 `pnpm lint` 与 `pnpm typecheck`。
完整 pytest、集成与 Compose smoke 仍由 CI 执行。临时跳过可用 `git commit --no-verify`
（不推荐，跳过前需说明理由）。

真实依赖集成测试由 CI 在隔离 PostgreSQL/Redis 中运行。手动运行前必须配置隔离依赖并设置
`RUN_INTEGRATION=1`，不要指向包含业务数据的数据库。

## 服务与端口

| 服务 | 默认地址/端口 | 说明 |
| --- | --- | --- |
| Web | `http://127.0.0.1:5173` | nginx 静态托管并代理同源 `/api` |
| API | `http://127.0.0.1:8000` | 兼容接口为 `/api/v1`；`/api/v2` 提供临时 Assistant 对话、`/commands`、显式/自动 Skill 路由、取消和无正文 SSE |
| PostgreSQL | `127.0.0.1:5432` | PostgreSQL 16 + pgvector |
| Redis | `127.0.0.1:6379` | Dramatiq broker，启用 AOF |
| OTel Collector | `4317`、`4318` | 仅 `--profile otel` 启动 |
| Embedding | `127.0.0.1:8080` | Qwen3-Embedding-0.6B（TEI），需 `--profile embedding` |
| Reranker | `127.0.0.1:8081` | BAAI/bge-reranker-v2-m3（TEI），需 `--profile reranker` |

端口可通过 `.env` 中的 `WEB_PORT`、`API_PORT`、`POSTGRES_PORT`、`REDIS_PORT`、`EMBEDDING_PORT`
和 `RERANKER_PORT` 覆盖。

Embedding（Qwen3-Embedding-0.6B）服务需通过 `--profile embedding` 显式启动。首次启动时，TEI 会自动从 HuggingFace Hub 下载模型（缓存至命名 Docker 卷，后续启动无需重下载）。

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
- [knowledge_agent 证据扩池实验报告](docs/knowledge-agent-evidence-expansion-report.md)
- [阶段 1 验收记录](docs/stage-1-acceptance.md)
- [阶段 2 验收记录](docs/stage-2-acceptance.md)
- [故障排查与已知限制](docs/troubleshooting.md)
- [阶段 1 实施计划](docs/stage-1-implementation-plan.md)
- [阶段 2 实施计划](docs/stage-2-implementation-plan.md)
- [阶段 3 实施计划](docs/stage-3-implementation-plan.md)
- [阶段 3 验收记录](docs/stage-3-acceptance.md)
- [阶段 4 实施计划](docs/stage-4-implementation-plan.md)
- [阶段 4 临时验收记录](docs/stage-4-acceptance.md)
- [阶段 4 持久化设计](docs/stage-4-persistence-design.md)
- [ADR-007：Grounded QA 持久化、引用、执行与 SSE](docs/adr/007-grounded-qa-persistence-and-sse.md)
- [阶段 5 实施计划](docs/stage-5-implementation-plan.md)
- [阶段 5 实现审查记录](docs/stage-5-implementation-review.md)
- [阶段 5 临时验收记录](docs/stage-5-acceptance.md)
- [阶段 4/5 收尾看板](docs/stage-4-5-completion-tracker.md)
- [阶段 3 v1 开发集复核记录](docs/stage-3-reopen-development-v1.md)
- [阶段 3 终止后的阶段 4/5 收尾计划](docs/post-stage-3-stage-4-5-completion-plan.md)
- [OpenAPI](docs/openapi.json)
- [架构决策记录](docs/adr/README.md)

## 安全与数据边界

- 密钥只从环境变量或被 Git 忽略的 `.env` 读取，不得提交或写入日志。
- 默认模型为确定性 fake；外部 Provider 必须显式配置并允许外发。
- 只允许处理 `cases/evals/corpus/v0/manifest.yaml` 明确列出的来源，并在读取前校验 SHA-256。
- 阶段 0 语料仅在 manifest 允许列表内使用；未确认外部授权的来源仍限于组员本地，禁止 Git 上传、公开演示和外部 Provider 外发。

问题恢复步骤和当前限制见[故障排查文档](docs/troubleshooting.md)。

## Local Agent Workspaces

Assistant conversations may select a pre-existing folder with `/workspace <folder>` or `/ws`. The
selected path is constrained below `AGENT_WORKSPACE_ROOT_PATH`, stored as a logical relative path,
and included in the Agent context so file paths and command cwd values are interpreted relative to
that workspace. `fs_list` and `fs_read` are available only in a selected local workspace;
`fs_write` and `shell_exec` require a durable approval before execution.

The selected local workspace is independent from uploaded, current-Space knowledge. A workspace
file listing cannot determine whether uploaded knowledge exists. When a request combines a
knowledge question with a local save, the Agent decides whether to retrieve, inspect the workspace,
and write based on the request and each Tool result. If no filename is provided, it chooses a
descriptive Markdown path that avoids an unintended collision; there is no fixed default filename.
The workspace root is always `.` in Tool paths and `shell_exec.cwd`; the displayed workspace
selection (for example, `project-a`) is not a child cwd.

The Agent timeline lists each workspace Tool's target path, and records the command and relative
cwd for `shell_exec`. It provides in-page actions for pending write and command approvals:
approve once, reject, or **always allow this Tool type**. The last option is limited to the current
Conversation and only suppresses later approval prompts for the same Tool type; workspace
containment, protected-path checks, command aliases, invocation validation, and all other runtime
policy checks still apply. `shell_exec` output and `fs_list` results are shown as bounded previews
in the Tool details. Long previews are marked as truncated and may be expanded in the timeline.

The Tools are registered for `MODEL_PROVIDER=fake` by default. A non-fake Chat Provider requires
explicit `AGENT_WORKSPACE_MODEL_VISIBILITY_CONSENT=true`. Run
`.\scripts\start-local.ps1 -AllowExternalWorkspaceTools` to enable that consent for one process;
the script warns that selected workspace content may be sent to the configured endpoint. In Compose,
set `AGENT_WORKSPACE_HOST_PATH` and keep its `/data/agent-workspaces` mount shared by API and Worker.
See
[development environment](docs/development-environment.md#local-agent-workspaces) and
[ADR-016](docs/adr/016-conversation-workspace-tools.md) for configuration, approval endpoints, and
security constraints.
# 当前实现说明（2026-08-11）

当前运行时只支持 Assistant 主路径和每个 Skill 的唯一固定版本。既有 Skill 保持 `1.0.0`，
`research_reading_workflow` 为 `1.1.0`。知识请求固定使用 `knowledge_agent 1.0.0`；旧 Skill 目录、
旧 Prompt、`knowledge_qa` 恢复适配器、Skill 激活/回滚/清理 API 以及 Web 的兼容问答模式均已移除。
`GET /api/v1/skills` 仅返回当前安装的只读 Skill 清单。

下文的阶段记录保留历史背景；其中出现的旧版本、回滚开关和兼容入口不再是当前可用配置。
