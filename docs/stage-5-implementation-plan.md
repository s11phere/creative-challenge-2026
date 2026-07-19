# 阶段 5 实施计划：Agent Runtime 与 Skill 标准化

> 文档状态：Draft v1
>
> 适用范围：`docs/project-implementation-plan.md` 中的阶段 5
>
> 计划基线日期：2026-07-19

## 1. 结论摘要

阶段 5 的目标是把阶段 2 至阶段 4 已验证的摄入、检索、引用和问答能力封装为稳定、
可版本化、可审计、可恢复的 Skill，并确保同一个 Skill 通过 Web、HTTP API 和测试入口
调用同一 Application 用例。

当前项目处于阶段 2 Step 0/1 完成后的状态。现阶段可以先行实现 Agent Runtime Port、
Tool/Skill 契约、Registry、版本固定、预算、权限、检查点接口和确定性测试替身；但以下
部分必须等待前序阶段提供真实能力后才能接入和验收：

- `knowledge_qa` 的真实执行依赖阶段 2 的已发布 Chunk、阶段 3 的 `RetrievalStore` 和阶段 4
  的引用问答 Application 用例。
- Runtime API、会话和运行持久化应复用阶段 4 已落地的 Conversation、AgentRun、Evidence
  及 SSE/取消协议；不得由阶段 5 重复建立平行模型。
- `summarize_document`、`compare_sources` 和 `create_review_cards` 在 `knowledge_qa` 闭环稳定
  前只允许完成契约和确定性 fixture，不计为可用 Skill。
- 阶段 0 数据门禁关闭前，只能使用最小合成输入和 manifest 明确允许
  `repository_fixture` 的来源做工程验证，不能据此宣称真实语料上的 Skill 质量达标。

阶段 5 采用单 Agent、确定性有限状态机。不会引入多 Agent、自主开放式规划、第二套任务
队列或直接绑定具体 Agent 框架。

## 2. 启动条件与当前缺口

### 2.1 当前已具备的基础

- ADR-001 已固定模块化单体与独立 Worker 边界。
- ADR-003 已固定自有 Agent Runtime Port、单 Agent 有限状态机、工具白名单、预算、检查点
  和结构化审计方向。
- ADR-004 已固定本地优先和外部模型显式授权边界。
- ADR-005 已固定 Space、Document、DocumentVersion、Chunk、发布和删除语义。
- ADR-009 已固定 Redis + Dramatiq 只负责投递，PostgreSQL 负责持久状态和幂等事实。
- `ModelGateway` 已提供 `fast_chat`、`embedding_zh` 能力别名、确定性 fake、稳定错误码和
  使用量字段。
- PostgreSQL、Redis、API、Worker、Web、OpenTelemetry、Compose 和 CI 工程基线已验收。

这些基础足以支持纯契约、状态机、Registry 和测试替身开发，但不等同于真实 Skill 业务
已经可用。

### 2.2 前序阶段依赖

| 前序能力 | 阶段 5 使用方式 | 未满足时允许的工作 | 未满足时禁止的声明 |
| --- | --- | --- | --- |
| 阶段 0 授权复核、标注复核和版本冻结 | Skill eval、真实语料验收和演示 | 合成输入、已批准 repository fixture、契约测试 | 真实语料质量达标、阶段退出 |
| 阶段 2 原子发布和增量摄入 | 只读取已发布 DocumentVersion 和 Chunk | 使用 fake Knowledge Port | 可读取或处理真实知识库 |
| 阶段 3 `RetrievalStore` 与评测基线 | 检索 Tool、证据候选和版本化 retrieval profile | fake RetrievalStore、固定 SearchResult fixture | `knowledge_qa` 检索有效 |
| 阶段 4 引用问答 Application 用例 | 回答、拒答、claim-evidence、引用解析和会话 | 固定 GroundedAnswer fixture | 带引用问答可用 |
| 阶段 4 SSE、取消和错误协议 | Runtime API 的流式事件和取消映射 | 仅测试内部事件模型 | UI/API 端到端可用 |

阶段 5 不直接读取检索表、不绕过发布边界拼接上下文，也不在 Skill 内复制阶段 3/4 的
业务逻辑。

### 2.3 已固定的架构约束

- `domain` 只包含纯类型和 Port，不依赖 Pydantic、FastAPI、SQLAlchemy、Dramatiq、
  LangGraph 或模型 SDK。
