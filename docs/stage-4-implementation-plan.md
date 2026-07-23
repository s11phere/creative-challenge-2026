# 阶段 4 实施计划：可追溯问答闭环

> 文档状态：Draft v1；正式启动门禁尚未关闭
>
> 适用范围：`docs/project-implementation-plan.md` 中的阶段 4
>
> 计划基线日期：2026-07-23

## 1. 结论摘要

阶段 4 的目标是在阶段 2 的已发布文档版本和阶段 3 的稳定检索 Port 之上，交付一个可以从
Web 和 HTTP API 使用的可追溯问答闭环：用户在指定 Space 中提问，系统经过有界查询处理、
检索和上下文构建后生成结构化回答；每个关键 claim 绑定可解析的 Evidence/Citation；证据不足
时明确拒答；来源冲突、撤下、模型故障和取消具有不同且稳定的语义。

标准问答链路固定为：

```text
Question + Conversation context
  -> Space/会话/运行权限校验
  -> 查询分类与有界改写
  -> SearchService.search(SearchRequest, RetrievalProfileV1)
  -> 上下文预算、去重和证据编号
  -> ModelGateway.fast_chat 结构化生成
  -> claim-evidence、Space、版本和 locator 校验
  -> GroundedAnswer / Refusal / Conflict
  -> 持久化终态与引用
  -> SSE/API/Web 展示、原文定位和反馈
```

阶段 4 按以下原则推进：

- 检索结果只是候选证据，不是已验证答案；回答必须经过 claim-evidence 校验。
- Citation 是版本化领域对象，不是从答案文本中事后提取的装饰编号。
- 无证据、证据冲突、模型不可用、检索失败和用户取消必须是不同终态或错误。
- 只通过阶段 3 的 `SearchService` 获取候选，不读取检索 ORM 表，不复制 FTS、向量、RRF、
  Reranker 或过滤逻辑。
- 文档内容始终是不可信数据；它不能改变系统指令、Space、工具权限、模型外发策略或预算。
- PostgreSQL 保存会话、运行、回答、引用和反馈的业务事实；Redis/Dramatiq 如被采用，只负责
  既有 Worker 边界内的投递和唤醒。
- CI 和绝大多数测试使用 deterministic fake；真实模型只用于门禁关闭后的受控 development
  评测和最终 holdout。
- 阶段结束时只激活一个默认 QA profile、一个 prompt 版本和一个 Chat 模型 identity。

当前阶段 0、阶段 2 Step 9 和阶段 3 正式退出均未完成，因此阶段 4 不能正式启动。门禁关闭前
只允许执行本计划中的文档、领域契约、确定性算法、fake Adapter、公开 fixture 和安全边界验证；
不得读取未批准私有语料、运行正式 holdout、落地受门禁限制的业务表迁移，或宣称引用问答质量
已经达标。

## 2. 启动条件与当前缺口

### 2.1 正式启动门禁

正式执行阶段 4 前必须满足：

1. 阶段 0 已有明确退出记录，语料授权、敏感度、`allowed_uses`、人工标注和版本均已复核，
   corpus manifest 不再是 `draft_pending_license_review`。
2. 阶段 2 Step 9 已在批准的 P0 语料上完成正式验收，解析成功率、定位保留、幂等摄入、原子
   发布、删除撤下和失败恢复达到记录的门槛。
3. 阶段 3 已按 `docs/stage-3-acceptance.md` 的正式完成清单冻结 Embedding、索引和
   `RetrievalProfileV1`，完成 development 消融、一次性 holdout 和正式退出。
4. 阶段 4 的 development/holdout、claim/evidence 标注、拒答口径、冲突口径和指标计算已在
   查看回答 holdout 结果前冻结。
5. 每次读取评测来源前校验原始字节 SHA-256，且只处理 manifest 允许列表；私有来源不得发送
   到未明确批准的外部 Provider。
6. 新增 Conversation、Message、AgentRun、Evidence、Citation 或 Feedback 等业务表前，已经
   按仓库约束确认数据模型归属并接受相应 ADR；迁移只能新增 revision，不能修改既有迁移。

`status=frozen` 只解除真实语料门禁，不会自动完成阶段 2、阶段 3 或阶段 4 的质量验收。

### 2.2 门禁关闭前允许的 provisional 工作

在保持阶段状态为“未正式开始”的前提下，可以开展：

- 编写和评审本计划、ADR-007 草案、公开 schema 与错误协议。
- 定义纯 Domain 类型、Application Port、确定性引用校验和上下文预算算法。
- 使用合成输入和 manifest 明确允许 `repository_fixture` 的来源编写单元、契约和安全测试。
- 使用 fake `SearchService`、fake `ModelGateway` 和内存仓库验证完整问答状态机。
- 验证 prompt injection、跨 Space 引用、伪造 citation、撤下版本、超长输入和日志脱敏边界。
- 设计但不启用正式 QA profile、prompt、模型配置、报告 schema 和 holdout 命令。

门禁关闭前禁止：

- 读取或摄入未批准的 `private_local`/`restricted` 来源用于问答开发。
- 用 development 或 holdout 问题向真实模型调 prompt、阈值或检索参数。
- 运行正式回答 holdout，或将 provisional 报告作为阶段退出证据。
- 把模型不可用、检索失败或解析失败统计为“正确拒答”。
- 自动把用户反馈或临时问题写入已冻结 dataset；评测集变更必须发布新版本并人工复核。
- 宣称 `knowledge_qa` Skill、真实问答、引用或阶段 4 已经可用。

### 2.3 前序阶段提供的入口

- 阶段 2 提供 `Space -> Source -> Document -> DocumentVersion -> Chunk` 归属链、不可变版本、
  原子发布和 tombstone 删除语义。
- `Chunk` 保存文本、`chunk_hash`、ordinal、结构 metadata 和一基 locator；
  `Document.current_version_id` 是当前发布集合的唯一指针。
- 阶段 3 提供 `SearchService.search(SearchRequest, RetrievalProfileV1) -> SearchResult`，结果包含
  Space 范围内的 source/document/version/chunk、文本、locator、阶段分数和
  `matched/context_only` 语义。
- 阶段 3 已提供稳定检索错误、降级诊断、Embedding identity、索引版本和 profile 版本。
- `ModelGateway` 已提供 `fast_chat` 能力别名、确定性 fake、OpenAI-compatible Adapter、稳定
  错误码和 Token 使用量，但当前只有非流式 `chat()` 契约。
- 阶段 5 通用基础已经提供 AgentRun 纯领域状态、预算、Tool/Skill Registry 和确定性执行器；
  阶段 4 不复制这些通用能力，持久化和 SSE 语义必须与阶段 5 共用。

