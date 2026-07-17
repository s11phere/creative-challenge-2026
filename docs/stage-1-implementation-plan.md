# 阶段 1 实施计划：工程骨架与可观测基线

> 文档状态：Draft v1
>
> 适用范围：`docs/project-implementation-plan.md` 中的阶段 1
>
> 估算口径：人日，1 人日按 6 小时有效工程时间计算
>
> 计划基线日期：2026-07-17

## 1. 结论摘要

阶段 1 的目标不是实现知识摄入或问答业务，而是建立后续阶段可以持续交付的最小工程底座：API、Worker、Web、PostgreSQL/pgvector、Redis、数据库迁移、模型网关、结构化日志、追踪、测试替身、CI 和本地启动文档。

当前仓库只有项目说明和实施计划，尚无正式构建配置、锁文件、应用代码、Compose 或 CI。因此阶段 1 属于从零初始化，而不是在现有脚手架上补齐配置。

建议按 **31-42 人日** 排期，其中基础工作量为 27-36 人日，另含约 15% 的集成与环境差异缓冲。预计日历周期为：

| 可投入人员 | 预计周期 | 说明 |
| --- | ---: | --- |
| 1 人 | 7-9 周 | 基本无法并行，且环境与 CI 问题会直接落在关键路径上 |
| 2 人 | 3.5-4.5 周 | 后端/基础设施与前端/质量两线并行 |
| 3 人 | 2.5-3 周 | 推荐配置，可形成后端、前端、平台质量三条工作流 |
| 4 人 | 2-2.5 周 | 接近总计划中的 1-2 周目标，但需要明确接口负责人并控制集成成本 |

总实施计划中的“第 1-2 周”只有在 4 人接近全时投入、开发机已有 Docker 环境且依赖下载稳定时才较可信。若是 2-3 人团队，应以上表重新校准里程碑，而不是压缩测试、迁移或可观测性工作。

## 2. 启动条件与当前缺口

### 2.1 阶段 0 门禁

阶段 0 当前尚未正式退出：

- `../cases/evals/corpus/v0/manifest.yaml` 状态仍为 `draft_pending_license_review`。
- 评测集结构和 Evidence 定位已机械校验，但许可证和人工标注复核尚未完成。
- 私有、受限或授权未确定的语料不得进入仓库、CI、公开截图或外部模型调用。

因此采用“条件式启动”：阶段 1 的无语料工程工作可以并行开始，但项目阶段状态仍保持在阶段 0，直到授权复核、人工标注复核和版本冻结完成。阶段 1 的测试只使用代码内新建的最小确定性数据或 manifest 明确允许 `repository_fixture` 的公开 fixture，并在复制前校验哈希和许可证义务。

阶段 0 收尾建议单独安排 2-5 个人工复核人日，不计入本阶段工程估算。它不是 API、Web 和 CI 初始化的阻塞项，但会阻塞真实语料集成、外部 Provider 验证和阶段 1 的正式退出声明。

### 2.2 已固定的架构约束

阶段 1 直接遵守以下已接受决策，不重复选型：

- ADR-001：模块化单体，加独立运行的 Worker。
- ADR-002：PostgreSQL 16+、pgvector 和 PostgreSQL FTS。
- ADR-003：自有 Agent Runtime Port，LangGraph Adapter 延后。
- ADR-004：本地优先，外部模型显式选择，CI 只使用确定性 fake。

ADR-001 至 ADR-004 已迁入本仓库 `docs/adr/`，该目录是后续 ADR 的唯一权威位置。`../cases/docs/adr/` 中的原文件仅作为阶段 0 历史来源，不再继续维护。

### 2.3 阶段 1 已关闭的决策

以下决策已于 2026-07-17 固定：

| 决策 | 固定基线 | 唯一来源 |
| --- | --- | --- |
| Python 包与锁定工具 | Python 3.12 + `uv`，后端使用单个 workspace/锁文件 | `docs/development-environment.md`；步骤 1 写入 `.python-version` 和 `uv.lock` |
| 前端包管理 | Node 24 LTS + Corepack 管理的 `pnpm` 10.20.0 | `docs/development-environment.md`；步骤 1 写入 `packageManager` 和锁文件 |
| 后台队列 | Redis + Dramatiq；PostgreSQL 保存持久任务状态，Redis 只负责投递 | ADR-009 |
| 模型实现 | 确定性 fake + 默认关闭外发的 OpenAI-compatible HTTP Adapter，优先连接本地端点 | ADR-004 与 `docs/development-environment.md` |
| 类型检查 | `mypy` 作为后端门禁，TypeScript `tsc --noEmit` 作为前端门禁 | CI 与本地使用相同配置 |
| 可观测输出 | OpenTelemetry trace + JSON 结构化日志 | 开发环境可接本地 Collector；正文采集默认关闭 |

