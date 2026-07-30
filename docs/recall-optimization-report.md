# Recall 优化实验报告

> 日期：2026-07-30
> 分支：`dev/recall-optimization`
> 基线：阶段 3 Step 10 验收（development P0 五路径消融）
> **最终结果：88.57% ✅ 已超过 85% 门禁**

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

### 指标说明

**Recall@5 是 chunk-level recall**（`sum(matched_chunks) / sum(gold_chunks)` across all evidenced
cases），不是 case-level 的全覆盖率。平均每个 answerable case 有 2.08 个 gold chunks，系统能
找到其中约 1.1 个。Case 全覆盖率（所有 gold chunks 都找到）约为 41%。

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

### 深度失败分析

经过对最新结果（53.33%）的逐 case 分析，发现瓶颈不在文档级，而在 chunk 级：

| 指标 | 值 | 含义 |
|------|:--:|------|
| **文档级 Recall@5** | **97.0%** | 模型找到正确文档的能力已接近天花板 |
| 正确文档在 dense@rank #1 | 91% | dense 检索极强，92/101 case 第一个匹配即正确文档 |
| 正确文档不在 top-30 | 3% | 仅有 3 个 dense_recall 失败 |
| **Chunk 级 Recall@5** | **53.3%** | 真实瓶颈——正确文档在 top-5，但正确段落不全是 |

**60 个失败 case 的分类：**

| 失败模式 | 数量 | 占比 | 描述 |
|---------|:---:|:----:|------|
| 同文档多段溢出 | 50 | **83%** | 正确文档在 top-5，至少 1 个 gold chunk 找到，但剩下的排不进 top-5 |
| 跨文档垄断 | 7 | 12% | gold 分布在 2+ 文档，但一个文档的 chunk 占满 top-5 |
| dense_recall | 3 | 5% | 正确文档不在 top-30 |

**52/60 的失败 case 中，第一个 gold chunk 排在 dense@rank #1。** 模型能精准定位到正确段落，
但当需要 2+ 个 gold chunks 时，剩下的就排不出 top-5 了。

**按类别分析 Chunk Recall@5：**

| 类别 | Gold | Matched | ChunkR@5 | 难度因素 |
|------|:---:|:-------:|:--------:|----------|
| single_document_factual | 79 | 48 | **60.8%** | 单段落为主，相对容易 |
| version_or_conflict | 16 | 10 | **62.5%** | 版本对比，多段落分散 |
| bilingual | 44 | 23 | **52.3%** | 中英混杂，语义空间分裂 |
| cross_document_synthesis | 56 | 26 | **46.4%** | **最难**——多文档各自贡献一段，top-5 难以覆盖全部 |
| code_and_nl | 15 | 5 | **33.3%** | **chunk 级最差**——代码注释放入 embedding 后段落区分力最弱 |

**按来源领域分析 Chunk Recall@5：**

| 领域 | Cases | ChunkR@5 | 说明 |
|------|:----:|:--------:|------|
| cs229 | 7 | **92.3%** | 数学笔记段落间语义差异大，容易区分 |
| math | 20 | **67.2%** | 分析/代数笔记，段落结构清晰 |
| physics | 21 | **43.8%** | PDF 页码切割，chunk 边界与 gold 不匹配 |
| papers (VLA) | 27 | **44.7%** | 多论文对比，top-5 被一个垄断 |
| devtools | 20 | **39.5%** | **最难**——Docker/Git/Linux 功能点多，同文档 4-5 chunks 全找不到 |

**缺口量化：** `112/210 gold chunks matched → 98 缺失`。其中 32 个 case 只缺 1 段，
21 个 case 缺 2 段。如果解决「缺 1 段」的 case，就能拿到 +32 matched chunks（提升到 ~68%）。

### Reranker 实验

Reranker（BGE-Reranker-Base, 0.6B）在 embedding 修复后的表现仍然低于纯 cosine 排序：

| 实验 | 路径 | Recall@5 | MRR | P95 |
| --- | --- | ---: | ---: | ---: |
| dense-exact (baseline) | cosine top-5 | **53.33%** | 0.4875 | 109ms |
| dense-rerank-k30 | dense@30 → reranker@10 → top-5 | 48.57% | 0.4662 | 199ms |
| dense-rerank-k100 | dense@100 → reranker@30 → top-5 | 44.76% | 0.4487 | 212ms |

BGE-Reranker-Base 也是 0.6B 级模型，同样对中英技术内容的段落级区分力不足。

## 核心瓶颈（实验 6 前）

> **注**：实验 6（Section-aware chunking）后此瓶颈已基本解决。保留此节作为历史诊断记录。