### 2.4 必须关闭的工程缺口

1. 已有 provisional 的 GroundedAnswer、Claim、Evidence、Citation、Refusal、Conflict 纯领域契约
   和确定性状态机；JSON transport schema、兼容规则和 Application 编排仍未落地。
2. 已有 provisional 的 SearchHit -> Evidence -> Citation -> 最小原文片段服务及 Port；PostgreSQL
   target Adapter、正式 retention 数据和批准语料定位验收仍未落地。
3. 已有 provisional 的查询分类/改写回退、多查询合并、上下文预算和多轮裁剪；真实 development
   对比、正式默认参数及模型改写 Adapter 仍未落地。
4. `ModelGateway.chat()` 只返回完整响应；SSE delta、取消、断线重连和最终结构校验尚无统一协议。
5. 尚无 Conversation/Message/AgentRun/Evidence/Feedback 的持久化归属、不可变边界和保留策略。
6. 尚无问答 API、对话工作台、证据查看器和引用高亮。
7. 尚无回答评测执行器、报告 schema、支持率/引用/拒答/冲突指标和正式运行门禁。
8. 当前 `knowledge-qa-v0` 只有 30 例，其中 holdout 10 例、拒答 5 例；阶段 0 冻结前必须决定
   是否发布扩充后的新版本，或正式接受统计分辨率限制，不能原地修改已查看的 holdout。
9. 阶段 3 正式默认 profile 和真实模型尚未冻结，当前 fake 检索结果不能作为问答质量基线。

### 2.5 已固定的架构约束

- 遵守 ADR-001：保持模块化单体和独立 Worker；不为问答新增微服务或第二套任务队列。
- 遵守 ADR-002：问答只调用 `SearchService`/`RetrievalStore` 抽象，不直接访问检索 SQL 或更换
  PostgreSQL/pgvector 基线。
- 遵守 ADR-003：首期是单 Agent、有界、显式状态流程；不引入开放式规划或多 Agent。
- 遵守 ADR-004：默认本地优先，任何文档片段外发都需要部署配置、来源策略和可见同意同时允许。
- 遵守 ADR-005：引用固定到不可变 DocumentVersion 和 locator；只从当前发布版本生成新引用，
  已撤下来源不得进入新回答，历史引用按 tombstone/保留策略报告状态。
- 遵守 ADR-009：如果问答通过 Worker 执行，PostgreSQL 是运行状态和幂等事实源，Redis/Dramatiq
  只投递 ID 和控制元数据，消息不得包含问题正文、文档正文或模型响应。
- `domain` 只包含纯类型和 Port；`application` 编排问答；SQL、Blob、队列、Provider 和 HTTP
  细节位于 Adapter/传输层。
- 日志和 trace 只记录 ID、版本、计数、耗时、状态和安全错误码，不记录问题、回答、prompt、
  文档正文、引用原文、模型原始响应或密钥。

### 2.6 决策登记

阶段 4 开始时建立并关闭以下决策项：

| ID | 决策 | 关闭条件 |
| --- | --- | --- |
| R4-01 | GroundedAnswer、Claim、Evidence、Citation、Refusal、Conflict v1 schema | 领域不变量、JSON schema、兼容规则和契约测试通过 |
| R4-02 | Conversation、Message、AgentRun、Evidence、Feedback 的归属、身份和保留语义 | ADR-007 接受，ORM/迁移设计评审通过 |
| R4-03 | Citation 固定版本、locator 解析、撤下/删除/过期行为 | Golden locator、tombstone、跨 Space 测试通过 |
| R4-04 | 查询分类、改写、多轮历史裁剪和上下文预算 | development 对比证明有收益，失败时可回退原问题 |
| R4-05 | 结构化生成、prompt injection 边界、校验和有限修复策略 | fake/真实 Adapter 共用契约，非法响应不会发布 |
| R4-06 | 问答运行、SSE 事件、重连、取消、重试和幂等协议 | 事件 schema、状态机、终态和 API 契约测试通过 |
| R4-07 | 同步 API 与 Worker 执行边界、状态事实源和超时预算 | 故障恢复、重复投递、断连和取消集成测试通过 |
| R4-08 | QA profile、prompt/model identity、评测指标和报告 schema | 在查看回答 holdout 前冻结并生成唯一 config hash |
| R4-09 | 用户反馈进入评测候选队列及人工审核规则 | 不可直接污染冻结 dataset，隐私和幂等测试通过 |

ADR-007 至少固定 R4-01、R4-02、R4-03、R4-06 和 R4-07。若实现改变模块化单体、队列、
数据边界、核心实体、删除语义或发布不兼容 API/事件，必须更新现有 ADR 或新增 ADR，不能只在
本计划中记录。

## 3. 范围

### 3.1 本阶段交付

1. 版本化 GroundedAnswer/Claim/Evidence/Citation/Refusal/Conflict 领域契约和稳定错误。
2. QueryPlan/QAProfile v1、查询分类、可选改写、多轮历史裁剪和 Token 上下文预算。
3. 只调用阶段 3 `SearchService` 的问答 Application 用例。
4. Citation 绑定、claim-evidence 支持校验、Space/版本二次校验和原文解析。
5. 证据不足拒答、来源冲突提示、模型/检索/存储故障区分及有限重试。
6. Conversation、Message、AgentRun、Evidence/Citation、Feedback 的最小持久化和新 Alembic
   revision；具体表边界由 ADR-007 固定。
7. 版本化问答 API、运行查询/取消/重试和 SSE 事件协议，更新 OpenAPI。
8. Web 对话工作台、证据查看器、原文定位/高亮、真实加载/失败/取消/重试状态。
9. 反馈进入评测候选队列的人工审核入口，不直接修改冻结评测集。
10. 回答离线评测 CLI、配置、机器可读报告和失败归因。
11. 导入 -> 提问 -> 引用 -> 原文 -> 反馈，以及更新/删除后再提问的端到端测试。
12. 阶段 4 验收记录、OpenAPI、架构、README 和故障排查移交文档。

### 3.2 明确不做

- 不实现 `knowledge_qa` Skill、Skill API 或 Skill Web 入口；阶段 4 只交付可复用的问答
  Application 用例，阶段 5 再封装 Skill。
- 不实现摘要、提纲、观点对比、复习卡片或知识写入工作流。
- 不实现多 Agent、Supervisor、开放式工具规划、任意 shell/SQL/URL 调用或 LangGraph 业务绑定。
- 不新增向量库、关键词引擎、任务队列、Agent 框架或前端状态库。
- 不在问答层重写检索、RRF、Reranker、版本过滤或 Space 过滤。
- 不允许模型生成任意 source/document/version/chunk ID；模型只能引用当前运行预先分配的
  Evidence ID。
