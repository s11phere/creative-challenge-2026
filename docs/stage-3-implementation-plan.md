# 阶段 3 实施计划：混合检索与评测基线

> 文档状态：Draft v1
>
> 适用范围：`docs/project-implementation-plan.md` 中的阶段 3
>
> 计划基线日期：2026-07-20

## 1. 结论摘要

阶段 3 的目标是用可复现数据证明检索链路有效，而不是实现回答生成、引用绑定或
`knowledge_qa` Skill。阶段 2 已经提供已发布 `DocumentVersion`、带定位信息的 `Chunk`、
768 维 pgvector 列、摄入状态机和原子发布边界；阶段 3 在这些入口之上实现关键词、向量、
混合和可选精排四种检索配置，并建立版本化离线评测基线。

标准检索链路固定为：

```text
SearchRequest
  -> Space/元数据/当前发布版本过滤
  -> Keyword 候选 + Dense 候选
  -> 加权 RRF 融合
  -> 去重
  -> 可选 Reranker
  -> 父块/相邻块扩展
  -> SearchResult + 分阶段诊断
```

阶段 3 按以下原则推进：

- 先建立精确、可诊断的单路基线，再做融合和精排；不允许只报告最终总分。
- Space 隔离、已发布版本和 tombstone 过滤属于候选集合边界，不能在召回后补救。
- 真实 Embedding 默认通过本地模型服务提供；私有语料不得发送到外部 Provider。
- 模型、索引、查询指令、归一化、检索配置和评测数据全部显式版本化。
- CI 和确定性契约测试继续使用 fake；真实模型只用于受控集成测试和离线评测。
- 阶段结束时只能激活一个默认 Embedding 模型和一个默认 retrieval profile；候选模型按同一
  契约顺序评测，不在运行时并存为多套业务实现。

## 2. 启动条件与当前缺口

### 2.1 前置门禁

正式执行阶段 3 前必须满足：

1. 阶段 0 完成语料授权复核、人工标注复核和版本冻结，
   `cases/evals/corpus/v0/manifest.yaml` 不再处于 `draft_pending_license_review`。
2. 阶段 2 Step 9 完成正式质量验收，P0 格式解析、幂等摄入、原子发布、删除撤下和失败恢复
   在批准语料上通过。
3. 每次读取评测来源前校验原始字节 SHA-256，且只处理 manifest 允许列表。
4. 冻结 development/holdout 划分和指标口径；holdout 不参与模型、分块或检索参数调优。

门禁关闭前允许使用合成输入和 manifest 明确允许 `repository_fixture` 的公开来源完成契约、
SQL、API 和安全工程验证，但不得生成正式检索基线或宣称阶段 3 达标。

### 2.2 阶段 2 提供的入口

- `Space -> Source -> Document -> DocumentVersion -> Chunk` 已形成明确归属链。
- `Document.current_version_id` 是唯一发布指针；失败候选版本不进入可检索集合。
- `Document.deleted_at` 先撤下内容，再异步清理派生数据。
- `Chunk` 已保存文本、定位 metadata、`chunk_hash` 和 `Vector(768)`。
- `DocumentVersion` 已保存 parser、normalizer、chunker、embedding 和处理配置版本。
- ModelGateway 已提供 `embedding_zh` 能力别名、fake 和 OpenAI-compatible Adapter。
- `Space.retrieval_profile` 已预留 `top_k`、`rerank_k` 和 `fusion_alpha` 等配置。

### 2.3 必须先关闭的工程缺口

1. **已于 Step 2 关闭**：ModelGateway 已按 Chat/Embedding 能力独立配置和报告健康状态，
   Embedding-only 本地服务不再要求部署 Chat 模型。
2. **已于 Step 2 关闭实现缺口**：`Chunk.meta` 已映射非空的 `parent_ordinal`、
   `prev_ordinal`、`next_ordinal`；待阶段 0 门禁关闭后再对冻结语料执行正式重建。
3. **已于 Step 3 关闭**：新 Alembic revision 已为 `chunks` 增加持久生成的 FTS 文档列和
   GIN 索引，既有迁移未修改，旧数据回填和降级保留已验证。
4. **已于 Step 3 建立工程基线**：exact cosine 为默认路径；IVFFlat 已记录 `lists`、`probes`、
   shortlist、ANALYZE 状态、执行计划和 exact overlap。阶段 0 冻结语料上的正式门槛未执行，
   因此近似路径尚未激活为默认配置。
5. 现有 `RetrievalProfile` 字段不足以无歧义描述候选数、RRF 常数、最终条数、扩展窗口、
   Reranker 开关和降级策略，需要定义版本化 profile schema 和兼容默认值。
6. 评测集已有问题、证据和标签，但还没有“证据 locator 如何映射到 Chunk”的统一算法、
   检索指标实现、运行清单和机器可读报告 schema。
7. 当前数据集只有 30 个问题（development 20、holdout 10），而总实施计划建议首期准备
   60～100 个高质量问题。阶段 0 冻结前必须决定是发布扩充后的新数据集版本，还是保留 v0
   并在报告中明确统计限制；不得在查看 holdout 结果后原地修改用例。

### 2.4 已固定的架构约束

- 遵守 ADR-001：保持模块化单体；同步检索在 API/Application 边界内有界执行，不为检索新增
  第二套任务队列或微服务。
- 遵守 ADR-002：首期检索后端为 PostgreSQL FTS + pgvector，通过 `RetrievalStore` Port
  隔离；没有评测证据时不引入 Qdrant、OpenSearch 或另一套向量库。
- 遵守 ADR-004：本地优先，外部 Provider 默认关闭，私有文档、查询和派生向量不外发。
- 遵守 ADR-005：只读取当前发布版本；Embedding 仍为 768 维，模型变化产生新
  `embedding_version` 并全量重建，不能混用不兼容向量。
- `domain` 只包含纯类型和 Port；`application` 编排检索；SQL、模型服务和 HTTP Adapter
  位于 `infrastructure` 或 `model_gateway`。
- PostgreSQL FTS 的首期排序函数是 `ts_rank_cd`，文档和报告必须称为 Keyword/FTS 基线，
  不得误称为 BM25。只有评测证明该基线不足时，才评估中文分词或新的关键词检索 Adapter。

### 2.5 决策登记

阶段 3 开始时建立并关闭以下决策项；只要不触发 ADR 条件，可记录在本计划和评测配置中：

| ID | 决策 | 关闭条件 |
| --- | --- | --- |
| R3-01 | `RetrievalStore`、SearchResult 和分阶段诊断契约 | 纯领域类型、错误语义和契约测试通过 |
| R3-02 | FTS 配置、查询语法、排序和中文失败触发条件 | exact SQL 基线和语言切片报告可复现 |
| R3-03 | 本地 Embedding 模型、revision、指令、归一化和精度 | 候选按同一数据与 profile 完成 development 对比 |
| R3-04 | 加权 RRF、去重、扩展顺序和参数范围 | 单元性质测试与离线消融实验通过 |
| R3-05 | Reranker 能力别名、失败降级和默认开关 | fake/本地 Adapter 契约及相对收益报告通过 |
| R3-06 | 评测指标口径、运行清单、报告 schema 和延迟预算 | 在查看 holdout 结果前冻结配置 |