- `application` 编排 Runtime、Skill 和业务用例，不承载文件扫描、数据库或 Provider 实现。
- `agent_runtime` 实现有限状态机、预算、Tool/Skill Registry 和执行契约，不直接访问 ORM。
- `infrastructure` 实现 PostgreSQL 检查点、Skill 包文件读取、审计和队列 Adapter。
- Tool 必须通过明确注册的 handler 调用 Application/Domain Port，禁止执行任意 shell、SQL、
  URL 或从 manifest 动态导入未审核代码。
- 模型调用只通过 `ModelGateway` 能力别名；文档内容始终是不可信数据，不能改变系统指令、
  工具权限、预算或审批结果。
- 运行开始后固定 Skill 名称、版本和内容摘要；活动版本变化只影响新运行。
- 所有写入必须经过显式权限校验、用户确认和幂等 Application 用例。

### 2.4 待关闭的决策门禁

阶段 5 开始实现 Registry 和持久化前，必须新增并接受 ADR-006，至少固定以下内容：

1. Skill manifest 版本、必填字段、版本标识和兼容性规则。
2. Skill 包内容摘要、不可变版本、活动版本切换和旧版本保留规则。
3. 受信目录、路径规范化、符号链接边界和生产环境代码加载策略。
4. Tool 权限分类、写入确认、外部网络/模型能力和敏感操作策略。
5. 运行版本固定、恢复时完整性校验和已引用版本的删除限制。
6. Registry 失败、兼容性失败、摘要不一致和回滚失败的稳定错误语义。

如果阶段 4 尚未接受 ADR-007，阶段 5 可以定义内部 Runtime 事件，但 Runtime API 的 SSE、
取消、恢复和后台任务协议必须等待 ADR-007 关闭，或与阶段 4 共同完成该 ADR。ADR-008 的
多 Agent/服务拆分触发条件当前未满足，本阶段不新增相关实现。

## 3. 范围

### 3.1 本阶段交付

1. Agent Runtime 纯领域状态、Port、有限状态机和确定性执行器。
2. Tool manifest、Tool Registry、输入输出 schema 校验、权限和审计契约。
3. Skill manifest、Skill Registry、内容摘要、兼容性校验、版本固定和活动版本切换。
4. 运行预算、超时、取消、检查点、恢复、错误分类和审计记录。
5. AgentRun/Checkpoint 所需的最小持久化实现及新的 Alembic revision；优先复用阶段 4
   已有 Conversation、AgentRun 和 Evidence 模型。
6. `knowledge_qa` Skill：封装阶段 4 已验证的问答 Application 用例，不复制检索或引用逻辑。
7. Runtime API、Skill 列表/版本查询、运行创建/查询/取消/恢复/审批接口。
8. Web 中的 Skill 调用入口、真实运行状态、失败恢复和写入确认交互。
9. `summarize_document`、`compare_sources`、`create_review_cards` 三个工作流型 Skill。
10. 受信目录热加载、兼容性检查、运行版本固定和活动版本回滚。
11. Skill 开发模板、契约测试、集成测试、安全测试和端到端测试。

### 3.2 明确不做

- 不实现多 Agent、Supervisor、专家 Agent、Agent 间消息协议或并行自治规划。
- 不引入 LangGraph 作为业务代码依赖；只有自有 Runtime Port 无法满足已测量需求时才重新
  评估 Adapter。
- 不引入第二个任务队列、向量库、工作流引擎或前端状态库。
- 不让模型选择未列入当前 Skill 白名单的 Tool，也不让模型扩大权限或预算。
- 不扫描仓库、用户目录或任意路径发现 Skill；只扫描配置的受信根目录。
- 不执行 manifest 指向的任意 Python 模块、shell、SQL 或远程 URL。
- 不在 Skill 中直接访问 ORM、私有表、原始 Blob 或其他 Space 内容。
- 不把 prompt 文本等同于 Skill；Skill 必须包含 manifest、workflow、schema、prompt、eval 和
  版本信息。
- 不把 `create_review_cards` 的预览结果静默写入知识库；缺少用户确认或幂等写入用例时只
  返回预览，并明确标记写入未执行。
- 不在阶段 0 门禁关闭前使用私有语料、真实 Provider 响应或 Embedding 产物完成质量验收。

## 4. 目标工程形态

阶段 5 完成后的建议最小结构如下；目录只在对应能力实际实现时创建：