- 不用模型自报 confidence 代替确定性证据校验；confidence 只能是验证结果和可解释规则的输出。
- 不让 SSE 临时文本成为业务事实；只有通过结构化校验并持久化的终态回答可供后续读取。
- 不在日志、trace、OpenAPI 示例、测试快照或提交报告中保存私有问题、正文、prompt 或引用原文。
- 不自动把反馈加入 frozen dataset，不在查看 holdout 后原地修改 prompt、阈值或用例。
- 不提前实现阶段 6 Eval Dashboard、发布门禁平台或 LLM 微调。

## 4. 目标工程形态

目录只在对应能力实际落地时创建，最终命名以 ADR-007 和现有包模式为准：

```text
packages/
  domain/
    grounded_qa.py              Claim/Evidence/Citation/Answer/Refusal/Conflict 纯类型
    conversations.py            Conversation/Message/Feedback 纯类型和 Repository Port
    agent_runtime.py            复用既有 AgentRun 状态，不建立平行运行模型
  application/
    qa/
      service.py                问答主用例与显式状态编排
      query_planning.py         查询分类、改写和历史裁剪
      context_builder.py        上下文预算、证据编号和不可信数据封装
      verifier.py               claim-evidence、归属、locator 和完整性校验
      evaluation.py             回答指标、失败分类和报告输入
    conversations/              创建/读取会话、取消/重试、反馈审核用例
  infrastructure/
    qa/
      repositories.py           PostgreSQL 会话、运行、引用和反馈 Adapter
      citation_resolver.py      DocumentVersion/Blob/Chunk 到原文片段的解析
      task_dispatch.py          可选 Dramatiq 问答任务投递与幂等恢复
  model_gateway/
    contracts.py                Chat 结构化输出/流式能力边界
apps/
  api/
    routers/
      conversations.py         会话、消息和反馈 API
      qa_runs.py                创建、查询、取消、重试和 SSE API
  worker/
    qa_tasks.py                 仅在 ADR-007 决定持久异步执行时创建
  web/
    src/                        对话工作台、证据抽屉/面板和反馈交互
scripts/
  evaluate_answers.py           版本化回答评测入口
cases/evals/
  configs/                      QA profile、prompt/model 和评测配置
  reports/                      通过隐私扫描的机器可读指标和摘要
migrations/versions/
  <new_revision>_add_grounded_qa_tables.py
docs/
  adr/007-*.md                  问答、引用、持久化和 SSE 语义
  stage-4-acceptance.md         阶段退出时生成的验收记录
```

依赖方向固定为：

```text
Web / HTTP / Evaluation
  -> QA Application Service
      -> SearchService Port
      -> ModelGateway Port
      -> Conversation/Run/Evidence Repository Ports
      -> CitationResolver Port
          -> PostgreSQL / BlobStore / Dramatiq Adapters
```

问答 Application 用例不直接读取 ORM、Blob 路径或 Provider SDK。CitationResolver 必须先校验
Space、Document、DocumentVersion 和 locator 归属，再返回最小必要片段；引用展示不能成为绕过
检索和权限边界的任意文档读取接口。

## 5. 分步实施

### Step 0：冻结启动基线、ADR 和评测协议

1. 核对阶段 0、阶段 2 Step 9 和阶段 3 正式退出记录；当前缺失时只登记 provisional 状态。
2. 新增并接受 ADR-007，固定问答状态、核心实体、持久化、引用生命周期、执行边界和 SSE 语义。
3. 复核 dataset 中 answer/refuse、单文档、跨文档、冲突、版本、恶意文档和双语切片。
4. 决定保留 30 例 v0 的统计限制，或发布扩充后的新 dataset version；不得修改已查看的
   holdout case、split、gold claim 或 evidence。
5. 冻结回答主要指标、分母、失败分类、人工复核规则和目标部署条件。
6. 建立 `QAProfileV1` 和评测配置 schema，初始标记为 `provisional`、
   `formal_runs_enabled: false`。
7. 记录 corpus、dataset、schema、development、holdout、prompt 模板和配置摘要。
8. 关闭 R4-01 至 R4-09，或为每个未关闭项明确阻塞后续哪个 Step。

**门禁**：本 Step 的文档、schema 和合成验证可以立即执行；不得因为计划或 ADR 完成而读取
未批准语料或启动正式评测。

**完成标准**：同一输入可由不同实现者得到相同的 answer/refuse/conflict/failed 判断；配置摘要
唯一标识一次运行；正式运行在门禁未满足时被机器拒绝。

#### 2026-07-23 provisional 实现与验证总结

- 已核对：阶段 0 manifest 仍为 `draft_pending_license_review`；`docs/stage-2-acceptance.md`
  不存在；`docs/stage-3-acceptance.md` 明确阶段 3 未正式退出。因此阶段 4 保持“未正式开始”，
  不读取语料正文、不运行正式回答 holdout，新增业务表和 API/SSE 实现继续受阻。
- 已完成：接受 ADR-007，固定唯一 QA Application Port、Conversation/Message/AgentRun/Evidence/
  Citation/Feedback 的归属边界、不可变 citation 生命周期、Worker/取消/重试、SSE v1 及隐私
  规则。该 ADR 是协议决定，不解除任何阶段门禁。
- 已完成：新增 `QAProfileV1` provisional 配置、JSON Schema、受哈希固定的 provisional prompt
  contract，以及 `qa-eval-config-v1`。配置固定 corpus/dataset/schema、development 20 例和
  holdout 10 例的摘要，以及 answer/refuse/conflict/failed 的分母规则；`failed` 明确单独报告，
  绝不计为 `refuse`。`formal_runs_enabled: false` 由 schema 强制，无法通过这份配置启动正式运行。
- 已复核数据集元数据：30 例中 25 个 answer、5 个 refuse；包含单文档 8、跨文档 8、版本/冲突 2、
  无答案 5、恶意文档 1、双语 3 和代码/自然语言 3。未修改 holdout、split、gold claim 或 evidence。
- 已验证：`tests/unit/test_qa_step0_baseline.py` 覆盖 profile/config schema、正式运行被拒绝，以及
  可用受控输入的 hash 与所需数据集切片；Git 未分发的阶段 0 manifest/dataset 仅在配置仍为
  provisional 且正式运行关闭时允许缺失，干净 CI checkout 不再被误判为输入损坏。
- 未关闭决策：R4-01 的纯领域部分已在 Step 1 完成，仍等待 JSON transport schema 和兼容测试；
  R4-02、R4-03、R4-06、R4-07 等待阶段 0 门禁关闭后再进行持久化/执行实现和隔离集成验证；
  R4-04、R4-05、R4-08、R4-09 分别等待 development 对比、结构化生成、正式 profile/model 冻结
  和反馈审核实现。v0 的 30 例统计限制尚未接受或扩充，必须在读取回答 holdout 前由负责人作
  版本化决定。

