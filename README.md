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
旧的版本激活、回滚和清理接口均已移除。已安装的固定与个人 Skill 默认激活；`GET /api/v1/skills`
返回当前激活状态，`PATCH /api/v1/skills/{name}/activation` 和个人 Skill 对应端点可即时切换下一轮
Assistant 的可用目录（ADR-021）。
`/skills` 会列出全部已安装 Skill 及其激活状态；输入框的指令面板仅列出激活 Skill 的命令。
阶段 5 工程功能已完成；正式 Skill 评测和阶段退出仍受阶段 3/4 质量门禁约束，不能把临时
结果写成正式质量通过。
`summarize_document`、`compare_sources` 和 `create_review_cards` 已从当前目录和新 Run API 删除。
普通摘要与比较由未改动的 `knowledge_agent 1.0.0` 处理；复习卡属于审批式 Exam 能力。既有 Run、
引用和派生知识仍可只读访问。Research、Exam 与 Course Project 当前均为 `2.0.0` 原生 Tool-use
Skill，共享 16 步、12 次 Tool 调用和 600 秒的 provisional 运行预算。
`knowledge_agent 1.0.0` 提供当前默认的受约束 LLM/Tool 循环：顶层 Agent 会在每轮观察 Tool
结果后自主选择下一步；模型仅可调用服务端注册的 `knowledge_retrieve` 与 `knowledge_answer`。
`grounded_answer` 继续通过现有 QA Run、Worker、SSE、Grounded QA Port 和 Citation 链路完成问答。
动态数值由服务端 profile 封顶，Space/版本边界不能由模型扩大；规划失败会降级到原问题的
Grounded QA，而不是把 Run 变成基础设施失败。Tool 仅向外层模型返回状态和覆盖计数，不返回回答
或原文。首个意外重复的同一 Tool 调用不会重新执行，而是作为可恢复的模型可见观察返回；第二次相同重复才以
`RUN_LLM_NO_PROGRESS` 停止。旧 Agent 版本、`knowledge_qa` 适配器和回退开关已删除。默认 fake 可跑通流程，配置允许的
OpenAI-compatible `fast_chat` Provider 会执行真实模型决策。非 fake Provider 仍不会注册本地工作区
读写或命令 Tool；仅 fake Provider 的已选工作区可使用这些 Tool，且写入和命令必须经过持久审批。
Assistant v2 使用独立的活动调用目录，包含 `/ask`、`/research`、`/prepare-exam` 和
`/course-project`；模型只能返回 Skill 意图，服务端负责当前 Space 资源解析、版本 pin、权限和
原生 Tool 执行。已删除的历史固定 Skill 身份不再恢复。普通聊天不会强制进入

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
uv run --frozen python scripts/evaluate_agent_harness_v2.py --validate-only
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

个性化 Phase 5（跨会话长期记忆）让 agent 跨会话记得用户。Worker 蒸馏任务（`memory_distill`）从
`conversation_summaries` + Phase 2 `usage_patterns` 用 LLM 提炼持久的 fact/preference/pattern，
按 content 哈希去重 + 余弦近邻合并（同实体更新而非重复插入，sensitivity 继承源摘要最严格值）写入
`memory_entries`；新 Assistant 回合组装上下文快照时按当前问题做向量 + 近因加权检索，注入有界
`<long-term-memory>` 块（top-K 默认 5），restricted 内容不注入、且不得把记忆泄漏到更低 sensitivity
的会话。蒸馏为显式触发，支持本地或调度到 Worker：

```powershell
uv run python scripts/distill_memories.py --json
uv run python scripts/distill_memories.py --enqueue
```

记忆表 pgvector 检索复用 `embedding_zh` 能力别名；注入 best-effort，检索失败只记日志、不打断回合。

个性化 Phase 6（自动提取工作模式，Path B）从使用痕迹自动提议个人 skill，走 creator 定稿 + eval 门禁 +
用户审批，**绝不自动激活**。每次 Assistant/Skill 回合结束 Worker 会自动调度 `skill_pattern_extract`
（Redis 节流，默认每 30 分钟最多一次）；也可手动 `--enqueue` 或本地执行。挖掘按 Phase 2 维度聚类
usage_traces，过拟合防护（仅 COMPLETED + 未绑定 skill + 使用工具，频次 ≥3、跨 ≥2 会话、30 天窗口、
排除 general）→ 用 Phase 4 creator 机制草拟候选包（category 定制 prompt + 从 exemplar 蒸馏的
eval cases，case_id 锚定来源 run）→ **双闸验证**（Phase 1 结构化门禁全过 + 历史锚定：每个 eval case
都溯源到真实来源 run）→ 通过的进入 SkillsPanel 草稿区并附 `evidence.json`；未过闸的候选删除、不
surfacing。

```powershell
uv run python scripts/extract_skill_candidates.py --json
```

候选草稿的 `GET /api/v1/skills/personal/drafts/{name}/evidence` 暴露证据（频率/跨会话数/工具序列/来源
run），SkillsPanel 草稿卡片显示「候选模式」徽章；激活仍需用户走原 drafts 的 activate 审批。

个性化 Phase 3（个人 Skill 存储与信任模型）让用户可写自己的 Skill，但严格复用内置校验与信任边界：
个人 Skill 存放于 `PERSONAL_SKILLS_DIR`（默认 `./data/personal_skills`），只组合既有 handler/tool、
不引入新 Python 行为，且不得覆盖内置 Skill 名（ADR-018）。个人 Skill 创建后默认激活；CRUD 和即时
激活切换经 `/api/v1/skills/personal`，状态沿用 `skill_activations` 持久化并在 API/worker 启动时重放；
`SkillsPanel` 显示状态并提供切换入口（ADR-021）。
调试可先验证个人根能加载：

