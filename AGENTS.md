# AGENTS.md

本文件适用于整个仓库。它将 `README.md` 中的项目目标和
`docs/project-implementation-plan.md` 中的实施方案转化为日常开发约束。

## 1. 项目目标

本项目构建一个本地优先、来源可追溯的个人知识工作台：用户导入笔记、论文、
课程资料和代码后，系统完成解析、索引、混合检索、问答和知识整理，并将稳定流程
封装为可版本化的 Agent Skill。

首个可用版本的核心闭环是：

1. 导入真实文档并展示解析、索引和失败状态。
2. 针对单文档或跨文档问题返回有依据的回答。
3. 每个关键结论能够定位到对应文档版本和原文位置。
4. 文件修改、移动或删除后进行幂等增量更新。
5. 同一知识能力能够从 Web、HTTP API 和 Skill 调用。
6. 检索和回答质量能够通过版本化评测集回归验证。

完整方案见 `docs/project-implementation-plan.md`。实现与方案冲突时，不要静默偏离；
对影响模块边界、数据模型、公开接口或技术基线的改变，应新增或更新 ADR。

## 2. 当前阶段与优先级

仓库当前已建立阶段 0 的语料、评测、产品范围和首批 ADR 基线，但语料仍处于
`draft_pending_license_review`，尚未冻结；代码仓库仍处于工程初始化阶段。开始任务前
先检查实际目录、配置、ADR、测试和 Git 状态，不要假设计划中的目录或技术已经存在。

按以下顺序推进：

1. 完成阶段 0 语料的授权复核、人工标注复核和版本冻结。
2. 遵守已接受的 ADR-001 至 ADR-004，不重复讨论已固定基线，除非触发重新评估条件。
3. API、Worker、Web、PostgreSQL 和 CI 工程骨架。
4. 核心数据模型与数据库迁移。
5. 单个 Markdown 文件的幂等摄入闭环。
6. 关键词、向量和混合检索基线及评测工具。
7. 引用协议、原文定位和带引用回答。
8. 完整端到端用户旅程。
9. `knowledge_qa` Skill 标准化。
10. 其他 Skill 和扩展能力。

优先级定义：

- P0：增量摄入、空间隔离、混合检索、可定位引用、拒答、知识工作台、
  `knowledge_qa` Skill、离线评测和端到端测试。
- P1：更多解析格式、知识整理 Skill、检查点恢复、评测界面、多模型 Provider。
- P2：知识图谱、多模态、多 Agent、团队协作、插件客户端和模型微调。

P0 闭环未完成或没有评测基线时，不实现 P2。接口可以预留，但不要创建未被真实需求
使用的抽象或基础设施。

### 2.1 阶段 0 基线文件

阶段 0 资料当前位于仓库同级目录 `../cases/`。路径必须相对于本仓库解析，不要在代码、
配置或文档中写死某台机器的绝对路径。以下文件具有不同职责：

- `../cases/docs/product/mvp-scope.md`：P0/P1/P2 范围、暂定质量门槛和阶段退出条件。
- `../cases/docs/product/personas-and-stories.md`：persona 与端到端验收故事。
- `../cases/docs/product/workbench-flow.md`：工作台页面、异常状态和 API/任务交互低保真流程。
- `../cases/docs/glossary.md`：领域术语的统一定义；代码、schema 和文档优先沿用这些名称。
- `../cases/docs/privacy/demo-data-policy.md`：语料分类、脱敏、外部模型、日志和公开演示规则。
- `../cases/docs/adr/001-*.md` 至 `004-*.md`：模块化单体、PostgreSQL/pgvector、Agent
  Runtime 边界和本地优先数据边界的已接受决策。
- `../cases/evals/corpus/v0/manifest.yaml`：评测语料的唯一允许列表、Space、来源版本和
  使用权限；周边目录不是可摄入语料。
- `../cases/evals/corpus/v0/README.md`：manifest 的路径、哈希和使用说明。
- `../cases/evals/corpus/v0/fixtures/`：版本、来源冲突和恶意文档的确定性测试 fixture，
  不用于代表真实业务语料质量。
- `../cases/evals/datasets/knowledge-qa-v0/cases.jsonl`：30 条带 claim-evidence 关系的
  开发/holdout 评测用例。
- `../cases/evals/datasets/knowledge-qa-v0/schema.json`：每条 JSONL 记录必须满足的 JSON
  Schema。