若改用 Celery、npm、Poetry 或其他同类工具，需要先更新相应 ADR 或计划、工作量和命令设计；不要同时引入功能重叠的工具。

## 3. 范围

### 3.1 本阶段交付

1. 可启动的 FastAPI 应用，公开接口从 `/api/v1` 开始。
2. 独立 Worker 进程，能连接 Redis、注册一个无正文的诊断任务并输出关联日志。
3. PostgreSQL 16+ 和 pgvector 的初始 Alembic 迁移及迁移验证。
4. React、TypeScript、Vite 和 TanStack Query 工作台外壳，能够展示 API 就绪状态。
5. 统一配置加载、环境变量校验、错误响应、`trace_id`、结构化日志和基础 trace。
6. `ModelGateway` 稳定接口、确定性 fake，以及一个 Chat/Embedding Provider Adapter。
7. 本地 Compose：PostgreSQL/pgvector、Redis、API、Worker、Web；可选 profile 启动 OpenTelemetry Collector。
8. CI：格式检查、lint、类型检查、单元测试、迁移验证、前端测试与构建。
9. OpenAPI 输出、健康检查、依赖就绪检查、贡献说明和故障排查说明。

### 3.2 明确不做

- 不创建 Space、Source、Document、DocumentVersion、Chunk 等阶段 2 业务表。
- 不实现 Parser、Chunker、Embedding 入库、索引发布或真实语料摄入。
- 不实现搜索、问答、SSE、会话、引用、Skill 或 Agent 状态机。
- 不加入 LangGraph、Reranker、第二个模型 Provider 或第二套任务队列。
- 不为建议目录创建空包；只有被健康检查、模型网关或 Worker 实际使用的模块才落盘。
- 不把私有语料、Provider 响应、完整 prompt、Embedding 或正文日志放入仓库和 CI。

## 4. 目标工程形态

阶段 1 完成后，实际存在的目录应由真实代码驱动，建议最小形态如下：

```text
apps/
  api/                 FastAPI 入口、HTTP schema、异常映射
  worker/              Worker 入口和诊断任务
  web/                 React 工作台外壳
packages/
  domain/              仅放本阶段真正使用的纯类型或 Port
  application/         健康/诊断用例编排
  model_gateway/       能力别名、Port、fake 和 Provider Adapter
  infrastructure/      配置、DB、队列、日志、追踪 Adapter
migrations/            Alembic 配置和初始迁移
deploy/                Compose、容器构建和 OTel 开发配置
tests/
  unit/
  integration/
  contract/
docs/
  adr/
```

依赖方向保持为：

```text
API / Worker -> Application -> Domain Ports
                              <- Infrastructure Adapters
                              <- ModelGateway Adapters
```

`domain` 不依赖 FastAPI、SQLAlchemy、Redis、Dramatiq、OpenTelemetry 或具体模型 SDK。传输层只做协议、校验和响应映射。

## 5. 分步实施

### 步骤 0：关闭启动决策

**工作量：1-2 人日**

- 统一 ADR 权威目录和编号规则，保留已接受 ADR-001 至 ADR-004 的历史。
- 形成队列选型 ADR，明确任务状态存数据库、队列只负责投递的边界。
- 固定 Python、Node、包管理器、本地支持版本和基础镜像标签；镜像首次落地时锁定 digest。
- 确认首个 Provider Adapter 是本地端点还是显式启用的外部端点，并记录数据边界。

**完成标准**：不存在会改变仓库结构、队列语义或数据外发边界的未决问题；所有工具版本都有唯一来源。

**状态**：已于 2026-07-17 完成。

### 步骤 1：初始化仓库与统一命令

**工作量：2-3 人日**

- 建立 Python 项目和锁文件，只创建首批实际使用的包。
- 初始化 Vite/React/TypeScript 应用和锁文件。
- 配置 Ruff、mypy、pytest、Oxlint、TypeScript 和前端测试框架。
- 添加 `.env.example`，只提供非敏感默认值和变量说明。
- 定义本地与 CI 使用的规范命令，避免维护两套入口。

建议最终命令契约：

```text
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest
uv run alembic upgrade head
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
docker compose -f deploy/compose.yaml up --build
```

