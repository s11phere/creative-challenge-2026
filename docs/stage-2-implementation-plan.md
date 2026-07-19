# 阶段 2 实施计划：知识摄入 MVP

> 文档状态：Draft v7 — Step 7 已完成 (2026-07-19)
>
> 适用范围：`docs/project-implementation-plan.md` 中的阶段 2
>
> 计划基线日期：2026-07-18

## 1. 结论摘要

阶段 2 的目标是打通「单个来源 → 稳定入库 → 可检索索引 → 可增量维护」的知识摄入闭环，而不是实现检索排序或问答。它在阶段 1 工程骨架和阶段 2 Step 1 数据模型之上，实现真实的 Application 摄入用例、Worker 管道、Embedding、发布边界和 Web 数据源状态。

标准管道固定为：

```text
DISCOVER -> FINGERPRINT -> PARSE -> NORMALIZE -> ENRICH
-> CHUNK -> EMBED -> INDEX -> VALIDATE -> PUBLISH
```

每一步输入输出明确、可缓存、可重试、可单独观测；只有 `PUBLISH` 后的新版本对检索可见，避免用户查到半成品索引。首期格式优先级为 P0：Markdown、TXT、可复制文本 PDF。

阶段 2 Step 0（技术决策）和 Step 1（数据模型基础及 R2-01～03 修正）已于 2026-07-18 完成。后续先完成单个 Markdown 文件的端到端幂等摄入，再在同一 Parser 契约下扩展 TXT 和可复制文本 PDF，避免三个格式同时推进时掩盖版本、发布和重试语义的问题。

阶段 0 语料门禁未关闭前，只能使用代码内最小合成输入和 manifest 中明确允许 `repository_fixture` 的来源做工程验证，不能据此完成阶段 2 验收或声称真实语料成功率达标。当前 manifest 没有允许提交到仓库的 PDF fixture；正式验收 PDF 前必须先通过阶段 0 复核补齐合规样本。

## 2. 启动条件与当前缺口

### 2.1 阶段 0 门禁仍未关闭

阶段 0 的语料授权复核、人工标注复核和版本冻结仍为等待状态（见 `stage-1-acceptance.md`）。按仓库当前优先级，它是步骤 2～9 正式实施和阶段 2 验收的前置门禁，不能以合成数据或工程完成度替代。门禁关闭前只关闭决策和既有数据模型缺陷，并可开展不计入阶段进度的隔离验证：

- 完成 ADR-005，以及 R2-01～03 所需的模型、迁移和真实 PostgreSQL 集成验证。
- Parser/Chunker/Embedding 只可用最小合成输入验证 Port 和失败协议，不接入未批准语料，不宣称摄入链路已经可用。
- 测试只使用代码内最小合成输入，或 manifest 明确允许 `repository_fixture` 的公开 fixture；不得递归摄入 `cases/`，每次读取来源前必须校验原始字节 SHA-256 与 `content_sha256` 一致。
- 基于 fixture 的结果只能标记为工程验证，不能写入正式解析质量基线，也不构成阶段退出。
- 阶段 0 关闭后，解析质量报告仍只能处理 manifest 允许列表中的来源，并遵守每个来源的 `allowed_uses`、敏感级别与外部 Provider 策略。

### 2.2 阶段 1 提供的已验证入口

阶段 2 复用以下已落地基础，并明确尚未验证的业务能力：

- 数据库层已经提供异步会话和事务边界；阶段 2 仍需在 Application 用例中建立摄入事务语义，不能把仓库 CRUD 等同于完整用例。
- Worker 已验证无正文消息、超时、有限重试、死信转移和优雅停止；业务摄入 actor 的幂等重入、取消、崩溃恢复和数据库/队列一致性仍是阶段 2 交付内容。
- ModelGateway 按 `embedding_zh` 能力别名调用 Embedding，CI 中替换为确定性 fake。
- Web 已具备 TanStack Query、错误和健康状态基线；数据源页面和长任务状态组件尚未实现。
- CI 可运行真实 PostgreSQL/pgvector、Redis 和迁移集成测试。

### 2.3 已固定的架构约束

阶段 2 直接遵守以下已接受决策，不重复选型：

- ADR-001：模块化单体 + 独立 Worker；摄入长任务放在 Worker，不在 API 进程内同步执行。
- ADR-002：PostgreSQL 16+、pgvector 和 PostgreSQL FTS；向量列和索引使用 pgvector。
- ADR-004：本地优先；Embedding 默认走确定性 fake，外部 Provider 必须显式启用，私有语料不外发。
- ADR-009：Redis + Dramatiq 只负责投递，摄入任务的持久状态存 PostgreSQL（`ingestion_tasks`）。

### 2.4 阶段 2 已关闭的技术决策

以下决策已由已接受的 ADR-005 固定；ADR-005 是文档、版本、块、任务和删除语义的唯一权威来源：

