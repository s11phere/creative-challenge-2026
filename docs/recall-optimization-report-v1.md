# Recall 优化实验报告 v1

> 日期：2026-07-30
> 分支：`dev/recall-optimization`
> 基线：阶段 3 Step 10 验收（development P0）
> **最终：Recall@5 = 90.48% ✅（门禁 85%）**
>
> ⚠️ **状态：已判定为虚假结果（见 [v2 报告](recall-optimization-report-v2.md)）**。90.48% 依赖「丢弃纯 TOC 段」，误删了 PDF 正文/结构，commit `3b97c5f` 已回退。回退后的真实基线 Recall@5 = 58.57%。v2 独立重查表明：TOC 诱饵仅占失败 blocker 的 9%，本报告的核心叙事已被推翻，仅作为历史记录保留。

优化历程：依次修复了 **指令前缀不对称** → **TOC 段关键词偏差**（核心突破） → **文档配额 + 重叠去重**，走过了「发现问题 → 分析瓶颈 → 定位根因 → 击中要害」的完整闭环。

---

## 一、问题与基线

Dense Recall@5 约 **51%**，远低于 85% 门禁。评估配置：

- 模型：Qwen/Qwen3-Embedding-0.6B, 768d, L2 归一化
- 数据集：`knowledge-qa-v0`, development 111 P0 cases
- 分块：chunk_size=512, overlap=64
- 检索：dense_candidate_k=30, final_k=5

| 基线实验 | Recall@5 | MRR | 说明 |
|---------|:--------:|:---:|------|
| Keyword | 30.00% | 0.2917 | FTS OR 查询修复后 |
| **Dense exact (k=30)** | **51.43%** | 0.4822 | **当前最优** |
| Dense IVFFlat | 44.29% | 0.4191 | 比 exact 低 |
| Hybrid (alpha=0.5) | 42.86% | 0.4106 | RRF 稀释了 dense |
| Hybrid + Reranker | 48.57% | 0.4662 | 也不如纯 dense |

## 二、早期侦查（全部无效或边际有效）

| 尝试 | 结果 | 结论 |
|------|:----:|------|
| 扩大候选池 dense@30→100 | 51.43% → 51.43% | 正确答案在 30~100 位，但 top-5 被同一高分错误段落占满 |
| RRF alpha=0.5→0.9 | 42.86% → 49.52% | Keyword 信号太弱，hybrid 在此数据集不合适 |
| Chunk size 512→256 | 51.43% → 44.76% ⬇️ | 缩小丢失上下文完整度 |
| 查询规则扩展 | 51.43% → 51.43% | Qwen3 指令前缀 + 原始问句已是最优输入 |

## 三、根因发现：文档 Instruction Prefix 缺失

在逐层审查中发现一个关键 bug：**文档嵌入时没有应用 Instruction Prefix**。

Qwen3-Embedding 是指令微调的不对称 embedding 模型。查询嵌入时使用了 `qwen3-web-search-v1` 指令前缀（`Instruct: Given a web search query...\nQuery: ...`），但文档块在摄入阶段直接用原始文本——查询向量和文档向量处于**不同的 embedding 空间**，cosine similarity 的区分力被系统性削弱。

### 修复

在两处添加对称前缀机制：`dense.py` + `_DOCUMENT_PREFIXES` 注册表和 `document_embedding_config()`；`ingestion/embedding.py` 嵌入前给 chunk 加上前缀。

同时发现指令模板描述的是「web search」而非实际的知识问答任务，设计了更匹配的 `qwen3-knowledge-qa-v1` 指令。

### 结果

| 实验 | Recall@5 | MRR | dense_recall | locator_mapping |
|-----|:--------:|:---:|:-----------:|:---------------:|
| 原始（broken） | **51.43%** | 0.4822 | **102** | ~70 |
| 修复后（对称指令） | **53.33%** | 0.4875 | **3** | 57 |

**失败模式的质变**：修复前 102 个证据单元连正确文档都找不到；修复后 60 个失败中只有 **3 个**是文档级未命中（dense_recall），其余全是 locator_mapping——**正确文档在 top-5，但排到的是错误段落**。瓶颈从文档级移到了 chunk 级。

### 深度失败分析

After-fix 失败分类：

| 失败模式 | 数量 | 占比 | 描述 |
|---------|:---:|:----:|------|
| 同文档多段溢出 | 50 | **83%** | 正确文档在 top-5，但剩下的 gold chunks 排不进去 |
| 跨文档垄断 | 7 | 12% | gold 分布在 2+ 文档，一个文档占满 top-5 |
| dense_recall | 3 | 5% | 正确文档不在 dense@30 |

按类别看，`cross_document_synthesis`（46.4%）和 `code_and_nl`（33.3%）最差。按领域看，devtools（39.5%）和 papers（44.7%）最差。

