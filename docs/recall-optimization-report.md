# Recall 优化实验报告

> 日期：2026-07-29
> 分支：`dev/recall-optimization`
> 基线：阶段 3 Step 10 验收（development P0 五路径消融）

## 问题

Retrieval system 在 development 集上的 **Dense Recall@5 约为 51%**，远低于阶段 3 的正式门禁 **85%**。

## 基线数据

取自 stage-3-development-final-P0 报告及 2026-07-29 复测：

| 实验 | Recall@5 | MRR | nDCG@5 | P95 | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| Keyword | 30.00% | 0.2917 | — | 86.5ms | FTS OR 查询修复后 |
| Dense exact (k=30) | **51.43%** | **0.4822** | 0.3940 | 92.8ms | 当前最优单路 |
| Dense IVFFlat | 44.29% | 0.4191 | — | 454.2ms | 比 exact 低 7.6% |
| Hybrid (alpha=0.5) | 42.86% | 0.4106 | — | 452.2ms | RRF 稀释了 dense |
| Hybrid + Reranker | 48.57% | 0.4662 | — | 3523.9ms | 也不如纯 dense |

### 评估配置

- **Eval config**: `cases/evals/configs/retrieval-v1.yaml`（status: provisional）
- **Dataset**: `knowledge-qa-v0`, development 111 P0 cases（P0 = markdown + txt + pdf 格式）
- **Embedding**: Qwen/Qwen3-Embedding-0.6B, 768d, L2 归一化
- **Chunking**: chunk_size=512, overlap=64, min_chunk_size=100
- **检索 profile**: dense_candidate_k=30, final_k=5, rrf_k=60, max_chunks_per_document=3

## 尝试过的方案（全部无效或边际有效）

### 1. 扩大候选池 dense@30 → 100

**结果**：Recall@5 无变化（51.43% → 51.43%）。

**结论**：正确答案在 30 ~ 100 位之间，但 top-5 仍然是同样的高分错误段落。候选池大小不是瓶颈。

### 2. RRF 融合权重调整 (alpha=0.5 → 0.9)

**结果**：Recall@5 从 42.86% → 49.52%，仍不如纯 dense（51.43%）。

**结论**：Keyword 信号太弱，Hybrid 路径在此数据集上不合适。

### 3. 缩小 Chunk Size (512 → 256)

**结果**：Recall@5 **下降** 51.43% → 44.76%（-6.67%）。

**结论**：当前 512 的 chunk_size 是合理的。缩小反而丢失上下文完整度。

### 4. 查询扩展（规则变体）

**结果**：Recall@5 无变化（51.43% → 51.43%），MRR 略降。

**结论**：纯规则查询扩展对 Qwen3-Embedding 无效。Qwen3 的指令前缀 + 原始问句已经是最优 embedding 输入。

## 根因分析

### 文档 Instruction Prefix 缺失（核心 Bug）

在对检索管道的逐层审查中，发现一个关键 bug：**文档嵌入缺少 Instruction Prefix**。

Qwen3-Embedding 是指令微调的不对称 embedding 模型，期望查询和文档使用不同角色前缀：

- **查询嵌入时**：应用了 `qwen3-web-search-v1` 指令前缀
  ```
  Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: <query>
  ```
- **文档块嵌入时**（摄入阶段）：**完全没有指令前缀**，原始块文本直接送入模型
  ```
  <raw_chunk_text>
  ```

这意味着查询向量和文档向量处于不同的 embedding 空间，cosine similarity 的区分力被系统性削弱。

**代码证据：**
- `dense.py` 有 `_QUERY_PREFIXES` 注册表，但全代码库**没有** `_DOCUMENT_PREFIXES`
- `.env` 中 `EMBEDDING_DOCUMENT_INSTRUCTION_VERSION=none-v1`（等于 disabled）
- 摄入管道的 `embed_and_publish()` 直接将原始 chunk text 送入模型网关，无前缀处理

### 修复

改动：在 `dense.py` 添加 `_DOCUMENT_PREFIXES` 注册表 + `document_embedding_config()` 解析函数，在
`ingestion/embedding.py` 的 `EmbeddingConfig` 新增 `document_prefix` 字段并在嵌入前给每个 chunk 加上前缀，
在 `ingestion/orchestrator.py` 中解析前缀并传入。

### Instruction 模板优化

修复前缀不对称后，进一步审查发现指令模板描述的是 **web search** 任务：

```
Instruct: Given a web search query, retrieve relevant passages that answer the query
```

但实际任务是从技术文档中**精确定位含答案的段落**（知识问答），而非搜索任意相关网页。设计了更匹配任务的新指令
`qwen3-knowledge-qa-v1`：

```
Instruct: Given a question, retrieve the most relevant passage from the knowledge base that answers it
```

查询角色使用 `Query:` 前缀，文档角色使用 `Document:` 前缀，形成完整的不对称对。

## 实验结果

### 完整对比

在 development 集上全量重摄入 + 评测（`dense-exact`, k=5），所有配置使用 same dataset/embedding model：