若实现需要更换 PostgreSQL/pgvector、引入新的检索服务、改变 768 维契约或允许私有语料
外发，必须先新增或更新 ADR，不能仅修改本计划。

## 3. 范围

### 3.1 本阶段交付

1. `RetrievalStore` Port、检索领域输入输出、稳定错误和分阶段诊断契约。
2. 只读取指定 Space 当前发布版本的 PostgreSQL 候选集合边界。
3. PostgreSQL Keyword/FTS 和 pgvector Dense 两条独立检索路径。
4. 加权 RRF 混合检索、去重、父块/相邻块扩展和确定性排序。
5. 可配置 Reranker Port、fake/no-op 实现及一个本地真实 Adapter。
6. Embedding-only 能力配置、本地真实模型部署、模型版本固定和全量重建流程。
7. `POST /api/v1/spaces/{space_id}/search` 检索 API 和更新后的 OpenAPI。
8. 离线评测命令、证据到 Chunk 的映射、指标计算、配置清单和机器可读报告。
9. 关键词、向量、混合、混合+精排四条可比较基线及失败分类报告。
10. 默认 retrieval profile、阶段 3 验收记录及运行/故障排查文档。

### 3.2 明确不做

- 不实现查询分类、LLM 查询改写、问题分解或多轮会话；这些属于阶段 4。
- 不生成答案，不绑定 claim-evidence，不实现引用完整性校验或证据不足拒答。
- 不实现 SSE、Conversation、AgentRun、Web 对话工作台或 `knowledge_qa` Skill。
- 不引入知识图谱、多模态检索、学习排序、在线训练或模型微调。
- 不因单次榜单结果同时保留多个默认 Embedding/Reranker Provider。
- 不把真实语料、查询正文、Embedding 数组、模型响应或私有引用片段写入日志和提交报告。
- 不把 Keyword/FTS 分数与 Dense cosine 分数直接线性相加；跨路融合只使用排名或经过验证
  的校准方法。
- 不在阶段 3 顺带替换解析器、重写分块器或扩展 P1 文件格式；分块参数只作为受控评测变量。

## 4. 目标工程形态

目录只在对应能力落地时创建：

```text
packages/
  domain/
    retrieval.py                 检索纯类型、profile、错误和 RetrievalStore Port
  application/
    retrieval/
      search.py                  Space 校验、双路召回、融合、精排、扩展编排
      evaluation.py              评测用例执行与失败分类，不读取具体文件格式
  infrastructure/
    retrieval/
      postgres_store.py          published candidate、FTS、pgvector SQL
      evidence_mapping.py        locator 到 Chunk 的确定性映射
  model_gateway/
    contracts.py                 Embedding/Reranker 能力级契约
    openai_compatible.py         本地 Embedding/Reranker HTTP Adapter
apps/
  api/
    routers/search.py            Space-scoped 检索 API
scripts/
  evaluate_retrieval.py          离线评测入口
  rebuild_embeddings.py          指定版本的受控重建入口
cases/evals/
  configs/                       版本化 retrieval/eval 配置
  reports/                       经过隐私检查的机器可读指标和摘要
migrations/versions/
  <new_revision>_add_chunk_fts.py
docs/
  stage-3-acceptance.md           阶段退出时生成的验收记录
```

`PostgresRetrievalStore` 只负责在已授权候选集合内执行 Keyword 和 Dense 查询；加权 RRF、
去重、Reranker、扩展和降级策略由 Application 用例编排，避免把 PostgreSQL 私有 SQL 语义
泄漏到 Domain 或 API。

## 5. 分步实施

### Step 0：冻结启动基线与评测协议

1. 验证阶段 0 与阶段 2 Step 9 的退出记录，不以当前代码完成度替代数据门禁。
2. 校验 manifest 中每个入选来源的 SHA-256、`allowed_uses`、sensitivity 和评测用途。
3. 冻结 corpus、dataset、development/holdout 划分及其内容摘要。
4. 复核当前 30 个问题的语言、代码、跨文档、版本、无答案和安全切片覆盖；如需扩充，使用
   新 dataset version 完成人工复核后再冻结，不覆盖既有 case 或 split。
5. 定义 gold evidence 映射：每个 `evidence[]` 是一个证据单元；只有来源版本一致且 locator
   与 Chunk locator 重叠才算命中，不能只按文档 ID 粗略命中。
6. 定义主要指标：Evidence Recall@5、MRR、nDCG@K、全证据覆盖率、must-exclude 违规数、
   P50/P95 延迟和失败率。
7. 无证据用例不进入 Recall 分母；分别报告无答案、跨 Space、撤下版本和恶意文档切片，
   阶段 3 只评估候选行为，不提前宣称阶段 4 的拒答准确率。
8. 在查看 holdout 前冻结目标部署机器描述、并发条件、预热次数、采样方式和
   `retrieval_p95_budget_ms`。
9. 建立评测配置 schema 和最小 CLI 骨架，报告中只保存 ID、版本、分数、排名、耗时和安全
   的失败分类。

**当前实现与临时基线记录（2026-07-21）**：

- 阶段 0 尚未关闭，manifest 仍为 `draft_pending_license_review`，且仓库中没有阶段 2 Step 9
  正式验收记录。因此 `cases/evals/configs/retrieval-v1.yaml` 固定为 `provisional`、
  `formal_runs_enabled: false`；当前只能做本地工程验证，不能生成正式质量基线。
- manifest 当前包含 43 个来源，本地文件全部存在且原始字节 SHA-256 匹配；43 个均允许
  `local_development`/`local_evaluation`，其中 9 个允许 `repository_fixture`，敏感度分布为
  34 个 `private_local`、9 个 `public_demo`。
- 临时数据快照为：corpus `v0` manifest SHA-256
  `dd096e1aefc5b612124ed239d88fb035d0c42ee724eb52da7d8c72d12287d8b6`；dataset
  `knowledge-qa-v0` 共 30 例，SHA-256
  `e0947b3c028c562ad99ad00c44b78642c2a9dd53542784da7a3dcf7f0bd086c2`；development
  20 例摘要 `4628f7192042a4eea03861026b003d776fda535d633d20e3d0146f22e199874b`；
  holdout 10 例摘要 `67b34a4d071a8bc8512281701a2239467efb736e98dc639d93926d3f9d5fb2ff`；
  dataset schema 摘要 `4949f3f4a9212f5fa02c915850f06a64bde501df0568e57fab582057a3b45e16`。
- 当前切片为单文档事实 8、跨文档综合 8、版本/冲突 2、无答案 5、恶意文档 1、双语 3、
  代码与自然语言 3。规模低于计划建议的 60～100 例；冻结前必须发布新 dataset version，
  或正式接受 v0 的统计限制，不得原地修改已查看的 holdout。