| 决策 | 建议基线 | 影响步骤 |
| --- | --- | --- |
| `stable_key` 与移动识别 | `stable_key` 由规范化来源 URI 派生，唯一范围为 `(source_id, stable_key)`；移动识别只在同一 Space/Source 内按哈希匹配，出现多个候选时报告冲突而不是猜测 | Step 3、Step 7、Step 8 |
| 原始哈希与内容指纹 | `blob_hash` 是原始文件字节 SHA-256，用于 manifest 校验、Blob 完整性和快速重复判断；`content_hash` 是 NORMALIZE 后内容的 SHA-256，符合术语表中的 Content Fingerprint。两者不得混用 | Step 2、Step 3 |
| Chunk 幂等身份 | `(version_id, ordinal)` 是同一版本内的写入/重入唯一键；`chunk_hash` 只基于规范化后的 Embedding 输入，不包含 `version_id` 或 `ordinal`，以便跨版本复用向量 | Step 4、Step 5、Step 6 |
| 处理版本与发布身份 | `parser_version`、`chunker_version`、`embedding_version` 及影响结果的配置摘要独立记录；相同文档、内容指纹和处理配置不得产生重复版本，配置变化产生新的可审计候选版本 | Step 2、Step 4、Step 5 |
| 原子发布与回滚 | 只有完整通过 VALIDATE 的版本才能在同一数据库事务内切换 `current_version_id`；失败或取消不改变当前已发布版本 | Step 5、Step 6 |
| 删除语义 | 先保留 tombstone 元数据并从发布集合撤下，再由可恢复的删除任务清理 Chunk、Embedding 和 Blob；恢复窗口、物理清理范围及引用失效所需最小元数据必须明确 | Step 7 |
| 任务状态与可靠投递 | `operation`、`status`、`stage`、取消请求、重试/租约信息和幂等键分离；采用数据库先记录、可重放派发/对账机制处理数据库提交成功但 Redis 投递失败 | Step 6 |

**状态**：已完成。不存在会改变实体字段、幂等身份或删除语义的未决技术问题；ID、版本、发布、任务和删除规则以 ADR-005 为准。阶段 0 人工复核与 PDF 合规样本仍是 Step 2 之前的外部门禁，不属于技术决策未决项。

## 3. 范围

### 3.1 本阶段交付

1. 6 个核心实体、仓库接口、ORM、迁移和测试（Step 1，已完成）。
2. Markdown/TXT/可复制文本 PDF 三个 parser，输出统一 `ParsedDocument` schema。
3. 结构感知分块、内容指纹、Embedding 调用和索引发布，只在 `PUBLISH` 后可见。
4. 异步摄入 Worker：显式阶段状态、进度、重试、取消和失败原因展示。
5. 同文件重复导入、内容修改、路径变化、删除与重建的增量流程。
6. 摄入 API：创建来源、上传/登记文件、触发摄入、查询任务状态、取消和重试。
7. 数据源与摄入状态页面，展示真实阶段和进度，不用无限旋转掩盖失败。
8. parser fixture、幂等测试、故障注入测试和解析质量报告脚本。

### 3.2 明确不做

- 不实现检索召回、融合、精排或多阶段检索（阶段 3）。
- 不实现问答、SSE、会话、引用协议、拒答或 Skill（阶段 4+）。
- 不实现 P1/P2 格式：DOCX、HTML、代码文件、带目录 PDF、扫描 PDF/OCR、PPTX、图片、网页抓取。
- 不实现 Notion/语雀/网盘等外部连接器。
- 不引入第二套队列、第二个 Embedding Provider 或 Reranker。
- 不把私有语料、Provider 响应、完整文档正文或 Embedding 产物放入仓库和 CI。

## 4. 目标工程形态

阶段 2 完成后，摄入相关模块建议最小形态如下（只在实际使用时落盘）：

```text
packages/
  domain/
    models.py            核心实体（Step 1 已建，后续迁移补充版本与任务字段）
    repositories.py      仓库 Port（Step 1 已建）
    parsing.py           ParsedDocument、结构节点、定位信息的纯类型与 Port
    chunking.py          Chunker Port 和统一 Chunk 输出 schema
    blob_store.py        BlobStore Port
  application/
    ingestion/           摄入用例编排：登记来源、运行管道、状态查询、取消/重试
  infrastructure/
    orm.py               ORM（Step 1 已建并完成身份、版本与任务字段修正）
    repositories.py      仓库实现（Step 1 已建）
    parsers/             Markdown/TXT/PDF parser Adapter
    chunkers/            结构感知 chunker 实现
    blob_store.py        本地文件 BlobStore Adapter
apps/
  worker/
    ingestion_tasks.py   摄入管道 Dramatiq actor
  api/
    routers/sources.py   来源与摄入 HTTP 接口
  web/
    数据源页面、摄入任务状态组件
migrations/
  versions/              阶段 2 后续迁移（字段、约束和任务状态补充）
cases/
  evals/corpus/v0/       manifest 允许列表与已批准 fixture
```

依赖方向保持阶段 1 约束：Parser/Chunker/BlobStore 作为可替换边界，通过 `domain` 的 Port 接入；`application` 只编排用例，具体格式和存储实现留在 `infrastructure`。

## 5. 分步实施

### 步骤 0：阶段 2 决策与 ADR

- 固定 §2.4 的 `stable_key` 规范化、`blob_hash`/`content_hash` 口径、Chunk 幂等身份、版本字段和删除语义。
- 完成预留的 ADR-005，记录核心实体的稳定 ID、双哈希、处理版本、原子发布、任务状态与删除决策。
- 确认 P0 格式边界（Markdown、TXT、可复制文本 PDF），扫描件/OCR 明确留给 P2。
- 确认解析质量报告只使用阶段 0 已批准并冻结的 manifest 来源，并记录当前缺少合规 PDF 验收样本的数据门禁。
- 处理阶段 1 Worker actor 日志差异：修复，或记录风险接受结论和替代诊断手段。

**完成标准**：实体字段、幂等身份、版本和删除语义无未决问题，均有唯一权威来源。

**状态**：已于 2026-07-18 完成。ADR-005 已接受；P0 格式边界保持 Markdown、TXT、可复制文本 PDF；扫描件/OCR 保持 P2；阶段 1 diagnostic actor 日志差异按 ADR-005 接受为进入风险，PostgreSQL 持久任务状态、Redis 队列、trace ID 和死信证据作为替代诊断依据。阶段 0 人工复核和 PDF 合规样本仍阻断 Step 2 正式实施与阶段验收。

