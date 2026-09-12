# 易知（Agent Knowledge Repository）运维手册

本文档面向 CC2026 官网接入后的部署与运维负责人，覆盖
`tmp/cc2026-获奖作品技术交付规范.md` 第 8 节要求的
「持久卷、备份、升级、回滚、临时文件清理」和「日志输出 stdout/stderr、包含 request id」。
所有命令与数值都来自当前仓库文件；引用格式为 `路径:行号`。
无法从仓库确认的事项统一写「需在目标机确认」，未在仓库验证过的命令参数不写入本文档。

相关文档索引（本文档不重复其内容）：

| 主题 | 权威文档 |
| --- | --- |
| 官网接入边界、生产环境变量、CPU Compose、持久化/升级/回滚摘要 | `docs/cc2026-yizhi-handoff.md` 第 6 节（6.1–6.3，第 109–153 行） |
| 交付范围与外部验收前置 | `docs/cc2026-delivery.md` |
| 故障现象与处置 | `docs/troubleshooting.md` |
| 本地环境、模型配置 | `docs/development-environment.md`、`docs/model-setup.md` |
| 模块边界与数据模型 | `docs/architecture.md` |

## 1. 服务拓扑与端口

### 1.1 两种 Compose 组合

| 组合 | 文件 | 用途 |
| --- | --- | --- |
| 开发 | `deploy/compose.yaml`（可选 `deploy/compose.cpu.yaml`） | 本地工作台、单机联调；发布 Web/API/DB/Redis 端口 |
| 生产内网 | `deploy/compose.yaml` + `deploy/compose.intranet.yaml`（+ `deploy/compose.cpu.yaml`） | 官网网关接入；**不发布任何项目服务端口** |

生产（无 GPU）启动命令，与 `docs/cc2026-yizhi-handoff.md:133-140` 一致：

```bash
docker compose -f deploy/compose.yaml \
  -f deploy/compose.cpu.yaml \
  -f deploy/compose.intranet.yaml \
  --env-file .env \
  --profile embedding --profile reranker \
  up --build --detach --wait
```

只做流程联调时可以不启用 `embedding`/`reranker` profile（`docs/cc2026-yizhi-handoff.md:142-143`）。
`deploy/compose.cpu.yaml` 把两个 TEI 服务从 CUDA 镜像切换为 CPU 镜像并移除 GPU 预留
（`deploy/compose.cpu.yaml:17`、`deploy/compose.cpu.yaml:21`、`deploy/compose.cpu.yaml:24-39`）；
它不改变卷、构建或端口。`--env-file .env` 必须显式传入，因为仓库的 `.env` 在仓库根目录，
而 Compose 默认的项目目录是 `deploy/`。

### 1.2 端口矩阵

| 服务 | 容器内端口 | 开发发布端口（默认） | 生产内网 override | 依据 |
| --- | --- | --- | --- | --- |
| `api` | 8000 | `${API_PORT:-8000}` | `!reset []` 不发布 | `deploy/compose.yaml:114-115`、`deploy/Dockerfile.api:47`、`deploy/compose.intranet.yaml:13` |
| `worker` | 无监听端口 | 无 | 无 | `deploy/compose.yaml:152-241` |
| `web`（Vite + nginx） | 80 | `${WEB_PORT:-5173}` | `!reset []` 不发布 | `deploy/compose.yaml:249-250`、`deploy/compose.intranet.yaml:30-33` |
| `postgres` | 5432 | `${POSTGRES_PORT:-5432}` | `!reset []` 不发布 | `deploy/compose.yaml:18-19`、`deploy/compose.intranet.yaml:9` |
| `redis` | 6379 | `${REDIS_PORT:-6379}` | `!reset []` 不发布 | `deploy/compose.yaml:33-34`、`deploy/compose.intranet.yaml:11` |
| `tei`（embedding profile） | 80 | `${EMBEDDING_PORT:-8080}` | `!reset []` 不发布 | `deploy/compose.yaml:291-292`、`deploy/compose.intranet.yaml:25` |
| `tei-reranker`（reranker profile） | 80 | `${RERANKER_PORT:-8081}` | `!reset []` 不发布 | `deploy/compose.yaml:318-319`、`deploy/compose.intranet.yaml:27` |
| `otel-collector`（otel profile） | 4317/4318/55679 | 4317/4318/55679 | `!reset []` 不发布 | `deploy/compose.yaml:266-269`、`deploy/compose.intranet.yaml:29` |

生产内网的边界语义（`deploy/compose.intranet.yaml:1-42`）：

- 唯一公网入口是官网的 Next.js/Nginx 网关；项目 API/Worker/DB/Redis/web/TEI/Collector 都不发布端口。
- 网关必须与项目服务处于同一私有网络才能访问 `api:8000`；具体接入方式（网关加入本 Compose 网络、
  或由宿主机网络策略转发）**需在目标机确认**。
- 生产 override 中默认网络仍为 `internal: false`（`deploy/compose.intranet.yaml:37-42`），
  因为 `internal: true` 会同时阻断模型下载所需的受控出站。出站限制必须由宿主机/官网侧网络策略实现。
- `web` 服务保留在 `default` 网络但不再发布端口（`deploy/compose.intranet.yaml:34-35`），它只是本地开发 UI。

### 1.3 健康检查命令与预期输出

开发（端口已发布，`docs/troubleshooting.md:21-27`、`README.md:123-131`）：

```bash
docker compose -f deploy/compose.yaml --env-file .env ps
curl --fail http://127.0.0.1:8000/api/v1/health/live
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:5173/healthz
```

预期输出（响应模型见 `apps/api/src/api/main.py:116-142`、`apps/api/src/api/main.py:467-537`）：

- `/api/v1/health/live`（HTTP 200）：`{"status":"alive"}`。只表示进程事件循环可响应。
- `/api/v1/health/ready`（HTTP 200）：`status` 为 `ready`，`checks` 至少包含
  `postgresql`=`POSTGRESQL_OK`、`redis`=`REDIS_OK`；`model` 报告各能力状态码，
  fake 默认下为 `MODEL_FAKE_READY`，真实配置下为 `MODEL_CAPABILITY_CONFIGURED`
  （`packages/model_gateway/src/model_gateway/fake.py:59`、
  `packages/model_gateway/src/model_gateway/factory.py:214`）。任一必需依赖不健康时返回
  HTTP 503、`status=degraded`，机器码含义见 `docs/troubleshooting.md:110-121`。