- 每个 `evidence[]` 独立计分，命中必须同时满足 `source_key`、原始字节 SHA-256 版本，以及
  同类型一基闭区间 locator 重叠。Recall@5 按 gold evidence unit 微平均；MRR、nDCG@5 和
  全证据覆盖率按有证据 case 宏平均；无证据 case 不进入这些指标分母。上下文扩展块不能用于
  Recall 命中。evidence-unit nDCG 只计算每个证据单元的首次命中，折损为
  `1 / log2(rank + 1)`，K 内未命中记 0。
- 临时运行条件为 Windows 10.0.26200、Intel64 family 6 model 183、32 logical processors、
  并发 1、预热 3 次、完整单次遍历、`retrieval_p95_budget_ms=1000`；正式冻结前必须在目标
  部署环境复核。当前 CLI 仅允许
  `scripts/evaluate_retrieval.py --config cases/evals/configs/retrieval-v1.yaml --split development --validate-only`，
  且只输出 ID、版本、摘要、计数、协议和门禁原因，不输出问题、正文、引用片段或向量。

**完成标准**：同一 fixture 的证据映射和指标结果确定性一致；development 与 holdout 无泄漏；
配置摘要可唯一标识一次评测。

### Step 1：建立检索领域契约与安全边界

1. 新增纯领域类型：
   - `RetrievalMode`: `keyword`、`dense`、`hybrid`、`hybrid_rerank`；
   - `SearchRequest`: query、space_id、mode、允许的 source/document filters；
   - `SearchHit`: chunk/version/document/source ID、locator、各阶段 rank/score 和安全摘要；
   - `SearchDiagnostics`: profile/model/index 版本、候选数、阶段耗时、降级和过滤原因；
   - `RetrievalProfileV1`: 各路候选数、`rrf_k`、`fusion_alpha`、`rerank_k`、`final_k`、
     相邻窗口和失败策略。
2. 定义 `RetrievalStore` Port，只暴露 Keyword/Dense 原始候选查询，不暴露 ORM 或 SQLAlchemy。
3. 定义 Query Embedder 和 Reranker Port；业务层只依赖能力，不依赖模型 SDK 或具体模型名。
4. 固定错误码：Space/过滤器无效、Embedding 不可用或维度错误、检索超时、Reranker
   不可用、profile 不兼容和 Provider 策略拒绝。
5. 明确降级：
   - Keyword 模式不依赖 Embedding；
   - Dense 模式 Embedding 失败时返回明确错误；
   - Hybrid 仅在 profile 显式允许时降级为 Keyword，并标记实际执行模式；
   - 在线 Reranker 失败可按 profile 回退到融合结果并标记 degraded；离线正式评测不允许
     静默回退，发生回退则该次运行无效；
   - 无命中是成功空结果，不映射为系统错误。
6. 用性质测试固定 RRF、去重、tie-break、top-k 截断和 filter 不可扩权等不变量。

**当前实现与契约记录（2026-07-21）**：

- R3-01 的工程实现已关闭；`packages/domain/src/domain/retrieval.py` 是检索输入、输出、错误和
  Port 的唯一领域契约，不依赖 FastAPI、SQLAlchemy、ORM、模型 SDK 或具体 Provider。
  `SearchFilters` 只允许 source/document IDs，不能覆盖请求 `space_id`；Application 在召回前
  校验归属，后续 PostgreSQL Adapter 仍必须在 SQL 候选边界重复强制 Space、当前发布版本和
  tombstone 条件。
- `RetrievalStore` 只返回 Keyword/Dense 原始候选；Query Embedding 和 Reranker 分别通过
  `QueryEmbedder`、`Reranker` Port 接入。查询向量必须为有限的 768 维值，模型版本必须与
  活动 profile 完全一致。Diagnostics 记录请求/实际模式、候选数、阶段耗时、版本、filter
  类型和降级原因；可记录的 `safe_summary` 只含 Chunk hash、字符数和 locator 数。
- 稳定错误码为 `RETRIEVAL_SPACE_NOT_FOUND`、`RETRIEVAL_INVALID_FILTER`、
  `RETRIEVAL_EMBEDDING_UNAVAILABLE`、`RETRIEVAL_EMBEDDING_DIMENSION_MISMATCH`、
  `RETRIEVAL_TIMEOUT`、`RETRIEVAL_RERANKER_UNAVAILABLE`、
  `RETRIEVAL_PROFILE_INCOMPATIBLE`、`RETRIEVAL_PROVIDER_POLICY_DENIED`。
- Keyword 不调用 Embedding；Dense 的 Embedding 失败直接报错。Hybrid 只有在线 profile 明确
  选择 `keyword_fallback` 时才能对 unavailable/timeout 降级，Provider 策略拒绝不能降级；
  Reranker 只有在线 profile 明确选择 `fused_fallback` 时才能回退。`offline_evaluation` 中任何
  降级都会使评测报错，无命中则返回成功空结果。
- 加权 RRF 固定为
  `(1 - fusion_alpha) / (rrf_k + keyword_rank) + fusion_alpha / (rrf_k + dense_rank)`；
  以 `chunk_id` 去重，依次按 fused score、最佳单路 rank、稳定 Chunk ID 排序，输入列表顺序
  不改变结果。Reranker 必须一一返回输入索引，不允许丢失、重复或越界。
- 本步只关闭 R3-01 和 RRF 契约/性质测试；R3-04 的上下文扩展、参数消融和离线收益验证仍在
  Step 6/9，且阶段 0、阶段 2 Step 9 门禁未关闭，因此不构成正式检索验收。

**完成标准**：Domain 不依赖 FastAPI/SQLAlchemy/模型 SDK；fake Store/Embedder/Reranker 能执行
完整 Application 用例；跨 Space filter 无法通过请求覆盖。

### Step 2：部署真实本地 Embedding 并建立版本化重建

1. 将 ModelGateway 改为能力级配置和健康状态：Embedding-only 配置不再要求
   `FAST_CHAT_MODEL`，模型不可用也不阻断数据库、Redis 和管理页面 ready。
2. 为 Embedding 增加独立 endpoint/model 配置，保留现有通用配置的兼容读取路径；endpoint
   继续执行本地/私网/显式外发策略校验。
3. 通过可选 Compose profile 部署本地模型服务；镜像使用 tag + digest，模型使用仓库
   revision/commit 固定，不在运行时漂移到 latest。
4. 首轮候选固定为：
   - 默认候选 `Alibaba-NLP/gte-multilingual-base`，原生 768 维；
   - 效果挑战者 `Qwen/Qwen3-Embedding-0.6B`，服务端强制输出 768 维；
   - 必要时使用 `intfloat/multilingual-e5-base` 作为稳定对照。
5. 候选按相同 parser/chunker、语料和检索配置顺序评测；每次只激活一个模型，不建立多个
   业务 Provider 分支。