### Step 1：建立问答领域契约与状态机

1. 定义 `QuestionInput`、`QueryPlan`、`EvidenceCandidate`、`Claim`、`Citation`、
   `GroundedAnswer`、`Refusal`、`ConflictNotice` 和 `QAResult` 纯类型。
2. Citation 至少固定 `evidence_id`、Space、source/document/version/chunk、locator、状态和
   可验证摘要；禁止只保存展示编号或模型自由文本。
3. 每个可验证 claim 必须引用一个或多个当前运行 Evidence ID；Evidence 可支持多个 claim。
4. 定义 CREATED/QUEUED/RUNNING/VERIFYING/COMPLETED/REFUSED/FAILED/
   CANCEL_REQUESTED/CANCELLED/TIMED_OUT 等状态与合法迁移。
5. 定义稳定错误：输入、Space、检索、模型、结构化响应、引用验证、存储、超时、取消和策略拒绝。
6. 明确空结果是成功的“证据不足”输入，不是检索系统错误；模型错误不得转换为拒答。
7. 复用现有 AgentRun 通用预算和终态概念，避免阶段 4/5 出现两个不兼容的运行模型。

**完成标准**：Domain 不依赖 FastAPI、Pydantic、SQLAlchemy、Dramatiq 或模型 SDK；状态迁移、
非法引用、重复 Evidence ID、未引用 claim 和终态不可重开均有确定性测试。

#### 2026-07-23 provisional 实现与验证总结

- 已完成：新增 `domain.grounded_qa`，提供 `QuestionInput`、`QueryPlan`、`EvidenceCandidate`、
  `Claim`、`Citation`、`GroundedAnswer`、`Refusal`、`ConflictNotice`、`QAResult` 及版本 1 枚举。
  Citation 固定 Evidence、Space、source/document/version/chunk、locator、状态和 excerpt SHA-256；
  claim 必须引用当前回答发布的 Evidence ID，重复/未知/跨 Space/身份不一致引用均确定性拒绝。
- 已完成：定义 QA 生命周期和稳定 `QA_*` 错误码。answer/refuse/conflict/failed 使用互斥 payload；
  `INSUFFICIENT_EVIDENCE` 是唯一 provisional refusal reason，检索、模型、结构、存储、超时、取消
  和策略错误不能伪装成拒答。
- 已完成：QA 状态通过 `project_qa_status` 投影既有 `AgentRun`，复用其预算、步骤和终态；没有新增
  第二个 Run 实体、预算类型、框架或基础设施依赖。CREATED/QUEUED/RUNNING/VERIFYING/
  COMPLETED/REFUSED/FAILED/CANCEL_REQUESTED/CANCELLED/TIMED_OUT 的非法迁移和终态重开被拒绝。
- 已验证：`tests/unit/test_grounded_qa_domain.py` 与既有 Agent Runtime/Retrieval 领域回归共
  37 passed；Ruff format/check 通过；`mypy packages/domain` 通过（11 个源文件）。pytest 仅有
  已记录的 Windows `.pytest_cache` 权限警告。
- 未关闭：R4-01 的 JSON transport schema 与兼容测试留给结构化生成/API 步骤；citation 对当前
  发布版本、tombstone、Blob/hash 和 locator 边界的二次校验属于 Step 2。本步未访问 corpus
  正文、数据库、Provider 或 holdout，阶段 4 状态仍为“未正式开始”。

### Step 2：实现证据绑定、引用解析与安全二次校验

1. 将 SearchHit 映射为当前运行不可伪造的 Evidence ID，保留 `matched/context_only` 区别。
2. `context_only` 块可帮助理解上下文，但不能单独计为检索 gold 命中；是否允许支撑 claim 必须
   在 R4-03 中明确并保持评测一致。
3. 在生成前和发布前分别校验 Space、Source、Document、当前/固定版本、Chunk 和 locator 归属。
4. 实现 locator 解析：Markdown/TXT 使用一基闭区间行号，PDF 使用一基页码；不猜测缺失位置。
5. 通过 DocumentVersion/BlobStore 解析原文时重新校验 blob hash；只返回最小必要片段。
6. 处理历史引用状态：有效、来源已更新、已撤下、已删除、保留期已过和不可解析。
7. 引用失效时保留安全的 tombstone 元数据，不返回其他版本或其他 Space 的“相似文本”替代。
8. 对伪造 ID、越权 Space、旧版本混入、删除竞态、路径遍历和 locator 越界编写回归测试。

**门禁**：门禁关闭前只使用合成版本和批准的 repository fixture；不得读取私有 Blob 验证定位。

**完成标准**：有效 fixture 的 Citation 100% 回到声明的版本和位置；跨 Space、撤下来源和伪造
引用违规为 0；解析失败返回稳定状态而不是错误原文。

#### 2026-07-23 provisional 实现与验证总结

- 已完成：`EvidenceBindingService` 将 `SearchHit` 按稳定 rank 映射为服务端分配的 Evidence ID，
  固定 Space/source/document/version/chunk/locator 和规范化 excerpt SHA-256，并保留
  `matched/context_only`。重复 Chunk、rank 或 Evidence ID 被拒绝。
- 已完成：`EvidenceVerifier` 在生成前和发布前分别查询 `CitationTargetPort`，重复校验 Space、
  target 身份、当前发布版本、Chunk 和 locator，覆盖检索后版本切换的 TOCTOU 竞态。ADR-007
  已固定：`context_only` 可辅助或共同支撑，但不能成为任何 claim 的唯一证据，也不计为 retrieval
  gold hit。
- 已完成：`CitationResolver` 从固定 DocumentVersion 的 Blob key 读取原始字节并重新校验
  `blob_hash`；文本按明确 encoding 和一基闭区间行号提取，PDF 通过既有 `Parser` Port 按一基单页
  提取。缺失/越界/格式不匹配/摘要不一致/路径遍历返回 `invalid` 或 `unavailable`，不猜测位置。
- 已完成：历史 Citation 区分 `valid`、`source_updated`、`withdrawn`、`deleted`、
  `retention_expired`、`unavailable` 和 `invalid`。`source_updated` 只解析固定旧版本；撤下、删除、
  过期和不可用状态不读取 Blob、不返回片段，也不重定向到相似文本或新版本。
- 已验证：`tests/unit/test_qa_evidence.py` 与领域、BlobStore、Parser 回归共 61 passed；新增模块
  Ruff format/check 通过；精确 mypy 3 个源文件和规范 `mypy apps packages` 67 个源文件均通过。
  pytest 仅有已记录的 Windows `.pytest_cache` 权限警告。