- `/healthz`（web）：返回 `ok`（`deploy/nginx.conf:13-17`）。
- `docker compose ps` 中 `api`、`web`、`postgres`、`redis`、`worker` 的 `STATUS` 应包含 `healthy`。

生产内网（无发布端口）在容器内自检，与 Compose 健康检查使用同一条探针
（`deploy/compose.yaml:121-132`）：

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.intranet.yaml --env-file .env exec -T api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=5).read().decode())"
```

各容器健康检查定义与依据：

| 服务 | 探针 | 依据 |
| --- | --- | --- |
| `api` | `urllib` 请求 `http://127.0.0.1:8000/api/v1/health/ready` | `deploy/compose.yaml:121-132` |
| `worker` | `grep -aq 'worker' /proc/1/cmdline` | `deploy/compose.yaml:218-223` |
| `postgres` | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` | `deploy/compose.yaml:22-27` |
| `redis` | `redis-cli ping` | `deploy/compose.yaml:37-42` |
| `web` | `wget http://127.0.0.1/healthz` | `deploy/compose.yaml:254-259` |
| `tei` / `tei-reranker` | `curl http://127.0.0.1:80/health` | `deploy/compose.yaml:302-307`、`deploy/compose.yaml:329-334` |

## 2. 配置与密钥

### 2.1 必须设置的变量

| 变量 | 必需条件 | 依据 |
| --- | --- | --- |
| `APP_SECRET_KEY` | 始终必需（Compose 强制 `${APP_SECRET_KEY:?}`） | `deploy/compose.yaml:63`、`deploy/compose.yaml:160` |
| `POSTGRES_PASSWORD` | 始终必需（`${POSTGRES_PASSWORD:?}`） | `deploy/compose.yaml:9`、`deploy/compose.yaml:17` |
| `INTERNAL_SERVICE_TOKEN` | 生产内网必需（`${INTERNAL_SERVICE_TOKEN:?}`） | `deploy/compose.intranet.yaml:19` |
| `APP_ENV=production`、`APP_DEBUG=false`、`SERVICE_AUTH_REQUIRED=true`、`PUBLIC_MODE=true` | 生产内网由 override 直接写入 | `deploy/compose.intranet.yaml:15-18`、`deploy/compose.intranet.yaml:22-23` |

生产最小变量集与 `docs/cc2026-yizhi-handoff.md:113-127` 一致；若启用外部 Chat Provider，
再按该节评审后显式增加 Provider 变量。端口覆盖变量、日志变量、模型变量、QA 调试变量的
完整清单见 `.env.example`（第 9–139 行），此处不重复。

### 2.2 secret 注入方式

- 变量只从环境或 secret manager 注入；生产机上由部署系统把 secret 渲染成 Compose 读取的
  `--env-file`（或进程环境），不要写进仓库。
- `.env` 被 Git 忽略（`.gitignore:51`；`tmp/`、`data/` 另见 `.gitignore:88-89`），
  也被构建上下文忽略（`.dockerignore:6-8` 排除 `.env`/`.env.*`，仅保留 `.env.example`），
  因此密钥不会进入镜像层。
- 镜像内没有 `.env` 文件（`deploy/Dockerfile.api:7-17` 只 COPY 依赖、代码、skills、migrations、prompt/schema），
  容器配置只来自 Compose `environment`。修改 `.env` 后必须重建容器（`up --detach --force-recreate api worker`），
  只重启进程不会生效（`docs/troubleshooting.md:151-159`）。
- 容器以非 root 的 uid/gid `10001` 运行（`deploy/Dockerfile.api:28-29`、`deploy/Dockerfile.api:43`、
  `deploy/Dockerfile.worker:27-28`、`deploy/Dockerfile.worker:40`）。

### 2.3 生产启动校验 `Settings.validate_secrets`

生产启动时 API 与 Worker 都会先校验必需 secret，缺失即拒绝启动：

- 实现：`packages/infrastructure/src/infrastructure/config.py:205-223`，仅当 `APP_ENV == "production"`
  时检查 `APP_SECRET_KEY`、`POSTGRES_PASSWORD`，以及 `SERVICE_AUTH_REQUIRED` 打开时的
  `INTERNAL_SERVICE_TOKEN`；缺失时抛出 `ValueError: Required configuration values are missing: ...`。
- 调用点：API 在 lifespan 起始处 `settings.validate_secrets()`
  （`apps/api/src/api/main.py:333`）；Worker 在 `main()` 起始处
  （`apps/worker/src/worker/main.py:15`）。
- 现象与处置：容器反复重启、日志出现上述 `ValueError`。补齐被点名的变量后
  `up --detach --force-recreate` 对应服务；不要通过把 `APP_ENV` 改回 `development` 绕过。
- 非法取值（例如 `QA_DEBUG_TRACE_MAX_BYTES` 超出 `100000`–`500000000`，见
  `packages/infrastructure/src/infrastructure/config.py:139`）同样在启动时由 Settings 校验失败，表现为进程直接退出。

## 3. 持久卷与宿主目录

### 3.1 卷清单

Compose 项目名默认取第一个 compose 文件所在目录名 `deploy`，因此默认卷名为 `deploy_*`
（`deploy/compose.yaml:344-345` 的 `deploy_teidata` 默认值即该前缀）。使用
`scripts/start-local.ps1` 或自定义 `-p` 时前缀不同，实际名称用 `docker volume ls` 确认。