6. `embedding_version` 至少包含模型 revision、维度、查询/文档指令版本、归一化和推理精度；
   完整配置进入 `processing_config` 及其摘要。
7. 增加受控全量重建命令：创建新候选 `DocumentVersion`、重新生成 Chunk/Embedding、验证后
   原子发布；失败时保留旧发布版本，不原地覆盖既有向量。
8. 补齐 `parent_ordinal`、`prev_ordinal`、`next_ordinal` 到 Chunk metadata，并通过重建应用到
   已批准语料。
9. 合同测试验证批量顺序、768 维、有限超时/重试、归一化、空输入、无效响应和离线状态。

**当前实现与验证记录（2026-07-21）**：

- ModelGateway 已支持能力级 endpoint、凭据、模型、健康状态及 TEI `/embed` 协议；Gateway
  增加显式异步关闭契约，Worker 将其生命周期限制在单次 actor 事件循环，避免连续任务复用
  已关闭的 HTTP client。
- 可选 `embedding` profile 固定 TEI CPU 镜像
  `cpu-1.9@sha256:c26a226262ad4ff3330fb30b76653c1bb65da2fcf413b92284545a010e0a8a48`
  和 `BAAI/bge-base-zh-v1.5` revision
  `f03589ceff5aac7111bd60cfc7d497ca17ecac65`。该模型仅作为阶段 0 门禁关闭前的部署烟测
  基线，不替代第 4 项候选的正式同配置评测和默认模型选择。
- `EmbeddingIdentity` 将 revision、768 维、查询/文档指令版本、归一化和精度写入
  `embedding_version`、`processing_config` 及摘要；受控重建命令只创建候选版本，仍经
  EMBED/INDEX/VALIDATE/PUBLISH 后原子切换当前版本。
- 真实 CPU 部署首次权重下载约 152 秒；服务返回 HTTP 200，批量响应为 `2 x 768`、全部有限、
  L2 范数为 1。保留模型卷后重启至健康约 5.94 秒，重启后响应仍为 768 维且归一化一致。
- 隔离 Compose 全栈通过迁移和健康依赖；API 报告 `MODEL_EMBEDDING_CONFIGURED`，未配置 Chat
  独立保持不可用且不阻断 ready。经 manifest SHA-256 校验的公开 TXT/Markdown fixture 已走通
  API -> Redis -> Worker -> TEI -> PostgreSQL -> PUBLISH；修复 Gateway 生命周期后连续任务均为
  `retry_count=0`。
- 多 Chunk 实测发布 152 个非空 768 维向量，151 条前驱和 151 条后继关系全部按 ordinal
  对齐。单块或无父块时对应 metadata 键保持缺省，不伪造不存在的关系。
- 默认单元测试和 CI 仍使用 deterministic fake，不下载或调用真实模型。阶段 0 仍为
  `draft_pending_license_review`，因此本记录只关闭 Step 2 的工程流程，不构成正式语料评测或
  阶段 3 最终验收。

**完成标准**：批准的公开 fixture 可在无外部推理请求的本地服务上稳定得到 768 维向量；
模型版本可复现；重建失败不影响当前发布版本；CI 仍不下载或调用真实模型。

### Step 3：建立 PostgreSQL 发布集合与检索索引

1. 在唯一的 published candidate 查询中强制连接并验证：
   - `Source.space_id == request.space_id`；
   - `Document.deleted_at IS NULL`；
   - `Document.current_version_id == DocumentVersion.id`；
   - `DocumentVersion.status == published`；
   - Chunk 属于该当前版本；
   - Dense 查询的 `embedding_version` 与活动 profile 一致。
2. Source/Document filters 只能缩小上述集合，并在执行召回前验证归属。
3. 新建 Alembic revision，为 Chunk 增加基于 PostgreSQL `simple` 配置的 FTS 文档列和 GIN
   索引；升级时回填旧数据，降级路径可恢复，既有迁移保持不变。
4. 对向量路径建立 exact cosine 查询作为正确性基线；记录 IVFFlat 的 probes、lists、ANALYZE
   状态和执行计划。
5. 比较 exact 与 IVFFlat 的结果重合率和 Space 过滤行为；近似索引没有达到召回门槛时，
   默认 profile 继续使用 exact，不以延迟为由牺牲未记录的正确性。
6. 查询使用 SQLAlchemy 参数绑定或结构化 SQL；禁止拼接用户查询、排序字段或过滤表达式。
7. 为迁移回填、当前版本切换、删除撤下、跨 Space 和并发发布/检索编写真实 PostgreSQL
   集成测试。

**当前实现与验证记录（2026-07-21）**：

- Alembic revision `d4e5f6a7b8c9` 为 `chunks.search_vector` 增加基于 PostgreSQL `simple`
  配置的持久生成列，并创建 `idx_chunks_search_vector` GIN 索引；ORM metadata 与迁移保持一致。
- `PostgresRetrievalStore` 在 Infrastructure 层实现 `RetrievalStore` Port。Keyword 与 exact Dense
  共用同一 `_apply_published_scope` 边界，强制目标 Space、未删除 Document、当前且已发布
  DocumentVersion；Dense 额外匹配 `embedding_version`，Source/Document filter 只能追加条件。
- exact cosine 默认关闭 index/bitmap scan，作为正确性基线。IVFFlat 诊断使用 materialized
  vector shortlist 后在同一 SQL 中应用相同发布边界；这是为了让 pgvector 索引计划可测，也会
  显式暴露过滤后可能少召回的风险，因此正式语料门槛关闭前不作为默认路径。
- `DensePathComparison` 记录 exact/IVFFlat 候选、overlap、`lists=100`、`probes`、shortlist 大小、
  ANALYZE 状态和两份 JSON 执行计划，并明确标记 `idx_chunks_embedding` 是否实际使用。合成的
  120 向量测试在 top 10 上得到 10/10 overlap，exact 计划包含顺序扫描，IVFFlat 计划实际命中
  `idx_chunks_embedding`。
- 隔离 PostgreSQL 上从 `c3d4e5f6a7b8` 插入旧 Chunk 后升级至 head，生成列可立即命中旧文本；
  降级后列被删除而原 Chunk 文本保留，再升级成功。真实 PostgreSQL/Redis 集成测试 31 个通过，
  覆盖当前版本切换提交前后、tombstone、未发布版本、Embedding 版本、跨 Space、filters、FTS、
  exact/IVFFlat 和迁移 head/索引契约。
- SQL 查询由 SQLAlchemy 表达式和绑定参数构造；诊断中的 EXPLAIN 只编译内部生成的受控语句，
  用户查询、ID 和 filter 不参与 SQL 字符串拼接。
- 阶段 0 仍为 `draft_pending_license_review`，以上只关闭 Step 3 的工程流程；尚未在冻结语料上
  证明近似索引召回门槛，也不构成阶段 3 的正式质量验收。