**完成标准**：干净环境可以按文档安装依赖；所有命令在本地和 CI 中名称与参数一致。

### 步骤 2：API、配置与错误协议

**工作量：3-4 人日**

- 建立应用工厂和显式生命周期管理，禁止在模块导入时连接外部依赖。
- 使用 Pydantic Settings 加载配置，启动时校验必需变量。
- 实现稳定错误 schema：`code`、`message`、`trace_id` 和可选 `details`。
- 为未知异常返回通用错误，不暴露堆栈、连接串、文件正文或密钥。
- 实现 `/api/v1/health/live` 与 `/api/v1/health/ready`。
- OpenAPI 中包含错误响应和健康响应 schema。

健康语义：

- `live` 只表示进程事件循环可响应，不探测外部依赖。
- `ready` 有界并发检查 PostgreSQL 和 Redis；任何必需依赖不可用时返回非 2xx 和稳定机器码。
- 模型 Provider 默认不是 API 启动的硬依赖；其可用性单独报告，避免外部网络故障导致本地管理功能不可用。

**完成标准**：健康检查、配置失败和未知异常均有自动化测试；响应不泄露内部异常细节。

### 步骤 3：数据库迁移与 Worker 基线

**工作量：5-6 人日**

- 建立异步 SQLAlchemy Engine、会话工厂和事务边界辅助设施。
- 初始迁移只启用 `vector` 扩展和阶段 1 必需的基础设施表；不提前创建阶段 2 核心实体。
- 建立 Redis/队列连接和独立 Worker 入口。
- 实现一个只处理 ID、计数和时间的诊断任务，用于验证投递、重试和 trace 传播，不处理语料正文。
- 明确优雅停止、任务超时、重试上限和永久失败日志字段。
- 验证重复执行迁移和诊断任务不会产生重复副作用。

**完成标准**：空数据库可升级到 `head`；当前迁移可降级再升级；API 与 Worker 可独立启动；诊断任务能关联请求/任务 ID。

**状态**：已于 2026-07-17 完成。

实际交付：

- 新增异步 SQLAlchemy Engine、会话工厂、事务边界和有界连接检查；API readiness
  复用该数据库基础设施。
- Alembic 与应用统一从 `Settings` 构造数据库 URL；初始迁移仅维护
  `alembic_version` 并启用 `vector`，没有提前创建阶段 2 业务表。
- 新增 RedisBroker、独立 Dramatiq Worker 入口和无正文诊断任务。消息只包含
  `task_id`、`trace_id`、`event_version`、计数和请求时间。
- 诊断任务配置了任务超时、最多 3 次重试、优雅停止和永久失败回调；超过重试上限的
  消息进入 Dramatiq 死信队列，单个坏任务不会终止 Worker。
- 修正 Compose `develop.watch` 的同步目标，使 Compose 配置能够通过校验。

2026-07-17 验证记录：

- 隔离启动 PostgreSQL/pgvector 与 Redis，两个容器健康检查均通过；验证后使用独立
  Compose project 执行 `down -v`，未影响其他数据。
- 在空数据库完成 `upgrade head -> downgrade base -> upgrade head`，再次执行
  `upgrade head` 无重复副作用；`vector` 扩展随迁移正确创建和删除。
- 数据库中只有 `alembic_version`，确认未提前创建 Space、Document、Chunk 等业务表。
- 在 Worker 未启动时先向 Redis 投递诊断消息，Worker 启动后成功消费并记录开始、完成
  事件，证明 API 与 Worker 无需同时在线。
- 使用不支持的 `event_version=99` 验证异常路径：达到 3 次重试上限后执行永久失败
  回调、消息进入死信队列，Worker 保持运行。
- Dramatiq 未配置 Results middleware 的提示符合 ADR-009：Redis/Dramatiq 只负责
  投递，持久任务状态不存放在结果后端。
- 后端格式、lint、严格类型检查和 23 个单元测试通过；迁移正反向离线 SQL 与 Compose
  配置校验通过。

### 步骤 4：可观测性基线

**工作量：3-4 人日**

- 每个请求生成或验证 `trace_id`，写入响应头和错误体。
- 配置 JSON 结构化日志，统一时间、级别、服务、环境、trace、request 和 task 字段。
- 将 FastAPI、SQLAlchemy、Redis/Worker 和出站模型调用接入 OpenTelemetry。
- 定义日志脱敏过滤器，覆盖 Authorization、Cookie、API Key、数据库密码、prompt 和文档正文。
- 开发环境支持 OTLP 输出；Collector 不可用时应用明确降级，不影响业务启动。
- 禁止默认开启完整请求体、SQL 参数、模型输入输出和正文采样。