### 步骤 1：数据模型基础

- 定义 Space、Source、Document、DocumentVersion、Chunk、IngestionTask 领域实体、枚举和 `RetrievalProfile` 值对象。
- 定义 6 个仓库 Protocol：CRUD + `get_by_source` / `get_by_stable_key` / `get_latest` / `create_batch` / `delete_by_version`。
- 实现 6 个 SQLAlchemy 2.0 ORM 模型，含外键、CASCADE、JSONB、pgvector `Vector(768)` 和向量索引。
- 实现 6 个纯异步仓库实现，含 domain↔ORM 双向映射器。
- 编写 Alembic 迁移创建 6 张业务表，可升级/降级。
- 单元测试覆盖域实体和 ORM 映射。

**完成标准**：域实体不依赖 ORM/框架；迁移在空库升级成功并可降级；仓库通过单元与集成 CRUD 测试。

**状态**：已于 2026-07-18 完成实现及 R2-01～03 修正；单元、静态、迁移和真实依赖集成验证均通过。

实际交付：

- 新增 6 个领域实体（纯 `frozen` dataclass）、`SourceType`/`DocumentStatus`/`TaskOperation`/`TaskStatus`/`TaskStage` 枚举和 `RetrievalProfile` 值对象；`domain` 不依赖 ORM、FastAPI 或模型 SDK。
- 新增 6 个仓库 `Protocol`，覆盖 CRUD 及 `get_by_source`、`get_by_stable_key`、`get_latest`、`create_batch`、`delete_by_version`。
- 新增 6 个 SQLAlchemy 2.0 `Mapped` ORM 模型，含外键 `ondelete="CASCADE"`、JSONB（`retrieval_profile`、`chunk.meta`）、pgvector `Vector(768)` 和常用索引。
- 新增 6 个纯异步仓库实现和 domain↔ORM 映射器；`retrieval_profile` 以 JSONB 存取，Chunk `embedding` 在读回时转为 `list[float]`。
- 新增迁移 `a1b2c3d4e5f6`，创建 `spaces`、`sources`、`documents`、`document_versions`、`chunks`、`ingestion_tasks` 6 张表及 IVFFlat 向量索引，可降级/升级。
- ADR-005 固定首期 pgvector 维度为 768，并移除不能控制数据库 schema 的运行时 `embedding_dimensions` 配置。
- 新增迁移 `b2c3d4e5f6a7`，补齐双哈希、处理版本与配置、tombstone、Chunk 幂等、任务可靠性字段、外键、检查约束和唯一约束；保留 6 张业务表。
- 更新 README、architecture、project-implementation-plan、AGENTS.md 文档。

2026-07-18 验证记录：

- Step 1 受影响范围的 `ruff check`、`ruff format --check` 与 `mypy packages` 通过。
- `tests/unit/test_domain_models.py`、`tests/unit/test_orm_models.py`、`tests/unit/test_config.py` 和 `tests/unit/test_model_gateway.py` 共 50 个相关单元测试通过。
- 隔离 PostgreSQL/Redis 上 21 个数据模型与本地依赖集成测试通过；临时 schema 中由 `Base.metadata.create_all` 成功创建 `vector_cosine_ops` IVFFlat 索引。
- 新迁移完成“旧 head + 旧格式合成数据 → 新 head → 降级 → 再升级”往返验证；双哈希、Chunk hash 和旧任务终态兼容回填通过。

**遗留修正项关闭记录**：

| ID | 修正 | 验证 |
| --- | --- | --- |
| R2-01 | ORM IVFFlat 索引补充 `postgresql_ops={"embedding": "vector_cosine_ops"}`，与迁移一致 | 隔离 PostgreSQL 临时 schema `create_all` 和索引定义检查通过 |
| R2-02 | ADR-005 固定 768 维 schema 契约；移除 `Settings.embedding_dimensions` | ORM schema/配置单元测试通过，维度变化必须走 ADR + 迁移 + 全量重建 |
| R2-03 | 新 revision `b2c3d4e5f6a7` 补齐字段、外键、检查约束及重试安全唯一键；仓库 stable key 查询改为 Source 作用域 | 单元/集成约束测试和含旧数据的迁移往返验证通过 |

### 步骤 2：Parser 与统一 ParsedDocument

- 在 `domain` 定义 `ParsedDocument` schema：文本、结构节点（标题层级、段落、代码块）、页码/行号定位信息和附件引用。
- 定义 Parser Port，输入原始文件字节和元数据，输出 `ParsedDocument`；具体格式实现放 `infrastructure/parsers`。
- 先实现 Markdown parser 并打通到 PUBLISH 的垂直链路，保留标题层级、代码块、列表和 1-based 行号；垂直链路通过后再接入其他格式。
- 实现 TXT parser：按行/空行段落切分，保留 1-based 行号和原始编码判定结果。
- 实现可复制文本 PDF parser：提取文本、1-based 页码和基础结构；扫描件/图片 PDF 明确返回「不支持/需 OCR」而非静默产出空文本。
- 对文件扩展名、声明 MIME 和实际内容做一致性校验；限制文件大小，并将 parser 放入有 CPU、内存和超时限制的隔离执行边界。
- 统一解析错误分类：格式不支持、类型不匹配、内容损坏、编码失败、空文档、超大文件、资源超限。
- NORMALIZE 后计算 `content_hash`，同时保留原始 `blob_hash`；规范化算法和版本必须可复现。