```text
packages/
  domain/
    agent_runs.py            AgentRun、预算、权限、检查点和状态纯类型
    agent_runtime.py         Runtime、Registry、Checkpoint 等 Port
  application/
    agent_runs/              创建、执行、取消、恢复和审批用例
    skills/                  Skill 查询、激活、回滚和调用用例
  agent_runtime/
    state_machine.py         有限状态迁移和终止规则
    budgets.py               步骤、Tool、Token 和时间预算
    tools.py                 Tool 定义、Registry 和调用校验
    skills.py                Skill manifest、Registry 和版本固定
    executor.py              确定性 workflow 执行器
    errors.py                稳定 Runtime/Tool/Skill 错误
  infrastructure/
    agent_runs.py            PostgreSQL AgentRun/Checkpoint Adapter
    skill_loader.py          受信目录读取、摘要和路径边界校验
    runtime_audit.py         脱敏审计 Adapter
apps/
  api/
    routers/
      runs.py                Runtime API
      skills.py              Skill 查询、版本和激活 API
  worker/
    agent_tasks.py           仅在运行需要后台恢复时提供 Dramatiq actor
  web/
    src/                     Skill 选择、运行状态、审批和恢复交互
skills/
  knowledge_qa/
  summarize_document/
  compare_sources/
  create_review_cards/
tests/
  unit/                      状态机、预算、manifest、schema 和权限
  contract/                  Runtime、Tool、Skill 和 Application Port
  integration/               PostgreSQL、Registry、队列和恢复
  e2e/                       UI/API/测试共用 Skill 调用链路
migrations/
  versions/                  新增运行/检查点字段或表，不修改既有迁移
```

依赖方向固定为：

```text
Web/API/Test
    -> Application Skill/Run Use Cases
        -> Agent Runtime Port
            -> Skill Registry + Tool Registry
                -> Knowledge QA / Retrieval / Citation Application Ports
                -> ModelGateway
        -> AgentRun / Checkpoint Repositories
```

Skill workflow 不直接依赖 FastAPI、SQLAlchemy、Redis、Dramatiq 或具体 Provider SDK。

## 5. 分步实施

### 步骤 0：关闭 ADR-006 与跨阶段契约

- 新增 ADR-006，固定 manifest、版本、兼容性、信任、权限、摘要、固定和回滚语义。
- 复核阶段 4 的 GroundedAnswer、Citation、Conversation、AgentRun、Evidence、SSE 和取消
  契约，列出阶段 5 可直接复用的接口。
- 明确 `knowledge_qa` 只封装阶段 4 Application 用例，不重新实现 query rewrite、检索、
  上下文构建、生成、拒答或引用校验。
- 明确 Skill 包是数据还是可执行代码；首期优先声明式 workflow 或仓库内显式注册的 handler。
- 固定稳定错误码命名、版本升级兼容规则和 API `event_version` 处理方式。
- 若需要新增或改变核心实体，先记录数据模型和迁移影响，不修改既有迁移伪造历史。

**完成标准**：ADR-006 被接受；跨阶段接口有唯一权威来源；信任、版本、权限、恢复和失败
语义不存在需要实现者自行猜测的空白。

### 步骤 1：Agent Runtime 领域契约

- 定义 `RunStatus`、`RunStep`、`RunBudget`、`BudgetUsage`、`ToolPermission`、
  `ToolCallRecord`、`RunCheckpoint`、`RunError` 等纯类型。
- 运行状态至少覆盖创建、规划、检索、执行、验证、等待批准、完成、失败、取消和超时。
- 用显式状态迁移表限制合法事件；终态不可被普通事件重新打开。
- 将 `status` 与 `current_step` 分离，避免用一个字段同时表达业务阶段和终止状态。
- 预算至少覆盖最大步骤、最大 Tool 调用、Token 上限和总超时；使用量只能单调增加。
- 定义 `AgentRuntime`、`CheckpointStore`、`ToolRegistry`、`SkillRegistry` 和审批 Port。
- 运行上下文必须包含 `run_id`、`space_id`、Skill 固定信息、trace 信息和调用者身份；
  Space 隔离不能只依赖前端参数。

建议状态主路径：

```text
CREATED -> PLANNING -> RETRIEVING -> EXECUTING -> VERIFYING -> COMPLETED
                                      |
                                      v
                               WAITING_APPROVAL
                                      |
                                      v
                                  EXECUTING

active -> CANCEL_REQUESTED -> CANCELLED
active -> FAILED
active -> TIMED_OUT
```