**完成标准**：一次 readiness 请求可以在 API 日志、数据库 span 和响应头中用同一 trace 定位；诊断任务可从入队日志关联到 Worker 日志。

**状态**：已于 2026-07-17 完成。

实际交付：

- API 中间件支持 W3C `traceparent`，并生成或校验 `X-Trace-ID`、`X-Request-ID`；
  正常响应、错误响应和统一错误体使用同一 trace。
- 新增进程级 JSON 日志配置和 ContextVar 关联上下文，统一输出时间、级别、服务、环境、
  trace、request、task、事件和有限的操作元数据。
- 新增集中脱敏过滤器，覆盖 Authorization、Cookie、API Key、密码、数据库 URL、prompt、
  请求体和文档正文；异常日志默认只保留错误类型，不输出异常消息或正文。
- 配置 OpenTelemetry SDK 与 OTLP/HTTP exporter；SQLAlchemy、Redis 和 HTTPX 使用官方
  instrumentation，readiness 额外提供 PostgreSQL 与 Redis 依赖 span。
- 诊断任务新增 producer/consumer span 和入队日志，消息中的 trace 在生产、消费、失败
  回调及结构化日志间保持一致。
- Collector 不可用时 exporter 异步降级，不阻塞 API/Worker 启动；未配置 endpoint 时
  保留本地 tracing，不尝试外发。
- 锁定并核验 PostgreSQL/pgvector、Redis 和 OTel Collector 的有效镜像 digest，修复
  原摘要无法从 registry 解析的问题。
- ModelGateway 尚未实现；HTTPX 自动插桩已在进程启动时启用，Step 5 Provider Adapter
  将直接继承出站 trace，不在本步骤伪造模型调用。

2026-07-17 验证记录：

- 后端格式、lint、严格类型检查和 33 个单元测试通过；测试覆盖恶意/超长关联 ID、
  错误体与响应头一致性、日志 schema、敏感信息脱敏及数据库/Worker trace 延续。
- 使用不可达 OTLP endpoint 启动 smoke test，配置成功且进程正常退出，证明 Collector
  故障不阻塞启动。
- 隔离启动 PostgreSQL/pgvector、Redis 和 OTel Collector；一次真实 readiness 返回
  `200/ready`，响应头和 API JSON 日志使用 trace
  `1234567890abcdef1234567890abcdef`。
- Collector 在同一 trace 下收到 HTTP server、`postgresql.ready`、SQLAlchemy
  `connect/SELECT 1`、Redis `PING` 和 `redis.ping` span；未记录请求体或 SQL 参数。
- 通过真实 Redis 执行诊断任务，enqueue、started、completed 日志共享同一 trace/task，
  Collector 收到对应的 `diagnostic_task.enqueue` 与 `diagnostic_task.process` span。
- 验证后删除隔离容器、网络和数据卷，未影响其他 Compose project。

### 步骤 5：ModelGateway 与测试替身

**工作量：4-5 人日**

- 定义 Chat 与 Embedding 的输入、输出、错误分类和能力别名。
- 能力别名至少包含 `fast_chat` 和 `embedding_zh`，具体模型名只存在于部署配置。
- 实现确定性 fake，支持正常、超时、限流、结构错误和不可用场景。
- 实现一个 Provider Adapter，封装 SDK/HTTP 类型，不向 application 或 domain 泄漏。
- 加入超时、有限重试、trace、Token/耗时元数据和脱敏日志。
- 外部 Provider 默认关闭；未配置或策略禁止时返回“模型不可用/策略禁止”，不得伪装成无知识答案。

本阶段不实现 Reranker，也不把任何阶段 0 私有语料发送给 Provider。Provider 合同测试使用合成输入；真实调用只作为显式、本地运行的 smoke test，不进入默认 CI。

**完成标准**：fake 与 Provider Adapter 通过同一套契约测试；业务层只依赖 Port 和能力别名。

**状态**：已于 2026-07-17 完成。

实际交付：

- 在 `model_gateway` 包内定义 Provider-neutral Chat/Embedding 数据契约、`ModelGateway`
  Protocol、`fast_chat`/`embedding_zh` 能力别名、Token/耗时元数据和稳定错误分类。
- 实现确定性 fake；正常 Chat/Embedding 输出可重复，并可配置模拟 timeout、rate limit、
  invalid response 和 unavailable，不需要网络或密钥。