**完成标准**：任何检索模式都只能看到目标 Space 的当前发布版本；迁移往返通过；explain
计划和 exact/approx 差异进入诊断报告；跨 Space 与撤下来源违规为 0。

### Step 4：实现 Keyword/FTS 单路基线

1. 规范化查询但保留技术符号、英文标识符和 C/C++ 等领域词；空白或超长查询在传输层
   有界拒绝。
2. 使用 PostgreSQL FTS 生成候选并以 `ts_rank_cd` 排序；相同分数按稳定 ID tie-break，保证
   重复运行顺序一致。
3. Keyword 结果保存原始 rank 和 score；不在 Store 内执行 RRF、Reranker 或上下文扩展。
4. 分别报告中文、英文、混合语言、代码与自然语言查询切片。中文召回不足时先归类为分词/
   查询/语料问题，不静默引入新扩展。
5. 预先定义升级触发条件：若 Keyword 路径导致混合检索无法满足门槛，再在相同 Port 下比较
   受控分词方案或 PGroonga；更换关键词后端或新增数据库扩展前更新 ADR-002 或新增 ADR。

**当前实现与验证记录（2026-07-21）**：

- 查询入口契约使用 Unicode NFKC、空白折叠和前后去空白，保留 `C++`、`std::vector`、
  `snake_case_identifier` 等技术符号与标识符；原始或规范化查询超过 512 字符、或规范化后为空
  时拒绝。Step 8 的 Pydantic 传输模型必须复用该上限和领域校验，不能建立第二套不一致规则。
- `KeywordQueryAnalysis` 将每个查询标记为 Chinese/English/Mixed/Other 语言切片及 Code/
  Natural Language 类型，并记录需要字面匹配的技术词；Search diagnostics 暴露切片、类型和
  字面词数量，供后续离线报告聚合，不记录原始查询正文。
- PostgreSQL 仍以 `websearch_to_tsquery('simple', ...)`、持久 FTS 列、GIN 和 `ts_rank_cd` 为
  唯一 Keyword 基线。对于 `++`、`::`、`#`、`_` 技术词，Store 使用绑定参数追加大小写不敏感
  的字面条件，只会缩小 FTS 结果，避免 `C++` 被 parser 退化成宽泛的 `c`；没有引入第二后端。
- Keyword 候选保留原始 rank/score，按 score 降序、稳定 Chunk UUID 排序；三次重复查询的等分
  候选顺序一致。Application 的 Keyword 模式不调用 Embedding，也不执行 RRF、Reranker 或上下文
  扩展。
- 合成公开测试覆盖英文自然语言、显式分隔的中文 token、中文与 `std::vector` 混合查询、
  全角 `Ｃ＋＋` 规范化、精确标识符和等分 tie-break。真实 PostgreSQL 测试 4 个通过；阶段 0
  私有语料未用于本步验证，因而这里只形成工程切片基线，不报告正式 Recall。
- PostgreSQL `simple` 不提供中文分词，连续中文子词可能无法命中，这是已知基线限制。升级触发
  条件固定为：冻结 development 集的中文 Keyword evidence Recall@5 低于 85%，或 Hybrid 未达
  总门槛且逐 case 证据定位到 Keyword 分词损失；届时在同一 `RetrievalStore` Port 下比较受控
  分词方案或 PGroonga，并在更换后端或增加数据库扩展前更新 ADR-002。不得查看 holdout 后调参。
- 检索 HTTP API 和 OpenAPI 仍属于 Step 8；本步没有提前新增公开端点，也没有声明阶段 3 正式
  验收完成。

**完成标准**：Keyword 模式可独立调用、可解释且确定性；技术词和精确标识符用例通过；
语言切片失败可定位到查询解析、候选召回或排序阶段。

### Step 5：实现 Dense 单路基线

1. 查询通过活动 Embedding 模型和查询指令生成 768 维向量，验证维度和有限数值后再访问
   pgvector。
2. 使用 cosine distance 查询，返回原始 distance、转换后的可展示 score 和 rank；应用逻辑
   不依赖不同模型间不可比较的绝对 score。
3. Dense 查询严格匹配 `embedding_version`，模型切换期间不查询旧模型向量或混合两个向量
   空间。
4. 实现批量评测查询的受控并发和取消；在线 API 使用独立有界超时。
5. 对跨语言、中文问英文资料、代码和长技术术语建立切片报告，并与 Keyword 基线逐 case
   比较。
6. 比较 exact 与活动近似索引，记录 Recall@K、延迟和结果重合，不只记录 SQL 执行时间。

**当前实现与验证记录（2026-07-21）**：

- `QueryEmbeddingService` 是 provider-neutral `QueryEmbedder` 实现：它通过 TextEmbedder 适配
  ModelGateway，先规范化 query，再可选追加受版本约束的 query prefix；返回恰好一个、768 维、
  全部有限的向量，并按 `EmbeddingIdentity` 进行可复现归一化，`model_version` 固定为完整
  identity version。带 prefix 的配置必须声明非 `none-v1` 的 query instruction version，防止
  改变查询格式却复用旧 `embedding_version`。
- 活动 `RetrievalProfileV1` 现在固定 768 维并持有独立 `dense_timeout_seconds`。SearchService 在
  访问 pgvector 前再次检查维度和有限值，严格匹配 `embedding_version`；模型或 Store 超时统一映射
  为可重试 `RETRIEVAL_TIMEOUT`，取消不被吞掉，模型响应数量/维度/非有限/L2 零向量映射为稳定
  RetrievalError，不能以裸 `ValueError` 越过 Application 边界。
- `QueryEmbeddingBatchRunner` 为离线评测的 query embedding 提供有界并发、每项超时、输入顺序
  保持和取消清理。它只覆盖 Dense 输入生成；完整 retrieval evaluation/report 仍在后续评测步骤
  完成，避免将当前工程 smoke test 误报为正式 Recall。
- Dense Store 使用 cosine distance，候选中保留数据库原始 score (`1 - distance`) 与 rank；Application
  不在模型间比较绝对分数。真实 PostgreSQL 合成数据验证 query 与同向量 Chunk 的 score 为 `1.0`
  且 rank 为 `1`，Embedding version 不同的当前发布 Chunk 不可见。exact/IVFFlat 的 lists、probes、
  ANALYZE、计划和 top-10 overlap 仍沿用 Step 3 诊断，默认保持 exact。
- Search diagnostics 记录 Dense 的语言及 Code/Natural Language 切片，单元测试覆盖英文、混合中文
  与 `std::vector`；隔离 PostgreSQL/Redis 集成套件 32 个通过。跨语言、中文问英文、代码和长术语
  的正式逐 case 比较必须等阶段 0 冻结 development/holdout 后执行，不能以当前合成 fixture 代替。
- 阶段 0 仍为 `draft_pending_license_review`，本记录只关闭 Step 5 的工程流程和可复现执行边界，
  不声明 Dense、Hybrid 或近似索引已经达到 Recall@5 质量门槛。