恢复从最近一个已验证检查点继续，不通过伪造状态跳转绕过权限、预算或版本校验。

**完成标准**：纯领域包不依赖框架；非法迁移、终态保护、预算单调性、取消、超时和恢复前置
校验均有单元测试。

### 步骤 2：Tool 契约与 Registry

- 定义 Tool 唯一名称、语义版本、输入/输出 JSON Schema、权限、超时、重试、幂等声明和
  审计元数据。
- 注册时校验名称冲突、版本冲突、schema 合法性、权限集合、handler 白名单和能力依赖。
- 调用前校验 Skill 是否声明所需 Tool、运行是否具有权限、输入是否满足 schema、预算是否
  足够以及 Space/资源是否归属当前上下文。
- 调用后校验输出 schema，记录耗时、稳定错误码、重试次数和脱敏摘要，不记录完整正文。
- 将阶段 3/4 能力通过 Application Port 暴露为只读 Tool；Tool 不直接访问检索表或 ORM。
- 写 Tool 必须接收幂等键，并在执行前生成持久化审批请求；模型输出不能视为用户确认。
- Tool handler 由应用启动代码显式注册；manifest 只引用已注册名称和版本。

**完成标准**：未知 Tool、版本不匹配、非法 schema、越权调用、预算不足、跨 Space 访问和
未确认写入都在副作用发生前被拒绝；Tool 契约测试可使用确定性 fake 独立运行。

### 步骤 3：Skill manifest 与 Registry

- 定义版本化 `skill.yaml` schema，至少包含：
  `manifest_version`、`name`、`version`、`description`、`input_schema`、`output_schema`、
  `required_tools`、`required_capabilities`、`permissions`、`budgets`、`entrypoint`、
  `compatibility`、`prompts` 和 `evals`。
- JSON Schema 引用只允许解析到当前 Skill 包内的文件，禁止远程引用和路径逃逸。
- 规范化 Skill 包内容后计算摘要；注册键至少能稳定区分名称、版本和内容摘要。
- 同名同版本不同摘要必须拒绝，不能静默覆盖；同摘要重复注册必须幂等。
- 分离“已安装不可变版本”和“活动版本”；活动版本只是新运行的默认选择。
- 运行创建时固定 manifest、workflow、prompt、schema 和内容摘要；恢复时重新校验固定版本。
- Registry 只读取 ADR-006 指定的受信根目录，规范化路径并拒绝符号链接或 junction 逃逸。
- 提供最小 Skill 开发模板，包含 manifest、workflow、prompts、schemas、evals 和 README。

**完成标准**：manifest 校验、包摘要、重复注册、版本冲突、路径边界、兼容性和版本固定均有
单元/契约测试；Registry 不执行未审核代码。

### 步骤 4：有限状态执行器、预算与审计

- 实现确定性 workflow 执行器；节点和转移来自受支持的声明式定义或仓库内注册的 workflow。
- 每次节点执行前检查固定 Skill、当前状态、权限、剩余预算、取消请求和总超时。
- Tool 和 ModelGateway 使用量写入统一 `BudgetUsage`；超限产生稳定终态而不是继续执行。
- 明确错误分类：输入/manifest/schema/权限错误不可重试；Provider 超时、队列或瞬时依赖错误
  按有界策略重试；证据不足是业务拒答，不映射为基础设施失败。
- 每个状态迁移、Tool 调用、审批、恢复和终止事件携带 `run_id`、`trace_id`、
  `event_version`、Skill 固定信息和脱敏字段。
- 运行日志不记录完整文档、完整 prompt、模型原始响应、密钥或凭据。
- 使用 fake Tool、fake Skill 和 FakeModelGateway 覆盖成功、拒答、超时、限流、非法响应、
  预算耗尽和取消路径。

**完成标准**：相同输入、相同固定版本和相同 fake 场景产生相同状态序列和结构化结果；
任何失败都落入明确终态并保留安全、可定位的错误信息。

### 步骤 5：AgentRun、检查点与持久化恢复

- 首先检查阶段 4 已有 Conversation、AgentRun 和 Evidence 模型，复用其身份和归属语义。
- 仅补充阶段 5 必需且尚不存在的字段或实体，例如 Skill 固定摘要、预算快照、当前步骤、
  检查点序号、审批状态、租约、心跳和安全错误摘要。