- 实现 OpenAI-compatible HTTP Adapter，使用 `httpx.AsyncClient` 调用
  `/chat/completions` 与 `/embeddings`，不向上层暴露 HTTP 或 Provider 类型。
- Adapter 对 timeout、传输失败、429 和 5xx 执行指数退避的有限重试；认证、策略、4xx
  和结构错误不重试。错误消息不包含 Provider 响应。
- Chat 与 Embedding 响应使用结构化解析；拒绝缺失字段、非法 Token、重复/缺失向量
  index、维度不一致以及 NaN/Infinity。
- 所有模型调用创建 OTel span，只记录能力别名、Provider 类型、耗时、Token 和重试次数；
  日志与 span 不记录输入、输出、API Key 或完整 Provider 响应。
- 默认 `MODEL_PROVIDER=fake`。OpenAI-compatible 必须显式选择并配置 endpoint 和部署模型名；
  本机/私网 endpoint 默认允许，公网 endpoint 必须额外设置 `MODEL_ALLOW_EXTERNAL=true`。
  带 URL 凭据、query、fragment 或非 HTTP(S) endpoint 始终拒绝。
- API readiness 增加非阻塞模型状态。模型禁用、配置缺失或策略拒绝会返回明确机器码，但
  不会使 PostgreSQL/Redis 已就绪的本地 API 降级。
- Reranker 未实现，仍属于后续阶段能力。

2026-07-17 验证记录：

- 后端格式、lint、严格类型检查和 59 个测试通过。
- fake 与 HTTP Adapter 运行同一套 Chat/Embedding 契约测试；Provider 使用合成
  `httpx.MockTransport`，没有真实模型或付费调用。
- 覆盖 429 成功重试、timeout 有限重试、401 不重试、畸形响应、非法向量、配置缺失、
  禁用状态、公网策略拒绝、API Key `SecretStr` 和模型状态非阻塞 readiness。
- 使用本地临时 HTTP stub 完成显式 smoke test：Chat/Embedding URL、Authorization、
  部署模型映射、Token 统计和向量维度均通过；进程结束后 stub 已停止。
- 检查异常、日志和 span，合成的模型输入、输出和 Provider 响应均未进入记录。

### 步骤 6：Web 工作台外壳

**工作量：3-4 人日**

- 建立工作台布局骨架，而不是营销页。
- 接入 TanStack Query，只调用健康/就绪接口。
- 展示 API、数据库、队列和模型的可用/不可用/检查中状态，颜色不是唯一状态信号。
- 提供明确的配置或依赖错误状态；不使用无限 loading。
- 完成键盘焦点、窄屏布局和错误重试的基础检查。
- 不用静态假数据伪装文档、会话、证据或摄入流程。

**完成标准**：桌面和移动宽度均无溢出或遮挡；API 不可用时有可恢复错误提示；前端构建与基础组件测试通过。

**状态**：已于 2026-07-18 完成。

实际交付：

- 将默认 Vite 欢迎页替换为本地知识工作台的系统状态界面，包含工作台导航、运行摘要、
  服务连接列表和请求关联信息；没有伪造文档、会话、证据或摄入数据。
- 使用 TanStack Query 并行读取 `/api/v1/health/live` 和 `/api/v1/health/ready`，严格校验
  响应结构，展示 API、PostgreSQL、Redis 和模型网关的检查中、可用或不可用状态。
- 健康请求使用 8 秒超时、30 秒自动刷新和显式手动重试；API 不可达、超时及非法响应均会
  退出 loading，并显示稳定错误码。
- 开发服务器将同源 `/api` 代理到本地 FastAPI；也可通过 `VITE_API_BASE_URL` 指向其他
  API 地址。
- 使用文本、图标和机器码共同表达状态；刷新和重试控件支持键盘焦点，并完成窄屏重排。

2026-07-18 验证记录：

- 前端 lint、TypeScript 类型检查、6 个 Vitest 组件测试和生产构建通过。
- 自动化覆盖健康、依赖降级、API 不可达后的手动重试、非法响应、8 秒请求超时和键盘焦点。
- 使用真实本地 API、PostgreSQL/pgvector、Redis 和确定性模型 fake 联调，页面正确显示
  `POSTGRESQL_OK`、`REDIS_OK` 和 `MODEL_FAKE_READY`，并展示响应 Trace/Request ID。
- 在 1440 x 1000 桌面视口完成视觉检查；通过浏览器调试协议强制 390 x 844 移动视口，
  `scrollWidth` 与 `clientWidth` 均为 390，页面无横向溢出或控件遮挡。