**完成标准**：Dense 模式独立达到可复现基线；维度/版本错误不会发布或查询；同一配置重复
运行排名稳定；模型和数据库阶段耗时可分开观察。

### Step 6：实现 Hybrid、去重与上下文扩展

1. Keyword 与 Dense 候选在同一请求内有界并发执行，共享同一 Space/filter/published
   candidate 约束。
2. 使用加权 RRF：

   ```text
   score = (1 - fusion_alpha) / (rrf_k + keyword_rank)
         + fusion_alpha / (rrf_k + dense_rank)
   ```

   缺失某一路排名时该项为 0；`fusion_alpha`、`rrf_k` 和候选数进入 profile 版本摘要。
3. 以 `chunk_id` 去重；相同文本但不同位置不按 `chunk_hash` 合并，以免丢失可定位证据。
4. 固定 tie-break 顺序，至少包含 fused score、最佳单路 rank 和稳定 Chunk ID。
5. 融合后再按 profile 执行父块/相邻块扩展；扩展块必须来自同一 DocumentVersion，不能跨
   文档、跨版本或跨 Space。
6. 扩展结果区分 `matched` 与 `context_only`，评测 Recall 只按原始命中候选计算，不能用扩展
   掩盖召回失败。
7. 控制每个文档的最大候选数和扩展窗口，避免单一长文档占满结果；多样性规则必须配置化
   并进入消融实验。

**当前实现与验证记录（2026-07-21）**：
- `SearchService` 在 Hybrid/Hybrid+Rerank 请求内并行执行 Keyword 与 Dense；两路分别受
  `keyword_timeout_seconds`/`dense_timeout_seconds` 限制，取消时清理未完成任务；Embedding
  失败仍按既有在线 Keyword fallback 策略处理。
- `fuse_candidates` 使用加权 RRF，按 `chunk_id` 去重，并校验跨通道身份（含版本、来源、定位、
  ordinal 和 metadata）；融合后执行 `max_chunks_per_document` 配额。
- `RetrievalCandidate` 保留 chunk ordinal/结构 metadata；新增 `ContextCandidateQuery` 和
  PostgreSQL 上下文查询，只读取当前已发布版本并复用 Space/source/document filter。应用层再次
  校验 version/document/source 边界，扩展结果标记 `context_only`。
- `SearchDiagnostics.candidate_counts.final` 只统计原始 matched 命中，`context_only_count` 单独
  统计扩展块，避免上下文掩盖召回失败。
- 单元测试 52 个通过；新增 PostgreSQL/pgvector 上下文集成测试 5 个通过，完整隔离依赖集成
  回归 33 个通过（含父/邻接块、当前版本、文档、Space 和 Redis 边界）；`ruff check`、
  `ruff format --check`、`mypy apps packages` 通过。
- 阶段 0 仍为 `draft_pending_license_review`，上述仅为本地工程和安全边界验证，不声明正式
  Recall@5 或阶段 3 质量门槛达标。

**完成标准**：融合、去重、tie-break 和扩展均为纯确定性逻辑；Hybrid 相对单路结果的变化
可以逐候选解释；扩展不会扩大安全边界。

### Step 7：接入可选 Reranker

1. 在 ModelGateway 增加 `reranker_multilingual` 能力别名及 provider-neutral
   `RerankRequest/Response`，包含文档索引、相关性分数、模型版本、用量和延迟。
2. 提供确定性 fake/no-op Reranker 用于单元、契约和 CI；真实 Adapter 只调用本地固定版本
   模型服务，不在 Application 中引入 Provider SDK。
3. Reranker 只处理融合后的前 `rerank_k` 项，返回顺序必须映射回原 Chunk ID，并验证无丢失、
   重复或越界索引。
4. Reranker 输入只包含必要 query/chunk 文本，不包含其他 Space 内容；不记录正文和模型输入。
5. 默认 profile 只有在 development 消融显示相对最佳融合基线的 nDCG/证据覆盖提升，且 P95
   满足冻结预算时才启用 Reranker。
6. 在线降级和离线评测失败语义遵守 Step 1；禁止把回退结果标记为成功的精排结果。
7. 若 Reranker 接入正确但没有净收益，阶段验收不能静默偏离总实施计划的“混合+精排有提升”
   退出条件；应先用评测证据更新 `docs/project-implementation-plan.md` 的门槛和后果，再决定
   是否以默认关闭 Reranker 的 Hybrid 配置退出。

**当前实现与验证记录（2026-07-21）**：
- ModelGateway 新增 `reranker_multilingual` 能力 alias、provider-neutral `RerankRequest`/
  `RerankResponse`/`RerankScore`（含 model version、usage、latency），Fake、Unavailable 和
  OpenAI-compatible/TEI HTTP Adapter 均实现同一契约。
- `GatewayReranker` 将 ModelGateway 错误映射为稳定 `RETRIEVAL_RERANKER_UNAVAILABLE`，有界
  timeout，不把 query/chunk 正文写入日志；Application 仍只发送融合后的 `rerank_k` 项并校验
  index 完整性、重复和越界。
- Compose 新增可选 `reranker` profile，固定 TEI CPU 镜像 digest 和
  `BAAI/bge-reranker-base` revision `2cfc18c9415c912f9d8155881c133215df768a70`，服务端点为
  `/rerank`；默认 fake/profile 关闭，不改变既有本地管理功能。
- ModelGateway/Adapter 契约与失败、超时、非法 index 测试通过；完整默认回归为
  `467 passed, 34 skipped`，隔离 PostgreSQL/Redis 集成回归为 `33 passed`，`ruff` 和 `mypy`
  通过。
- 固定 revision 的 TEI 容器前两次因 `huggingface.co` 下载 `unexpected EOF` 退出；复用
  `stage3-step7-test_rerankerdata` 缓存卷重试后成功下载 1.11 GB ONNX 权重、完成 warm-up 并
  达到 healthy。直接中文 `/rerank` smoke 中相关文档得分 `0.7438486`，无关项低于
  `0.000038`；经 `GatewayConfig -> ModelGateway -> GatewayReranker` 的英文 smoke 中相关文档
  得分 `0.4751372`，无关项低于 `0.000056`，返回固定 model version，Adapter 延迟约 `279 ms`。
  这关闭了真实模型部署与契约 smoke，但不替代阶段 0 冻结后的 development 消融、P95 和
  holdout 质量验收；默认 Reranker 仍保持关闭。

**完成标准**：开关 Reranker 不改变召回集合安全边界；真实和 fake Adapter 通过同一契约；
精排收益和新增延迟分别报告。

### Step 8：提供检索 API 与可观测性

1. 新增 `POST /api/v1/spaces/{space_id}/search`，请求包含 query、mode 和只会缩小范围的可选
   filters；服务端选择版本化 profile，不接受任意 SQL、模型名或未经允许的配置覆盖。