## 四、实验 5：上下文增强嵌入（+0.48pp，边际有效）

### 假设

Chunker 已记录 `heading_path`（章节路径），但 embedding 时未利用。将章节信息作为前缀嵌入可以拉大同文档内不同 chunk 的向量距离。

### 结果

| 配置 | Recall@5 | MRR | 失败数 |
|------|:--------:|:---:|:------:|
| 基线 | **53.33%** | 0.4875 | 60 |
| 上下文嵌入 | **53.81%** | 0.4894 | 60 |
| **Δ** | **+0.48pp** | — | — |

### 分析

效果微乎其微。0.6B 模型的 768 维向量中，文档级信号淹没章节级信号。改动已保留在代码中（仅 4 行），换上更大模型时可能有效。

## 五、实验 6：Section-Aware Chunking（核心突破，+34.76pp）

### 发现真实瓶颈

对 60 个失败 case 逐层追溯，发现**主要瓶颈不是模型容量，而是文档目录（TOC）段的「关键词密集向量」系统性主导了检索结果**。

四种失败模式：

| 模式 | 占比 | 根因 |
|:----:|:----:|------|
| **TOC 段打败细节段** | **~40%** | TOC/简介的 embedding 是纯关键词密集阵（含所有 topic 名），cosine 远超含代码和长文本的具体内容段 |
| README 覆盖专用文档 | ~25% | README 技术栈表格撞中关键词，但实际不含答案 |
| PDF 页面偏差 | ~20% | gold 在第 3 页，但第 10 页的 embedding 更匹配 |
| 真实语义混淆 | ~15% | 专有名词将向量拉向同名文档而非正确来源 |

以 qa-249（Docker Namespace）为例：

```
Q: 各 Namespace 的隔离能力和局限性分别是什么？
Gold: PID (lines 263-311), Mount (315-317), User (319-321), Network (363-395)

Chunk 1 (TOC，500 纯关键词): "1.1 UTS / 1.2 IPC / 1.3 PID / ..." → cosine=0.84，排第 1
Chunk 5-8 (PID/Mount/User/Network 内容段，含代码): → cosine=0.73，排 6-30
```

**TOC 段不是「embedding 不够强」，而是「恰好包含了太多关键词」。** 0.6B 模型「正确」地将 TOC 排到第 1，但它没有答案内容。

### 改动（~20 行）

`structure_chunker.py`，两处改动：

1. **标题文本作为 segment**：之前 HEADING 节点只更新 `heading_path` 不生成 segment，标题文本在 chunk 中丢失。现在嵌入时 `[heading_path]\ntitle_text\ncontent`，专有名词信号加强。

2. **丢弃纯 TOC 段**：检测文档开头 `heading_path=""` + `list_item`/`raw_text` 类型的纯 TOC 组，将其丢弃——heading 文本已作为 segment 嵌入正文，TOC 的关键词完全冗余，去掉后消除纯关键词密集向量对检索的干扰。

### 结果

| 配置 | Recall@5 | MRR | Case 全覆盖率 | 匹配 chunks |
|------|:--------:|:---:|:-------------:|:----------:|
| 基线（实验 5） | **53.81%** | 0.4894 | 40.6% | 113/210 |
| **Section-aware** | **88.57%** | **0.9274** | **84.2%** | **186/210** |
| **Δ** | **+34.76pp** | — | +43.6pp | **+73** |

所有类别均显著提升，**22 个之前零召回的 case 完全修复**：

| 类别 | 基线 | Section-aware | Δ |
|:----|:---:|:------------:|:-:|
| code_and_nl | 33.3% | **86.7%** | +53.4pp |
| cross_document_synthesis | 46.4% | **92.9%** | +46.4pp |
| bilingual | 50.0% | **79.5%** | +29.5pp |
| single_document_factual | 63.3% | **91.1%** | +27.8pp |
| version_or_conflict | 62.5% | **87.5%** | +25.0pp |

### 分析

效果远超预期。**三点原因**：

1. **丢弃 TOC 消除系统性偏差**——TOC 段以纯关键词列表形态占据 embedding 空间的「话题质心」位置，任何查询都先匹配 TOC。丢弃后正确内容段自然浮现。heading 文本已作为 segment 嵌入，不丢失专有名词信号。
2. **标题文本使 chunk 自包含**——每个 chunk 以标题开头（如 `1.3 PID Namespace`），在 embedding 空间增加专有名词信号强度。
3. **devtools（最难领域）升至 85%+**——之前 devtools 的三个 markdown 以巨型 TOC 开头，是失败最多的来源。TOC 丢弃后大量 case 从零召回变为完全命中。
1. **TOC 合并不是微调，而是消除系统性偏差**——TOC 段以纯关键词列表形态占据 embedding 空间的「话题质心」位置，任何查询都先匹配 TOC。去掉此偏差后正确内容段自然浮现。
2. **标题文本使 chunk 自包含**——每个 chunk 以标题开头（如 `1.3 PID Namespace`），在 embedding 空间增加专有名词信号强度。
3. **devtools（最难领域）升至 85%+**——之前 devtools 的三个 markdown 以巨型 TOC 开头，是失败最多的来源。TOC 合并后大量 case 从零召回变为完全命中。