- API 进程停止后页面在有限时间内进入 `API_UNREACHABLE` 可恢复错误态，重试入口保持可用。

### 步骤 7：Compose、本地运行与 CI

**工作量：5-6 人日**

- 为 API、Worker、Web 建立可复现容器构建。
- Compose 启动 PostgreSQL/pgvector、Redis、API、Worker 和 Web，并配置健康依赖。
- 使用命名卷保存本地数据；密钥不写入镜像、Compose 或日志。
- CI 分离后端静态检查、后端测试、迁移、前端检查和构建任务。
- PostgreSQL/pgvector 与 Redis 集成测试使用真实依赖，模型测试使用 fake。
- 配置依赖缓存和并发取消，避免旧提交占用资源。
- 对锁文件、Compose 配置、迁移链和 OpenAPI 生成结果做一致性检查。

**完成标准**：全新环境执行一次文档化命令即可启动；CI 从空缓存运行通过；停止后再次启动不会破坏数据库状态。

**状态**：已于 2026-07-18 完成实现与本地验收；等待提交后的首次 GitHub Actions 运行确认。

实际交付：

- API、Worker 和 Web 使用多阶段构建；Python、Node 和 nginx 基础镜像以 tag + digest 锁定。
  当前网络无法访问 Docker Hub token 服务，因此使用 AWS 的 Docker 官方镜像只读缓存；三个
  manifest digest 已与 Docker Hub 官方 API 逐一核对一致。
- 新增根 `.dockerignore`；生产镜像只安装目标 workspace 包，API 镜像包含 Alembic 配置和
  migrations，Worker 不安装未使用的 HTTPX 目标库。
- Compose 新增一次性 `migrate` 门禁、API/Worker/Web 健康检查、nginx 同源 `/api` 代理、
  PostgreSQL 与 Redis 命名卷及 Redis AOF。`APP_SECRET_KEY` 和 `POSTGRES_PASSWORD` 必须从
  环境或忽略的 `.env` 提供，Compose 文件不包含默认密钥。
- 新增 3 个显式启用的真实依赖集成测试，覆盖 pgvector、单一 Alembic head、Redis 往返和
  API readiness；默认测试入口不依赖宿主机服务。
- 新增 OpenAPI 确定性导出脚本和提交产物，以及分离的后端质量、后端测试、迁移集成、
  前端和 Compose smoke CI 作业；配置 uv/pnpm 缓存和并发取消。

2026-07-18 验证记录：

- `actionlint 1.7.12` 校验 CI workflow 通过；后端格式、lint、严格类型检查、59 个默认测试
  通过，3 个真实依赖集成测试通过；前端 lint、类型检查、6 个测试和生产构建通过。
- 使用独立 Compose project 和独立端口从空镜像缓存构建 API、Worker、Web，`migrate`
  正常退出，PostgreSQL、Redis、API、Worker 和 Web 均达到 healthy。
- 直连 API 与 nginx `/api` 代理均返回 ready；数据库检查为
  `vector:328a3caa2960`，Redis `appendonly=yes`，无测试密钥进入容器日志。
- 删除全部容器和网络但保留命名卷后重新启动，Redis 合成哨兵仍存在，pgvector 和 Alembic
  head 保持不变，readiness 恢复为 ready。
- Worker 成功消费并确认无正文诊断消息，Redis 中无诊断队列残留；非法版本消息完成有限重试
  后进入 `diagnostics.XQ`。本次 Docker 日志未重现 Step 3 已验证的 actor started/completed
  事件，因此该容器日志差异保留为后续排查项，不影响队列消费与健康验收结论。
- GitHub Actions 文件尚未提交到远端，托管 runner 的首次实际运行只能在提交并推送后确认；
  本地已逐项执行等价命令与冷构建。

### 步骤 8：验收、文档与移交

**工作量：1-2 人日**

- 更新 README 或贡献文档中的安装、启动、格式化、lint、类型检查、测试和迁移命令。
- 记录环境变量、端口、健康语义、常见故障和清理方式。
- 输出并校验 OpenAPI 文件，确保只暴露预期接口。
- 执行干净环境验收，并保存命令和结果摘要。
- 检查日志、测试数据、构建产物和 Git diff 中是否存在密钥或私密正文。

**完成标准**：未参与初始化的开发者能仅按文档启动系统并完成 smoke test。

## 6. 工作量汇总

