# 阶段 3 验收记录

> 验收日期：2026-07-23
>
> 结论：阶段 3 Step 10 的工程集成验收与文档移交完成；阶段 0 和阶段 2 已分别按
> `docs/stage-0-acceptance.md`、`docs/stage-2-acceptance.md` 正式交接，但阶段 3 的真实模型定版和
> holdout 质量门槛仍未关闭，因此本记录不宣称阶段 3 正式退出。

## 验收范围

本次验收覆盖阶段 3 已实现的摄入/发布边界、检索 Application/API、模型故障策略、Space 与
版本隔离、日志隐私、评测门禁和运行文档。测试使用确定性 fake 或隔离 PostgreSQL/Redis；
未向外部 Provider 发送私有语料。阶段 4 的查询改写、引用绑定、问答、Conversation、SSE 和
`knowledge_qa` Skill 不在本次范围内。

## 环境与安全边界

- Python 3.12、uv 0.11.x、Node 24、pnpm 10.20.0。
- PostgreSQL 16 + pgvector、Redis 7 + AOF；每次集成测试使用独立数据库/Redis 实例。
- 默认 `MODEL_PROVIDER=fake`。本地模型服务使用 Compose `embedding`/`reranker` profile，镜像
  digest 和模型 revision 固定；`MODEL_ALLOW_EXTERNAL=false`。
- 评测配置为 `provisional`，报告只写入被忽略的 `tmp/retrieval-eval-*.json`。

## 2026-07-29 冻结语料 development 复核（未通过）

阶段 0 和阶段 2 正式交接后，使用冻结 `knowledge-qa-v0`、本地 Qwen3 Embedding 与
BGE Reranker 重新执行了完整 development 消融。评测协议显式固定
`included_source_formats=[markdown,text,pdf]`，与 MVP/阶段 2 的 P0 Parser 边界一致；依赖
Code/Notebook P1 来源的 16 个 development case 不进入 P0 指标，无证据安全 case 仍全部执行。
validation 记录原始 development 127 例、P0 纳入 111 例；配置保持 `provisional`，config hash 为
`f244a026b1a54769425334f3aee28424c9ca669ae7fb0c09c0ee25bd5e8b1a4c`。

准备阶段修复了三个真实缺陷：新 PostgreSQL backend 在首次向量 SQL 前未注册
`ivfflat.probes`；自然语言 FTS 把全部词项以 AND 组合导致 Keyword Recall 为 0；结构分块器会把
空白结构节点生成空 Chunk 并发送给 TEI。修复后隔离评测库有 74 个 P0 published 版本、6085 个
Chunk，向量完整率为 6085/6085；三个此前 pending 的 Markdown 来源均恢复发布。IVFFlat 不再出现
100% infrastructure failure，Keyword Recall@5 从 0 提升到 30%。

最终同 bundle development 报告为 `tmp/stage3-development-final-p0.json`：

| 路径 | Recall@5 | MRR | P95 | failure rate | must-exclude |
| --- | ---: | ---: | ---: | ---: | ---: |
| Keyword | 30.00% | 0.2917 | 86.5 ms | 0% | 0 |
| Dense exact | **51.90%** | **0.4822** | 455.0 ms | 0% | 0 |
| Dense IVFFlat | 44.29% | 0.4191 | 454.2 ms | 0% | 0 |
| Hybrid | 42.86% | 0.4106 | 452.2 ms | 0% | 0 |
| Hybrid + BGE Reranker（top 10 -> top 5） | 48.57% | 0.4662 | 3523.9 ms | 0% | 0 |

结论：development 正式复核已执行，但阶段 3 质量门禁未通过。最佳 Recall@5 低于 85%；Reranker
相对最佳单路 Dense 没有净收益，且 P95 超过 1000 ms 预算。深度诊断显示 P0 Dense 候选
Recall@100 为 90.48%，说明下一轮需要版本化改进候选到最终 top 5 的排序/查询策略，而不是继续
无边界调整 RRF 权重。`retrieval-v1.yaml` 必须保持 `status: provisional` 和
`formal_runs_enabled: false`，本轮未执行、读取或选择性重跑 holdout，阶段 3 仍未正式退出。

## 实际命令与结果

### 工程与契约检查

```powershell
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest
$env:RUN_INTEGRATION='1'; uv run pytest tests/integration
uv run alembic upgrade head
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
```

结果：`ruff format --check`（122 个文件）、`ruff check`、`mypy apps packages` 通过；默认后端
测试 `479 passed, 41 skipped`；隔离 PostgreSQL/Redis 集成测试 `40 passed`；前端 lint、
typecheck、测试 `12 passed` 和 production build 通过；OpenAPI 导出后无 diff。pytest 仍有
Windows 沙箱无法写 `.pytest_cache` 的警告，不影响结果。