**完成标准**：三种 parser 对同一 fixture 输出结构一致的 `ParsedDocument`；不支持格式返回明确错误码，不伪造空结果。

**状态**：已于 2026-07-19 完成。

实际交付：

- 新增 `packages/domain/src/domain/parsing.py`：`ParsedDocument`（含 `ParseMetadata`）纯类型 schema、`StructNode`（含 node_type/level/text/start_line/end_line/start_page/end_page/language/children）、`StructNodeType` 枚举（DOCUMENT/HEADING/PARAGRAPH/CODE_BLOCK/LIST_ITEM/QUOTE_BLOCK/THEMATIC_BREAK/TABLE/RAW_TEXT）、`ParseErrorCode` 枚举（8 类错误码）、`ParseError`/`ParseSuccess` 结果类型、`Parser` Protocol（`async def parse(raw, metadata) -> ParseResult`）、`compute_blob_hash` 辅助函数。
- 新增 `packages/infrastructure/src/infrastructure/parsers/` 包：
  - **`MarkdownParser`**：基于 `markdown-it-py`，保留标题层级、代码块（含语言标注）、段落、列表项、引用和分隔线，输出 1-based 行号。空文档和编码失败返回明确错误码。
  - **`TxtParser`**：空白行分段落，多编码回退（utf-8 → utf-16 → latin-1 → cp1252），1-based 行号。
  - **`PdfParser`**：基于 `pypdf`，每页输出 `RAW_TEXT` 节点附带 1-based 页码；全页无提取文本时返回 `scanned_pdf` 错误码而非静默空结果。
  - **`ParserFactory`**：校验文件扩展名 + MIME 一致性 + 文件大小上限（默认 50 MB），按类型分派到对应 parser。
- 新增 `tests/fixtures/sample.md`、`sample.txt`、`sample.pdf` 测试夹具。
- 新增 32 个单元测试覆盖：三种 parser 的正常解析路径、空文档、编码失败、扫描件 PDF、损坏 PDF、MIME 不匹配、不支持格式、超大文件。所有 parser 结构节点保持 1-based 行号/页码。
- 新增配置：`max_upload_size_mb`（Pydantic Settings，默认 50 MB）。
- 新增依赖：`markdown-it-py>=3.0`、`pypdf>=5.0`。
- 更新文档：README.md、AGENTS.md、architecture.md、project-implementation-plan.md。

2026-07-19 验证记录：

- `ruff format --check .`、`ruff check .`、`mypy packages apps` 全部通过。
- 160 个单元测试全部通过（含 50 个原有领域/ORM/配置/ModelGateway 测试 + 32 个新增解析测试 + 78 个其他单元测试）。
- 三种 parser 对各自 fixture 输出结构正确的 `ParsedDocument`；不支持格式（.docx、无扩展名文件）返回 `unsupported_format`；MIME 不匹配返回 `type_mismatch`；空文件返回 `empty_document`；超大文件返回 `oversized_file`；损坏 PDF 返回 `content_corrupt`；扫描件 PDF 返回 `scanned_pdf`。

### 步骤 3：内容指纹与来源登记

- 实现 `stable_key` 规范化、原始字节 `blob_hash` 和规范化内容 `content_hash`（Step 0 口径）。
- 实现来源登记用例：创建 Source、登记文件、按 `(source_id, stable_key)` 查重定位到已有 Document；所有查询先受 Space 边界约束。
- 定义 BlobStore Port 并实现本地文件 Adapter。服务端生成存储键，禁止用上传文件名拼接物理路径；原始文件写入后校验哈希再进入后续阶段。
- `FINGERPRINT` 阶段先用 `blob_hash` 识别原始字节完全相同的输入；NORMALIZE 后再用 `content_hash` 与处理配置判断复用或创建候选 DocumentVersion。
- 用数据库唯一约束和冲突重读保证并发重复请求幂等，不能只依靠应用层“先查再写”。

**完成标准**：同一文件重复登记不新建 Document 或 DocumentVersion；内容变化产生新版本；原始文件可通过 BlobStore 稳定读回且哈希一致；跨 Space 不能命中或复用身份。

### 步骤 4：结构感知分块

- 在 `domain` 定义 Chunker Port 和统一 Chunk 输出（含 `chunk_hash`、定位 metadata、父子/邻接关系）。
- 实现结构感知 chunker：先按标题、段落、代码块、页分隔，再按 Token 上限切分。
- 保存父子和邻接关系，为阶段 3 的上下文扩展预留。
- 块大小、重叠率作为可配置项（来自 Space `RetrievalProfile`），进入后续评测变量。
- 实现分块幂等：以 `(version_id, ordinal)` upsert，`chunk_hash` 基于规范化 Embedding 输入且不包含版本 ID/ordinal；相同输入和 chunker 配置重复运行得到相同结果。

**完成标准**：同一 `ParsedDocument` 与 chunker 配置重复分块产出稳定顺序和 `chunk_hash`；块保留可验证的原文定位与结构关系；重复运行不新增重复块，也不改变已发布版本。

**状态**：已于 2026-07-19 完成。

实际交付：

- 新增 `packages/domain/src/domain/chunking.py`：`ChunkerConfig`（chunk_size/chunk_overlap/min_chunk_size）、`ChunkOutput`（ordinal/chunk_hash/heading_path/定位/adjacency/node_type）、`ChunkingResult`、`compute_chunk_hash`、`compute_chunker_config_hash`、`Chunker` Protocol（`async def chunk(document: ParsedDocument, config) -> ChunkingResult`）。
- 新增 `packages/infrastructure/src/infrastructure/chunkers/` 包：
  - **`StructureChunker`**：树遍历提取结构上下文（标题路径、行号、页码），按标题边界和 chunk_size 分组，支持重叠（overlap）、最小分块合并、超大分段按行/字符回退切分。无 structure 的文档回退到段落级分块。
  - 输出包含 `heading_path`（点分隔标题链）、`prev_ordinal`/`next_ordinal`（邻接链接）、`parent_ordinal`（父子关系预留）、`node_type` 和 `chunk_hash`（纯内容派生 SHA-256，不包含版本 ID/ordinal）。