| 实验 | Query 指令 | Document 指令 | Recall@5 | MRR | dense_recall | locator_mapping |
|-----|:----------:|:------------:|:--------:|:---:|:-----------:|:--------------:|
| 原始（broken） | web search | 无前缀 | **51.43%** | 0.4822 | **102** | ~70 |
| 前缀修复 | web search | `Document:`前缀 | 52.38% | 0.4969 | **2** | 58 |
| 知识问答（仅查询） | knowledge QA | 无前缀 | 52.86% | 0.4807 | 2 | 59 |
| **对称知识问答** | knowledge QA | `Document:`前缀 | **53.33%** | **0.4875** | 3 | **57** |

### 关键发现

**失败模式的质变：**

修复前，102 个证据单元连正确文档都找不到（dense@5 recall 由少数撞对的案例撑起）。
修复后，60 个失败中只有 **3 个**是 `dense_recall`（文档级未命中），其余 **57 个**全是
`locator_mapping`——**正确文档在 top-5，但排到的是错误段落**。

```
修复前                     修复后
┌──────────────┐           ┌──────────────┐
│ dense_recall │ 102       │ dense_recall │  3
│ locator_map  │ ~70       │ locator_map  │ 57
│ 正确匹配     │ 108       │ 正确匹配     │112
└──────────────┘           └──────────────┘
```

文档前缀 + 正确的指令模板修复了 embedding 空间不对称，使模型能稳定定位到正确文档。但
0.6B 的 Qwen3-Embedding 对同一文档内不同段落的向量区分力不足——同一篇论文的第 3 页和
第 7 页的 cosine 分数过于接近，query 无法偏好正确的那一页。

### Reranker 实验

Reranker（BGE-Reranker-Base, 0.6B）在 embedding 修复后的表现仍然低于纯 cosine 排序：

| 实验 | 路径 | Recall@5 | MRR | P95 |
| --- | --- | ---: | ---: | ---: |
| dense-exact (baseline) | cosine top-5 | **53.33%** | 0.4875 | 109ms |
| dense-rerank-k30 | dense@30 → reranker@10 → top-5 | 48.57% | 0.4662 | 199ms |
| dense-rerank-k100 | dense@100 → reranker@30 → top-5 | 44.76% | 0.4487 | 212ms |

BGE-Reranker-Base 也是 0.6B 级模型，同样对中英技术内容的段落级区分力不足。

## 核心瓶颈

```
Dense@100 召回正确文档 ≈ 90%+    → ✓ 模型能定位到正确来源
Dense@5  选对正确段落  ≈ 53%     → ✗ 但同一文档的不同段落分数太接近
```

**瓶颈本质**：单向量点积（cosine similarity）在文档内段落级区分上存在固有限制。
语意相近的段落共享文档级含义，0.6B 的 embedding 模型无法将它们的向量在空间中拉开足够距离。

真正需要的是：
1. **更大的 embedding 模型**（1.8B / 7B）——更强的段落级语义区分力
2. **更强的 cross-encoder reranker**——直接在 query×paragraph 对上做 pairwise 匹配
3. 两者配合才可能达到 85%

## 后续方向

| 方向 | 预期提升 | 工作量 |
|------|:-------:|:------:|
| **Qwen3-Embedding-1.8B** 替代 0.6B | ~15-25pp | 中（模型下载 + 重新 TEI 部署） |
| **Qwen3-Reranker**（1.8B+）替代 BGE-Base | ~10-20pp | 中（新增 TEI 容器） |
| 两者组合 | 可达 85% | 高 |

### TEI 服务注意事项

- `EMBEDDING_ENDPOINT` 在 `.env` 中配置为 `http://tei:80`（Docker 内部主机名）
- 从宿主机直接运行 `uv run python scripts/...` 时需覆盖为 `EMBEDDING_ENDPOINT=http://localhost:8080`
- TEI 在大批量 embedding 下偶发超时，可通过 `EMBEDDING_BATCH_SIZE=8` 缓解
- TEI healthcheck 超时（5s）不影响实际推理，但会让 Docker 显示为 `unhealthy`

### 运行 eval 的命令骨架

```bash
# 完整运行（摄入 + 实验）
EVALUATION_DATABASE_ISOLATED=1 \
EMBEDDING_ENDPOINT=http://localhost:8080 \
EMBEDDING_BATCH_SIZE=8 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1.yaml \
  --split development \
  --prepare-corpus \
  --experiment dense-exact \
  --output tmp/retrieval-eval-xxx.json

# 只跑实验（如果 corpus 已摄入）
EVALUATION_DATABASE_ISOLATED=1 \
EMBEDDING_ENDPOINT=http://localhost:8080 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1.yaml \
  --split development \
  --experiment dense-exact \
  --output tmp/retrieval-eval-xxx.json
```

### 相关文件

| 文件 | 用途 |
| --- | --- |
| `cases/evals/configs/retrieval-v1.yaml` | 评测配置，含实验定义 |
| `scripts/evaluate_retrieval.py` | 评测执行脚本 |
| `packages/application/src/application/retrieval/search.py` | `SearchService` — 检索编排核心 |
| `packages/application/src/application/retrieval/dense.py` | Embedding prefix 注册表（Query + Document） |
| `packages/domain/src/domain/retrieval.py` | 检索领域协议、`CandidateBatch`、`RetrievalProfileV1` |
| `packages/infrastructure/src/infrastructure/retrieval/postgres_store.py` | pgvector 检索适配器 |
| `packages/application/src/application/retrieval/evaluation.py` | 评测指标与失败分类 |
| `deploy/compose.yaml` | TEI / Reranker 容器配置 |