2. 响应返回实际执行模式、profile 版本、模型/索引版本、degraded 状态和有界 SearchHit；
   debug diagnostics 通过显式开发配置启用，生产默认不暴露内部执行细节。
3. 查询为空、过长、Space 不存在、Provider 不可用、超时和 profile 不兼容映射为稳定 HTTP
   错误；无结果返回成功空列表。
4. 每个请求记录 trace ID、各阶段耗时、候选数、模式、profile/version 和安全错误码；不记录
   query 正文、Chunk 正文、Embedding、完整模型响应或 API Key。
5. 为 FTS、query embedding、vector SQL、fusion 和 rerank 建立独立 span，限制低基数属性。
6. 新增公开 API 后重新生成 `docs/openapi.json`，运行 schema diff 和 API 隔离集成测试。

**当前实现与验证记录（2026-07-22）**：

- 新增 `POST /api/v1/spaces/{space_id}/search`。请求只允许 `query`、四种已定义的
  `mode` 和 `source_ids`/`document_ids` 缩小过滤；Pydantic 使用领域层的 NFKC、空白折叠和
  512 字符上限，额外字段和超过 100 项的过滤器被拒绝。API 通过
  `RetrievalProfileResolver` 将已持久化的 Space profile 白名单映射为 `RetrievalProfileV1`，
  Embedding 身份和超时只来自服务端配置。
- 响应包含 requested/executed mode、profile、Embedding/Reranker/索引版本、degraded 状态、
  有界命中和 locator；无结果返回 200 空列表。debug diagnostics 由
  `RETRIEVAL_DEBUG_DIAGNOSTICS` 显式开启，且 production 强制隐藏。稳定错误覆盖校验失败、
  Space/过滤越权、Provider 不可用、超时、维度和 profile 不兼容，统一使用 `ErrorResponse`。
- Search application 统一承载 scope、候选、版本和融合规则；API 只负责传输映射。FTS、query
  embedding、vector SQL、fusion 和 rerank 分别建立低基数 span。Fusion 记录实际耗时；结构化
  日志记录 trace context、阶段耗时、候选数、模式、profile/version、降级和错误码，日志白名单
  排除 query、Chunk/Embedding 正文、模型响应和密钥。
- OpenAPI 已由 `scripts/export_openapi.py` 重新生成；新增端点的 400/403/404/409/422/500/503/504
  均声明 `ErrorResponse`。全仓 `ruff format --check`（121 files）、`ruff check`、`mypy apps packages`
  通过，默认测试为 `475 passed, 40 skipped`；搜索 API 隔离 PostgreSQL 集成测试 6 个通过，
  全量隔离集成回归 39 个通过。
- 专用 `stage3-step8-test` Compose 栈从现有迁移构建并启动，migrate 正常退出，API、Worker、Web、
  PostgreSQL、Redis 全部 healthy。实际 HTTP smoke 返回 readiness `ready` 和 Keyword 200 空结果；
  容器日志带相同 trace ID、profile、索引版本、候选计数及 `keyword=9.61 ms`，未出现查询正文，
  diagnostics 默认隐藏。测试未使用阶段 0 私有语料，因此不作最终质量验收或 Recall 声明。

**完成标准**：API、Application 和 Store 不重复实现规则；OpenAPI 与运行代码一致；超时与
取消有界；日志和 trace 通过隐私检查。

### Step 9：完成离线评测、消融和默认配置选择

1. 评测命令至少支持：

   ```text
   uv run python scripts/evaluate_retrieval.py \
     --config cases/evals/configs/retrieval-v1.yaml \
     --split development \
     --output <ignored-or-reviewed-report-path>
   ```

2. 每次运行记录：git commit、corpus/dataset/config hash、parser/chunker/embedding 版本、模型
   revision、维度、指令、归一化、索引参数、profile、机器描述和依赖版本。
3. 依次建立 Keyword、Dense exact、Dense approximate、Hybrid、Hybrid+Reranker 基线；每次只
   改一个变量，并保留 per-case 阶段候选与失败分类。
4. development 上比较有限、预注册的参数集合：chunk size、各路 top-k、`fusion_alpha`、
   `rrf_k`、`rerank_k` 和相邻窗口。禁止查看 holdout 后继续调参。
5. Embedding 候选使用完全相同的语料、分块、检索和评测协议。若多个候选都通过硬门槛，
   优先选择更简单、原生 768 维且满足延迟预算的模型；只有预注册的质量提升达到显著门槛
   时才接受更高部署复杂度。
6. 冻结最佳 development 配置后只运行一次正式 holdout；配置失败或基础设施降级时作废并
   记录原因，不选择性重跑失败 case。
7. 生成机器可读报告和人工摘要，按以下类别归因：解析/locator 映射、Keyword 召回、Dense
   召回、融合、精排、扩展、版本/过滤、安全违规、Provider 和基础设施。
8. 提交报告前执行隐私扫描；报告只包含最小证据标识和指标，不提交私有正文、查询全文或
   Embedding 产物。

**完成标准**：同一冻结配置可重复得到相同通过/失败结论；默认模型和 profile 的选择有
development 消融与 holdout 证据；每个失败 case 可定位到具体阶段。

### Step 10：集成验收与文档移交

1. 在隔离 PostgreSQL/Redis 和本地模型服务上运行全链路：批准来源摄入、发布、检索、修改
   重建、原子切换、删除撤下和再次检索。
2. 验证 Keyword、Dense、Hybrid、Hybrid+Reranker 四种模式及 Reranker/Embedding 故障策略。
3. 验证跨 Space、撤下版本、旧版本、伪造 filter、恶意文档、SQL 注入、超长查询、日志泄漏
   和外部 Provider 策略。
4. 验证 Compose 模型 profile 的首次启动、缓存后离线启动、健康状态、模型不可用时管理能力
   和保留卷重启；不得为方便下载取消镜像 digest 或外发门禁。
5. 运行规范后端、迁移、OpenAPI 和受影响前端检查。
6. 新增 `docs/stage-3-acceptance.md`，记录实际命令、环境、报告摘要、退出条件、已知限制和
   外部确认；同步更新 README、architecture、development-environment 和 troubleshooting。
7. 向阶段 4 移交固定的 Search Application Port、SearchResult、默认 profile、模型/索引版本、
   错误协议和评测基线；阶段 4 不直接读取检索表或复制融合逻辑。

**完成标准**：所有阶段退出条件均由保存的命令和报告支持，阶段 4 可以仅通过 Application
Port 获取有版本、分数、Space、来源和 locator 的证据候选。

## 6. 配置与版本策略

### 6.1 RetrievalProfileV1

建议的稳定字段至少包含：

```text
profile_version
keyword_candidate_k
dense_candidate_k
fusion_candidate_k
rrf_k
fusion_alpha
reranker_enabled
rerank_k
final_k
adjacent_window
max_chunks_per_document
hybrid_embedding_failure_policy
reranker_failure_policy
```