| 工作包 | 人日 | 主要依赖 | 可并行性 |
| --- | ---: | --- | --- |
| 启动决策 | 1-2 | 无 | 关键路径 |
| 仓库与工具链 | 2-3 | 决策完成 | 关键路径 |
| API、配置、错误协议 | 3-4 | 工具链 | 可与 Web 并行 |
| DB 迁移与 Worker | 5-6 | 工具链、队列决策 | 可部分并行 |
| 可观测性 | 3-4 | API/Worker 基线 | 跨模块集成 |
| ModelGateway | 4-5 | 配置、Provider 决策 | 可与 DB/Web 并行 |
| Web 工作台外壳 | 3-4 | API 健康契约 | 高度可并行 |
| Compose 与 CI | 5-6 | 各进程可启动 | 后半程关键路径 |
| 验收与文档 | 1-2 | 全部工作包 | 关键路径 |
| **基础合计** | **27-36** |  |  |
| **含 15% 缓冲** | **31-42** |  |  |

缓冲主要覆盖 Windows/Linux 路径差异、容器健康时序、pgvector 镜像兼容、锁文件和 CI 缓存问题。阶段 0 的人工授权复核、购买云资源、真实模型费用和后续业务模型设计不包含在内。

## 7. 推荐排期与责任划分

以 3 人团队为例：

| 时段 | 后端/架构 | 前端 | 平台/质量 |
| --- | --- | --- | --- |
| 第 1-2 天 | 决策、Python workspace、API 契约 | Web 工具链、工作台外壳 | Compose 骨架、CI 骨架、ADR 整理 |
| 第 3-5 天 | DB、迁移、ModelGateway Port | 健康状态接入、错误与响应式状态 | Redis/Worker、日志和 OTel 基线 |
| 第 6-8 天 | Provider Adapter、契约测试 | 组件测试、可访问性修正 | 集成测试、容器构建、迁移检查 |
| 第 9-11 天 | 跨模块修复与审查 | 联调与构建修复 | CI 稳定化、干净环境 smoke test |
| 第 12-15 天 | 验收、文档、缓冲 | 验收、文档、缓冲 | 安全检查、OpenAPI、移交 |

合并顺序建议为：工具链和决策 -> API/健康契约 -> DB/Worker/Web/ModelGateway -> 可观测性 -> Compose/CI -> 验收。每个合并请求保持可启动或明确标记为不进入主分支的短期分支，不把所有脚手架积累到最后一次集成。

## 8. 测试与验证矩阵

| 层级 | 必测内容 | 是否依赖真实外部服务 |
| --- | --- | --- |
| 单元 | 配置校验、错误映射、trace ID、日志脱敏、能力别名 | 否 |
| 契约 | ModelGateway fake/Provider 一致性、健康响应 schema | Provider 使用 stub server |
| 集成 | PostgreSQL/pgvector 连接、迁移升降级、Redis 投递、Worker 重试 | 仅本地容器 |
| 前端 | 健康状态、失败重试、loading 终止、键盘焦点 | API mock 或本地 API |
| Smoke | Compose 启动、readiness、诊断任务、Web 构建 | 本地容器；模型可用性可降级 |
| 安全 | 错误不泄漏、日志脱敏、无密钥提交、外部 Provider 默认关闭 | 否 |

至少覆盖以下失败模式：

- PostgreSQL 未启动、迁移未执行或 pgvector 扩展不可用。
- Redis 不可用、任务超时、Worker 重启和超过重试上限。
- Provider 未配置、超时、限流、返回无效结构和策略禁止外发。
- OpenTelemetry Collector 不可用。
- 配置缺失、未知异常、恶意或超长 request ID。
- Web 首次加载时 API 不可用，恢复后可手动重试。

## 9. 阶段退出条件

以下条件必须全部满足：

1. 阶段 0 已正式退出，或项目状态明确记录为“阶段 1 工程完成、等待阶段 0 数据门禁”，不能宣称整个阶段完成。
2. 全新环境按文档用一个 Compose 命令启动 API、Worker、Web、PostgreSQL/pgvector 和 Redis。
3. `/api/v1/health/live` 与 `/api/v1/health/ready` 的成功和失败语义有自动化测试。
4. 一次 readiness 请求可用同一 trace 关联 API 日志、数据库 span 和响应头。
5. 一个诊断任务可以从投递日志关联到 Worker 日志，且重试不会改变最终语义。
6. 初始迁移在空数据库升级成功，并完成一次降级/再升级验证。
7. ModelGateway fake 和一个 Provider Adapter 通过同一套 Chat/Embedding 契约测试；默认 CI 无付费模型调用。
8. Web 展示真实依赖状态，不展示伪造的文档、会话或检索数据。
9. 后端格式、lint、类型检查、测试、迁移检查，以及前端 lint、类型检查、测试和构建均在 CI 稳定通过。
10. 日志、错误响应、测试 fixture、OpenAPI 和 Git 变更中没有密钥、私密正文、完整 prompt 或敏感 Provider 响应。