- `../cases/evals/datasets/knowledge-qa-v0/README.md`：Evidence、拒答、split 和评分语义。

开始摄入、检索、引用、问答、Skill 或评测相关任务前，必须阅读与变更相关的上述文件。
工程文档迁入本仓库时应保留历史并一次性更新引用；不得在 `code/docs` 与 `cases/docs`
长期维护内容分叉的两个权威版本。

## 3. 架构不变量

### 3.1 总体形态

- 首期采用模块化单体，并用独立 Worker 执行解析、Embedding 和索引等长任务。
- 不在没有容量、隔离或团队边界证据时拆分微服务。
- 保持 API、Application、Domain、Knowledge、Agent Runtime、Model Gateway 和
  Infrastructure 的边界清晰。
- 长任务必须有显式状态，可重试、可取消、可观测；不能只存在于请求进程内存中。

### 3.2 依赖方向

- `domain` 包含纯领域实体、值对象和 Port，不依赖 FastAPI、ORM、队列、具体 Agent
  框架或模型 SDK。
- `application` 编排用例和事务，不承载供应商实现细节。
- `infrastructure` 实现数据库、队列、文件、模型和外部服务 Adapter。
- 传输层负责协议、认证、输入校验和响应映射，不直接实现领域规则。
- 跨模块调用依赖明确接口，禁止为了方便从其他模块读取内部表或私有实现。

### 3.3 可替换边界

以下能力必须通过稳定接口接入：

- `ModelGateway`：Chat/Tool Calling、Embedding、Reranker。
- `RetrievalStore`：关键词、向量、混合检索和元数据过滤。
- `BlobStore`：本地文件系统及未来的 S3/MinIO。
- Parser/Chunker：不同格式与领域分块策略。
- Agent Runtime Adapter：具体图执行或编排引擎。

业务模块不得直接依赖某家模型 Provider SDK。以能力别名引用模型，例如
`fast_chat`、`reasoning_chat`、`embedding_zh` 和 `reranker`，不要把具体模型名散落
在业务代码中。

### 3.4 Agent 与 Skill

- 首期使用单 Agent、有界、显式状态工作流。只有权限隔离、上下文隔离或可证明的
  并行收益出现时才引入多 Agent。
- 每次运行限制工具白名单、最大步骤、超时、Token 和费用。
- 敏感写操作必须经过确认，所有工具调用必须校验结构化输入和输出。
- Skill 是包含 manifest、workflow、prompt、schema、eval 和版本的工作流包，不是
  单个 prompt 文件。
- 运行开始后固定 Skill 版本；热加载只影响新运行，旧版本必须可回滚。
- 不加载未经信任、校验或审核的任意代码。

## 4. 数据与摄入规则

### 4.1 核心实体

核心模型包括 `User`、`Space`、`Source`、`Document`、`DocumentVersion`、`Chunk`、
`IngestionTask`、`Conversation`、`AgentRun`、`Evidence`、`Feedback` 和 `EvalRun`。
即使首期只支持单用户，也要保留 owner/space 隔离边界。

### 4.2 版本与幂等

- 逻辑文档和文档版本分离；内容变化创建新 `DocumentVersion`。
- 使用规范化来源 URI 形成稳定来源身份，使用内容哈希识别变化。
- parser、chunker、embedding 和 retrieval 配置分别版本化。
- Chunk ID 必须稳定且可重建，重复任务不能产生重复块或重复索引。
- 删除先撤出当前检索集合，再异步清理；已删除或其他空间的内容绝不能被召回。
- 所有生成结果记录模型、prompt、Skill、检索配置和证据版本，以便复现。

### 4.3 摄入管道

标准阶段为：

```text
DISCOVER -> FINGERPRINT -> PARSE -> NORMALIZE -> ENRICH
-> CHUNK -> EMBED -> INDEX -> VALIDATE -> PUBLISH
```

每个阶段应输入输出明确、可缓存、可重试并带观测数据。只有通过 `VALIDATE` 的完整
版本才能 `PUBLISH` 给检索使用，禁止暴露半成品索引。

Parser 输出统一的 `ParsedDocument`，保留标题层级、页码、行号、代码块等定位信息。
默认使用结构感知分块；固定字符长度只能作为基线，不应成为唯一策略。

## 5. 检索、回答与引用

Knowledge Service 只负责返回带分数、来源和定位信息的证据，不负责生成最终自然语言
答案。推荐链路为：

