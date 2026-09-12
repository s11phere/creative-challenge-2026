# 易知———个人知识工作台

> 易知简能，万化皆通。

本地优先、来源可追溯的个人知识工作台：上传个人文档，完成增量索引和检索，并通过带引用的问答、Assistant 对话和 Skill 工作流使用知识。

## 目录

- [易知———个人知识工作台](#易知个人知识工作台)
  - [目录](#目录)
  - [快速启动](#快速启动)
    - [使用外部 Chat Provider](#使用外部-chat-provider)
  - [使用](#使用)
    - [数据来源](#数据来源)
    - [知识问答与 Assistant 对话](#知识问答与-assistant-对话)
    - [命令与 Skill](#命令与-skill)
    - [个性化](#个性化)
    - [本地 Agent 工作区](#本地-agent-工作区)
  - [运行状态](#运行状态)
  - [停止与清理](#停止与清理)
  - [服务与端口](#服务与端口)
  - [开发与质量门禁](#开发与质量门禁)
  - [安全与数据边界](#安全与数据边界)
  - [故障排查](#故障排查)

## 快速启动

前置条件：Docker Engine 29+ 和 Docker Compose 5+。本机不需要单独安装 PostgreSQL 或 Redis。

默认使用确定性的 fake provider，可以直接体验完整流程。Embedding 服务默认使用 GPU；无 GPU 时请使用 CPU Compose 配置。

1. 创建本地环境文件：

```bash
cp .env.example .env
# 编辑 .env，至少设置 APP_SECRET_KEY 与 POSTGRES_PASSWORD
```

2. 启动本地服务：

```bash
docker compose -f deploy/compose.yaml --env-file .env up --build --detach --wait
```

无 GPU 环境：

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml \
  --env-file .env up --build --detach --wait
```

需要本地 Embedding 和 Reranker 时，显式启用对应 profile：

```bash
docker compose -f deploy/compose.yaml --env-file .env \
  --profile embedding --profile reranker up --build --detach --wait
```

3. 打开工作台：<http://127.0.0.1:5173>

首次启动 Embedding 服务时会下载模型并缓存到命名卷。中国网络环境可参考[模型配置说明](docs/model-setup.md)配置镜像源。

### 使用外部 Chat Provider

外部 Chat Provider 需要在 `.env` 中显式配置：

```dotenv
MODEL_PROVIDER=openai-compatible
FAST_CHAT_ENDPOINT=https://example.invalid/v1
FAST_CHAT_MODEL=your-model
MODEL_ALLOW_EXTERNAL=true
```

Embedding 和 Reranker 仍需分别配置，不会因为启用外部 Chat 自动切换为 fake。修改 `.env` 后重新创建 `api` 和 `worker` 服务。

## 使用

### 数据来源

在「数据来源」页面上传文档，等待解析、索引和发布完成。发布后，文档即可在当前 Space 的知识问答中使用；来源版本、定位信息和引用身份会被保留。

### 知识问答与 Assistant 对话

- 在「知识问答」中直接提问，回答会基于当前 Space 的已发布文档，并展示引用、限制和文档定位。
- Web 会保留会话、运行记录和引用；点击引用可查看固定版本中的最小原文片段并高亮定位。
- Assistant 会根据请求选择合适的 Skill 或知识工具。知识检索与回答由服务端执行，模型不会直接读取数据库或绕过引用校验。
- 默认 fake provider 返回确定性的抽取式回答，适合先验证流程；配置外部 Chat Provider 后可使用真实模型。
- Assistant 支持 GFM Markdown 和 LaTeX 渲染。输入 `/` 可打开可搜索、支持键盘操作的命令面板。

### 命令与 Skill

| 命令 | 作用 |
| --- | --- |
| `/ask` | 知识问答、普通摘要和来源比较 |
| `/research` | 论文精读和文献综述 |
| `/prepare-exam` | 诊断、针对性复习和复测 |
| `/course-project` | 按当前进度推进课程项目 |
| `/create-skill`（别名 `/skill`） | 创建个人 Skill，经过草稿、校验、评估和审批后激活 |
| `/effort` | 读取或设置后续运行的 reasoning effort：`low`、`medium`、`high`、`xhigh`、`max` |
| `/compact` | 创建后台上下文压缩任务 |
| `/workspace`、`/ws` | 选择本地 Agent 工作区 |
| `/help`、`/skills`、`/new`、`/stop` | 查看帮助、Skill、创建会话或停止当前运行 |

已安装但关闭的 Skill 不会出现在下一轮 Assistant 的可用目录中。Skill 的运行记录、引用和派生内容会沿用当前会话与 Space 的权限边界。

### 个性化

- **长期记忆**：从会话摘要和使用模式提炼用户事实、偏好和工作模式，在后续对话中按当前问题检索并注入有界上下文。
- **个人 Skill**：在 `PERSONAL_SKILLS_DIR`（默认 `./data/personal_skills`）中创建个人 Skill，只能组合系统已有的 handler/tool，不能覆盖内置 Skill。
- **工作模式建议**：系统可以根据使用痕迹生成个人 Skill 草稿；草稿不会自动激活，必须经过用户审批。

### 本地 Agent 工作区

使用 `/workspace <folder>` 选择 `AGENT_WORKSPACE_ROOT_PATH` 下的现有目录。工作区独立于上传的知识；选中后 Agent 可列出和读取文件，写入文件或执行命令需要持久审批。

默认只有 fake provider 注册工作区工具。使用非 fake Provider 时，需要显式设置 `AGENT_WORKSPACE_MODEL_VISIBILITY_CONSENT=true`，或启动时使用：

```powershell
.\scripts\start-local.ps1 -AllowExternalWorkspaceTools
```

工作区工具始终受路径 containment、受保护路径、命令别名和审批策略限制；发送内容到外部 Provider 前请确认来源策略和用户同意。

## 运行状态

```bash
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
docker compose -f deploy/compose.yaml --env-file .env ps
```

`live` 应返回 `alive`，`ready` 应返回 `ready`。模型能力会根据实际配置报告 fake 或已配置状态。

## 停止与清理

停止容器但保留 PostgreSQL 和 Redis 数据：

```bash
docker compose -f deploy/compose.yaml --env-file .env down
```

确认要永久删除当前项目数据时，才执行：

```bash
docker compose -f deploy/compose.yaml --env-file .env down --volumes --remove-orphans
```

## 服务与端口

| 服务 | 默认地址/端口 | 说明 |
| --- | --- | --- |
| Web | `http://127.0.0.1:5173` | 工作台页面，并代理同源 `/api` |
| API | `http://127.0.0.1:8000` | 问答、来源、Skill、会话和运行接口 |
| PostgreSQL | `127.0.0.1:5432` | 数据、版本和索引 |
| Redis | `127.0.0.1:6379` | 后台任务队列 |
| Embedding | `127.0.0.1:8080` | 需启用 `embedding` profile |
| Reranker | `127.0.0.1:8081` | 需启用 `reranker` profile |

端口可通过 `.env` 中的 `WEB_PORT`、`API_PORT`、`POSTGRES_PORT`、`REDIS_PORT`、`EMBEDDING_PORT` 和 `RERANKER_PORT` 覆盖。

面向官网的生产（内网）profile 使用 `deploy/compose.intranet.yaml`：项目服务不发布公网端口，
API 要求网关令牌，并只开放首期知识工作流。部署、卷、备份与回滚见[运维手册](docs/operations.md)。

## 开发与质量门禁

```bash
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest tests/unit tests/contract
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
```

真实 PostgreSQL/Redis 集成测试需要隔离依赖并显式设置 `RUN_INTEGRATION=1`：

```bash
RUN_INTEGRATION=1 uv run pytest tests/integration
```

CI 的单元/契约测试运行在没有任何本地数据库的机器上。本地若同时跑着 Compose 栈，个别用例
可能因为“恰好能连上数据库”而走上不同分支，因此提交前建议用不可达端口复现 CI 条件：

```bash
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=59999 REDIS_HOST=127.0.0.1 REDIS_PORT=59998 \
  uv run pytest tests/unit tests/contract
```

网站 profile 的端到端 smoke（在 API 容器内执行，覆盖上传、摄入、问答、引用和租户隔离）：

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.intranet.yaml \
  exec -T -e SMOKE_TOKEN="$INTERNAL_SERVICE_TOKEN" api \
  python - < examples/first_phase_smoke.py
```

## 安全与数据边界

- 密钥只从环境变量或被 Git 忽略的 `.env` 读取，不要提交或写入日志、trace、评估报告。
- 仅处理来源清单明确允许的文件；读取前校验 SHA-256 与 manifest 中的 `content_sha256` 一致。
- 文档内容不可信，不能借由内容提升工具权限或覆盖系统指令。
- `private_local` 和 `restricted` 内容默认不得离开本地；使用外部 Provider 前必须满足来源策略、部署策略和用户可见同意要求。
- 结构化指标日志不包含对话内容、prompt、文档正文、Provider 响应或内部资源 ID。

## 故障排查

常见启动、模型下载、Provider 配置和数据恢复问题见[故障排查文档](docs/troubleshooting.md)；
部署、持久卷、备份恢复、升级回滚和临时文件清理见[运维手册](docs/operations.md)；
第三方模型、依赖、数据、字体与素材许可见[数据与许可说明](docs/data-and-licenses.md)。

CC2026 官网接入的服务认证、生产内网 Compose 覆盖和首期能力边界见
[官网接入交付说明](docs/cc2026-delivery.md)；面向网站负责人的接口、部署和验收步骤见
[易知官网适配交付手册](docs/cc2026-yizhi-handoff.md)；可执行的端到端验收脚本见
[`examples/first_phase_smoke.py`](examples/first_phase_smoke.py)。