- 更新 `packages/domain/src/domain/__init__.py`：导出 Chunker、ChunkerConfig、ChunkingResult、ChunkOutput、compute_chunk_hash、compute_chunker_config_hash。
- 新增 41 个单元测试覆盖：空文档、空白文本、短文本、段落分组、确定性检验（相同输入+配置产出相同 chunk_hash 和 ordinal）、重叠验证、config hash、Markdown 结构感知分块（标题边界、邻接链接、行号递增）、不同配置组合、超大单段切分、最小分块合并、无结构回退路径。
- 所有新代码通过 `ruff format --check .`、`ruff check .` 和 `mypy apps packages`。

2026-07-19 验证记录：

- `ruff format --check .`、`ruff check .`、`mypy apps packages` 全部通过。
- 248 个单元测试全部通过（原有 207 个 + 新增 41 个），21 个集成测试跳过（需 `RUN_INTEGRATION=1`）。
- 覆盖场景：空文本、短文本、段落分组、Markdown 结构分块（标题边界）、段落/代码块/列表元素识别、超大单段切分、重叠、不同 chunk_size/overlap 组合、确定性验证（相同输入重复运行产出相同 chunk_hash 和 ordinal）、配置变化产生不同 config_hash。

### 步骤 5：Embedding 与索引发布

- `EMBED` 阶段通过 ModelGateway `embedding_zh` 能力别名批量向量化，CI 用确定性 fake。
- 以 `(chunk_hash, embedding_version, embedding 配置摘要)` 作为向量复用键；只能复用策略允许的数据，跨 Space 复用必须在 ADR-005 明确允许，否则默认禁止。
- `INDEX` 阶段写入 pgvector；`embedding_version` 和实际维度独立追踪，支持后续以版本为条件重建。
- `VALIDATE` 阶段校验块数、向量维度、空文本比例；`PUBLISH` 后才更新 Document 的 `current_version_id`，使新版本对检索可见。
- PUBLISH 在单一数据库事务内验证候选版本属于目标 Document/Space 并切换 `current_version_id`；半成品版本在发布前不进入统一的 published candidate 查询。
- ADR-005 必须固定向量维度策略：若首期固定 768，则移除可运行时更改的误导配置；若允许配置，则维度变化只能通过迁移和全量重建完成，不能在同一向量列中混用。

**完成标准**：发布前索引不可见；Embedding 维度符合 ADR-005 固定的 768 维 schema 契约；重复内容安全复用 Embedding；发布是原子的版本切换。

**状态**：已于 2026-07-19 完成。

实际交付：

- `domain/models.py`：`DocumentStatus` 枚举新增 `EMBEDDED`、`PUBLISHED` 两个状态值。
- `domain/repositories.py`：`DocumentVersionRepository` Protocol 新增 `update()` 方法。
- `infrastructure/repositories.py`：实现 `DocumentVersionRepository.update()`，更新 blob_hash、content_hash、status、版本字段和 processing_config。
- 新增 `application/ingestion/embedding.py`：
  - `TextEmbedder` Protocol（应用层 Port，适配 ModelGateway）。
  - `EmbeddingConfig`（batch_size、max_empty_text_ratio、embedding_dimensions、embedding_version）。
  - `EmbeddingPipelineResult`（version、chunk_count、total_tokens、latency）。
  - `EmbeddingService.embed_and_publish()` 实现 EMBED→INDEX→VALIDATE→PUBLISH 四步流水线：分批调用 ModelGateway，delete+reinsert 幂等写入 Chunk，状态依次更新为 EMBEDDED→PUBLISHED，原子切换 Document.current_version_id，含向量维度/空文本比例/块数校验。
- 新增 9 个单元测试覆盖：正常路径、批处理、单块、空块校验拒绝、高空文本阈值允许、零块、幂等重入、自定义配置、元数据保留。

### 步骤 6：异步摄入 Worker 与状态机

- 实现摄入 Dramatiq actor，串联 DISCOVER→…→PUBLISH，每步更新 `ingestion_tasks` 的 `stage` 和 `progress`。
- 使用 Step 1 已补齐的 `operation`、`status`、`stage`、幂等键、目标版本、取消请求、尝试次数、租约/心跳和安全错误码实现状态转换。
- 所有状态转换遵守 `b2c3d4e5f6a7` 提供的字段、唯一约束和检查约束；后续 schema 变化继续使用新 revision，禁止修改既有迁移。
- 显式状态覆盖排队、运行、成功、部分失败、失败、取消中、已取消和死信；错误响应只保留可定位且脱敏的信息，不吞异常、不保存完整正文。
- 实现有界重试和幂等重入：任务从失败阶段恢复不重复已完成副作用。
- 实现取消：用户取消后停在安全边界，不留半发布版本。
- 处理 PostgreSQL 提交与 Redis 投递非原子问题：任务先持久化，派发失败可重放，并由对账逻辑重新投递无有效租约的 queued/running 任务；消息继续只携带 ID 和控制元数据。
- 复用阶段 1 的 trace/task 关联和脱敏日志，不记录文档正文。

**完成标准**：长任务有真实 operation/status/stage 和进度；失败可定位与重试；取消不产生可见半成品；重试不产生重复版本或块；数据库/Redis 部分失败和 Worker 崩溃可由对账恢复。