| 卷/挂载 | 容器路径 | 保存内容 | 能否删除 | 依据 |
| --- | --- | --- | --- | --- |
| `pgdata`（命名卷） | `/var/lib/postgresql/data` | Space、来源、文档与版本、Chunk、embedding、会话、Run/Attempt/Event、Citation、Skill 激活、审批等全部权威数据 | **不能删除**（删除即丢失全部业务数据） | `deploy/compose.yaml:20-21`、`deploy/compose.yaml:338` |
| `redisdata`（命名卷） | `/data` | Dramatiq 队列与可恢复任务状态；AOF `appendonly yes` + `appendfsync everysec` | 只在确认队列可丢弃时删除；Worker 启动会接管 queued 或租约过期的任务，Redis 丢失会丢失排队中的任务 | `deploy/compose.yaml:32`、`deploy/compose.yaml:35-36`、`deploy/compose.yaml:339`、`docs/cc2026-yizhi-handoff.md:149` |
| `blobdata`（命名卷） | `/app/data/blobs` | Local BlobStore 的原始上传文件，布局 `{root}/{source_id}/{blob_hash[:2]}/{blob_hash}` | **迁移到官网 R2 之前不能删除**；删除后 Citation 原文解析与重摄入都会失败 | `deploy/compose.yaml:148`、`deploy/compose.yaml:238`、`deploy/compose.yaml:340`、`packages/infrastructure/src/infrastructure/blob_store.py:23-25` |
| 绑定挂载 `${AGENT_WORKSPACE_HOST_PATH:-../data/workspaces}` | `/data/agent-workspaces`（api、worker） | 本地 Agent 工作区文件 | 属于用户数据；只有在确认无会话引用时才清理 | `deploy/compose.yaml:149`、`deploy/compose.yaml:240` |
| 绑定挂载 `${PERSONAL_SKILLS_HOST_PATH:-../data/personal_skills}` | `/data/personal-skills`（api、worker） | 个人 Skill 包 | 会让已保存的个人 Skill 失效；需确认后再清理 | `deploy/compose.yaml:150`、`deploy/compose.yaml:241` |
| 绑定挂载 `${QA_DEBUG_TRACE_HOST_PATH:-../tmp/qa-debug}` | `/data/qa-debug`（仅 worker） | QA debug trace（开发排查用，含正文） | **可以随时删除**，见 6.1 | `deploy/compose.yaml:206`、`deploy/compose.yaml:239` |
| `teidata` / `rerankerdata`（命名卷） | `/data`（TEI 服务） | TEI 下载的 Embedding / Reranker 模型权重缓存 | 可以删除，但会触发重新下载；首次下载中断时可用预热卷复用 | `deploy/compose.yaml:293-294`、`deploy/compose.yaml:320-321`、`deploy/compose.yaml:342-347` |
| `personalskills`（命名卷） | 未被任何服务挂载 | 仅声明，当前实际使用上面的绑定挂载 | 保留；不要依赖它存数据 | `deploy/compose.yaml:341`（对比 `deploy/compose.yaml:150`、`deploy/compose.yaml:241`） |

绑定挂载的宿主路径以 compose 文件所在目录 `deploy/` 为基准解析，`../data/workspaces`
即仓库根的 `data/workspaces`。实际解析结果用
`docker compose -f deploy/compose.yaml --env-file .env config | grep -A3 volumes` 核对。

### 3.2 重要约束：`blobdata` 属主与非 root 运行

`blobdata` 挂载到容器内 `/app/data/blobs`，而镜像以 uid/gid `10001` 运行。
Docker 对**全新命名卷**的处理是：如果目标路径在镜像内不存在，Docker 以 `root:root`
创建挂载点，空卷根目录也是 `root:root`；只有该路径在镜像内已存在时，才会用镜像内目录的
属主初始化新卷。BlobStore 写文件前需要在该目录下创建 `{source_id}/…` 子目录
（`packages/infrastructure/src/infrastructure/blob_store.py:38-42`），因此 root 属主会导致
`PermissionError`，上传接口返回 500。

仓库已按此要求处理：`deploy/Dockerfile.api:35-39` 和 `deploy/Dockerfile.worker:34-36` 在
**切换 USER 之前**执行 `mkdir -p /app/data/blobs && chown -R app:app /app`，注释也写明了这条
Docker 语义。默认 blob 根目录是 `./data/blobs`（`packages/infrastructure/src/infrastructure/config.py:87-89`），镜像 `WORKDIR /app`
（`deploy/Dockerfile.api:31`），Compose 未设置 `BLOB_STORE_PATH`，所以容器内路径就是
`/app/data/blobs`。修改 Dockerfile 时不要把这两行删掉。

排查方法（上传 500 / 日志出现 `PermissionError`）：

```bash
# 1. 看属主与运行身份
docker compose -f deploy/compose.yaml --env-file .env exec -T api ls -ld /app/data/blobs
docker compose -f deploy/compose.yaml --env-file .env exec -T api id
# 期望：目录属主为 app app（10001），进程 uid=10001 gid=10001

# 2. 直接探测可写性
docker compose -f deploy/compose.yaml --env-file .env exec -T api \
  python -c "from pathlib import Path; p=Path('/app/data/blobs/.writecheck'); p.write_bytes(b'ok'); p.unlink(); print('writable')"
```

- 修复 A（永久，推荐）：确认 Dockerfile 保留 `mkdir -p /app/data/blobs`，然后
  `docker compose ... up --build --detach --wait api worker`。
  注意：镜像内目录只用于初始化**空卷**；已经存在且属主为 root 的旧卷不会自动改属主。
- 修复 B（既有卷一次性处理）：在容器内以 root 修正属主，改动落在卷上：

  ```bash
  docker compose -f deploy/compose.yaml --env-file .env exec -T --user root api chown -R 10001:10001 /app/data/blobs
  docker compose -f deploy/compose.yaml --env-file .env exec -T --user root worker chown -R 10001:10001 /app/data/blobs
  ```

- 绑定挂载目录（`data/workspaces`、`data/personal_skills`、`tmp/qa-debug`）有同样的风险：
  目录不存在时 Docker 会以 root 创建。部署前在宿主机预建并授权：

  ```bash
  mkdir -p data/workspaces data/personal_skills tmp/qa-debug
  sudo chown -R 10001:10001 data/workspaces data/personal_skills tmp/qa-debug
  ```

  `tmp/qa-debug` 不可写时不会中断运行，但会静默停用 trace 并记录
  `qa_debug_trace_initialization_failed`（`packages/infrastructure/src/infrastructure/qa_debug_trace.py:108-115`）。
  Windows/Docker Desktop 或 SELinux 主机上的 uid 映射与挂载标签**需在目标机确认**。