- 使用新的 Alembic revision 创建或扩展表；禁止修改阶段 2/4 已执行迁移。
- 检查点必须包含恢复所需的最小结构化状态、schema 版本、固定 Skill 摘要、预算使用量和
  下一安全步骤；不把不可序列化对象或 Provider 客户端写入数据库。
- 检查点写入与 AgentRun 状态迁移保持明确事务边界；失败时不能出现状态已前进但恢复点缺失。
- 后台投递遵守 ADR-009：PostgreSQL 为事实源，Redis/Dramatiq 只传递 ID 和控制元数据。
- 恢复时校验调用者、Space、Skill 摘要、checkpoint schema、运行终态和租约，避免重复执行
  已完成副作用。
- 保留旧检查点的受控清理策略；被运行引用的 Skill 版本和审计信息不能被活动版本切换删除。

**完成标准**：API/Worker 重启、重复投递和租约过期后可从最近安全检查点恢复；已完成 Tool
副作用不重复；跨 Space、摘要不一致和损坏检查点被拒绝；迁移可在空库和既有数据上升级。

### 步骤 6：封装 `knowledge_qa` Skill

本步骤必须等待阶段 2 至阶段 4 的相关退出条件和接口完成。

- 定义 `knowledge_qa` 输入 schema：调用者/Space 上下文由服务端提供，业务输入包含问题、
  可选会话和阶段 4 已支持的过滤条件，不接受客户端伪造证据或 Skill 版本固定字段。
- 输出复用阶段 4 的结构化 GroundedAnswer：回答或拒答、claims、citations、evidence、
  版本和运行摘要，不新增不兼容的平行回答格式。
- workflow 调用阶段 4 Application 用例；阶段 3 检索、引用解析和 ModelGateway 仍由原业务
  边界负责。
- 固定 Skill、prompt、模型能力别名、retrieval profile、DocumentVersion 和 Evidence 版本。
- 将证据不足映射为正常的拒答结果，将 Provider/检索/数据库/队列故障映射为不同错误类型。
- 保持恶意文档为数据：检索到的 prompt injection 文本不能调用 Tool、改变系统 prompt、
  取消引用要求或扩大权限。
- 使用阶段 0 已批准 fixture 和 `knowledge-qa-v0` 数据集建立 Skill 契约；holdout 不参与调参。

**完成标准**：Web、API 和测试通过同一 Application 服务调用同一个固定 Skill；回答引用可
解析到正确文档版本和位置；拒答、跨 Space、已撤下来源和恶意文档用例通过。

### 步骤 7：Runtime API 与 Web 调用入口

- 在 `/api/v1/skills` 下提供 Skill/版本查询和受控激活/回滚接口。
- 在 `/api/v1/runs` 下提供运行创建、查询、取消、恢复和审批接口；具体方法和响应码遵守
  已接受的 API/后台任务协议。
- 长运行返回稳定 `run_id`；流式状态复用阶段 4 的 SSE 事件协议，不创建第二套事件格式。
- API 不接受任意 Skill 路径、entrypoint、Tool 名称、系统 prompt、权限或服务端预算覆盖。
- 每个请求解析调用者和 Space，并在 Application 层再次验证 Skill、Conversation、Run、
  Document 和 Evidence 归属。
- Web 展示可用 Skill、固定版本、运行状态、失败原因、取消/恢复和写入确认；不得使用伪造
  结果或无限 loading 掩盖终态。
- 测试调用通过 Application 服务，不为测试建立绕过权限、版本固定或检查点的特殊入口。
- 新增公开 API 后重新生成 `docs/openapi.json` 并执行一致性检查。

**完成标准**：同一 `knowledge_qa` Skill 可从 Web、HTTP API 和测试调用；三种入口产生一致
的运行、版本、预算和错误语义；取消、恢复和审批状态在刷新或进程重启后仍可查询。

### 步骤 8：知识整理 Skill 与写入确认

本步骤只在 `knowledge_qa` 的真实链路和引用完整性已经稳定后开始。

- `summarize_document`：输入固定到当前 Space 内一个已发布 DocumentVersion；输出摘要中的
  关键结论必须带引用，版本更新不能悄悄改变运行中使用的来源。
- `compare_sources`：只比较当前 Space 内显式选择或检索命中的来源；输出区分一致、冲突和
  证据不足，比较项分别绑定引用。