**状态**：已于 2026-07-19 完成。

实际交付：

- 新增 `packages/application/src/application/ingestion/orchestrator.py`：`IngestionOrchestrator` 应用服务，包含 `run_pipeline()`（完整 DISCOVER→…→PUBLISH 状态机，每步更新 `stage`/`progress`）、`cancel_task()`、`handle_pipeline_error()`（递增重试次数，超限转 FAILED）、`handle_cancellation()`。使用 `_stage_needed()` 和 DB 状态实现幂等重入——重试时从 DB 记录的 stage 继续，不重复已完成副作用。`_update_task_stage()` 从 DB 读取 `cancel_requested_at` 防止覆盖外部取消请求。
- 新增 `apps/worker/src/worker/ingestion_tasks.py`：
  - `ingestion_task` Dramatiq actor：接收 `task_id`/`trace_id`，加载任务和依赖，运行管道。异常分三类处理：`CancelledError` 记录取消后静默退出；业务异常记录错误后 `raise` 触发 Dramatiq 重试；`return` 表示成功。
  - `ingestion_task_permanently_failed` 死信 handler：重试耗尽后更新任务状态为 `DEAD_LETTER`。
  - 适配器 `_ParserAdapter`（ParserFactory → domain Parser protocol）和 `_GatewayTextEmbedder`（ModelGateway → TextEmbedder protocol）。
  - `_run_ingestion_async` 使用独立的 pipeline session 和 error-handling session，保证异常时错误记录不被已回滚 session 阻塞。
- 新增 `packages/infrastructure/src/infrastructure/config.py` 配置：`ingestion_task_timeout_ms`（600s）、`ingestion_task_max_retries`（3）、`ingestion_task_min_backoff_ms`（5s）、`ingestion_task_heartbeat_interval_s`（30s）、`ingestion_task_lease_seconds`（120s）。
- 更新 `packages/application/src/application/ingestion/__init__.py`：导出 `IngestionOrchestrator`、`IngestionConfig`、`IngestionResult`、`CancelledError`。
- 新增 34 个单元测试覆盖：成功管道全流程、重试从中断 stage 恢复（chunker/embedder 不重复调用）、解析失败、分块失败、来源缺失、Blob 缺失、外部取消检测、取消任务标记、错误递增重试、超限转 FAILED、取消终态记录。所有测试使用 in-memory fake repos/services。

### 步骤 7：增量维护与删除

- 未变化：`blob_hash`、`content_hash` 和处理配置均一致时，不新增 DocumentVersion 或 Chunk，并复用已完成结果。
- 内容变化：创建候选 DocumentVersion；只复制或复用满足版本与数据策略的 Embedding，不让新旧版本共享可变 Chunk 行。
- 路径变化：仅在同一 Space/Source 内唯一匹配 `blob_hash` 或 `content_hash` 时更新 `stable_key`/URI；零个或多个候选均返回可处理的冲突状态。
- 删除：先原子撤下 `current_version_id` 并留下 tombstone，再用 `operation=delete` 的持久任务异步清理 Chunk、Embedding 和 Blob；清理失败可重试，不得重新发布内容。
- parser/chunker/embedding 升级：以版本为条件批量重建，旧版本可回滚。

**完成标准**：重复导入不新增版本或块；修改产生可回滚新版本；移动不改变 Document ID 且歧义不会误合并；删除后不进入 published candidate 集合，清理任务最终完成或呈现明确失败；版本升级可重建与回滚。

**状态**：已于 2026-07-19 完成。

实际交付：

- `IngestionOrchestrator` 新增：
  - `is_content_unchanged()`：比较 `blob_hash` 和 `content_hash` 判断内容是否与当前已发布版本一致，一致时下游可跳过全量管道。
  - `delete_document()`：原子设置 `current_version_id=None` + `deleted_at=now`（tombstone），创建 `operation=DELETE` 的 `IngestionTask`，返回任务供 Worker 执行异步清理。
  - `update_document_path()`：更新 Document 的 `stable_key`（路径重命名），检查同 Source 内 `stable_key` 冲突。
  - `run_cleanup()`：遍历 Document 所有版本，删除对应 Chunks 和 Blob 存储，最后标记 DELETE task 为 SUCCEEDED。
- 未实现（推迟到阶段 3/4）：跨版本 Embedding 复用、parser/chunker/embedding 版本升级自动重建（需要更复杂的版本比较策略和批量迁移逻辑）。
- 新增 9 个单元测试覆盖：内容不变检测（匹配/无发布版本/不同字节）、删除创建 tombstone+task、已删除跳过、路径更新、路径冲突、清理删除 chunks 和 blobs、无文档清理。所有测试使用 in-memory fake repos/services。

### 步骤 8：摄入 API 与数据源页面

- API：创建来源、上传文件、触发摄入、查询任务状态与进度、取消、重试；错误响应复用阶段 1 稳定 schema。首期不接受客户端提供任意服务器文件路径。
- 每个 API 都显式接收或解析 Space 上下文，并在 Application 层校验 Source、Document、Version 和 Task 的归属；不能依靠前端过滤实现隔离。
- 上传端点限制请求体/文件大小，校验实际文件类型并防止路径遍历；先完成 Blob 写入和哈希校验，再创建可派发任务。
- 数据源页面：展示来源列表、文档和版本状态、摄入任务真实阶段与进度。
- 状态覆盖：空知识库、导入中、部分失败、无结果、任务重试、用户取消；不用无限旋转掩盖失败。
- 失败任务展示可读错误原因和重试入口；键盘焦点、窄屏布局和错误重试的基础检查。
- 新增公开 API 后重新生成 `docs/openapi.json` 并运行一致性检查。