- 未关闭：当前 `CitationTargetPort` 只用合成内存快照验证；PostgreSQL Adapter、真实 retention
  事实源和批准 `repository_fixture` 的 locator golden 验收需等待相应数据/迁移门禁。本步未读取
  corpus 正文、未新增表或 API，也未运行 Provider/holdout；阶段 4 仍为“未正式开始”。

### Step 3：实现查询分类、改写和上下文构建

1. 建立确定性的输入规范化、长度限制、语言/代码切片和问题类型分类。
2. 默认以原问题检索；查询改写/分解是有界可选步骤，必须保留原问题并限制子查询数量。
3. 改写失败、超时或返回非法结构时回退原问题，并记录安全诊断，不把原始问题写入日志。
4. 每个查询只调用 `SearchService.search(...)`；多查询结果合并必须确定性去重并保留来源诊断。
5. 构建 `ContextBundle`：系统规则、问题、必要会话摘要、Evidence 元数据和文本分别封装。
6. 将文档片段标记为不可信数据，使用稳定分隔和 Evidence ID；片段内指令不能进入系统消息。
7. 按 QA profile 执行总 Token、单来源、单文档、单块和会话历史预算，避免单一来源垄断上下文。
8. 预算裁剪不得产生超出原 locator 的“拼接引用”，也不得改变 Evidence ID 与内容映射。
9. 在 development 上分别比较原问题和改写策略；没有可复现净收益时默认关闭改写。

**完成标准**：相同输入和 profile 产生相同 Evidence 顺序及上下文摘要；预算永不超限；改写不能
扩大 Space/filter；prompt injection fixture 不能改变系统策略。

#### 2026-07-23 provisional 实现与验证总结

- 已完成：新增 `QAPlanningProfileV1` 受信配置投影，补齐改写 timeout、历史 Token、单来源
  Evidence 和单文档 Chunk 预算；`qa-v1.yaml` 已更新 profile SHA-256，现有 schema/hash 门禁测试
  继续通过。Token 预算采用 UTF-8 字节数作为确定性保守上界，不依赖 Provider tokenizer。
- 已完成：`QueryPlanner` 复用检索层规范化与中英/混合/代码切片，确定性分类 factual/
  comparison/procedural/synthesis。默认只保留原问题；可选 rewriter 有最大查询数和 timeout，重复、
  原问题、空白、越界、异常或超时均回退原问题，诊断只含长度、计数和安全 reason，不含问题正文。
- 已完成：`QASearchCoordinator` 对每个查询只调用 `SearchService.search(...)`，从同一不可变
  `SearchRequest` 复制 Space、filters、mode 和 execution context；多查询结果按 matched、查询序号、
  rank 和 Chunk ID 确定性去重，身份冲突立即失败，并保留每个查询的检索诊断而不记录查询文本。
- 已完成：`ContextBuilder` 将系统规则、问题、会话历史和 Evidence 分离；Evidence 使用稳定
  `untrusted_document` 边界和服务端 ID。历史按最近消息裁剪，Evidence 按总量、总 Token、历史、
  单来源、单文档和单块预算选择；超长块整块跳过，不截断/拼接 locator 或改变 Evidence 映射。
  相同输入/profile 产生相同顺序和不含正文的 safe summary hash。
- 已验证：改写成功/超时/非法回退、分类、Space/filter 不扩张、多查询去重、历史/来源/文档/块预算、
  整块裁剪和文档 prompt injection 隔离均有 deterministic fake 测试；Step 0～3 相关测试 36 passed，
  Ruff format/check 通过，规范 `mypy apps packages` 70 个源文件通过。pytest 仅有已记录的
  Windows `.pytest_cache` 权限警告。
- 未执行：阶段 0、阶段 2 Step 9 和阶段 3 正式退出未关闭，不能使用 development 问题和真实模型
  比较原问题/改写收益。R4-04 因此保持未关闭，`rewrite_enabled: false` 不变；本步没有读取 corpus
  正文、调用 Provider、运行 holdout、增加表或公开 API，阶段 4 仍为“未正式开始”。

### Step 4：实现结构化回答生成与完整性校验

1. 通过 `ModelGateway.fast_chat` 调用模型，固定 temperature、Token 上限、timeout、模型 identity
   和 prompt version。
2. 模型只能返回版本化结构：answer、claims、Evidence ID、limitations 和建议的结果类型。
3. 传输层或 Adapter 使用结构化解析器/schema 校验，不通过字符串或正则猜测引用。
4. 校验每个关键 claim 有 Evidence、Evidence 属于本次运行、locator 可解析、引用未跨 Space，
   且回答中没有未知 Citation ID。
5. 定义有界修复：只允许一次结构修复或按 QA profile 固定次数；修复仍失败则返回明确失败，
   不发布部分回答。
6. 发布前计算支持率和完整率；未达到阈值时转为拒答或低置信限制，具体规则由 R4-05 固定。
7. confidence 由确定性验证信号产生，模型自报值只能作为非权威输入。
8. 保存 prompt/model/profile/retrieval/corpus/dataset 版本和 Token/延迟计数，不保存原始 prompt。

**门禁**：门禁关闭前仅使用 deterministic fake 验证 schema、错误和校验路径；真实模型结果不
构成质量证据。

**完成标准**：模型无法伪造 Evidence、绕过 Space 或发布 schema 非法回答；相同 fake 输入得到
确定性结果；失败不会留下可读取的伪终态答案。

### Step 5：实现拒答、来源冲突和故障语义

1. 证据为空、低于最低支持门槛或无法覆盖关键 claim 时返回 `REFUSED_INSUFFICIENT_EVIDENCE`。
2. 多来源在版本、时间或事实陈述上冲突时，保留双方 Evidence，返回结构化 ConflictNotice，
   不静默选择单一来源或合并成虚假共识。
3. 模型不可用、超时、限流、鉴权、策略拒绝、检索失败和数据库失败保持独立错误码。
4. 用户取消返回 CANCELLED；超时返回 TIMED_OUT；二者不进入回答正确率或拒答准确率分母。
5. 重试只针对明确可重试错误，并创建新的 attempt/run identity 或版本化 attempt 记录；终态不重开。
6. 结构化回答校验失败不得降级为无引用自由文本。
7. 删除或撤下来源与正在执行的问答发生竞态时，在发布前重新校验；失效 Evidence 导致重新检索、
   拒答或失败，不能发布旧快照为当前答案。

**完成标准**：无答案、冲突、模型故障、检索故障、取消和超时可由 API/Web/评测无歧义区分；
错误响应和日志不泄漏正文、prompt 或 Provider 响应。

### Step 6：落地会话、运行、引用与反馈持久化