- `create_review_cards`：先生成带来源的预览；只有用户确认后才调用幂等写入 Tool，并记录
  审批人、输入摘要、输出目标和结果。
- 三个 Skill 复用统一 Runtime、Registry、预算、权限、检查点和审计，不创建专用执行器。
- 若阶段 2/4 尚未提供“保存派生知识条目”的稳定 Application 用例，
  `create_review_cards` 保持只读预览并标记写入能力阻塞，不直接写表或文件。
- 每个 Skill 都包含独立 manifest、schema、prompt、eval、README 和错误/拒绝用例。

**完成标准**：三个 Skill 的只读结果均可追溯到固定来源；跨 Space 和撤下来源不被使用；
任何写入均要求持久化确认且重试幂等，拒绝后无副作用。

### 步骤 9：热加载、版本回滚与恢复兼容

- 热加载只扫描 ADR-006 指定的受信目录，并在完整校验通过后注册新不可变版本。
- 新版本注册不自动终止或迁移正在运行的 AgentRun；现有运行继续使用固定旧版本和摘要。
- 活动版本切换使用原子更新；失败时保持原活动版本。
- 回滚只切换新运行默认版本，不重写历史运行和输出。
- 恢复旧运行前校验旧版本仍可用、摘要一致、checkpoint schema 可读取且所需 Tool 版本兼容。
- 不兼容升级必须注册新版本并明确兼容范围；禁止覆盖同名同版本文件模拟升级。
- 删除旧版本前检查 AgentRun、Checkpoint 和审计引用；有引用时拒绝删除或按已接受保留策略
  归档。
- 覆盖加载中断、部分文件、摘要变化、并发加载、并发运行、回滚失败和旧版本恢复测试。

**完成标准**：运行中升级不改变固定版本；新运行按活动版本执行；回滚后新运行使用目标旧
版本；任意加载或回滚失败不破坏已注册版本和运行恢复能力。

### 步骤 10：测试、文档与阶段验收

- 运行后端规范检查、单元/契约/集成/安全测试以及前端 lint、typecheck、test 和 build。
- 在隔离 PostgreSQL/Redis 中验证迁移、检查点、重复投递、Worker 重启、租约恢复和幂等。
- 运行 Web/API/测试三入口端到端旅程，验证相同 Skill、相同版本和相同 Application 用例。
- 验证 manifest 路径逃逸、符号链接逃逸、同版本摘要冲突、未注册 entrypoint、schema 攻击、
  跨 Space、prompt injection、危险 Tool 和日志泄漏。
- 对新增 API 重新生成 OpenAPI；检查 Compose、stage-1 acceptance 和 troubleshooting 是否需要
  同步。
- 文档覆盖 Skill 开发、安装/注册、调用、权限、审批、恢复、发布、回滚和已知限制。
- 检查 fixture、日志、span、错误响应、报告和 Git 变更中无密钥、私有正文、完整 prompt、
  Provider 原始响应或 Embedding 产物。

**完成标准**：本计划的阶段退出条件全部通过；验证命令和结果有可复现记录；文档与运行时
行为一致。

## 6. 执行依赖与状态

| 步骤 | 必须先满足 | 当前状态 |
| --- | --- | --- |
| 0. ADR-006 与跨阶段契约 | ADR-001～005、ADR-009；阶段 4 接口草案可核对 | 可先行，待办 |
| 1. Runtime 领域契约 | 步骤 0 的状态、预算、权限和版本语义确定 | 待办 |
| 2. Tool Registry | 步骤 1；阶段 3/4 Port 可先用 fake | 待办，可先行实现通用部分 |
| 3. Skill Registry | 步骤 0/1；受信目录和摘要规则确定 | 待办，可先行 |
| 4. 执行器、预算与审计 | 步骤 1～3；FakeModelGateway 已可用 | 待办，可先行 |
| 5. AgentRun 与检查点持久化 | 步骤 1/4；阶段 4 数据模型交接；迁移协调 | 等待阶段 4 模型，接口可先行 |
| 6. `knowledge_qa` | 阶段 2 摄入、阶段 3 检索、阶段 4 引用问答退出条件 | 等待前序阶段 |
| 7. Runtime API 与 Web | 步骤 5/6；ADR-007 或等价已接受协议 | 等待前序阶段 |
| 8. 三个知识整理 Skill | `knowledge_qa` 真实链路稳定；写入 Application 用例可用 | 等待步骤 6/7 |
| 9. 热加载与回滚 | 步骤 3/5；不可变版本和恢复语义已验证 | 待办 |
| 10. 测试与验收 | 步骤 0～9；阶段 0 数据门禁关闭 | 等待全部交付 |