**完成标准**：可从页面完成登记→摄入→查看状态→重试/取消闭环；所有长任务状态可见；不展示伪造数据。

### 步骤 9：测试、解析质量报告与验收

- parser fixture：为每种 P0 格式准备 manifest 已批准并冻结的 fixture，读取前校验 SHA-256，校验结构与 1-based 定位。
- 幂等测试：串行和并发重复导入均不新增版本/块，内容变化仅重建必要派生结果，删除后不进入 published candidate 集合。
- 故障注入测试：解析失败、Embedding 超时/限流、Worker 中途重启、超过重试上限、用户取消。
- 越权与安全回归：跨 Space 不读取、不复用、不发布；恶意文档始终作为数据且不触发指令/工具；路径遍历和伪造文件类型被拒绝；日志、span、错误和报告不泄漏正文。
- 一致性测试：数据库提交后投递失败可对账恢复；PUBLISH 前崩溃不切换版本；取消/失败保留旧发布版本；删除清理失败保持撤下状态。
- 解析质量报告脚本：对阶段 0 已批准语料输出各格式解析成功率、失败类型分布和各阶段耗时，并记录 corpus、parser、normalizer 和 chunker 版本。
- 从 Web/API 完成“上传 Markdown → 成功发布 → 重复上传 → 修改 → 移动/重命名 → 删除 → 失败重试/取消”的端到端验收。
- 在干净隔离环境执行规范命令、真实 PostgreSQL/Redis 集成测试、Compose 冷启动与保留卷重启，保存命令和结果摘要。

**完成标准**：阶段 0 已批准语料解析成功率达到冻结阈值；重复导入不新增版本或块；失败任务可定位和重试；报告和端到端旅程可复现。

## 6. 执行依赖与状态

| 步骤 | 必须先满足 | 状态 |
| --- | --- | --- |
| 0. 决策与 ADR-005 | 已接受 ADR-001、002、004、009 | 已完成；阶段 0 数据门禁仍待外部关闭 |
| 1. 数据模型基础 | 无 | 已完成；R2-01～03 已关闭 |
| 2. Parser 与 ParsedDocument | ADR-005 中双哈希、定位和处理版本语义确定 | 已完成；Markdown/TXT/可复制文本 PDF 三种 parser 已实现，32 个单元测试通过 |
| 3. 指纹与来源登记 | ADR-005 中 stable key、Blob 和并发幂等语义确定 | 已完成 |
| 4. 结构感知分块 | Markdown Parser 契约通过 | 已完成 |
| 5. Embedding 与发布 | 分块契约、向量维度和发布语义确定 | 已完成 |
| 6. Worker 与状态机 | 单进程 Markdown 管道通过；任务字段迁移完成 | 已完成 |
| 7. 增量与删除 | Worker 重入、发布和 tombstone 语义通过 | 已完成 |
| 8. API 与数据源页面 | Application 摄入用例和 Space 隔离完成 | 待办 |
| 9. 质量报告与验收 | 阶段 0 门禁关闭；P0 合规语料冻结 | 待办 |

执行时先完成 Markdown 垂直链路，再接入 TXT 和 PDF；每一步只有在其完成标准和受影响测试通过后才进入下一个依赖步骤。

## 7. 测试与验证矩阵

| 层级 | 必测内容 | 是否依赖真实外部服务 |
| --- | --- | --- |
| 单元 | 实体不变量、ORM 映射、`stable_key`/`blob_hash`/`content_hash`/`chunk_hash`、解析结构节点、分块边界 | 否 |
| 契约 | Parser Port 各实现输出一致性、Chunker 输出 schema、ModelGateway Embedding fake | Embedding 使用 fake |
| 集成 | 6 表 CRUD、后续迁移、pgvector 写读、原子发布、Worker 摄入管道、Redis 投递与对账 | 仅隔离的本地容器 |
| 幂等 | 串行/并发重复导入不新增版本或块、内容变化安全复用向量、删除后不进入发布集合 | 隔离 PostgreSQL |
| 故障注入 | 解析失败、Embedding 超时/限流、Worker 重启、提交后投递失败、租约失效、超重试上限、用户取消 | 隔离 PostgreSQL + Redis |
| 前端 | 来源列表、摄入进度、部分失败、重试、取消、无限 loading 终止、键盘焦点 | API mock 或本地 API |
| 安全 | 跨 Space 隔离、manifest/hash 校验、路径遍历、类型伪造、恶意文档、日志不泄漏正文、私有语料不外发 | 否 |
| 端到端 | 上传、发布、重复、修改、移动、删除、失败重试和取消的用户旅程 | 隔离的完整本地栈 |

至少覆盖以下失败模式：

- 不支持格式、损坏文件、空文档、超大文件。
- 同文件串行/并发重复导入、内容修改、路径变化歧义、删除与重建。
- Embedding Provider 超时、限流、维度不一致、不可用。
- Worker 中途重启、任务超时、数据库提交后 Redis 投递失败、租约过期、超过重试上限、用户取消。
- PUBLISH 前中断不产生可见半成品；跨 Space 身份匹配、向量复用和 published candidate 查询均隔离。

## 8. 阶段退出条件

以下条件必须全部满足：