1. 按 ADR-007 落地最小业务实体和 Repository Port，不提前复制阶段 5 的 Registry/Checkpoint 表。
2. Conversation 固定 Space；Message/Run/Evidence/Feedback 必须能沿外键或应用约束追溯到同一 Space。
3. 问题、回答和引用正文按来源敏感度继承本地数据策略；日志和审计只引用 ID。
4. Run 保存请求幂等键、状态、attempt、取消、超时、错误、版本清单、Token 和阶段耗时。
5. Evidence/Citation 保存不可变引用身份和 locator；原文片段优先按需解析，避免无必要复制私有正文。
6. Feedback 与目标 Message/Run 绑定，保存审核状态；重复提交按幂等键处理。
7. 新增 Alembic revision、外键、唯一约束和索引；验证 upgrade、downgrade 和单一 head。
8. 明确事务边界：创建 Run 先提交数据库，再投递；完成回答、claims 和 citations 原子发布。
9. 对重复请求、重复消息投递、进程中断、终态重放、并发取消和删除竞态编写集成测试。

**门禁**：本 Step 涉及新增业务表，必须等待阶段 0 门禁和 ADR/数据模型归属确认；此前只允许
内存 Repository 和迁移设计评审。

**完成标准**：数据库约束阻止跨 Space 归属和重复业务事实；故障不会暴露部分终态；迁移往返、
幂等重入和并发状态转换通过隔离 PostgreSQL 测试。

### Step 7：提供问答 API、SSE、取消与重试

1. 通过版本化 HTTP 端点创建/查询 Conversation、提交问题、查询 Run、取消、重试和反馈。
2. 问答运行先持久化再执行；若 ADR-007 选择 Worker，复用 Redis/Dramatiq，不新增队列实现。
3. 定义 SSE v1 事件：事件 ID、schema version、run ID、sequence、时间、类型和安全 payload。
4. 事件至少覆盖 accepted/started/phase/evidence/answer_delta/completed/refused/failed/
   cancel_requested/cancelled/heartbeat；最终终态只能出现一次。
5. 在流式文本开始前固定 Evidence/Citation ID 或采用不会重排展示编号的协议；最终回答校验失败时
   客户端必须丢弃 provisional delta。
6. 明确客户端断连不等于取消；取消只能通过显式命令和持久状态协作完成。
7. 支持 `Last-Event-ID` 或等价 sequence 恢复；重连不得重复发布业务终态或改变 Citation 编号。
8. 对慢客户端、重复连接、断线、乱序、重复事件、Worker 崩溃、模型超时和 API 重启编写测试。
9. 更新错误映射、请求/响应 Pydantic schema、OpenAPI 和 API 隐私日志测试。

**完成标准**：SSE 事件有严格单调 sequence 和唯一终态；取消/重试可恢复且幂等；API 只调用
Application 用例；`docs/openapi.json` 与运行时 schema 一致。

### Step 8：实现 Web 对话工作台与证据查看器

1. 在现有工作台中增加 Space 范围的 Conversation 列表、消息区、输入区和运行状态。
2. 桌面端使用稳定的主工作区和证据面板；小屏幕将证据区切换为抽屉，不遮挡回答和控件。
3. 流式阶段显示真实状态；完成后只渲染服务端已验证的 claims/citations，不从文本重新解析编号。
4. 点击 Citation 打开对应 source/version/locator，显示最小原文片段并高亮；失效引用显示明确状态。
5. 覆盖空知识库、无结果、拒答、冲突、模型不可用、回答中断、取消、重试和引用失效。
6. 重试创建新 attempt 并保留原失败记录；取消按钮只在可取消状态可用。
7. 反馈控件提交赞/踩和可选文字，明确进入待审核候选而非直接改变评测集。
8. 保持键盘可操作、焦点清晰、状态不只依赖颜色；Citation 编号和证据面板稳定对应。
9. 不在前端散布环境相关 API 地址，继续通过同源 `/api` 访问后端。

**完成标准**：核心状态在桌面和移动 viewport 无重叠、文本溢出或 Citation 跳号；键盘和屏幕阅读
顺序可用；前端测试覆盖断线、取消、重试、拒答和失效引用。

### Step 9：实现反馈审核和评测候选导出

1. Feedback 保存用户判断、可选说明、目标回答版本和安全元数据，不在日志记录说明正文。
2. 反馈初始状态为 pending_review；只有人工复核授权、脱敏、标准答案和 Evidence 后才能进入
   新 dataset version。
3. 导出只生成候选包，不修改现有 JSONL、manifest、split 或 holdout。
4. 对来源已删除、未授权引用、包含个人信息、秘密或恶意内容的候选拒绝导出并给出安全错误。
5. 导出记录原 run 的 corpus/dataset/retrieval/prompt/model/profile 版本，便于失败归因。
6. 同一 Message/Run 的重复反馈按明确规则合并或并存，不静默覆盖历史记录。

**完成标准**：反馈不能直接污染冻结评测集；候选导出可审计、可去重、经过隐私检查且不包含
未批准正文。

### Step 10：建立回答评测、消融和默认配置

1. 实现 `evaluate_answers.py`，先支持 `--validate-only`，正式运行受 corpus/dataset/config
   状态和显式确认参数阻断。
2. 主要指标至少包含：Supported-claim rate、Citation precision/completeness、Citation target
   resolution、Refusal accuracy、Conflict handling accuracy、跨 Space/撤下来源违规、P50/P95、
   Token 和失败率。
3. 将 parser、retrieval、context、generation、verification、citation、refusal、policy 和
   infrastructure 分开归因，不能只报告总正确率。
4. 无答案用例不进入 supported-claim 分母；基础设施失败不进入拒答正确率；每个指标记录明确分母。
5. LLM-as-judge 只能作为辅助信号；关键 case 使用规则、Evidence 匹配和人工复核。
6. 在 development 上比较预注册的查询改写、上下文预算、prompt 和模型候选；一次实验只改变一个
   主要变量。
7. 冻结一个默认 `QAProfileV1`、prompt version、Chat model identity 和 retrieval profile，生成
   config hash；冻结后不得查看 holdout 再调参。
8. 只在所有门禁满足后运行一次正式回答 holdout；发生模型/profile 降级、配置不匹配或基础设施
   故障时整次作废并保存原因，不拼接多次结果。
9. 报告只提交经过隐私扫描的版本、指标、计数、耗时和失败分类，不提交问题、回答或引用正文。

**完成标准**：同一配置的 development 结果可复现；默认配置选择有逐 case 诊断证据；正式
holdout 达到阶段 0 冻结阈值，且没有 Space/撤下/版本安全违规。

### Step 11：端到端验收与文档移交