允许通用 Runtime、Registry 和 fake 契约与阶段 2～4 并行开发，但合并时必须以阶段 3/4 的
正式 Port 和 schema 为准；不得要求前序模块反向依赖 Agent Runtime 私有类型。

## 7. 测试与验证矩阵

| 层级 | 必测内容 | 外部依赖 |
| --- | --- | --- |
| 单元 | 状态迁移、终态、预算、超时、版本比较、内容摘要、manifest/schema、路径边界、权限 | 无 |
| 属性/参数化 | 非法状态事件、预算不回退、同版本摘要冲突、重复注册幂等、随机路径不能逃逸受信根 | 无 |
| 契约 | AgentRuntime、Tool、Skill、CheckpointStore、审批 Port、阶段 3/4 Application Port | 使用 fake |
| 集成 | PostgreSQL AgentRun/Checkpoint、迁移、事务边界、Registry 文件读取、重复投递、租约恢复 | 隔离 PostgreSQL/Redis |
| API | Skill 查询、运行创建/查询/取消/恢复/审批、错误映射、OpenAPI | 本地 API + fake |
| 前端 | Skill 选择、版本展示、状态终止、错误、取消、恢复、审批、窄屏和键盘焦点 | API mock 或本地 API |
| E2E | Web/API/测试调用同一 Skill；版本固定；检查点恢复；引用查看；写入确认 | 隔离完整本地栈 |
| 安全 | 跨 Space、路径/链接逃逸、恶意 manifest、prompt injection、危险 Tool、日志和 span 泄漏 | 无或隔离本地栈 |
| Eval | 回答、拒答、引用、冲突来源、恶意文档、版本记录和重复运行稳定性 | 阶段 0 已批准语料 |

至少覆盖以下失败模式：

- manifest 缺失字段、未知字段策略冲突、schema 无效、远程引用和包内路径逃逸。
- 同名同版本不同摘要、活动版本不存在、旧版本缺失、加载中断和回滚失败。
- 未注册 Tool、Tool 版本不兼容、输入/输出 schema 不匹配、超时、不可重试失败和重复副作用。
- 权限不足、用户拒绝审批、审批过期、恢复时审批上下文不一致。
- 步骤、Tool、Token 或时间预算耗尽；模型超时、限流、不可用和非法响应。
- API/Worker 重启、重复消息、租约过期、检查点损坏、状态与检查点事务部分失败。
- 跨 Space 运行/检索/引用、撤下来源、恶意文档指令、危险写入和外部数据边界违规。

## 8. 阶段退出条件

以下条件必须全部满足：

1. ADR-006 已接受；manifest、版本、兼容性、信任、权限、固定、恢复和回滚有唯一权威语义。
2. `domain`、`application`、`agent_runtime` 和 `infrastructure` 依赖方向符合架构约束，无框架
   类型泄漏到领域层。
3. Tool/Skill manifest、schema、Registry 和内容摘要校验通过；生产路径不能执行未审核代码。
4. 单 Agent 有限状态机、预算、超时、取消、审批、审计和错误分类通过单元/契约测试。
5. AgentRun 和 Checkpoint 使用 PostgreSQL 持久化；进程/Worker 重启和重复投递可从最近安全
   检查点恢复，且不重复已完成副作用。
6. `knowledge_qa` 复用阶段 4 Application 用例，可从 Web、HTTP API 和测试调用同一个固定
   Skill，三种入口的版本、权限、预算和错误语义一致。
7. `knowledge_qa` 的回答、拒答、引用、跨 Space、撤下来源、冲突来源和恶意文档回归通过；
   每次运行记录 corpus、dataset、parser、chunker、embedding、retrieval、prompt、model 和
   Skill 版本。
8. `summarize_document`、`compare_sources` 和 `create_review_cards` 均通过 Skill 契约；只读结果
   带可定位引用，写入必须经过持久化确认且重试幂等。
9. 新 Skill 版本只影响新运行；运行中升级保持旧版本固定；活动版本可回滚；旧检查点在兼容
   范围内可恢复。
10. 阶段 0 数据门禁已关闭，所有 Eval 输入来自 manifest 允许列表且 SHA-256 匹配；私有内容
    未进入 CI、日志、报告、截图或外部 Provider。
