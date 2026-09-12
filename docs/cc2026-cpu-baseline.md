# 易知 CPU 模式资源基线（CC2026 交付记录）

本文记录一次可复现的 CPU 模式实测，用于规范第 8 节“提供典型输入的 CPU、内存、磁盘、时延
和并发实测”和第 7.5 节“先在测试机运行 CPU Compose，记录内存、磁盘、首次模型下载、典型索引
时间和并发”。

> **口径**：本次实测使用 `MODEL_PROVIDER=fake`（确定性 fake Chat/Embedding/Reranker），
> 度量的是服务、队列、数据库和文件存储的开销，**不包含 TEI 模型的推理与下载开销**。
> 真机启用 TEI 前必须按第 6 节命令在目标机复测，不能把本表当作模型容量结论。

## 1. 实测环境

| 项 | 值 |
| --- | --- |
| 主机 | WSL2（`Linux 6.6.87.2-microsoft-standard-WSL2`），Intel Core i9-14900HX，32 逻辑核 |
| 容器可见内存 | 15.47 GiB（`docker info` 报告 16 616 667 968 B） |
| Docker / Compose | Docker Engine 29.4.0，Docker Compose v5.1.2（Docker Desktop，overlayfs） |
| 部署组合 | `deploy/compose.yaml` + `deploy/compose.cpu.yaml` + `deploy/compose.intranet.yaml` |
| 覆盖文件 | 独立 project（`-p`）、独立镜像 tag、无公网端口、全新命名卷 |
| 模型配置 | `MODEL_PROVIDER=fake`，未启用 `embedding` / `reranker` profile |
| 并发设置 | `WORKER_PROCESSES=1`、`WORKER_THREADS=4`（Compose 默认值） |
| 备注 | 开发者本机另有 GPU 栈在运行，CPU/内存存在争用；下表时延偏保守 |

数据采集时间：2026-09-12；被测提交：见同批次交付 tag（`docs/cc2026-yizhi-handoff.md` 第 1 节）。

## 2. 内存占用（工作负载结束后的稳态）

`docker stats --no-stream`：

| 容器 | 内存 | 占可见内存 |
| --- | --- | --- |
| `worker` | 268.6 MiB | 1.70% |
| `api` | 196.2 MiB | 1.24% |
| `postgres` | 58.1 MiB | 0.37% |
| `redis` | 6.1 MiB | 0.04% |
| **合计** | **约 529 MiB** | 约 3.4% |

未启用 TEI 时，首期知识工作流的常驻内存低于 1 GiB。启用本地 Embedding/Reranker（TEI CPU）
后需额外计入模型常驻内存与首次下载，见第 6 节。

## 3. 磁盘占用

| 对象 | 大小 | 说明 |
| --- | --- | --- |
| `cc2026cpu-api:local` 镜像 | 582 MB | 含 Python 运行时与锁定依赖 |
| `cc2026cpu-worker:local` 镜像 | 519 MB | 与 API 共享大部分层 |
| `pgdata` 卷 | 70.27 MB | 迁移建表后的卷占用（`pg_database_size` = 13 MB） |
| `blobdata` 卷 | ~0 B（目录 76 KB） | 工作负载结束后测试 Space 已删除，blob 被清理 |
| `redisdata` 卷 | 869.5 KB | AOF 队列数据 |

注意：删除 Space 会清理其 blob（`SpaceDeletionService`），因此“上传测试数据后删除”不会留下
可观测的磁盘增量；容量规划应按真实留存资料量估算 `blobdata`。

## 4. 时延（单实例，fake provider）

典型输入：Markdown 文档约 18 KiB；问题为针对该文档的中文知识问答。

| 操作 | 中位数 | 最大 | 说明 |
| --- | --- | --- | --- |
| 上传（`POST .../upload`） | 22 ms | 24 ms | 含 SHA-256、Blob 落盘与文档登记 |
| 摄入（上传→任务 `succeeded`） | 1 288 ms | 1 716 ms | 含 Worker 解析、分块、fake 向量与发布 |
| 提问（提交→Run 终态） | 1 594 ms | 1 803 ms | 含队列、检索、fake 生成与引用落库 |

## 5. 并发（4 个问题同时在飞）

| 指标 | 值 |
| --- | --- |
| 4 个问题全部完成的总墙钟时间 | 4 838 ms |
| 单个问题耗时（4 并发） | 2 971 / 2 987 / 3 175 / 4 836 ms |
| 结果正确性 | 4/4 `completed`，每条回答均带 1 条可定位引用 |

`WORKER_THREADS=4` 时 4 并发问题可全部完成；耗时随并发近似线性增长，说明瓶颈在单实例
Worker 线程池而不是队列。首期官网按“成员偶发提问”量级设计即可，需要更高并发时优先增加
`WORKER_PROCESSES`/`WORKER_THREADS` 并复测数据库连接数。

## 6. 目标机必须复测的项（当前未覆盖）

1. **TEI CPU 首次模型下载时间与缓存体积**：`Qwen/Qwen3-Embedding-0.6B`（约 1.2 GB）与
   `BAAI/bge-reranker-v2-m3`（约 2.2 GB，revision 已固定为
   `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`）。
2. **真实向量下的摄入时延与常驻内存**：fake 向量不产生 CPU 推理开销，本表的 1.3 s 摄入
   时延不能外推到 TEI。
3. **Reranker 开启后的提问 P50/P95**：默认检索路径为 `dense_rerank`。
4. **50 MiB 上限附近的上传内存峰值**：当前实现按 `Content-Length` 先做 413 预检，但仍在
   内存中缓冲整个文件。
5. **与官网同机部署的可行性结论**：需用上面数据对比生产机剩余 CPU/内存/磁盘。

复测命令（在目标机执行，替换 `--env-file`）：

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml \
  -f deploy/compose.intranet.yaml --env-file .env \
  --profile embedding --profile reranker up --build --detach --wait

docker stats --no-stream
docker compose -f deploy/compose.yaml -f deploy/compose.cpu.yaml \
  -f deploy/compose.intranet.yaml --env-file .env exec -T \
  -e SMOKE_TOKEN="$INTERNAL_SERVICE_TOKEN" api \
  python - < examples/first_phase_smoke.py
```

首次下载模型时记录 `tei` / `tei-reranker` 容器从启动到 `/health` 通过的时间，以及
`docker system df -v` 中 `teidata` / `rerankerdata` 卷的大小。

## 7. 复现方式

```bash
# 1. 隔离部署（不使用开发者本机的卷）
docker compose -p cc2026cpu --env-file .env \
  -f deploy/compose.yaml -f deploy/compose.cpu.yaml -f deploy/compose.intranet.yaml \
  up --build --detach --wait postgres redis migrate api worker

# 2. 正确性验收（约 1 分钟）
docker compose -p cc2026cpu --env-file .env \
  -f deploy/compose.yaml -f deploy/compose.cpu.yaml -f deploy/compose.intranet.yaml \
  exec -T -e SMOKE_TOKEN="$INTERNAL_SERVICE_TOKEN" api \
  python - < examples/first_phase_smoke.py

# 3. 资源采样
docker stats --no-stream
docker system df -v
```

本表的时延/并发数字来自一次性实测脚本（3 篇文档、3 次顺序提问、1 轮 4 并发），不是持续
基准；CI 只保证功能正确性（见 `.github/workflows/ci.yml` 的 `Website profile smoke`）。