### Compose 与模型 profile

```powershell
$env:APP_SECRET_KEY='local-only-secret'
$env:POSTGRES_PASSWORD='local-only-password'
docker compose -p stage3-step10-test -f deploy/compose.yaml up --build --detach --wait
docker compose -p stage3-step10-test -f deploy/compose.yaml ps
docker compose -p stage3-step10-test -f deploy/compose.yaml down
```

结果：基础 Compose 冷启动构建并通过 `--wait`，migrate 正常退出，API、Worker、Web、
PostgreSQL 和 Redis 均 healthy。使用独立端口 `58000/55173/55432/56379` 的 HTTP smoke 返回
API/Web `ready`、`POSTGRESQL_OK`、`REDIS_OK` 和 `MODEL_FAKE_READY`；SQL 注入样例返回
`RETRIEVAL_SPACE_NOT_FOUND`，响应和 API 日志均不含查询 marker。重启 PostgreSQL/Redis 后
迁移仍为 `d4e5f6a7b8c9 (head)` 且 readiness 恢复。

模型 profile 首次启动两次均在 Hugging Face 固定 revision 的 `config.json` 下载处因
`unexpected EOF` 退出；没有修改 digest、revision 或外发策略。使用此前成功下载的
`stage3-step2-model-test_teidata` 和 `stage3-step7-test_rerankerdata` 固定缓存卷，并以
`--network none` 启动同一 TEI 镜像和 revision，Embedding、Reranker 容器均保持运行并报告
`Ready`，缓存后离线路径通过。模型服务失败时基础 API/Worker/Web 仍健康，管理面 readiness
不受阻断。不得移除 digest、改用 `latest` 或开启外部 Provider 绕过下载问题。

## Step 10 验收矩阵

| 项目 | 证据 | 结论 |
| --- | --- | --- |
| 摄入、发布、修改重建、原子切换、删除撤下、再次检索 | `tests/integration/test_stage3_retrieval_lifecycle.py`、`tests/unit/test_ingestion_orchestrator.py` | 隔离 PostgreSQL 全生命周期通过；正式质量结论仍受阶段 2 Step 9 门禁限制 |
| Keyword/Dense/Hybrid/Hybrid+Reranker | `tests/unit/test_retrieval_search.py`、`tests/integration/test_search_api.py` | 四种路径及稳定错误/降级策略有覆盖 |
| Space、当前发布版本、tombstone、伪造 filter | `PostgresRetrievalStore` 集成测试、Search API 隔离测试 | 过滤边界在召回前和 Adapter 内重复强制 |
| 恶意文档、SQL 注入、超长查询、日志泄漏 | 检索查询归一化、参数化 SQL、请求校验和 `test_retrieval_logging.py` | 未发现正文、查询或密钥进入日志/报告 |
| Embedding/Reranker 故障 | fake `UNAVAILABLE`/`TIMEOUT` 契约测试和 API 错误映射 | Dense 明确失败；Hybrid 仅按 profile 降级；离线评测不静默回退 |
| 离线评测与隐私扫描 | `scripts/evaluate_retrieval.py`、`tests/unit/test_retrieval_evaluation_cli.py` | 报告 schema 与 provisional holdout 门禁确定性可复现 |
| Compose 冷启动、缓存启动、保留卷重启 | Compose 命令与健康检查输出 | 基础栈冷启动、缓存卷禁网启动和保留卷重启已通过；全新模型卷首次下载因 `unexpected EOF` 未通过，需在可复现网络环境补证 |

## 阶段 3 评测摘要

Step 9 的 development 工程评测使用确定性 fake Embedding：Keyword Recall@5=0，Dense exact、
Hybrid 和 Hybrid+Reranker 均为 0.1081；IVFFlat 因基础设施错误归因而未计入质量通过。所有
报告 `formal_run_eligible=false`，holdout 因阶段 0/阶段 2 Step 9 门禁被拒绝。上述结果只说明
评测流程和失败分类可运行，不是 MVP 的 85% Recall@5 质量结论。

### 2026-07-25 Qwen3 development 全量消融（provisional）

在未读取、未执行 holdout 的前提下，对 `knowledge-qa-v0` 的全部 20 个 development case
执行了本地真实模型消融。语料当时尚未完成 Stage 0 冻结，且阶段 2 Step 9 也未正式
验收；即使现在 Stage 0 已内部冻结，本节仍只能用于 provisional 默认方案选择，不能关闭 R3-02～R3-06 或宣称阶段 3
正式退出。原始报告和向量缓存包含继承自 `private_local` 的派生数据，仅保存在被忽略的 `tmp/`，
没有提交问题、正文、locator 明细或向量。