1. 在隔离 PostgreSQL/Redis、独立 Blob 根和批准语料上验证导入 -> 发布 -> 提问 -> 引用 ->
   原文 -> 反馈完整旅程。
2. 验证文档修改、移动、删除、重新摄入后，新回答只引用当前发布版本，历史引用按 ADR 状态显示。
3. 验证模型、Embedding、Reranker、数据库、Redis、Worker 和网络故障下的提示、取消和恢复。
4. 运行单元、契约、集成、OpenAPI、迁移、前端、Playwright、安全、隐私和回答评测门禁。
5. 验证 Compose 空卷启动、健康依赖、问答 Worker（如有）、保留卷重启和模型不可用时管理面可用。
6. 扫描日志、trace、报告、截图和 fixture，确认无密钥、私有问题、正文、prompt 或 Provider 响应。
7. 更新 README、architecture、troubleshooting、OpenAPI、ADR 和阶段状态。
8. 生成 `docs/stage-4-acceptance.md`，记录命令、环境、版本摘要、指标、已知限制和退出结论。
9. 向阶段 5 移交唯一问答 Application Port、GroundedAnswer schema、运行/引用持久化和 SSE 协议。

**完成标准**：阶段退出条件全部有实际输出支持；没有实际执行的命令不得标为通过；阶段 5 能在
不复制检索、生成或引用逻辑的情况下封装 `knowledge_qa` Skill。

## 6. 核心契约与协议

### 6.1 GroundedAnswer v1

建议的领域输出形态如下，最终字段由 R4-01 和版本化 schema 固定：

```json
{
  "schema_version": "grounded-answer-v1",
  "result_type": "answer",
  "answer": "...",
  "claims": [
    {
      "claim_id": "c1",
      "text": "...",
      "evidence_ids": ["e1"]
    }
  ],
  "evidence": [
    {
      "evidence_id": "e1",
      "source_id": "...",
      "document_id": "...",
      "document_version_id": "...",
      "chunk_id": "...",
      "locator": {"type": "lines", "start": 12, "end": 18},
      "status": "available"
    }
  ],
  "confidence": "high",
  "limitations": []
}
```

`refusal` 和 `conflict` 使用同一版本化 envelope，但有各自必填字段；错误不伪装成任何一种业务
结果。API 展示编号可由 Evidence 顺序派生，但持久身份必须使用稳定 `evidence_id`。

### 6.2 Citation 解析规则

Citation 有效必须同时满足：

1. 属于请求 Space。
2. Source、Document、DocumentVersion 和 Chunk 归属链一致。
3. 新回答只引用当前发布且未 tombstone 的 DocumentVersion。
4. locator 类型受支持、边界一基且不越界。
5. 展示片段来自声明版本，原始字节 hash 与版本/manifest 事实一致。
6. Evidence ID 由服务端分配且属于当前 Run，模型不能创建或修改其目标。
7. 发布时再次验证，防止检索后删除、撤下或版本切换竞态。

### 6.3 SSE v1 不变量

- 每个事件含 `schema_version`、`event_id`、`run_id`、`sequence`、`type` 和安全 payload。
- sequence 在单 Run 内严格递增；重放可以重复传输，但客户端按 event ID 幂等处理。
- `completed`、`refused`、`failed`、`cancelled`、`timed_out` 只能出现一个终态。
- delta 是 provisional 展示，不可单独查询为已完成回答；最终结构化结果是业务事实。
- 客户端断连不自动取消 Run；显式取消通过持久状态传播。
- 心跳不包含问题、回答、正文、prompt、引用片段或模型原始响应。
- Citation 展示顺序在首次公布后不变；最终校验失败时不得留下看似有效的引用。

### 6.4 持久化和保留原则

- Conversation 固定一个 Space，不能在运行中切换。
- Message/Run/Evidence/Citation/Feedback 的归属可从数据库约束和 Application 校验双重证明。
- 问题、回答、反馈文字和引用片段继承源数据敏感度，默认只本地保存。
- Evidence 尽量保存 ID、版本和 locator；原文按需解析，避免复制形成难以清理的影子语料。
- 来源更新不改写历史 Citation；来源撤下/删除改变其可解析状态，不把历史目标悄悄重定向到新版本。
- Retry 产生新 attempt，保留原失败事实；同一幂等键不会生成多个活动 Run。
- PostgreSQL 是状态事实源；队列成功投递不等于运行成功。

## 7. 配置与版本策略

### 7.1 QAProfileV1

至少包含：

- profile/schema version。
- 查询分类和改写开关、子查询上限、改写 timeout 和失败策略。
- 使用的 `RetrievalProfileV1` identity。
- Evidence 数量、单来源/单文档限制、相邻上下文规则和 Token 预算。
- 会话历史轮数/Token 预算和摘要策略版本。
- Chat capability alias、model identity、prompt version、temperature、最大 Token 和 timeout。
- 结构化解析、有限修复次数、claim 支持阈值、引用完整率和拒答策略。
- 执行模式、总 timeout、重试、取消检查点和 SSE schema version。

profile 使用稳定 UTF-8 canonical JSON 计算 SHA-256。任何会改变查询、上下文、生成、验证、
拒答或流式语义的配置都必须进入摘要。

### 7.2 Prompt 和模型版本

- 系统 prompt、回答 schema、文档不可信边界和修复 prompt 分别版本化并进入总摘要。
- 以能力别名引用模型，但报告必须记录实际 Provider、模型、revision 和关键参数。
- 真实模型必须通过与 fake 相同的结构化契约；Provider 特有字段不能进入 Domain。
- 模型或 prompt 变化必须在 development 重跑，不能直接复用旧报告或旧 holdout 结论。

### 7.3 评测报告版本

`answer-report-v1` 至少记录：

- corpus/dataset/split/schema 摘要。
- parser/chunker/embedding/retrieval/QA profile/prompt/model identity。
- 运行环境、配置摘要、开始/结束时间和正式资格。
- 分指标分子、分母、切片、P50/P95、Token、失败率和安全违规数。
- 逐 case 的 ID、结果类型、指标和失败分类，不包含问题、回答或引用正文。
- 降级、重试、取消、无效运行原因和人工复核状态。

## 8. 测试与验证矩阵