### 3.3 扩容

- 命名卷实际存放在宿主机 `/var/lib/docker/volumes/<卷名>/_data`，用
  `docker volume inspect <卷名> --format '{{.Mountpoint}}'` 查看；容量由该挂载点所在文件系统
  决定，Docker 默认没有 per-volume 配额，也没有在线缩小语义。
- 扩容 = 扩容宿主数据盘/文件系统（云盘、LVM、挂载点），Docker 侧无需改动；
  具体方式与是否支持在线扩容**需在目标机确认**。
- 常用核对命令：`docker system df -v`、`df -h <Mountpoint>`、
  `docker compose -f deploy/compose.yaml --env-file .env exec -T postgres psql -U app -d agent_knowledge -c "select pg_size_pretty(pg_database_size('agent_knowledge'))"`。
- 绑定挂载目录受仓库所在卷容量限制，需要单独监控。
- 清理空间只能通过删除数据实现（blob 清理、QA trace 清理、旧镜像 `docker image prune`）；
  **禁止用删除卷的方式释放空间**。

## 4. 备份与恢复

### 4.1 现状

仓库**不包含任何自动备份任务或备份脚本**：`scripts/` 下没有备份脚本，仓库文档中也没有
`pg_dump`/`pg_restore` 的既有流程。备份的落盘位置、保留周期、加密、异地存放和
监控告警必须由官网/运维侧在部署系统中实现；本文档只给出可执行的命令与一致性要求。
生产备份路径与保留周期不能由项目默认值推断（`docs/cc2026-yizhi-handoff.md:153`）。

需要备份的对象：

| 对象 | 是否必须 | 说明 |
| --- | --- | --- |
| `pgdata`（PostgreSQL） | 必须 | 全部权威业务数据 |
| `blobdata`（`/app/data/blobs`） | 必须 | 原始文件；不在 `pg_dump` 内，必须单独备份 |
| `redisdata` | 可选 | 队列可恢复，见 4.4 |
| `teidata`/`rerankerdata` | 不必要 | 模型权重可重新下载 |
| `data/workspaces`、`data/personal_skills` | 按需 | 用户数据/个人 Skill；如需保留则随宿主目录一起备份 |

### 4.2 一致性要求（重要）

数据库行与 blob 文件必须来自**同一个静止窗口**。任何一侧单独回退都会产生不一致：
数据库引用了快照中不存在的 blob → Citation 原文解析失败；快照中有 blob 而数据库无引用 →
孤儿文件（无害）。因此备份前先停止写入方：

```bash
docker compose -f deploy/compose.yaml --env-file .env stop api worker
# 依次完成 PostgreSQL 与 blob 备份后再：
docker compose -f deploy/compose.yaml --env-file .env start api worker
```

停止 `api` 与 `worker` 即可静止（上传在 API 侧写 blob，摄入与清理在 Worker 侧写/删 blob）；
`postgres`、`redis` 保持运行以便执行备份命令。

### 4.3 PostgreSQL 逻辑备份

每次执行前用 `.env` 中真实的 `POSTGRES_USER` / `POSTGRES_DB` 替换下面命令里的 `app` /
`agent_knowledge`（默认值见 `deploy/compose.yaml:7-8`）。`-T` 关闭伪终端，避免二进制输出被破坏。

```bash
mkdir -p tmp/backup
STAMP=$(date +%Y%m%dT%H%M%S)
docker compose -f deploy/compose.yaml --env-file .env exec -T postgres \
  pg_dump -U app -d agent_knowledge --format=custom --no-owner \
  > "tmp/backup/agent_knowledge-${STAMP}.dump"
sha256sum "tmp/backup/agent_knowledge-${STAMP}.dump" | tee "tmp/backup/agent_knowledge-${STAMP}.dump.sha256"
```

纯 SQL 形式（便于人工检查、可直接用 `psql` 恢复）：

```bash
docker compose -f deploy/compose.yaml --env-file .env exec -T postgres \
  pg_dump -U app -d agent_knowledge --format=plain --no-owner > "tmp/backup/agent_knowledge-${STAMP}.sql"
```

建议频率与保留（**建议值**，需按数据重要性与合规要求由官网/运维确认）：

| 时机 | 建议 |
| --- | --- |
| 例行 | 每日 1 次全量逻辑备份，保留 14 天以上 |
| 发布前/后 | 各 1 次全量备份（升级与回滚的必备前置，见 5.2） |
| blob 卷 | 每日增量快照或每周全量 + 发布前快照；blob 只在删除时减少，增量可行 |
| 恢复演练 | 每季度或每次大版本前在隔离库演练一次（见 4.5） |

### 4.4 Redis 与 blob 卷的一致性说明

- Redis 不是权威存储：`redisdata` 是 AOF（`everysec`）持久化的任务队列
  （`deploy/compose.yaml:32-36`）。Worker 启动会接管 queued 或租约过期的任务
  （`docs/troubleshooting.md:313-316`、`docs/cc2026-yizhi-handoff.md:149`），
  因此 Redis 不需要常规备份；丢失的后果是排队中的任务需要重新提交。
  如需留档，可执行 `docker compose -f deploy/compose.yaml --env-file .env exec -T redis redis-cli BGREWRITEAOF`
  后复制 `redisdata` 卷。
- blob 卷与数据库的一致性只能靠 4.2 的静止窗口保证；`pg_dump` 不包含 blob。
- Power-loss 场景下 PostgreSQL 由自身 WAL 保证崩溃一致性；Redis AOF `everysec` 最多丢失约 1 秒
  的队列写入，不影响已提交到 PostgreSQL 的状态。

### 4.5 恢复演练步骤（在隔离库执行）