## 10. 主要风险与应对

| 风险 | 早期信号 | 应对 |
| --- | --- | --- |
| 阶段 0 未关闭却开始使用真实语料 | CI、截图或模型 smoke test 引用 `../cases/` 私有文件 | 阶段 1 只用合成数据和许可明确的 fixture；将语料验证设为独立门禁 |
| 工具链讨论拖延 | 同时出现多个锁文件或重复 lint/type 工具 | 步骤 0 限时决策；一类能力只保留一个工具 |
| 目录先于用例膨胀 | 大量空包、`pass` 和占位 README | 只在健康、Worker、Gateway 或 Web 实际引用时创建模块 |
| Worker 状态只存在于 Redis | Redis 清理后任务状态丢失 | 队列只负责投递；持久状态模型放到阶段 2 数据库设计中 |
| Readiness 变成昂贵的全链路探测 | 健康请求触发模型调用或长时间阻塞 | 只检查必需本地依赖，使用严格超时；模型状态单独报告 |
| 可观测性泄漏正文或密钥 | 调试日志出现请求体、SQL 参数或 Provider 输入 | 默认元数据日志、集中脱敏、泄漏回归测试 |
| Provider 适配绑死业务层 | application 中出现具体 SDK 类型或模型名 | 以能力别名和自有 schema 建契约，SDK 只在 Adapter 内部 |
| Compose 可用但 CI 不稳定 | 本地依赖启动顺序靠固定 sleep | 使用健康检查和有界重试，不依赖固定等待时间 |
| 阶段 1 偷跑阶段 2 | 开始设计完整核心实体或摄入表 | 以本计划“不做”清单审查范围，业务表留给阶段 2 ADR/迁移 |

## 11. 建议任务清单

可将以下条目直接转换为 Issue：

| ID | 任务 | 验收结果 |
| --- | --- | --- |
| S1-01 | 启动决策与 ADR 权威位置 | ADR 位置、工具版本、队列和 Provider 决策明确 |
| S1-02 | Python workspace 与后端质量工具 | 锁文件和规范命令可运行 |
| S1-03 | Web workspace 与前端质量工具 | 锁文件、测试和构建可运行 |
| S1-04 | FastAPI 工厂、配置和错误协议 | OpenAPI 与错误测试通过 |
| S1-05 | 健康与依赖就绪检查 | live/ready 成功和失败测试通过 |
| S1-06 | SQLAlchemy 与 Alembic 基线 | 迁移升降级通过 |
| S1-07 | Redis、队列和 Worker 入口 | 诊断任务与重试测试通过 |
| S1-08 | trace、JSON 日志和脱敏 | API/DB/Worker 可关联且无敏感字段 |
| S1-09 | ModelGateway Port 与 fake | 确定性契约测试通过 |
| S1-10 | 首个 Provider Adapter | Chat/Embedding 契约和错误分类通过 |
| S1-11 | Web 工作台健康状态 | 真实状态、错误重试和响应式检查通过 |
| S1-12 | Compose 与容器构建 | 干净环境单命令启动 |
| S1-13 | CI 全量门禁 | 所有规范命令从空缓存通过 |
| S1-14 | OpenAPI、贡献和故障排查文档 | 新开发者按文档完成 smoke test |

## 12. 下一阶段接口

阶段 1 结束时只为阶段 2 提供以下已验证入口：

- Application 可以通过事务边界访问 PostgreSQL，而不感知 FastAPI。
- Worker 可以接收带 `task_id`、`trace_id` 和 `event_version` 的结构化消息。
- ModelGateway 可以按能力别名调用 Embedding，并在 CI 中替换为 fake。
- Web 可以通过 TanStack Query 调用版本化 API 并展示长任务所需的有限状态组件。
- CI 可以运行真实 PostgreSQL/pgvector、Redis 和迁移集成测试。

阶段 2 再基于 ADR 明确核心实体、稳定 ID、版本与删除语义，并实现单个 Markdown 文件的幂等摄入闭环；阶段 1 不提前固化这些业务细节。