```
Dense@100 召回正确文档 ≈ 97%+   → ✓ 模型能定位到正确来源
Dense@5  选对正确段落  ≈ 53%    → ✗ 但同一文档的不同段落分数太接近（0.6B 模型容量不足）
```

**瓶颈本质**：单向量点积（cosine similarity）在文档内段落级区分上存在固有限制。
语意相近的段落共享文档级含义，0.6B 的 embedding 模型无法将它们的向量在空间中拉开足够距离。

**实验 6 的发现修正了这一判断：** 实际上，文档内段落区分困难的主要原因并非模型容量，
而是**文档目录 TOC 段的「关键词密集向量」系统性主导了检索结果**——TOC 段涵盖了
所有话题的关键词，embedding 向量像话题质心一样与几乎所有 query 高度相似。
去掉 TOC 段的偏差后，正确内容段的 cosine 分数自然浮现到 top-5，0.6B 模型也因此
达到了 88.57% 的 Recall@5。

## 实验 5：上下文增强嵌入（Contextual Embedding）

### 假设

Chunker 已将 `heading_path`（章节路径，如 `Docker > Storage > OverlayFS`）记录在每个 chunk 的
metadata 中，但 embedding 时只用纯 chunk text。如果在嵌入前将 heading_path 作为上下文前缀拼入，
可以给同一文档的不同 chunk 创造更独特的向量，减少同文档段落间的相似度。

```
改前: embed("Docker 使用 overlay2 存储驱动...")
改后: embed("[Docker > Storage > OverlayFS]\nDocker 使用 overlay2 存储驱动...")
```

### 改动

`packages/application/src/application/ingestion/embedding.py` 第 179 行，嵌入 chunk text 前
如果 `c.heading_path` 非空，则拼入 `[{heading_path}]\n` 前缀。

### 结果

在 development 集上全量重摄入 + 评测（dense-exact, k=5）：

| 配置 | Recall@5 | MRR | 失败数 | 失败分布 |
|------|:--------:|:---:|:------:|:---------|
| 基线（无上下文） | **53.33%** | 0.4875 | 60 | locator_mapping:57, dense_recall:3 |
| 上下文嵌入 | **53.81%** | 0.4894 | 60 | locator_mapping:58, dense_recall:4 |
| **Δ** | **+0.48pp** | +0.0019 | — | — |

### 分析

效果微乎其微（+0.48pp），原因：**0.6B 模型容量不足以利用章节级上下文信号**。

```
[Docker > Storage > OverlayFS]\nDocker 使用 overlay2...
[Docker > Networking > Bridge]\nDocker 支持 bridge...
```

0.6B 模型的 768 维向量中，文档级信号（"Docker"）淹没了章节级信号（"Storage" vs "Networking"）。
两个向量在空间中的距离几乎没有拉开。

此改动已保留在代码中（仅 4 行），不增加任何运行时开销。换上更大模型（1.8B+）后，
其更大的注意力头可能更有效地利用 heading_path 结构信息，届时可重新评测对比。

## 实验 6：Section-Aware Chunking 改进（TOC 合并 + 标题段）

### 假设

前序实验确认 bottleneck 在「同文档多段溢出」——正确文档在 top-5，但剩余的 gold chunks
排不进去。但后续抽样分析发现，溢出问题的主要原因**不是 embedding 模型容量不够**，而是
**文档目录（TOC）段产生的「关键词密集向量」主导了检索结果**。

### 抽样分析：四种失败模式

对 60 个失败 case 逐层追溯 query → gold evidence → top-5 实际命中，识别出四种不同根因：

| 模式 | 占比 | 描述 | 典型 case |
|:----:|:----:|------|:---------:|
| **A: 概述段打败细节段** | **~40%** | TOC/简介的 embedding 是纯关键词密集阵（包含所有 topic 名字）→ cosine 远超含代码和长文本的具体内容段 | qa-249（Docker） |
| **B: README 覆盖专用文档** | ~25% | README 技术栈表格从关键词上撞中 query，但实际不包含答案。CLAUDE.md / ARCHITECTURE.md 不在 top-30 | qa-004, qa-005（OmniStudio） |
| **C: PDF 页面偏差** | ~20% | gold 在第 3 页，但 embedding 认为第 10 页（具体架构细节）更匹配 | qa-201（π₀.₅ 论文） |
| **D: 真实语义混淆** | ~15% | query 中专有名词将向量拉向同名文档而非正确来源 | qa-179（Rudin） |

以 qa-249（Docker Namespace）为例：