```powershell
uv run python -c "from infrastructure.qa_execution import assistant_skill_registry; r=assistant_skill_registry(); print(r.personal_names())"
```

发布 Assistant 时应先使用 `MODEL_PROVIDER=fake` 或获批准的本地 Chat stub。启用外部 Chat Provider 仍需满足现有的
`MODEL_ALLOW_EXTERNAL`、来源策略、部署策略和用户可见同意检查；Web 发布配置不会绕过这些边界。应监控路由误判、
澄清循环、取消率、恢复失败以及实际 token/延迟回归。

个性化 Phase 4（Skill Creator，Path A）让用户经 Agent 引导创建/迭代个人 Skill，形成
`draft → 校验 → eval 门禁 → 用户审批 → active` 生命周期：

- `skills/skill_creator/` 是 manifest v2 + `invocation`（`command: create-skill`，别名 `skill`，
  `execution_mode: native_tool_use`），激活后其指令进入 native Tool-use 上下文；
  `/create-skill` 命令提交的 turn 由 assistant 循环驱动（与 `/research` 同路径）。assistant
  循环始终注册六个 creator 工具（`skill_scaffold` / `skill_write` / `skill_validate` /
  `skill_run_eval` / `skill_activate` / `skill_draft`），写类工具走既有 durable approval。
- 草稿存放于 `PERSONAL_SKILLS_DIR/_drafts/<name>/`（下划线前缀跳过 reload 扫描），
  CRUD + `/validate` + `/eval` + `/activate` 经 `/api/v1/skills/personal/drafts`；
  eval 门禁复用 Phase 1 的 `StructuralSkillEvalJudge` + case/check/报告类型，确定性、
  body-free、无需模型与数据库。`SkillsPanel` 展示 draft/active，支持运行 eval / 激活 / 拒绝。
- 轻量模式建议（Phase 6 前奏）：`/api/v1/skills/personal/drafts/suggestions` 基于 Phase 2
  的 `usage_patterns`，达到频率阈值且未被既有 skill 覆盖时才出现；点击「创建」进入预填
  脚手架的 creator 流程，必须人工确认。

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

考试准备工作流以持久化 `ExamSession` 贯穿多次短 `ConversationRun`，考试交互和评分通过既有
对话页面与 API 投影呈现。它是临时工程 vertical slice，正式质量门禁仍未关闭。

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
- 模型仅可调用服务端注册的 `knowledge_retrieve` 与 `knowledge_answer`。规划失败会降级到原问题的 Grounded QA，
  而不是把 Run 变成基础设施失败；Tool 只返回状态与覆盖计数，不返回回答或原文。
- 服务端 `research-grounded-answer-v2` 契约：单篇精读和多篇综述不再只依靠格式提示，多篇综述必须
  包含逐篇摘要、带引用证据矩阵、主题综合段，以及共识、条件差异、冲突、证据空白与局限。
- Web 为每次 Skill 调用保留默认折叠的调用记录卡；存在证据时提供引用操作，`run_id + evidence_id`
  解析固定版本的最小原文片段并高亮。默认 fake 模型返回确定性抽取式回答，可复现、适合先跑通流程。

### 命令与 Skill

输入 `/` 或点击「指令与技能」打开按「指令/技能」分组的可视化选择面板（可搜索、键盘可操作）。

| 命令 | 作用 |
| --- | --- |
| `/ask` | `knowledge_agent 1.0.0`：知识问答、普通摘要与比较 |
| `/research` | `research_reading_workflow 2.0.0`：论文精读与确认范围后的文献综述 |
| `/prepare-exam` | `exam_preparation_workflow 2.0.0`：诊断、针对性复习与复测 |
| `/course-project` | `course_project_workflow 2.0.0`：按当前阶段推进课程项目 |
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

### Exam Preparation 本地纵向演示

先校验独立的 CC0 合成课程包（不会修改冻结的 corpus v0）：

```bash
uv run python scripts/exam_preparation_demo.py --validate-only
```

启动常规 Compose 服务后运行 `uv run python scripts/exam_preparation_demo.py --prepare-space`
（它只调用正式 Source/Upload/Ingestion API），再在对话中输入 `/prepare-exam`。题卡会
嵌入当前对话；每次提交通过独立 v3 API 恢复同一
Session，答案不会写入聊天或 agent-run-sse-v4。真实 DeepSeek 运行使用下方环境配置，且
仅允许 `public_demo` 或用户明确授权外发的来源。当前工程与质量结论均为
development/provisional。

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

### Local Agent Workspaces

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

当前运行时只支持 Assistant 主路径和每个 Skill 的唯一固定版本。知识请求固定使用
`knowledge_agent 1.0.0`；Research、Exam 与 Course Project 为 `2.0.0`。旧 Skill 目录、
旧 Prompt、`knowledge_qa` 恢复适配器、Skill 的版本激活/回滚/清理 API 以及 Web 的兼容问答模式均已移除。
`GET /api/v1/skills` 返回安装目录及即时激活状态；关闭的 Skill 不会进入下一轮 Assistant 上下文。

下文的阶段记录保留历史背景；其中出现的旧版本、回滚开关和兼容入口不再是当前可用配置。