实验固定模型与处理身份如下：

- Parser/Chunker `1.1`，共生成 2,349 个 Chunk；修复 Markdown 嵌套列表、blockquote 和规范化
  多行节点 locator 后，37 个 evidence unit 的 locator 覆盖从 `34/37` 提升到 `37/37`。
- Embedding 为 `Qwen/Qwen3-Embedding-0.6B`，revision
  `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`，输出 768 维 float32，并对文档/查询向量执行
  L2 归一化。查询指令 identity 为 `qwen3-web-search-v1`，文档不加前缀，identity 为
  `qwen3-document-no-prefix-v1`。
- Reranker 为 `BAAI/bge-reranker-base`，revision
  `2cfc18c9415c912f9d8155881c133215df768a70`。

development 的候选与最终结果如下：

| 方案 | Recall | MRR | nDCG | 全证据覆盖 | must-exclude | 失败率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dense@5 | 75.68% | 0.7176 | 0.5862 | 55.56% | 0 | 0% |
| Dense@30（候选阶段） | 97.30% | 0.7176 | 0.6313 | 94.44% | 0 | 0% |
| Keyword@5 | 0% | 0 | 0 | 0% | 0 | 0% |
| Hybrid + 每文档最多 3 块 @5 | 75.68% | 0.7176 | 0.5870 | 55.56% | 0 | 0% |
| Hybrid + quota + Reranker@10 -> @5 | **75.68%** | **0.8167** | **0.6284** | **55.56%** | **0** | **0%** |

最终 provisional 方案为：Parser/Chunker `1.1` -> Qwen3 Dense@30 -> 每文档最多 3 块 ->
BGE Reranker 重排前 10 -> 返回前 5。Dense@30 在 37 个 evidence unit 中命中 36 个；唯一未进入
候选集的是 `qa-009` 的 SVM evidence unit（dense rank 70）。Parser 修复前后 Dense Recall@5
从 64.86% 提升到 75.68%，Dense Recall@30 从 89.19% 提升到 97.30%。

`fusion_alpha` 的预注册值 `0.35/0.5/0.65` 在本次数据上完全持平，因为 Keyword 召回为 0；
当前采用 `0.35` 只是稳定的 provisional 选择，不能解释为已证明的融合权重优势。最终 Recall@5
仍低于正式门槛 85%，且没有有效 P50/P95 在线延迟数据，所以不得切换配置状态为 `frozen`，
`formal_runs_enabled` 继续保持 `false`。

资源测试显示 Qwen3 使用 TEI 的 `max-batch-tokens=512`、`max-client-batch-size=4`、
`max-batch-requests=1`、`RAYON_NUM_THREADS=8` 时吞吐最好；修复后语料编码约 1.151 Chunk/s，
内存约 3.2～3.3 GiB。Reranker 约占 7.9 GiB；离线报告生成时应与 Qwen3 串行运行以降低峰值
内存。扩大 Qwen3 batch 或并发在本机反而降低吞吐，因此 Compose 保留上述实测限制。

仓库已将新建 Space、空 profile 和完全等于旧生成默认值的 Space 更新为上述检索 profile，Search
API 的缺省模式改为 `hybrid_rerank`，Compose `embedding` profile 改为固定 revision 的 Qwen3。
真实在线环境还必须配置对应 instruction identity 与 L2 normalization，并以新的
`embedding_version` 全量重建候选 DocumentVersion；在重建并发布前，旧向量不会与新查询向量混用。
## 退出条件与未关闭项

已完成：检索四种模式、固定 profile/Embedding/索引版本、Search Application Port、稳定错误
协议、OpenAPI、隐私日志边界和阶段 4 移交文档。

未完成：冻结 holdout 上的 Recall@5、Reranker 净收益和 P95 预算证明；在全新阶段 3 模型卷上完成真实模型首次下载并验证
Compose profile（既有固定缓存卷的禁网启动已通过，但不能替代全新卷的首次下载证据）。

## 阶段 0 关闭后的正式完成清单

阶段 0 的 `frozen` 状态只是解除真实语料门禁。下面的清单是从当前工程验收状态到阶段 3
正式退出的唯一建议顺序；每一项都应保存命令、摘要、哈希和责任人，不能用 fake 或合成 fixture
替代。