```
Q: 各 Namespace 的隔离能力和局限性分别是什么？
Gold: PID Namespace (lines 263-311), Mount (315-317), User (319-321), Network (363-395)

解析器输出：整个文档被解析为嵌套 LIST，没有 HEADING 节点。
所有 list_item 的 heading_path=""（无章节上下文）。

Chunk 1 (TOC, ~500 chars): heading_path=""
  text: "1.1 UTS Namespace / 1.2 IPC Namespace / 1.3 PID Namespace /
         1.4 Mount Namespace / 1.5 User Namesapce / 1.6 Network Namespace / ..."
  → embedding 前无 heading_path 前缀 → 纯关键词密集向量
  → cosine=0.84，排第 1

Chunk 5-8 (PID/Mount/User/Network 内容段): heading_path=""
  text: "PID namespace是用来隔离进程 id...\n以下程序执行的命令...```go\npackage main...```"
  → 含代码和长文本，稀释了语义信号
  → cosine=0.73，排 6-30
```

**TOC 段不是「embedding 不够强」，而是「恰好包含了太多关键词」。** 0.6B 模型「正确」地
将 TOC 排到第 1，因为它确实包含了所有 namespace 的名字。但问题在于 TOC 没有答案内容。

### 改动

`packages/infrastructure/src/infrastructure/chunkers/structure_chunker.py`，两处：

**改动 1：`_extract_segments` — 标题文本作为 segment（+9 行）**

2. **丢弃纯 TOC 段**：检测文档开头 `heading_path=""` + `list_item`/`raw_text` 类型的纯 TOC 组，将其丢弃——heading 文本已作为 segment 嵌入正文，TOC 的关键词完全冗余，去掉后消除纯关键词密集向量对检索的干扰。

### 结果

在 development 集上全量重摄入 + 评测（dense-exact, k=5），需清空已有评估数据强制重新摄入：

| 配置 | Recall@5 | MRR | Case 全覆盖率 | 匹配 chunks |
|------|:--------:|:---:|:-------------:|:----------:|
| 基线（实验 5） | **53.81%** | 0.4894 | 40.6% | 113/210 |
| **Section-aware chunking** | **88.57%** | **0.9274** | **84.2%** | **186/210** |
| **Δ** | **+34.76pp** | **+0.4380** | **+43.6pp** | **+73** |

**所有类别均显著提升：**

| 类别 | 基线 | 新 | 提升 |
|:----|:---:|:--:|:----:|
| code_and_nl | 33.3% | **86.7%** | **+53.4pp** |
| cross_document_synthesis | 46.4% | **92.9%** | **+46.4pp** |
| bilingual | 50.0% | **79.5%** | +29.5pp |
| single_document_factual | 63.3% | **91.1%** | +27.8pp |
| version_or_conflict | 62.5% | **87.5%** | +25.0pp |

**51 个 case 改进，仅 1 个微降（qa-177, 60%→40%）。** 22 个之前零召回的 case 完全修复。

### 诊断映像：前后对比

```
基线 (53.81%)                            Section-aware (88.57%)
┌────────────────────────────────┐       ┌────────────────────────────────┐
│  正确匹配 ████████████████ 113  │       │  正确匹配 ████████████████████ 186 │
│  缺失       ██████████████  97  │       │  缺失       ████  24            │
└────────────────────────────────┘       └────────────────────────────────┘
```

### 分析

改动仅 ~20 行，不涉及模型升级，不增加推理成本或存储开销。效果远超预期（+34.8pp），
直接超越 85% 门禁。

1. **丢弃 TOC 消除系统性偏差**——TOC 段以纯关键词列表形态占据 embedding 空间的「话题质心」位置，任何查询都先匹配 TOC。丢弃后正确内容段自然浮现。heading 文本已作为 segment 嵌入，不丢失专有名词信号。
2. **标题文本使 chunk 自包含**——每个 chunk 以标题开头（如 `1.3 PID Namespace`），在 embedding 空间增加专有名词信号强度。
3. **devtools（最难领域）升至 85%+**——之前 devtools 的三个 markdown 以巨型 TOC 开头，是失败最多的来源。TOC 丢弃后大量 case 从零召回变为完全命中。

1. **TOC 合并不是「微调」，而是消除了一个系统性偏差。** TOC 段以纯关键词列表形式占
   据了 embedding 空间的「话题质心」位置——无论 query 是关于文档的哪个方面，TOC 段
   的 cosine 总是最高。去掉这个偏差后，正确的内容段自然浮现到 top-5。

2. **标题文本作为 segment 使 chunk 自包含。** 之前 heading 文本只存在于 `meta` 字段，
   chunk 文本不包含它。现在每个 chunk 以标题开头（如 `1.3 PID Namespace`），在
   embedding 空间里增加了专有名词的信号强度。