11. 后端格式化、lint、类型检查、测试，前端 lint/typecheck/test/build，隔离依赖集成测试和
    端到端测试全部通过。
12. 新增迁移在空库、既有数据升级和回滚验证中通过；`docs/openapi.json` 与运行时 schema 一致。
13. Runtime、Skill 开发、权限审批、恢复、发布、回滚、故障排查和已知限制文档与实现一致。

## 9. 主要风险与应对

| 风险 | 早期信号 | 应对 |
| --- | --- | --- |
| 阶段 5 复制阶段 3/4 业务 | Skill 内出现 SQL、检索融合、引用拼装或独立问答 schema | 只依赖正式 Application Port；用契约测试约束输入输出 |
| Runtime 过度通用 | 出现开放规划、动态图、插件生命周期或未被 P0 使用的抽象 | 保持单 Agent、有限节点和显式状态；未触发 ADR 条件不扩展 |
| Skill 热加载执行任意代码 | manifest 可指定任意模块、路径或 URL | 受信根、路径规范化、摘要、显式 handler 白名单；生产不加载未审核代码 |
| 版本固定不完整 | 只记录 `version`，未记录包摘要、prompt 或 retrieval 配置 | AgentRun 固定完整版本集合和摘要；恢复前重新校验 |
| 检查点重复副作用 | Worker 重启后再次执行已成功写 Tool | Tool 幂等键、步骤提交边界、调用记录和恢复测试 |
| 审批只存在内存 | 刷新或重启后写入自动继续或无法判断 | 审批请求持久化；绑定运行、步骤、输入摘要、用户和过期时间 |
| 权限由模型决定 | prompt 或文档可声明“已获批准” | 权限来自服务端 Skill/用户上下文；模型和文档不能修改 |
| 跨 Space 泄漏 | Tool 只按文档 ID 查询或信任客户端 `space_id` | Application 层按调用者和 Space 校验；安全回归要求违规为 0 |
| 状态与检查点不一致 | 状态已前进但无可恢复快照 | 明确事务顺序；故障注入覆盖提交前后中断 |
| Registry 与运行并发冲突 | 活动版本切换改变正在运行实例 | 创建运行时固定版本和摘要；活动指针只供新运行解析 |
| 数据门禁被工程进度替代 | 使用私有语料或 synthetic 指标宣称阶段完成 | 报告记录样本分类；阶段退出必须引用已冻结批准语料 |
| 日志泄漏正文或 prompt | audit/event 保存 Tool 完整输入输出 | 默认只记录 ID、摘要、计数、耗时和安全错误；泄漏测试 |

## 10. 前序阶段交接接口

阶段 5 接入前，阶段 2～4 至少应提供以下稳定接口或等价 Application Port：

- 阶段 2：按 Space 解析已发布 DocumentVersion、Chunk 定位信息、撤下/删除状态和版本身份。
- 阶段 3：`RetrievalStore.search` 或等价接口，返回带分数、排名、版本、Space 和定位信息的
  SearchResult/Evidence 候选；不暴露私有表。
- 阶段 4：Grounded QA 用例，返回回答或拒答、claims、citations、evidence、版本和稳定错误。
- 阶段 4：Citation Resolver，能区分当前、历史、已更新和已删除来源。
- 阶段 4：Conversation/AgentRun/Evidence 所有权、持久化和 API/SSE/取消协议。

若正式接口与本计划假设不同，阶段 5 通过 Adapter 或更新本计划对齐；不能让 Skill 绕过
上游接口直接读取内部表。

## 11. 下一阶段接口

阶段 5 结束时应为阶段 6 的 Skill 质量门禁提供：

- 不可变 Skill 版本、manifest、prompt、schema、workflow 和内容摘要。
- 可复现的 AgentRun，记录模型、检索、证据、预算、Tool 调用、终止原因和版本集合。
- 统一的 Skill 测试调用入口和机器可读输出。
- 运行与 EvalCase/EvalRun 的稳定关联，不把 holdout 数据用于调参。
- 活动版本切换、回滚和旧版本恢复的审计记录。
- 按解析、检索、上下文、生成、引用、权限、预算和基础设施分类的安全失败信息。

阶段 6 可以据此比较 prompt、模型、检索配置和 Skill 版本，并设置质量、延迟和成本门禁，
而不需要解析非结构化运行日志或重建缺失版本信息。