> 直接越过 85% 门禁。改动只涉及分块器 ~20 行，不涉及模型升级或推理成本。

## 六、实验 7：文档配额 + 重叠去重（+1.91pp → 90.48%）

### 发现的两个 Bug

**Bug 1：dense-exact 未执行 `max_chunks_per_document`**

Profile 配了 `max_chunks_per_document: 3`，但只在 HYBRID 路径调用过 `_limit_document_quota()`，DENSE 路径直接取前 k。一个文档可占满 top-5（如 qa-192: 5/5 来自 `analysis-sequences`）。

**Bug 2：滑动窗口重叠 chunk 未去重**

chunk_size=512, overlap=64 产生相邻重叠 chunk，同一个段落出现在 2-3 个连续 chunk 中。如 qa-177 的 `analysis-reals` lines:158 出现 **3 次**，浪费 2 个 slot。

### 修复

`search.py` 的 `_raw_hits()`：排序后先应用文档级配额（`max_chunks_per_document`），再对同 `source_key` + 重叠 locator 区间的 chunk 只保留最高分一个。

### 结果

| 配置 | Recall@5 | MRR | 全覆盖率 | 匹配 | 失败 |
|------|:--------:|:---:|:-------:|:----:|:----:|
| Section-aware（实验 6） | **88.57%** | 0.9274 | 84.2% | 186 | 16 |
| **+ 配额 + 去重** | **90.48%** | **0.9318** | **87.1%** | **190** | **13** |
| **Δ** | **+1.91pp** | — | +2.9pp | **+4** | **-3** |

5 个 case 改进，1 个微量回退（qa-014, 42.9%→28.6%，7 个 gold 全在同份文档被 quota 误伤，预期 trade-off）。

### dense@100 验证

在新语料上重测 dense@100：

| 配置 | Recall@5 | dense_recall 失败 |
|------|:--------:|:----------------:|
| dense@30 | 90.48% | 6 |
| dense@100 | 90.48% | **6（相同）** |

**结论**：pi05/pi07/motionlib 的 embedding 偏差是全局性的——不仅不在 dense@30，也不在 @100 内。扩大候选池不能解决。

## 七、最终结果：90.48%

### 剩余瓶颈

配额+去重后 13 个失败 case：

| 类型 | 数量 | 根因 |
|:----|:----:|------|
| **dense_recall** | 6 | 正确文档不在 dense@30。pi05 是罪魁（4/6 涉及 pi05/pi07） |
| **locator_mapping** | 7 | 正确文档找到但部分 gold 遗漏。qa-014 被 quota 误伤（7 gold 全在一份） |

三大独立瓶颈总结：

| 瓶颈 | 影响 | 可修复？ | 代价 |
|:----|:----:|:--------:|:----:|
| **A: 模型容量**（6 dense_recall） | ~8 缺失 chunks | 🟡 PDF chunking 或 keyword supplement | 有限 |
| **B: Cheatsheet 关键词干扰**（4 case） | ~5 缺失 | ✅ MMR 多样性或合并 chunk | ~30 行 |
| **C: 5-slot 天花板**（4 case） | ~7 缺失 | ❌ 需更大 final_k 或多轮检索 | 配置级 |

### 后续方向

| 方向 | 预期 | 工作量 |
|------|:----:|:------:|
| 升级 Qwen3-Embedding-1.8B | ~3-5pp | 中（下载 + 部署） |
| MMR / cheatsheet 合并 | ~1pp | 低（~30 行） |
| holdout 集验证 | — | 低 |

## 附录

### 实验命令

```bash
# 完整运行（摄入 + 评测）
EVALUATION_DATABASE_ISOLATED=1 \
EMBEDDING_ENDPOINT=http://localhost:8080 \
EMBEDDING_BATCH_SIZE=8 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1.yaml \
  --split development \
  --prepare-corpus \
  --experiment dense-exact \
  --output tmp/retrieval-eval-xxx.json
```

### 输出文件

| 文件 | 实验 | Recall@5 |
| --- | --- | :-------: |
| `tmp/retrieval-eval-contextual-embed.json` | 实验 5：上下文嵌入 | 53.81% |
| `tmp/retrieval-eval-chunk-fix.json` | **实验 6：Section-aware chunking** | **88.57%** |
| `tmp/retrieval-eval-dedup-fix.json` | **实验 7：配额 + 去重** | **90.48% ✅** |
| `tmp/retrieval-eval-dense-k100.json` | dense@100 验证 | 90.48%（无变化） |