演练只碰 scratch 数据库，**不要**对 `agent_knowledge` 执行 `DROP`，也不要在含数据的环境执行
`downgrade`（`docs/troubleshooting.md:72-73`）。

```bash
COMPOSE="docker compose -f deploy/compose.yaml --env-file .env"
DUMP=tmp/backup/agent_knowledge-<时间戳>.dump

# 1. 建演练库
$COMPOSE exec -T postgres psql -U app -d postgres -c "CREATE DATABASE agent_knowledge_drill"

# 2. 恢复（pg_restore 未指定文件名时从标准输入读取；失败会明确报错，不会静默跳过）
$COMPOSE exec -T postgres pg_restore -U app -d agent_knowledge_drill --no-owner --no-privileges < "$DUMP"

# 3. 校验
$COMPOSE exec -T postgres psql -U app -d agent_knowledge_drill -c "select version_num from alembic_version;"
$COMPOSE exec -T postgres psql -U app -d agent_knowledge_drill -c "\dt"
$COMPOSE exec -T postgres psql -U app -d agent_knowledge_drill -c "select count(*) from documents;"

# 4. 清理演练库
$COMPOSE exec -T postgres psql -U app -d postgres -c "DROP DATABASE agent_knowledge_drill"
```

判定标准：`alembic_version.version_num` 等于备份时点的 revision（当前仓库 head 为
`c4d5e6f7a8b9`，见 5.1）；业务表存在；行数非零且与备份时点相符。演练还应核对
blob 备份可以解包（`tar -tzf` 列表）并与数据库中的 blob 引用大致匹配。

### 4.6 真实恢复

1. 先对当前库再做一次全量备份，保留回退路径。
2. `stop api worker`，避免恢复期间有新写入。
3. 用最近一次一致的「数据库 + blob」快照恢复：
   - 数据库（覆盖式恢复，`--clean --if-exists` 会先删除同名对象）：
     `docker compose -f deploy/compose.yaml --env-file .env exec -T postgres pg_restore -U app -d agent_knowledge --clean --if-exists --no-owner < <dump>`；
     也可以先 `DROP DATABASE`/`CREATE DATABASE` 再按 4.5 的方式恢复（需确认无其他连接）。
   - blob：解包回 `blobdata` 卷（见 6.3 的命令形式）；解包前确认目标卷内容已按需清空，
     否则会留下孤儿文件。
4. 若备份时点早于当前镜像需要的 revision，先跑迁移再起服务：
   `docker compose -f deploy/compose.yaml --env-file .env run --rm migrate`
   （api/worker 通过 `depends_on: service_completed_successfully` 等待它完成，
   见 `deploy/compose.yaml:116-118`、`deploy/compose.yaml:213-215`）。
5. `start api worker`，用 1.3 的命令确认 `ready`，并抽查上传、检索、引用解析。

## 5. 升级与回滚

### 5.1 固定版本

- 交付必须固定 Git tag 或 commit SHA。当前工作树 HEAD 为 `4a5ae80e582cbaff28000b0bad5a8b6d1a0db2b8`
  （`docs: complete yizhi website handoff and CI fixes`），但工作树仍含未提交改动，
  **冻结交付时必须以实际 tag/commit 为准**并在交付记录中写明。
- 三个应用镜像都是本地构建（`deploy/compose.yaml:2`、`deploy/compose.yaml:153`、
  `deploy/compose.yaml:244`），默认没有 registry digest；可追溯标识用构建出的镜像 ID：

  ```bash
  docker image inspect agent-knowledge-api:local --format '{{.Id}}'
  docker image inspect agent-knowledge-worker:local --format '{{.Id}}'
  docker image inspect agent-knowledge-web:local --format '{{.Id}}'
  ```

  如果推送到 registry，再记录 `RepoDigest`（`docker images --digests`）。
- 基础镜像与依赖已经锁定：Dockerfile 使用带 digest 的 Python/PostgreSQL/Redis/TEI 镜像
  （`deploy/Dockerfile.api:1`、`deploy/Dockerfile.api:22`、`deploy/compose.yaml:13`、
  `deploy/compose.yaml:31`、`deploy/compose.yaml:277`），Python 依赖用
  `uv sync --frozen`（`deploy/Dockerfile.api:19-20`）。不要为了绕过网络问题移除 digest
  （`docs/troubleshooting.md:295-299`）。
- 当前数据库迁移为 29 个 revision、单一 head `c4d5e6f7a8b9`
  （`migrations/versions/c4d5e6f7a8b9_merge_skill_activation_and_exam_preparation.py:8-11`）。

### 5.2 升级步骤

```bash
# 0. 备份（见第 4 节），记录当前镜像 ID 与 revision
# 1. 构建/拉取新镜像
docker compose -f deploy/compose.yaml --env-file .env build --pull api worker web
# 2. 先跑迁移（也可直接执行第 3 步，migrate 会先完成）
docker compose -f deploy/compose.yaml --env-file .env run --rm migrate
# 3. 启动 API/Worker/Web 并等待健康
docker compose -f deploy/compose.yaml --env-file .env up --detach --wait
# 4. 校验
docker compose -f deploy/compose.yaml --env-file .env ps
curl --fail http://127.0.0.1:8000/api/v1/health/ready
```

- `migrate` 服务是一次性容器，命令固定为 `alembic upgrade head`，`restart: "no"`
  （`deploy/compose.yaml:45-53`）。升级必须先迁移再起 API/Worker；`up` 时 Compose 会按
  `depends_on` 保证顺序。
- 升级期间旧版本 API 仍可短暂服务；但不要跳过迁移直接起新版 API。
- 生产内网组合请在第 1–3 步追加 `-f deploy/compose.intranet.yaml`（以及无 GPU 时的
  `-f deploy/compose.cpu.yaml`）与 `--profile embedding --profile reranker`。
- 升级后立即用新镜像 ID 和 revision 更新交付记录。

### 5.3 回滚步骤