`Space.retrieval_profile` 保存完整 profile 或已发布 profile 的引用与摘要。缺失字段使用版本化
默认值，未知字段拒绝或按 schema 的兼容规则处理，不能静默改变旧 Space 行为。

### 6.2 模型版本

Embedding 版本示例：

```text
gte-multilingual-base@<commit>:dim768:doc-v1:query-v1:l2:fp16
```

Reranker 版本示例：

```text
<reranker-model>@<commit>:input-v1:fp16
```

显示名称不能代替不可变 revision。模型、指令、维度、归一化或精度变化均产生新版本；
Embedding 变化触发新 DocumentVersion 和全量向量重建。

### 6.3 评测报告版本

机器可读报告至少包含：

```json
{
  "schema_version": "retrieval-report-v1",
  "run_id": "...",
  "git_commit": "...",
  "corpus_hash": "...",
  "dataset_hash": "...",
  "config_hash": "...",
  "embedding_version": "...",
  "retrieval_profile_version": "...",
  "metrics": {},
  "slices": {},
  "latency_ms": {},
  "violations": [],
  "failures": []
}
```

报告 schema 发生不兼容变化时提升版本；历史报告保留原配置，不回写伪造新结果。

## 7. 测试与验证矩阵

| 层级 | 必测内容 | 真实模型要求 |
| --- | --- | --- |
| 单元 | profile 校验、RRF、去重、tie-break、扩展、指标和 evidence 映射 | fake |
| 契约 | RetrievalStore、Query Embedder、Reranker、错误与降级语义 | fake + 本地 HTTP stub |
| 迁移 | FTS 列/索引升级、回填、降级、旧数据兼容 | 否 |
| 集成 | published candidate、FTS、pgvector exact/IVFFlat、Space/版本过滤 | fake 向量 + 隔离 PostgreSQL |
| 模型集成 | 768 维、批量顺序、归一化、指令、超时和模型 revision | 本地固定模型，非 CI 默认 |
| API | 请求校验、模式、filters、空结果、错误映射和 OpenAPI | fake 或本地模型 |
| 安全 | 跨 Space、撤下来源、伪造 ID、恶意文本、SQL 注入、日志泄漏、外发策略 | fake + 公开 fixture |
| 评测 | Keyword/Dense/Hybrid/Rerank、切片、指标、重复运行和报告 schema | 本地固定模型 |
| 性能 | P50/P95、模型/SQL/融合/精排分段耗时、exact/approx 对比 | 目标机器本地模型 |
| 端到端 | 摄入、发布、检索、重建切换、删除撤下、故障降级 | 隔离完整本地栈 |

规范验证命令至少包含：

```text
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest
$env:RUN_INTEGRATION='1'; uv run pytest tests/integration
uv run alembic upgrade head
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
```

Compose 或模型服务变更还必须验证空缓存首次启动、已有缓存离线启动、健康依赖、模型不可用
降级和命名卷保留重启。永久删除模型或数据库卷不属于常规验收命令。

## 8. 阶段退出条件

阶段 3 仅在以下条件全部满足时退出：

1. 阶段 0 和阶段 2 Step 9 已正式关闭，评测 corpus/dataset/profile 已冻结。
2. Keyword、Dense、Hybrid 和 Hybrid+Reranker 可独立运行并输出分阶段诊断。
3. 冻结 holdout 上 Retrieval evidence Recall@5 达到 `cases/docs/product/mvp-scope.md` 的
   至少 85% 门槛。
4. Hybrid+Reranker 相对最佳单路基线有可复现提升。若 Reranker 没有净收益，必须先按
   Step 7 更新总实施计划的退出门槛，不能在当前门槛不变时以最佳 Hybrid 配置静默通过。
5. P95 满足 Step 0 预注册的目标机器预算，且报告能拆分模型、数据库、融合和精排耗时。
6. 跨 Space、撤下来源和非当前版本召回违规为 0；权限过滤测试全部通过。
7. 默认 Embedding 模型、Reranker 开关、索引参数和 RetrievalProfileV1 已固定版本并可重建。
8. 机器可读报告、人工摘要、失败分类和复现实命令已保存且通过隐私检查。
9. 检索 API/OpenAPI、迁移、运行文档、troubleshooting 和阶段 3 验收记录同步完成。
10. 阶段 4 只需依赖正式 Search Application Port，不需要直接访问 ORM、私有表或模型 Adapter。

## 9. 风险与控制

| 风险 | 早期信号 | 控制措施 |
| --- | --- | --- |
| 阶段 0 未关闭即使用私有语料 | 报告或缓存出现未批准来源 | manifest/hash 强制门禁；只用公开 fixture 工程验证 |
| FTS 中文召回弱 | 中文切片 Keyword 明显落后 | 单独报告分词失败；达到触发条件后再评估受控分词/PGroonga |
| 向量模型与数据库版本不一致 | 维度正确但召回异常 | 查询强制匹配 embedding_version 和配置摘要 |
| IVFFlat 过滤后漏召回 | exact 命中而 approximate 未命中 | exact 基线、probes/计划诊断；未达门槛时默认 exact |
| RRF 参数过拟合 | development 提升、holdout 下降 | 预注册有限参数集合；holdout 只做最终验收 |
| Reranker 掩盖召回失败 | 最终排序好但 gold 不在候选集 | 同时报告 pre-rerank Recall 和 post-rerank nDCG |
| 扩展掩盖原始召回失败 | 邻块包含证据但命中块不包含 | matched/context_only 分离，Recall 只计算原始命中 |
| 模型服务成为管理面阻塞 | 模型宕机导致 API unready | 能力级健康；管理 ready 不依赖模型；搜索返回明确错误/降级 |
| 查询或正文泄漏 | 日志、span、报告包含文本/向量 | 默认只记 ID、hash、计数、版本和耗时；隐私回归测试 |
| 候选模型长期并存 | 配置和索引出现多个默认版本 | 顺序评测，阶段退出时只激活一个默认模型 |
| 评测集规模不足 | 少数 case 改变总体结论 | 报告置信区间和切片；按版本新增标注，禁止看 holdout 后改原用例 |

## 10. 阶段 4 移交清单

阶段 3 完成后向阶段 4 提供：

- `search(request) -> SearchResult` 的稳定 Application Port。
- 每个 SearchHit 的 Chunk、DocumentVersion、Document、Source、Space、locator 和阶段分数。
- `matched` 与 `context_only` 的明确区分，以及最终上下文候选顺序。
- 默认 RetrievalProfileV1、Embedding/Reranker/index 版本和配置摘要。
- 无结果、Provider 不可用、超时、profile 不兼容和降级的稳定错误协议。
- 可复现的 retrieval report v1、失败分类和 holdout 结果。
- 跨 Space、撤下版本和非当前版本不可召回的集成测试事实。

阶段 4 在此基础上实现查询改写、上下文预算、带引用回答、引用校验和拒答；不得在问答用例
内部复制阶段 3 的 SQL、融合、精排或过滤逻辑。