| 层级 | 必测内容 | 主要证据 |
| --- | --- | --- |
| 单元 | 状态迁移、Evidence ID、claim 覆盖、上下文预算、排序、阈值、错误分类 | pytest deterministic tests |
| 属性 | Citation 不越界、预算不超限、输入顺序不破坏稳定性、终态不可重开 | property/parameterized tests |
| 契约 | SearchService、ModelGateway、Repository、CitationResolver、SSE schema | fake 与真实 Adapter 共用测试 |
| Golden | Markdown/TXT 行号、PDF 页码、版本固定、高亮和失效引用 | 公开 fixture 快照 |
| 集成 | PostgreSQL 约束、迁移、原子回答发布、幂等、队列、取消和恢复 | 隔离 PostgreSQL/Redis |
| API | 输入上限、Space、错误映射、SSE 重连/乱序/终态、OpenAPI | FastAPI/HTTP 测试 |
| Web | 流式状态、引用面板、拒答、冲突、取消、重试、键盘和响应式布局 | Vitest/Playwright |
| Eval | supported claim、引用准确/完整/解析、拒答、冲突、延迟和 Token | answer-report-v1 |
| E2E | 导入 -> 提问 -> 引用 -> 原文 -> 反馈；更新/删除 -> 再提问 | Playwright + 隔离依赖 |
| 安全 | 跨 Space、撤下版本、伪造 citation、prompt injection、日志/报告泄漏 | 回归测试与隐私扫描 |
| 故障 | 模型/检索/DB/Redis/Worker 超时、重复投递、断连、取消、重启 | fault-injection tests |

规范验证命令沿用仓库基线；新增公开 API 后必须重新导出 OpenAPI，新增迁移后必须验证单一 head、
upgrade/downgrade 和隔离数据库。Playwright 截图不得包含私有语料。

## 9. 阶段退出条件

阶段 4 只有同时满足以下条件才可标记完成：

1. 阶段 0、阶段 2 Step 9 和阶段 3 正式退出均有可核验记录。
2. 核心旅程 A/B 自动化通过：批准语料导入后可提问、查看引用并回到正确原文位置。
3. Citation target resolution 为 100%，跨 Space、撤下来源和错误版本违规为 0。
4. 冻结 holdout 达到阶段 0 最终确认的 Supported-claim、引用准确/完整和 Refusal 门槛。
5. 冲突来源不被静默合并；无证据拒答与模型/检索/基础设施故障可区分。
6. SSE、取消、重试、重连和唯一终态通过契约与集成测试。
7. Conversation/Run/Evidence/Feedback 持久化幂等、可恢复，迁移和保留语义通过验证。
8. Web 桌面/移动布局、键盘操作、失败状态和 Citation 编号稳定性通过 Playwright 验证。
9. 模型、prompt、retrieval、QA profile、corpus 和 dataset 版本可从每个回答和报告追溯。
10. 默认配置下的 P95、Token 和失败率达到冻结预算；没有未说明的降级或严重回归。
11. 日志、trace、报告、截图和测试产物不包含密钥、私有正文、问题、prompt 或 Provider 响应。
12. README、architecture、troubleshooting、OpenAPI、ADR 和 `stage-4-acceptance.md` 完成移交。

任一条件未满足时，阶段状态保持“工程验证中”或“等待门禁”，不得以 UI 演示成功替代正式退出。

## 10. 主要风险与控制

| 风险 | 早期信号 | 控制 |
| --- | --- | --- |
| 检索质量不足被回答层掩盖 | 流畅答案但 gold Evidence 未召回 | 先看阶段 3 召回；按 pipeline 分层归因 |
| 有引用但不支持 claim | Citation 可打开但语义无关 | claim-evidence 结构、发布前校验、人工抽检 |
| 模型伪造引用 | 返回未知 ID、source 或 locator | 服务端预分配 Evidence ID，未知 ID 直接失败 |
| prompt injection | 文档要求改指令、扩大权限或泄露数据 | 不可信数据封装、无工具权限、安全 fixture |
| Space/版本泄漏 | 引用来自其他 Space 或撤下版本 | 检索前过滤、生成后双检、数据库约束 |
| 流式内容与最终答案不一致 | 用户看到后来被校验拒绝的文字 | delta 明确 provisional，最终结构为唯一事实 |
| SSE 断线导致重复运行 | 重连后新建 Run 或重复终态 | Run ID/sequence/Last-Event-ID/幂等键 |
| 取消仅停 UI 不停后台 | 模型或 Worker 继续执行并发布答案 | 持久取消状态、协作检查、发布前终态校验 |
| 会话历史无限增长 | Token/延迟持续上升、旧指令污染 | 版本化裁剪/摘要策略和硬预算 |
| 反馈污染 holdout | 用户问题直接进入 frozen JSONL | pending review、版本化导出、禁止原地修改 |
| 小样本指标失真 | 5 个拒答样本造成 20% 跳变 | 冻结前扩充新版本或明确统计限制 |
| 私有内容进入日志/报告 | trace、SSE 诊断或截图出现正文 | ID/计数日志、集中脱敏、提交前扫描 |
| 阶段 4/5 重复运行模型 | 两套 AgentRun/SSE/取消语义 | 复用 Domain Port，由 ADR-007 固定共同边界 |

## 11. 阶段 5 移交清单

阶段 4 完成后必须向阶段 5 提供：

1. 唯一的 Grounded QA Application Port；`knowledge_qa` Skill 只调用该 Port。
2. `GroundedAnswerV1`、Refusal、Conflict、Citation 和稳定错误 schema。
3. Conversation/Message/AgentRun/Evidence/Feedback 的领域及持久化边界。
4. 版本固定的 `QAProfileV1`、prompt/model/retrieval identity 和 config hash。
5. SSE v1、取消、重试、超时和恢复协议，Runtime API 不建立平行事件模型。
6. Space、当前发布版本、tombstone、locator 和 prompt injection 的安全不变量。
7. answer-report-v1 执行器、development/holdout 门禁和失败分类。
8. Web/API/Test 调用同一 Application 用例的契约证据。

阶段 5 不得在 Skill workflow 中重新拼接检索 SQL、自由生成 citation、绕过拒答校验或直接读取
会话/检索私有表。

## 12. 阶段 0 关闭后的正式执行顺序

1. 接收阶段 0 退出记录，校验 manifest、来源 SHA-256、授权、人工标注和 dataset 版本。
2. 完成阶段 2 Step 9 正式验收，保存解析质量、定位、幂等、发布、删除和恢复证据。
3. 按阶段 3 验收清单完成真实模型 development 消融、默认检索配置冻结和一次性 holdout。
4. 回到 Step 0 冻结阶段 4 dataset、指标、ADR-007、QA profile schema 和运行条件。
5. 依次完成 Step 1～9，并仅在 development 上选择查询、上下文、prompt 和 Chat 模型配置。
6. 冻结 QA config hash，先执行 `--validate-only`，再运行一次正式回答 holdout。
7. 若 holdout 未达标，保持阶段未退出；使用新 development 数据修复并发布新的版本化配置，
   不修改或反复试探既有 holdout。
8. 完成 Step 11 的端到端验收、隐私扫描、文档归档和阶段 5 移交。