1. 把代码与镜像回到上一份已验证的 commit/tag（重新 `build`，或使用升级前记录的镜像 ID）。
2. 只在新版本 migration **不兼容**时才回退数据库；回退前必须已完成备份并经过评审。
   迁移服务可以覆盖命令，但这是高风险操作：

   ```bash
   docker compose -f deploy/compose.yaml --env-file .env run --rm migrate alembic downgrade <目标revision>
   ```

   注意：并非所有 revision 的 `downgrade()` 都实现了真正的结构回退（例如 merge revision
   `c4d5e6f7a8b9` 的 `downgrade()` 是空操作，
   `migrations/versions/c4d5e6f7a8b9_merge_skill_activation_and_exam_preparation.py:18-19`），
   因此**必须在恢复出的克隆库上先演练**再决定。
3. `stop` 新版本服务，用上一份镜像 `up --detach --wait`，再用 1.3 的命令确认 `ready`。
4. 禁止事项：不得用删除卷的方式回滚；不得手工 `UPDATE/DELETE/ALTER` 业务表“修数据”
   （`docs/cc2026-yizhi-handoff.md:152`）；不得在含数据的环境执行 `downgrade` 作为常规发布步骤
   （`docs/troubleshooting.md:72-73`）。

## 6. 临时文件与数据清理

### 6.1 QA debug trace（开发排查用，含正文）

| 项 | 事实 | 依据 |
| --- | --- | --- |
| 开关 | `QA_DEBUG_TRACE_ENABLED`，默认 `false` | `packages/infrastructure/src/infrastructure/config.py:137`、`deploy/compose.yaml:205` |
| 落盘路径 | 容器内 `/data/qa-debug`；宿主机 `${QA_DEBUG_TRACE_HOST_PATH:-../tmp/qa-debug}` | `deploy/compose.yaml:206`、`deploy/compose.yaml:239`、`.env.example:130-131` |
| 文件名 | 每个 Run 一个 `<run_id>.jsonl` | `packages/infrastructure/src/infrastructure/qa_debug_trace.py:103-105` |
| 大小上限 | `QA_DEBUG_TRACE_MAX_BYTES`，默认 `10000000`（10 MB），允许 `100000`–`500000000`；超限轮转为 `<run_id>.jsonl.1`（覆盖旧的 `.1`） | `packages/infrastructure/src/infrastructure/config.py:139`、`packages/infrastructure/src/infrastructure/qa_debug_trace.py:139-147` |
| 权限 | 目录 0700、文件 0600（尽力而为） | `packages/infrastructure/src/infrastructure/qa_debug_trace.py:110-112`、`packages/infrastructure/src/infrastructure/qa_debug_trace.py:151-152` |
| 生效环境 | 仅 `APP_ENV=development`；`production` 下即使误设开关也强制禁用 | `packages/infrastructure/src/infrastructure/qa_debug_trace.py:102` |
| 写入进程 | Worker（`packages/infrastructure/src/infrastructure/qa_execution.py:752`、`apps/worker/src/worker/assistant_tasks.py:374`）；Compose 只在 `worker` 服务挂载该目录 | `deploy/compose.yaml:206`、`deploy/compose.yaml:239` |
| 内容 | 完整问题、证据上下文、模型响应、Tool payload；**不得提交、上传或粘贴到 Issue** | `docs/troubleshooting.md:192-196` |

清理步骤：

```bash
# 1. 关闭开关（.env 中 QA_DEBUG_TRACE_ENABLED=false）后重建 worker
docker compose -f deploy/compose.yaml --env-file .env up --detach --force-recreate worker
# 2. 删除宿主机上的 trace 文件
rm -f tmp/qa-debug/*.jsonl tmp/qa-debug/*.jsonl.1
# 3. 确认目录为空且没有新增文件
ls -la tmp/qa-debug
```

`tmp/` 与 `data/` 都在 `.gitignore:88-89` 中，不要为了让 trace “可共享”而移出该目录。

### 6.2 blob 生命周期

- 上传：`LocalFileBlobStore.store()` 在 `/app/data/blobs/{source_id}/{hash[:2]}/{hash}` 写入原始字节
  （`packages/infrastructure/src/infrastructure/blob_store.py:23-25`、`packages/infrastructure/src/infrastructure/blob_store.py:38-42`），存储键完全由服务端生成，路径穿越会被拒绝
  （`packages/infrastructure/src/infrastructure/blob_store.py:85-102`）。
- 文档级删除：`DocumentDeletionService.delete_document()` 先把文档置为 tombstone（`deleted_at`）
  并创建一个 `DELETE` 摄入任务（`packages/application/src/application/ingestion/deletion.py:36-72`）；
  Worker 执行清理时删除该文档各版本的 Chunk，并在**同一 source 内没有其他活动文档引用同一
  blob hash** 时才删除 blob（`packages/application/src/application/ingestion/orchestrator.py:826-834`）。
  即 blob 按内容 hash 在 source 内共享，删除是引用计数式的。
- Space 删除：`DELETE /api/v1/spaces/{space_id}` 返回 204。实现先在数据库级联删除前收集该 Space
  可达的全部 blob key（包含已 tombstone 的文档，避免遗留孤儿），提交事务后再尽力清理
  （`apps/api/src/api/routers/spaces.py:60-79`、`packages/application/src/application/ingestion/deletion.py:75-130`）。
  `purge_blobs` 是 **best-effort**：文件系统失败只记录 `space_blob_purge_failed` 警告并跳过，
  `OSError` 不会回滚已经提交的删除（`packages/application/src/application/ingestion/deletion.py:114-130`），因此**删除 Space 后可能残留 blob 文件**。
  这些残留属于孤儿数据（数据库已无引用，不影响功能），需要人工核对与清理；仓库不提供自动对账
  工具，相关的引用对账脚本**需维护者补充**。
- 因为 tombstone 与清理是异步任务，删除文档后 blob 不会立即消失；确认清理完成应查看摄入任务状态
  与 Worker 日志，而不是立即检查文件。

### 6.3 其他临时文件与目录清理