3. **devtools 领域（最难的 39.5%）升至 85%+。** devtools 的三个 markdown 文件以
   巨型 TOC 列表开头，是之前失败最多的来源。TOC 合并后，这些文件中大量 case 从零召回
   变为完全命中（qa-238~243, qa-249 等）。

### 剩余失败

16 个 case 仍未达到完全召回，主要分两类：

| 类型 | 数量 | 描述 |
|:----|:----:|------|
| **dense_recall** | 7 | 正确文档不在 dense@30 候选池中（需 1.8B embedding 模型）|
| **locator_mapping** | 9 | 正确文档找到，部分 gold chunks 仍遗漏（多段证据 >5 个 slots） |

平均 Recall 仍在 75-85% 之间，最差的 qa-187（代码类，33%）是唯一低于 50% 的剩余 case。
如果需要从 88.57% 提升到接近 100%，可以评测上一级模型（Qwen3-Embedding-1.8B）补上
dense_recall 的缺口。

### 实验文件

```bash
# 2026-07-30 最终实验
EVALUATION_DATABASE_ISOLATED=1 \
EMBEDDING_ENDPOINT=http://localhost:8080 \
EMBEDDING_BATCH_SIZE=8 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1.yaml \
  --split development \
  --prepare-corpus \
  --experiment dense-exact \
  --output tmp/retrieval-eval-chunk-fix.json
```

> **注意**：`--prepare-corpus` 会跳过已 PUBLISHED 的版本（通过 `content_sha256` 判断）。
> 如果代码有变更需要强制重新摄入，需先清空评估库的旧数据：
> ```sql
> DELETE FROM chunks WHERE version_id IN (
>   SELECT id FROM document_versions WHERE document_id IN (
>     SELECT id FROM documents WHERE source_id IN (
>       SELECT id FROM sources WHERE space_id IN (
>         SELECT id FROM spaces WHERE owner_id = 'evaluation'))));
> UPDATE document_versions SET status = 'pending'
>   WHERE document_id IN (...同上...);
> UPDATE documents SET current_version_id = NULL
>   WHERE source_id IN (...同上...);
> DELETE FROM ingestion_tasks WHERE source_id IN (...同上...);
> ```

## 后续方向

门禁 85% 已在当前 0.6B 模型 + 改进 chunker 下达成。后续提升可选：

| 方向 | 预期提升 | 工作量 | 备注 |
|------|:-------:|:------:|------|
| Section-aware chunking | **+34.8pp** ✅ | **20 行代码** | **已实现，已达到 88.57%** |
| **Qwen3-Embedding-1.8B** | ~5-10pp | 中（模型下载 + 重新 TEI 部署） | 补 dense_recall 缺口（7 个 case） |
| **Qwen3-Reranker-4B** | ~3-5pp | 中（新增 TEI 容器） | 在 dense@30 候选上做 cross-encoder 重排 |
| holdout 集验证 | — | 低 | 使用 `--split holdout` 检验泛化性 |

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

### 本次实验的输出文件

| 文件 | 对应实验 |
| --- | --- |
| `tmp/retrieval-eval-contextual-embed.json` | 实验 5：上下文嵌入（heading_path 前缀），53.81% |
| `tmp/retrieval-eval-chunk-fix.json` | **实验 6：Section-aware chunking（TOC 合并 + 标题段），88.57% ✅** |

### 相关文件

| 文件 | 用途 |
| --- | --- |
| `cases/evals/configs/retrieval-v1.yaml` | 评测配置，含实验定义 |
| `scripts/evaluate_retrieval.py` | 评测执行脚本 |
| `packages/application/src/application/retrieval/search.py` | `SearchService` — 检索编排核心 |
| `packages/application/src/application/retrieval/dense.py` | Embedding prefix 注册表（Query + Document） |
| `packages/application/src/application/ingestion/embedding.py` | 嵌入管道 — 含 heading_path 上下文增强（实验 5） |
| `packages/domain/src/domain/retrieval.py` | 检索领域协议、`CandidateBatch`、`RetrievalProfileV1` |
| `packages/domain/src/domain/chunking.py` | `ChunkOutput.heading_path` — 章节路径字段 |
| `packages/infrastructure/src/infrastructure/retrieval/postgres_store.py` | pgvector 检索适配器 |
| `packages/application/src/application/retrieval/evaluation.py` | 评测指标与失败分类（chunk-level recall） |
| `packages/infrastructure/src/infrastructure/chunkers/structure_chunker.py` | **结构感知分块器 — 含实验 6 的 TOC 合并 + 标题段改动** |
| `deploy/compose.yaml` | TEI / Reranker 容器配置 |