```text
查询分类/改写 -> 空间与元数据过滤
-> Dense + Keyword 候选召回 -> 融合 -> Reranker
-> 去重/多样性 -> 父块或邻块扩展 -> 上下文构建
-> 带引用生成 -> 引用完整性验证
```

实现时遵守：

- 关键词、向量、混合检索必须能独立评测和对比。
- 记录每阶段候选、分数、过滤原因和最终证据，以便定位失败阶段。
- 空间和权限过滤应尽早执行，生成后再次验证引用归属。
- 引用至少关联 `Evidence`、`Chunk`、`DocumentVersion` 和原文定位信息。
- 关键 claim 没有充分证据时明确拒答或说明限制，禁止生成虚假引用。
- 回答协议使用结构化 schema；不要依靠字符串后处理猜测引用关系。

## 6. 技术基线

在没有 ADR 推翻前，默认技术方向是：

- Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic。
- PostgreSQL 16+ 与 pgvector；首期关键词检索使用 PostgreSQL FTS。
- Redis 加 Dramatiq 或 Celery，正式落地前通过 ADR 固定其中一个。
- 本地文件系统加 `BlobStore` 接口。
- 自有 Agent Runtime 接口加 LangGraph Adapter。
- React、TypeScript、Vite、TanStack Query。
- OpenTelemetry 和结构化日志。
- pytest、Testcontainers、Playwright。

不要同时引入多个功能重叠的 Agent 框架、向量库、任务队列或前端状态库。新增运行时
依赖前要说明现有标准库或依赖为何不足，并考虑许可证、体积、维护状态和替换成本。

## 7. API 与异步任务约定

- 公开 API 从 `/api/v1` 开始；资源命名使用复数名词。
- 请求和响应使用结构化 schema，不直接暴露 ORM 对象。
- 创建长任务返回 `202` 和稳定的 `task_id`。
- 单向流式回答优先使用 SSE，只有明确需要双向实时控制时使用 WebSocket。
- 错误返回稳定机器码、用户可理解消息和 `trace_id`；不要泄漏内部堆栈或私密正文。
- 内部事件带 `event_version`，消费者必须显式处理兼容性。
- 写操作明确事务边界和幂等键；重试不能改变最终语义。

## 8. 安全与隐私

- 默认本地优先。任何把文档内容发送到外部 Provider 的能力必须配置明确且可见。
- 密钥只能来自环境变量或密钥存储，不得提交仓库或写入日志。
- 外部文档始终视为不可信数据，不能通过文档内容提升工具权限或覆盖系统指令。
- 校验文件类型、大小、路径、文件名、解压深度和解析超时，防止路径遍历和资源耗尽。
- Parser 和高风险转换器应可隔离执行并限制 CPU、内存和时间。
- 禁止模型直接执行任意 shell、SQL 或 URL；只允许注册、校验和审计过的 Tool。
- 日志默认不保存完整文档、完整 prompt、密钥或个人信息；调试采样需显式开启并脱敏。
- 对越权检索、恶意文档 prompt injection、危险工具调用和日志泄漏编写回归测试。

测试和演示语料必须脱敏。不要把真实个人笔记、论文授权受限内容、API 响应缓存或
Embedding 产物直接提交仓库，除非已有明确授权和数据策略。

### 8.1 基线语料使用规则

- 只处理 `../cases/evals/corpus/v0/manifest.yaml` 中明确列出的来源。禁止递归摄入整个
  `../cases/`；其周边包含缓存、二进制、个人标识、受限教学材料和未审核资源。
- manifest 中的 `path` 相对于 `../cases/` 根目录解析。读取来源前校验原始文件字节的
  SHA-256 与 `content_sha256` 一致；不一致视为新版本或语料损坏，不能静默继续。
- `sensitivity`、`allowed_uses` 和 `redistribution` 同时生效，使用范围取最严格交集。
  只有 `public_demo` 且明确包含 `repository_fixture` 的来源可进入仓库或公开演示包。
- `private_local`、`restricted_educational`、`undetermined` 和
  `prohibited_pending_review` 来源只能按 manifest 的授权在本地处理，不得发送给外部
  Provider、提交仓库、制作公开截图或写入包含正文的日志。
- 解析缓存、Embedding、Evidence quote、模型回答、评测报告和截图继承来源的敏感级别，
  不会因为是派生数据而自动变为可公开。