| 对象 | 位置 | 清理方式 |
| --- | --- | --- |
| QA debug trace | `tmp/qa-debug/*.jsonl*`（宿主机） | 见 6.1；可随时删除 |
| blob 卷备份前解包残留 | `/app/data/blobs` | 使用 4.6 的恢复流程；不要在服务运行时手工删除正在被引用的文件 |
| pytest / 本地脚本输出 | `tmp/pytest-*` 等（`tmp/` 已忽略） | 直接删除目录内容 |
| Agent 工作区 | `data/workspaces` | 只有在确认没有会话引用该工作区后删除；删除后相关 Run 会 fail closed（`docs/troubleshooting.md:432-435`） |
| 个人 Skill | `data/personal_skills` | 通过应用接口管理，避免手工删除已被引用的版本（`docs/troubleshooting.md:346-350`） |
| 镜像与构建缓存 | 宿主机 Docker | `docker image prune` / `docker builder prune`（不影响卷） |

blob 卷内备份/恢复使用的打包命令（`tar` 是否存在于 api 镜像**需在目标机确认**，缺失时用 Python
内置 `tarfile` 兜底，命令失败会明确报错，不会静默产生空包）：

```bash
mkdir -p tmp/backup
STAMP=$(date +%Y%m%dT%H%M%S)
# 打包（输出到宿主机）
docker compose -f deploy/compose.yaml --env-file .env exec -T api \
  tar -czf - -C /app/data/blobs . > "tmp/backup/blobs-${STAMP}.tgz"
# tar 不可用时的兜底
docker compose -f deploy/compose.yaml --env-file .env exec -T api \
  python -c "import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz'); t.add('/app/data/blobs', arcname='.'); t.close()" \
  > "tmp/backup/blobs-${STAMP}.tgz"
# 校验与解包恢复（恢复前按 4.2 停止 api/worker 写入）
tar -tzf "tmp/backup/blobs-${STAMP}.tgz" | head
docker compose -f deploy/compose.yaml --env-file .env exec -T api \
  tar -xzf - -C /app/data/blobs < "tmp/backup/blobs-${STAMP}.tgz"
```

> 上述打包命令会以 `api` 容器内的运行用户读取 `blobdata`；卷内容的属主必须满足 3.2 的约束，
> 否则会因权限失败（失败会直接报错，不会生成空包）。

## 7. 观测

### 7.1 日志：stdout/stderr + request id

- 应用日志通过 `logging.StreamHandler` 输出，默认流为 **stderr**
  （`packages/infrastructure/src/infrastructure/logging_config.py:126-141`）；
  `docker compose logs` 同时收集 stdout 与 stderr。API 与 Worker 都不写日志文件。
- `LOG_FORMAT=json`（Compose 默认，`deploy/compose.yaml:76`、`deploy/compose.yaml:170`）时每行一个
  JSON 对象，字段：`timestamp`、`level`、`service`、`environment`、`logger`、`event`，
  以及存在时的 `trace_id`、`request_id`、`task_id`
  （`packages/infrastructure/src/infrastructure/logging_config.py:94-123`）。
- 关联标识：入口读取 `X-Trace-ID` 与 `X-Request-ID`；`X-Request-ID` 缺失或非法时自动生成 UUID4，
  并在响应头回传 `X-Trace-ID`/`X-Request-ID`
  （`apps/api/src/api/observability.py:22-23`、`apps/api/src/api/observability.py:47-59`、
  `packages/infrastructure/src/infrastructure/telemetry_context.py:41-45`）。
  Worker 在每个任务开始时绑定同一套上下文，日志里出现 `task_id`（Run/Task ID）
  （`apps/worker/src/worker/tasks.py:102`、`apps/worker/src/worker/qa_tasks.py:117`）。
  实际效果：**即使调用方不传 request id，日志中也一定有 request id**。
- 脱敏：`RedactionFilter` 对 `authorization`/`cookie`/`api_key`/`password`/`secret`/`prompt`/
  文档正文、`request_body` 等键做替换，并对消息做通用密钥模式替换
  （`packages/infrastructure/src/infrastructure/logging_config.py:14-22`、`packages/infrastructure/src/infrastructure/logging_config.py:75-88`）；只有
  `_SAFE_EXTRA_FIELDS` 白名单内的扩展字段会进入 JSON（`packages/infrastructure/src/infrastructure/logging_config.py:25-66`）。
  结构化日志不包含对话正文、prompt、文档内容或 Provider 原始响应
  （`README.md:160-166`）。
- 常用查询：

  ```bash
  docker compose -f deploy/compose.yaml --env-file .env logs --tail 200 api worker migrate postgres redis
  docker compose -f deploy/compose.yaml --env-file .env logs api | grep '"request_id":"<id>"'
  ```

  `trace_id` 同时用于跨 API→Worker 关联；响应体中的错误对象也包含 `trace_id`
  （`docs/cc2026-yizhi-handoff.md:106-107`）。

### 7.2 OpenTelemetry 开关

- 未设置 `OTLP_ENDPOINT` 时只创建本地 TracerProvider，不导出；设置后才挂载 `OTLPSpanExporter`
  （`packages/infrastructure/src/infrastructure/telemetry.py:55-79`）。endpoint 自动补 `/v1/traces`
  （`packages/infrastructure/src/infrastructure/telemetry.py:36-40`）。
- Compose 中 `OTLP_ENDPOINT` 默认空（`deploy/compose.yaml:77`、`deploy/compose.yaml:171`）；
  需要本地 trace 时启动 `otel` profile（`deploy/compose.yaml:262-271`、
  `docs/troubleshooting.md:258-267`），Compose 内通常用 `http://otel-collector:4318`。
- Collector 不可达不是启动依赖：导出失败有界（`OTEL_EXPORT_TIMEOUT_SECONDS` 默认 2 秒），
  API 关闭时会按该超时 `force_flush`（`apps/api/src/api/main.py:343-349`）。
  生产内网 override 不发布 Collector 端口（`deploy/compose.intranet.yaml:28-29`）。
- 生产观测后端地址与采集方式属于官网侧资源，**需在目标机确认**。

### 7.3 客户端限制查询

`GET /api/v1/config/limits`（`apps/api/src/api/main.py:539-549`）返回实时配置：

```json
{"max_upload_size_mb":50,"max_upload_size_bytes":52428800}
```