1. 阶段 0 的授权复核、人工标注复核和版本冻结已关闭；验收 corpus 与阈值有冻结版本，所有输入均来自 manifest 允许列表且原始 SHA-256 匹配。
2. Markdown 垂直链路先独立通过；随后 Markdown、TXT、可复制文本 PDF 三种 P0 格式均可稳定摄入并输出统一 `ParsedDocument`。
3. 阶段 0 已批准语料的 parser 成功率达到冻结阈值（当前基线为 >= 95%），报告记录 corpus、parser 和 normalizer 版本；合成输入结果不计入该指标。
4. 同文件串行或并发重复导入不新增 Document、DocumentVersion 或 Chunk；内容修改产生可回滚候选版本，路径移动保持 Document ID，歧义不误合并。
5. 删除后文档立即退出 published candidate 集合；清理任务可恢复执行，并保留产品后续标识“来源已不存在”所需的 tombstone 元数据。
6. 摄入任务具有独立的 operation/status/stage、真实进度、取消、有限重试、死信和脱敏失败原因；Worker 重启及数据库/Redis 部分失败可以对账恢复。
7. 只有通过 VALIDATE 并完成原子 PUBLISH 的版本可见；失败、取消或崩溃不切换当前版本，回滚可恢复上一已发布版本。
8. 所有来源、身份匹配、派生结果和 API 查询受 Space 隔离；跨 Space 泄漏或错误复用为 0。
9. 数据源页面展示真实来源、文档、版本和任务状态，可完成重试/取消，不展示伪造数据或无限 loading。
10. 后端规范检查、单元/契约/集成/幂等/故障注入/安全测试，前端 lint/typecheck/test/build 及端到端旅程在 CI 或等价干净环境稳定通过。
11. 新迁移在空库升级、降级和已有阶段 2 Step 1 数据上升级成功；新增公开 API 后的 `docs/openapi.json` 与运行时 schema 一致。
12. 遗留项 R2-01、R2-02、R2-03 和阶段 1 Worker actor 日志差异均已修复，或对日志差异形成明确风险接受记录和替代验证证据。
13. 日志、span、错误响应、fixture、报告和 Git 变更中无密钥、私密正文、完整文档内容、Provider 响应或 Embedding 产物。

## 9. 主要风险与应对

| 风险 | 早期信号 | 应对 |
| --- | --- | --- |
| ORM 与迁移向量索引定义不一致 | 集成测试在真实 PG 上建索引报错（R2-01） | Step 0 后立即对齐 opclass，并在隔离 PG 实跑集成测试，不依赖仅单元测试的绿灯 |
| 幂等身份定义滞后 | 分块和重复导入实现后才补 `chunk_hash` 字段 | Step 0 先固定 `chunk_hash` 和唯一约束，再实现分块与增量（R2-03） |
| 原始哈希与内容指纹混用 | manifest 校验、重复检测和规范化版本使用同一含糊字段 | ADR-005 固定 `blob_hash` 与 `content_hash` 的输入、算法和用途，并为两者写 golden test |
| 并发请求绕过应用层查重 | 两个请求同时“先查后写”并产生重复版本 | 以数据库唯一约束为最终防线，捕获冲突后重读，不依赖进程内锁 |
| PDF 解析吞掉扫描件 | 图片型 PDF 静默产出空文本，成功率虚高 | 解析器显式区分可复制文本与扫描件，后者返回「需 OCR」错误码 |
| 半成品索引对检索可见 | 摄入中途版本被查询命中 | 严格 PUBLISH 门禁，只有发布后切换 `current_version_id` |
| 私有语料进入 CI 或外发 Embedding | fixture 引用 `cases/` 私有文件或真实 Provider | 只用合成/授权 fixture，CI Embedding 用 fake，外部 Provider 默认关闭 |
| Worker 重试产生重复块 | 重跑后同版本块数翻倍 | 以 `(version_id, ordinal)` 唯一约束和 upsert 实现幂等；`chunk_hash` 用于内容/向量复用，不承担行唯一性 |
| 数据库与 Redis 部分成功 | 任务已入库但未投递，或重复投递 | 数据库任务为事实源，使用可重放派发、租约和对账恢复；consumer 始终幂等 |
| 文件移动被错误合并 | 同一来源中多个文件具有相同内容 | 移动匹配限制在同一 Space/Source，只有唯一候选才自动合并，其他情况报告冲突 |
| Parser 消耗失控 | 损坏或恶意文件占满 CPU/内存 | 文件大小限制、类型嗅探、隔离执行、CPU/内存/超时边界和资源超限错误分类 |
| 阶段 2 偷跑阶段 3 | 开始实现检索排序或融合 | 以本计划「不做」清单审查范围，检索留给阶段 3 |
| 阶段 0 未关闭却声称真实成功率 | 退出声明引用真实语料指标 | 成功率口径显式标注样本来源，真实语料指标等阶段 0 放开后再声明 |

## 10. 下一阶段接口

阶段 2 结束时为阶段 3（混合检索与评测基线）提供以下已验证入口：

- 已发布 DocumentVersion 的 Chunk 带稳定 `chunk_hash`、定位 metadata、处理版本和 pgvector Embedding，可用于向量召回。
- Chunk 保留父子/邻接关系，供检索命中后上下文扩展。
- Space `RetrievalProfile` 提供 chunk 大小、重叠、`top_k` 等可进入评测的配置。
- 提供唯一的、按 Space 过滤且只返回 `current_version_id` 对应 Chunk 的 published candidate 查询边界；阶段 3 不应自行拼接私有表绕过发布和隔离语义。
- 摄入任务的阶段耗时和失败类型可作为运营与诊断数据来源。
- 增量与版本机制支持 parser/chunker/embedding 升级后的索引重建，为评测变量控制提供基础。

阶段 3 再基于这些可检索索引实现关键词、向量和混合召回、融合与精排，并输出每阶段候选与分数诊断；阶段 2 不提前实现检索排序逻辑。