- 当前 manifest 状态为 `draft_pending_license_review`。许可证复核完成前不得将整个
  corpus 宣称为公开、可发布或已冻结数据集。

## 9. 测试与评测要求

变更的测试范围与风险匹配：

- 领域逻辑：单元测试，必要时增加属性测试。
- Parser、Model Provider、RetrievalStore、Tool、Skill：契约测试。
- 数据库、pgvector、队列、迁移和索引发布：真实依赖集成测试。
- 解析结构和原文定位：固定 fixture 的 golden 测试。
- 导入、查询、引用、反馈及增量更新：端到端测试。
- 检索或 prompt 变化：运行相关 Eval，并记录数据集、模型和配置版本。

CI 中的大多数测试不得依赖真实付费模型。为确定性逻辑提供 fake/stub；少量真实模型
集成测试和完整评测按发布流程运行。

修复缺陷时先增加能复现问题的测试。检索和回答效果不能只凭单个演示问题判断；至少
比较关键词、向量和混合基线。LLM-as-judge 只能作为一个信号，关键用例还需规则、
证据匹配或人工抽检。

### 9.1 基线评测集使用规则

- `cases.jsonl` 每个非空行是一条独立 JSON 记录，必须通过同目录 `schema.json` 校验；
  不用注释、尾逗号或跨行 JSON 对象扩展 JSONL 格式。
- `space_id` 是查询唯一可见的 Space。回答用例的 Evidence 必须属于该 Space；拒答用例
  中的 `reference_scope` 只是人工复核范围，不是支持答案的证据。
- `source_version` 必须等于 manifest 中对应来源的 `content_sha256`。PDF 页码和文本行号
  都从 1 开始，文本行区间包含首尾行。
- `quote` 必须真实出现在指定页或行区间内。`excerpt_sha256` 的规范化算法为 Unicode
  NFKC、连续空白折叠为一个空格、去除首尾空白，然后对 UTF-8 字节计算 SHA-256。
- 每个 `answer_claims[].id` 必须至少被一个 `evidence[].supports_claims` 覆盖。拒答用例的
  `answer_claims` 和 `evidence` 必须为空，并通过 `forbidden_claims` 与
  `retrieval_expectations` 约束不可接受行为。
- `development` split 可用于诊断和调参；`holdout` split 不得用于 prompt、分块、权重、
  top-k、模型或阈值调优。只在预先约定的里程碑运行并记录 holdout 结果。
- `fixtures/` 只验证版本、冲突、隔离和安全等确定性行为，不得混入真实语料指标后声称
  检索质量提升。
- 修改来源、问题、claim、Evidence、split 或评分语义后，至少检查：YAML/JSON/JSONL
  可解析、JSON Schema 通过、ID/source_key 唯一、所有路径和哈希有效、quote 可回到定位、
  claim 覆盖完整、Evidence 不跨 Space、私有来源没有 `repository_fixture` 权限。
- 数据集冻结后，影响期望行为或评分语义的修改必须创建新版本；修复旧版本时保留变更
  记录，不得原地重写后仍声称结果可与旧 EvalRun 直接比较。

## 10. 可观测性要求

每个外部请求生成 `trace_id`，贯穿 API、Application、AgentRun、检索、ModelGateway
和 Worker。至少记录：

- 摄入阶段、输入/输出数量、耗时和失败类型。
- 检索阶段候选 ID、分数、过滤原因和证据选择。
- 模型能力别名、Provider、Token、耗时、重试和结构化输出错误。
- Agent 步骤、工具调用、预算、检查点和终止原因。
- Skill、prompt、检索配置版本及用户反馈。

日志必须结构化并带请求/运行/任务关联 ID。基础设施错误与“知识库中没有答案”是不同
状态，不能互相伪装。

## 11. 前端要求

- 首屏是可使用的知识工作台，不是营销落地页。
- 核心区域包括空间/会话导航、对话或任务工作区、证据与原文查看器。
- 长任务展示真实阶段、进度、失败原因、重试和取消，不用无限 loading 掩盖错误。
- 覆盖空知识库、部分导入失败、无检索结果、模型不可用、回答中断、引用失效等状态。
- 引用编号与证据面板稳定对应；移动端证据区可使用抽屉，避免遮挡回答和控件。
- 保持键盘可操作、焦点清晰、颜色不是唯一状态信号。
- 沿用已建立的设计系统和图标库；不要为单个页面另建视觉语言。