默认值来自 `MAX_UPLOAD_SIZE_MB`（默认 50，范围 1–500，`packages/infrastructure/src/infrastructure/config.py:86`）；nginx 侧上限设为 55m，
略高于后端限制，避免默认 1 MB 造成的 413（`deploy/nginx.conf:6-8`）。
生产内网下该路径同样需要网关凭据：`X-Internal-Service-Token` 与 `X-App-Scoped-User-Id`
（`apps/api/src/api/service_auth.py:29-30`、`docs/cc2026-delivery.md:39-41`）；
只有 `/api/v1/health/live` 与 `/api/v1/health/ready` 是公开探针
（`apps/api/src/api/main.py:430-437`）。

## 8. 常见运维操作速查表

以下命令默认在仓库根目录、`.env` 已配置的情况下执行。生产内网请在 `-f deploy/compose.yaml`
后追加 `-f deploy/compose.intranet.yaml`（无 GPU 时再加 `-f deploy/compose.cpu.yaml`）。

| 操作 | 命令 |
| --- | --- |
| 启动/更新（开发） | `docker compose -f deploy/compose.yaml --env-file .env up --build --detach --wait` |
| 启动/更新（生产内网，无 GPU） | `docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml -f deploy/compose.intranet.yaml --env-file .env --profile embedding --profile reranker up --build --detach --wait` |
| 停止（保留卷） | `docker compose -f deploy/compose.yaml --env-file .env down` |
| 停止但保留容器 | `docker compose -f deploy/compose.yaml --env-file .env stop` |
| 查看状态 | `docker compose -f deploy/compose.yaml --env-file .env ps` |
| 查看日志 | `docker compose -f deploy/compose.yaml --env-file .env logs --tail 200 -f api worker` |
| 重建单个服务（不重建卷） | `docker compose -f deploy/compose.yaml --env-file .env up --detach --force-recreate api worker` |
| 跑迁移 | `docker compose -f deploy/compose.yaml --env-file .env run --rm migrate` |
| 进入 PostgreSQL | `docker compose -f deploy/compose.yaml --env-file .env exec postgres psql -U app -d agent_knowledge` |
| 查看表 | `docker compose -f deploy/compose.yaml --env-file .env exec -T postgres psql -U app -d agent_knowledge -c '\dt'` |
| Redis 探活 | `docker compose -f deploy/compose.yaml --env-file .env exec -T redis redis-cli ping` |
| 健康检查 | `curl --fail http://127.0.0.1:8000/api/v1/health/live`、`curl --fail http://127.0.0.1:8000/api/v1/health/ready` |
| 列出项目卷 | `docker volume ls`、`docker system df -v` |
| 列出本项目容器 | `docker compose -f deploy/compose.yaml --env-file .env ps` |
| 列出所有 Compose 项目（核对项目名） | `docker compose ls` |
| 清理 QA trace | 见 6.1（先关开关、再删除 `tmp/qa-debug`） |
| 重置开发环境（会永久删除数据） | **仅在用户明确要求时**：`docker compose -f deploy/compose.yaml --env-file .env down --volumes --remove-orphans`；随后 `rm -rf tmp/qa-debug/* data/workspaces/*` 需另行确认 |

> **重要**：除用户明确要求永久删除数据外，**不要执行 `docker compose down --volumes`**。
> `down` 默认保留命名卷；`--volumes` 会永久删除 PostgreSQL、Redis 与 blob 数据
> （`AGENTS.md:59`、`docs/troubleshooting.md:301-304`、`README.md:133-145`）。
> 清理端口冲突或重建环境时，先用 `stop`/`down` 保留卷，不要用删卷解决。

## 9. 交付对照与待确认项

### 9.1 规范第 8 节对照

| 规范条目 | 本文档位置 |
| --- | --- |
| 日志输出 stdout/stderr，包含 request id，不记录正文和凭据 | 7.1 |
| 明确持久卷 | 3.1、3.2、3.3 |
| 明确备份 | 4.1–4.6（并如实说明仓库无自动备份） |
| 明确升级 | 5.1、5.2 |
| 明确回滚 | 5.3 |
| 明确临时文件清理方式 | 6.1、6.2、6.3 |
| 以非 root 用户运行 | 2.2、3.2 |
| 健康检查不依赖昂贵模型调用 | 1.3 |
| 密钥只通过环境变量或 secret 注入 | 2.2、2.3 |

### 9.2 需在目标机确认

- 生产 Compose 实际内容、官网网关与项目服务的私有网络接入方式（`docs/cc2026-yizhi-handoff.md:187`）。
- 备份落盘位置、保留周期、加密与异地存放；宿主机磁盘配额与告警阈值。
- 宿主机数据盘扩容方式与是否支持在线扩容。
- 卷名前缀（是否由 `scripts/start-local.ps1` 或 `-p` 指定项目名）与绑定挂载实际解析路径。
- api 镜像内是否存在 `tar`（否则固定使用 6.3 的 Python 兜底命令）。
- Windows/Docker Desktop、SELinux 主机上 uid `10001` 与绑定挂载属主映射。
- 生产 OTLP 后端地址与日志采集配置。
- 生产机 CPU 模式下的实际资源占用、首次模型下载、索引时延与并发（属规范第 8 节另一条要求，
  证据由官网侧留存，`docs/cc2026-yizhi-handoff.md:188`）。

### 9.3 需维护者补充

1. 自动备份与校验脚本、定时任务及恢复演练记录（仓库当前没有任何备份实现，见 4.1）。
2. blob 与数据库引用的对账工具，用于识别 Space 删除后可能残留的孤儿 blob（见 6.2）。
3. 生产 secret manager 清单与变量模板（当前只有 `.env.example` 占位示例）。
4. 磁盘/卷容量告警阈值与巡检频率。
5. 回滚演练记录，包含 `alembic downgrade` 的评审结论（部分 revision 的 `downgrade()` 为空操作，见 5.3）。
6. 冻结交付时写入实际 tag/commit 与三个镜像 ID（当前 HEAD 含未提交改动，见 5.1）。