| 顺序 | 必须完成的工作 | 通过证据 | 阻塞时的处理 |
| --- | --- | --- | --- |
| 1. 门禁交接 | 阶段 0 退出记录、manifest `status=frozen`、内部分发范围、敏感度/allowed uses、人工标注、source SHA-256 | `docs/stage-0-acceptance.md` + manifest/hash 校验输出（已完成） | 保持 provisional，不读未批准来源 |
| 2. 阶段 2 正式验收（已完成） | 全部批准 P0 来源的解析、定位、摄入、幂等、修改、原子发布、删除、失败恢复；解析成功率至少 95% | `docs/stage-2-acceptance.md`、100% 解析质量报告和隔离 E2E 输出 | 已关闭 |
| 3. 评测集冻结 | 冻结 dataset、schema、development/holdout、evidence locator 和指标；v0 的 30 例必须明确接受统计限制或发布新版本扩充到 60～100 例 | corpus/dataset/schema/split SHA-256 与决策记录 | 新增用例只能产生新 dataset version，不能修改已查看 holdout |
| 4. 真实模型定版 | 空缓存/缓存后离线启动、固定 revision、768 维、指令、归一化、精度、Provider 策略；解决当前 `bge-base-zh-v1.5` 与计划候选模型记录不一致 | 本地模型 smoke、离线启动日志、R3-03/R3-05 决策 | 无法复现时不得以 fake 结果开启正式评测 |
| 5. Development 消融 | Keyword、Dense exact/approx、Hybrid、Hybrid+Reranker 和预注册参数集合；记录每 case 阶段、语言/安全切片、失败分类、P50/P95 分段耗时 | `retrieval-report-v1` development 报告和复现实命令 | IVFFlat 失败需修复并重测，或明确 exact 默认及计划后果 |
| 6. 默认配置冻结 | 只激活一个 Embedding、一个 `RetrievalProfileV1`、一个索引默认路径；若 Reranker 无净收益，先更新总计划门槛 | config hash、模型/profile/index 摘要、R3-02～R3-06 关闭记录 | 不得查看 holdout 后调参或静默关闭 Reranker |
| 7. 正式 holdout | provisional 配置改为 frozen，`formal_runs_enabled=true`，隔离数据库执行 `--validate-only` 后只运行一次 `--confirm-holdout <config_hash>` | 完整 holdout report、人工摘要、隐私扫描 | 配置/基础设施降级则整次作废，保存原因，不拼接结果 |
| 8. 正式退出与移交 | Recall@5 ≥85%、精排相对最佳单路提升、P95 达标、权限/撤下/版本违规为 0，报告和文档归档 | Stage 3 正式验收结论、`cases/evals/reports/` 安全报告、阶段 4 Port 摘要 | 任一失败则保持工程完成状态，回到 development 或更新版本化门槛 |

### 建议执行命令骨架

```powershell
# 阶段 0 / 阶段 2 交接后先做只读校验
Get-FileHash cases/evals/corpus/v0/manifest.yaml -Algorithm SHA256
$env:EVALUATION_DATABASE_ISOLATED='1'
uv run python scripts/evaluate_retrieval.py `
  --config cases/evals/configs/retrieval-v1.yaml `
  --split development --validate-only

# 仅在 Stage 0、Stage 2 Step 9、配置和模型均正式冻结后
uv run python scripts/evaluate_retrieval.py `
  --config cases/evals/configs/retrieval-v1.yaml `
  --split development --prepare-corpus --output tmp/retrieval-eval-development.json

# 先从 development 报告读取并核对 config_hash，再执行一次 holdout
uv run python scripts/evaluate_retrieval.py `
  --config cases/evals/configs/retrieval-v1.yaml `
  --split holdout --confirm-holdout <config_hash> `
  --output tmp/retrieval-eval-holdout.json
```

`--prepare-corpus` 只能指向隔离数据库和 manifest 允许来源；`tmp/` 报告提交前必须通过隐私
扫描。当前脚本对阶段 2 验收路径只做“文件存在”检查，正式启用前必须人工确认其内容和通过
结论，不能把创建空文件当作关闭门禁。

## 阶段 4 移交

阶段 4 只能通过 `SearchService.search(SearchRequest, RetrievalProfileV1)` 获取带 Space、
来源、文档版本、Chunk、locator、分数和 `matched/context_only` 标记的 `SearchResult`。
它不得读取检索 ORM 表、复制 FTS/向量 SQL、RRF、精排或过滤逻辑。默认 profile、Embedding
identity、索引版本、错误码和降级语义均由 `packages/domain` 与 `packages/application` 的
稳定 Port 提供。

## 外部确认与已知限制

- 阶段 0 和阶段 2 Step 9 的正式门禁已关闭；仍需完成真实模型 development 消融和默认配置
  冻结，才能按上方清单把 `retrieval-v1.yaml` 从 provisional 切换到正式运行并执行一次 holdout。
- Docker Engine、私有模型缓存和真实批准语料不在本次仓库变更中；没有实际输出的命令不得标记
  为通过。
- Web 目前仍是数据源管理界面；搜索 API 已交付，问答和引用 UI 留给阶段 4。