## 12. 建议目录边界

项目初始化后优先采用以下结构；若实际工具链要求不同，应保留相同职责边界：

```text
apps/api/                 FastAPI 入口与传输层
apps/worker/              后台任务入口
apps/web/                 Web 工作台
packages/domain/          领域实体、值对象和 Port
packages/application/     用例编排与事务边界
packages/agent_runtime/   状态机、Tool、Skill 和检查点
packages/knowledge/       摄入、分块、检索和引用
packages/model_gateway/   模型能力与 Provider Adapter
packages/infrastructure/  DB、队列、Blob 和外部服务 Adapter
skills/                   版本化 Skill 包
evals/                    数据集、runner 和报告
tests/                    unit、integration、contract、e2e
migrations/               数据库迁移
docs/adr/                 架构决策记录
docs/architecture/        架构细节
deploy/                   本地与发布部署配置
```

不要创建空目录或占位模块来模拟进度；在首个真实用例需要时创建对应结构。

## 13. 工作方式

开始实现前：

1. 阅读 `README.md`、本文件、实施计划及相关 ADR；摄入、检索、引用、问答、Skill、
   前端旅程或评测任务还必须读取 `../cases/` 中对应的阶段 0 基线文件。
2. 检查当前工作树，保留并兼容已有用户改动。
3. 确认任务属于哪个阶段和优先级，并追踪从入口到数据层的现有实现。
4. 明确行为变化、失败模式、迁移影响和验证方式。

实现过程中：

- 选择满足当前需求的最小完整改动，不做无关重构。
- 优先沿用已有项目模式，再考虑增加抽象或依赖。
- 使用结构化 parser/schema 处理结构化数据，不用脆弱的字符串拼接。
- 数据模型、公开 API、事件、Skill 和 prompt 发生不兼容变化时显式版本化。
- 代码注释解释不明显的约束或原因，不复述代码行为。
- 不修改与任务无关的生成文件、格式或元数据。

完成前：

1. 运行受影响模块的格式化、lint、类型检查和测试。
2. 对共享接口、迁移、摄入、检索或用户旅程变更扩大测试范围。
3. 检查日志、错误响应和测试 fixture 是否泄漏隐私或密钥。
4. 更新受影响文档、OpenAPI、Skill schema、评测 schema/manifest、迁移说明或 ADR。
5. 汇报实际运行的验证命令；无法运行的检查必须说明原因。

## 14. 命令发现规则

当前仓库尚未建立正式构建和测试工具链，因此本文件不声明虚假的固定命令。代理必须从
实际存在的 `pyproject.toml`、`package.json`、锁文件、Compose 和 CI 配置中发现命令，
并优先使用锁定版本对应的包管理器。

初始化工程工具链时，应同时：

- 提供单一、明确的安装、启动、格式化、lint、类型检查、测试和迁移入口。
- 在 README 或贡献文档中记录这些命令。
- 将相同命令接入 CI，避免本地与 CI 使用两套流程。
- 更新本节，列出经过实际验证的规范命令。

不得声称未运行的命令已通过，也不得因缺少工具链而跳过对纯文档、schema 或静态内容
可执行的基本检查。

## 15. ADR 触发条件

出现以下情况时新增或更新 `docs/adr/` 下的记录：

- 改变模块化单体、Worker 或部署边界。
- 更换数据库、检索后端、任务队列、Agent 引擎或主要前端框架。
- 改变核心实体、版本/删除语义、Skill 信任模型或外部数据边界。
- 引入微服务、多 Agent、知识图谱、多模态、团队权限或模型微调。
- 发布不兼容 API/事件/schema，或放弃既有质量/安全门禁。

ADR 必须包含背景、决定、备选方案、后果和重新评估触发条件。

## 16. 完成定义

一项变更完成时应满足：

- 行为和验收条件已实现，没有用静态假数据掩盖核心流程。
- 受影响测试与静态检查通过，或明确记录未验证项及原因。
- 长任务可观测、可恢复；写入幂等；失败对用户可解释。
- 引用可定位，空间隔离和删除语义没有退化。
- 不泄漏密钥、私密正文或敏感日志。
- 语料使用符合 manifest 和隐私策略；评测引用、版本、Space 与 holdout 语义没有退化。
- API、数据迁移、Skill、prompt、评测集和文档按需版本化或更新。
- 没有为了展示扩展性而提前引入未使用的服务、Agent 或抽象。
