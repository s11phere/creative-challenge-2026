# Recall 优化实验报告 v2

> 日期：2026-07-31
> 分支：`dev/recall-optimization`
> 基线：`tmp/retrieval-eval-baseline.json`（commit `cde331c`，git clean）
> v1 报告：`docs/recall-optimization-report.md`
> **基线 Recall@5 = 58.57% → 实测最优 61.9%，门禁 85%**

v2 是一次**独立的根因重查**：v1 宣称通过丢弃 TOC 段达到 90.48%，但该结果已被判定为**虚假**（破坏了 PDF 结构、误删正文，commit `3b97c5f` 已回退）。v2 在回退后的代码上重新收集证据，结论与 v1 的叙事**不同**：**瓶颈不在 TOC 解码，而在「正确 chunk 的 dense 排名被压到 6–30 位」。**

v2 已把候选方向逐一带入真实评测验证（§七），全部收口：**配额 +1.4pp、bge-reranker-base +2.8pp、词边界分块修复 0、扩池 0、Oracle 上限 86.2%**。实测最优 61.9%（quota5 + reranker + 修复后分块）。到 85% 只剩更强 reranker / 更大 embedding 模型两条未测路径。

---

## 一、基线确认

| 指标 | 值 |
|---|:---:|
| Evidence Recall@5 | **0.5857** |
| MRR | 0.5959 |
| 全覆盖率（case 级） | 42.57% |
| dense_mode | exact |
| dense_candidate_k / final_k | 30 / 5 |
| max_chunks_per_document | 3 |
| 模型 | Qwen3-Embedding-0.6B, 768d, L2 |
| 指令版本 | qwen3-knowledge-qa-v1（查询/文档对称） |
| 语料 | 74 源成功 / 16 源 parser 失败，6336 chunks |

## 二、方法论（可复现）

1. **匹配逻辑复现**：用 `evaluation.py::evidence_matches_chunk` 的同款规则（`source_key` + `source_version` + `locator` 同类型区间重叠；`pdf_page` 按 `start ≤ page ≤ end`），对 111 个 dev case 逐 gold 单元复算，与报告内 `matched_evidence_count` **逐 case 零误差**。
2. **查询嵌入**：`QWEN3_KNOWLEDGE_QA_QUERY_PREFIX` + 原问句 → 本地 TEI（`http://localhost:8080`，Qwen3-Embedding-0.6B），与摄入侧文档嵌入同源。
3. **池位置测量**：对全库 6336 个已发布 chunk 计算查询余弦，还原 dense pool（排除 `node_type=table_of_contents`，与 `_is_direct_retrieval_content()` 一致），得到每个 gold 单元对应 chunk 的池内排名。
4. **策略仿真**：在真实池数据上重放 `_raw_hits` 的「去重 → 每文档配额 → top-k」，测各杠杆的相对收益。
5. **旁证**：`papers/pi07` 等源的 chunk 数 vs 唯一源范围数（SQL 聚合），确认分块层面的重复度。

## 三、失败全景（111 个 dev case）

按「正确文档是否进 top-5 / gold 单元是否命中」分类：

| 分类 | 数量 | 说明 |
|---|:---:|---|
| pass（全覆盖） | 53 | — |
| locator_mapping | 46 | 正确文档在 top-5，但部分 gold 单元未命中 |
| dense_recall_multi_doc | 12 | 部分 gold 文档未进 top-5 |
| dense_recall_doc_not_found | 0 | 无「正确文档完全缺失」的情况 |

### 关键量化：87 个未命中 gold 单元按其池内排名分布

对 58 个失败 case 的每个 gold 单元，测量它对应的 chunk 在 dense pool 中的最佳排名，并与真实基线命中情况交叉：

| 池内排名 | 单元数 | 真实命中率 | 未命中数 | 诊断 |
|:---:|:---:|:---:|:---:|---|
| ≤5 | 61 | **84%**（51/61） | 10 | 池内已进 top-5，被去重/配额最终选取挤掉 |
| **6–30** | **56** | **9%**（5/56） | **51** | **在 dense@30 候选池内，但被 5–29 个更高分 chunk 挤出** ← 主导 |
| >30 | 26 | 0% | 26 | 不在 dense@30 候选池 |
| **合计** | 143 | 39% | **87** | |

**结论**：瓶颈不是最终选取（rank≤5 单元 84% 都能命中），而是 **dense 排名本身把正确 chunk 压到 6–30 位**。扩大候选池、MMR 等都不能改变「正确 chunk 的 dense 分不够高」这一事实。

## 四、堵在正确 chunk 前面的是什么（685 个 blocker 分类）

对每个池内 6–30 位的 gold chunk，统计排名 1…(rank−1) 之间的 chunk：

| blocker 类型 | 数量 | 占比 |
|---|:---:|:---:|
| 同文档其他片段/页 | 340 | **50%** |
| 其他文档的真实内容 | 279 | **41%** |
| 速查表类（cheatsheet/README 技术栈表） | 44 | 6% |
| PDF 点线目录（`. . . . . . 72 / 4 / Continuity`） | 22 | 3% |

**这直接否定了 v1 的核心叙事**：关键词密集的 TOC 诱饵只占 **9%** 的 blocker。v1 的「+34.76pp」是**整段删掉目录**带来的过拟合（同时破坏了 PDF 结构），当前数据上即使完整跳过点线目录，仿真也只恢复 +1.4pp。

## 五、结构性放大器：PDF 页面切分洪水（重要发现）

`_split_oversize_segment`（`structure_chunker.py`）把超长 segment 按 512 字符**硬切**，且 `preserve_source_span`（commit `cde331c`）让**每片都携带整页源行号**：

```
papers/pi07 : 269 chunks / 25 个唯一源范围  → 每页 ~10–14 片
math/rudin  : 1240 chunks / 335 个唯一源范围
```

切片文本示例（按字符硬切，出现**半词**切片）：

```
ordinal 21: "t policies. These generalist policies..."
ordinal 22: "le model, building on top of the π0.6-MEM..."
ordinal 23: "ositional generalization and performant..."
```

后果（互为因果）：
1. **池泛滥**：一篇 10 页论文的 dense@30 池被 2–3 页的碎片占满，其余页进不来。
2. **配额饿死**：`max_chunks_per_document=3` 下，最先排名靠前的 1–2 页吃掉全部配额，**gold 页被配额拦截**。例：`qa-203` gold 为 pi07 第 1 页（池内排名 3），但第 10/15 页的碎片先占满 3 个配额，第 1 页最终未进 top-5。
3. **嵌入质量差**：半词切片无语义完整性，0.6B 模型难以把它们排到正确位置。

> 注：`preserve_source_span` 对 PDF 的 `pdf_page` 匹配无影响（页定位靠 `start_page/end_page`），对 markdown 的 `lines` 匹配是必要的正确性修复；它的副作用是让同一页的所有切片共享 locator，进而在去重/配额层互相竞争。本节的「按字符硬切」是基线状态——7.5 已修复为词边界切分，但不改变片数，池泛滥结构依旧。

## 六、根因总结

正确 chunk 通常**在** dense@30 池内（61+56=117 个单元 ≤30），但被 dense 排名压到 6–30 位（51 个未命中），主因是 0.6B 双编码器对「同文档其他页」「其他文档合法内容」的区分力不足；PDF 页面硬切造成的池泛滥放大了这一点，并与配额=3 叠加导致 10 个 rank≤5 单元也被饿死。关键词密集的 TOC 诱饵仅占 blocker 的 9%，**不是**当前瓶颈。

> 仿真先行筛掉了一批方向（MMR 负收益、候选池扩到 200 无效、跳过点线目录仅 +2 单元、页面级去重有 `lines` 误伤），详见 §七实测收口。

## 七、实验验证（真实评测管线 + Oracle 分析）

在回退后的代码上，对方向 A/B/C 逐一实测。全部复用 `eval-pg` 已有语料（74 源，6336 chunks），`git_commit=cde331c`。评测命令见附录。

### 7.1 实验 1：配额与候选池（配置级）

`tmp/retrieval-exp1.yaml`，四个变体一次跑完：

| 变体 | recall@5 | MRR | Δ |
|---|:---:|:---:|:---:|
| dense-exact（对照） | 58.6% | 0.596 | 精确复现基线 ✓ |
| **quota 3→5** | **60.0%** | 0.607 | **+1.4pp** |
| dense@100 | 58.6% | 0.596 | 0 |
| quota 5 + dense@100 | 60.0% | 0.607 | +1.4pp |

结论：配额 3 略偏紧（实测 +1.4pp，低于仿真的 +4.2pp——仿真高估）；扩大候选池 @30→@100 **无效**，与仿真及 v1 的 dense@100 验证一致。

### 7.2 Oracle 天花板（零成本仿真，决定性）

允许「偷看答案」地从现有 dense@30 池任选 5 个以最大化 gold 命中（greedy oracle）：

| 选取方式 | recall@5 | 含义 |
|---|:---:|---|
| 当前 dense 排序（实测） | 58.6% | — |
| Oracle 从 dense@30（quota=3） | **86.2%** | 池内容足以到 85% |
| Oracle 从 dense@30（quota=5） | 87.6% | |
| Oracle 从 dense@60 | 86.2% | 31–60 位无额外可恢复单元 |

**决定性结论**：dense@30 池已包含足以达到 85% 的信息；58.6% 与 86.2% 之间 **27.6pp 全部是排序/选取差距**——不是候选池内容、不是分块质量、不是模型语义覆盖。但这是**上限**，需要近乎完美的排序才能兑现。

### 7.3 实验 2：Dense + bge-reranker-base

部署 `~/models/bge-reranker-base`（XLM-RoBERTa 跨编码器，中英双语，1.1GB）到第二个 TEI 容器（`localhost:8081`）。用 `hybrid_rerank` 模式 + `keyword_candidate_k=1` 使 RRF 融合退化为 dense 主导。`tmp/retrieval-exp2.yaml`：

| 变体 | recall@5 | MRR | Δ |
|---|:---:|:---:|:---:|
| dense-exact（对照） | 58.6% | 0.596 | — |
| **rerank k30 + quota5** | **61.4%** | 0.631 | **+2.8pp** |
| rerank k30 + quota3 | 60.0% | 0.616 | +1.4pp |
| rerank k100 + quota5 | 58.1% | 0.585 | −0.5pp |

**逐 case 净效果**：修好 14 个、弄坏 9 个（净 +5）。修好的多为 dense 6–30 位单元（如 qa-187: 1→3/3）；弄坏的 9 个说明 reranker 的「相关性」评分与 gold locator 不对齐——把「相关但不含答案文本」的 chunk 排进 top-5。

**为什么远低于 Oracle**（rerank 分数分布，qa-005）：

```
rank1 readme  0.539   ← 唯一高分
rank2 readme  0.008   ← 之后全部噪声级
rank3 arch    0.004
rank4 readme  0.004
rank5 claude  0.001
```

bge-reranker-base 分数严重偏斜：top-1 之后第 2–5 名都落在 0.001–0.009 的噪声区间，排序基本靠碰。对中英数理混合语料判别力不足。

### 7.4 实验 3：reranker 看全池（配额放开到 30）

`tmp/retrieval-exp3.yaml`，`max_chunks_per_document=30`，让 reranker 看到完整 dense@30：

| 变体 | recall@5 |
|---|:---:|
| dense-rerank-k30-nq30 | **57.1%**（比基线还差） |

附带确认：配额前的 `_limit_document_quota` 会把融合池压到 ~15（qa-203 中 `fused=15`），reranker 看不到完整候选；但放开配额看全池**反而更差**——证明问题在 bge-reranker-base 本身的排序能力，而非池子被配额削弱的假象。

### 7.5 实验 4：PDF 分块修复（方向 A 实测，零收益）

对 `_split_oversize_segment` 实施**词边界切分**（每个 512 窗口内找最后一个空格，不再 `line[i:i+512]` 硬切半词），配套 2 个单元测试（不产生词中断点、内容完整保留），34 个 chunker 测试 + ruff/lint/mypy 全过。

重摄入后（6337 chunks，总量不变）评测：

| 变体 | recall@5 | Δ |
|---|:---:|:---:|
| dense-exact | **58.6%** | **0（无变化）** |
| quota 5 | 60.0% | +1.4pp（同旧 chunks） |

**结论**：半词切片是代码质量缺陷（修复后 chunk 文本不再以 `t`/`le`/`ositional` 开头），但**不是 recall 杠杆**。原因：该修复不改变片数——`papers/pi07` 仍 269 chunks / 25 个唯一源范围（10.8× 重复），池泛滥与配额饿死结构不变；且主因是 dense 排名，与 chunk 文本质量无关。

### 7.6 实验 5：新 chunks + reranker

在修复后语料上重跑 reranker（旧 chunks 上为 61.4%）：

| 变体 | recall@5 | Δ |
|---|:---:|:---:|
| dense-rerank-k30-q5 | **61.9%** | +3.3pp（vs 基线） |
| dense-rerank-k30-q3 | 59.5% | +0.9pp |
| dense-rerank-k100-q5 | 58.6% | 0 |

更干净的 chunks 给 reranker 带来 ~+0.5pp 增量，不改变方向判断。

### 7.7 最终收口

**全部候选方向已实测**：

| 方向 | 实测结果 | 结论 |
|---|:---:|---|
| C. 配额 3→5 | +1.4pp | 便宜、确定，保留 |
| B. reranker（bge-reranker-base） | +2.8~3.3pp，修 14 破 9 | **模型不够强**（top-1 后分数全部落到 0.001–0.009 噪声区间）；换 v2-m3 可再试 |
| A. 修复 PDF 分块（词边界） | 0 | 池泛滥结构未变，非 recall 杠杆 |
| 扩大候选池 @30→@100 | 0 | 无效 |
| Oracle 天花板 | 86.2% | 池内容足够，排序是唯一缺口 |

**当前状态**：58.6% → **61.9%**（quota5 + reranker + 修复后分块），离 85% 尚远。Oracle 证明池内容足够，但兑现需要**显著更强的排序器**——bge-reranker-base 做不到。剩余两条未测路径：更强的 reranker（bge-reranker-v2-m3）或升级 embedding 模型（Qwen3-Embedding-1.8B），均为下载新模型的不确定投入。

> **结构性提醒**：部分 case 受「5-slot 上限」锁死——如 qa-014 有 7 个 gold 单元分布在 7 个不同位置，5 个槽位最多覆盖 5 个，即便完美排序也到不了 100%。85% 目标刚好在 Oracle（86.2%）之下，**只有接近完美的排序才够得着**。

## 附录

### 复现命令

```bash
# 基线评测（前置：eval-pg + 本地 TEI 已启动）
EVALUATION_DATABASE_ISOLATED=1 \
POSTGRES_PORT=5433 POSTGRES_DB=evaluation POSTGRES_PASSWORD=eval_only_pw \
EMBEDDING_ENDPOINT=http://localhost:8080 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1.yaml \
  --split development --experiment dense-exact \
  --output tmp/retrieval-eval-baseline.json

# 本报告分析产物
python3 /tmp/analyze_baseline.py        # 失败分类（逐单元匹配）
python3 /tmp/diag_recall.py             # 测量 gold chunk 池内排名
python3 /tmp/classify_blockers.py       # blocker 分类
python3 /tmp/sim_all.py /tmp/sim_quota.py  # 杠杆仿真
python3 /tmp/oracle_ceiling.py          # Oracle 天花板（7.2）

# 实验 1（配额/候选池，7.1）
EVALUATION_DATABASE_ISOLATED=1 \
POSTGRES_PORT=5433 POSTGRES_DB=evaluation POSTGRES_PASSWORD=eval_only_pw \
EMBEDDING_ENDPOINT=http://localhost:8080 \
uv run python scripts/evaluate_retrieval.py \
  --config tmp/retrieval-exp1.yaml --split development \
  --output tmp/retrieval-eval-exp1.json

# 实验 2/3（reranker，7.3/7.4）—— 需先启动 reranker 容器
docker run -d --name eval-tei-rerank --gpus all -p 8081:80 \
  -v ~/models:/models ghcr.io/huggingface/text-embeddings-inference:cuda-1.9 \
  --model-id /models/bge-reranker-base
RERANKER_ENDPOINT=http://localhost:8081 RERANKER_MODEL=bge-reranker-base \
uv run python scripts/evaluate_retrieval.py \
  --config tmp/retrieval-exp2.yaml --split development \
  --output tmp/retrieval-eval-exp2.json

# 实验 4（词边界分块修复，7.5）—— 清空评测库强制重摄入
docker exec eval-pg psql -U app -d evaluation \
  -c "TRUNCATE ingestion_tasks, chunks, document_versions, documents, sources, spaces CASCADE;"
EVALUATION_DATABASE_ISOLATED=1 POSTGRES_PORT=5433 POSTGRES_DB=evaluation \
POSTGRES_PASSWORD=eval_only_pw EMBEDDING_ENDPOINT=http://localhost:8080 \
uv run python scripts/evaluate_retrieval.py \
  --config tmp/retrieval-exp1.yaml --split development --prepare-corpus \
  --output tmp/retrieval-eval-exp4.json

# 实验 5（新 chunks + reranker，7.6）
RERANKER_ENDPOINT=http://localhost:8081 RERANKER_MODEL=bge-reranker-base \
uv run python scripts/evaluate_retrieval.py \
  --config tmp/retrieval-exp2.yaml --split development \
  --output tmp/retrieval-eval-exp5.json
```

### 关键数据文件

| 文件 | 内容 |
|---|:---|
| `tmp/retrieval-eval-baseline.json` | 本次分析基线（v2） |
| `tmp/retrieval-eval-baseline.md` | 摘要 |
| `tmp/retrieval-eval-exp1.json` | 实验 1：quota/候选池变体（7.1） |
| `tmp/retrieval-eval-exp2.json` | 实验 2：reranker 变体（7.3，旧 chunks） |
| `tmp/retrieval-eval-exp3.json` | 实验 3：reranker 看全池（7.4） |
| `tmp/retrieval-eval-exp4.json` | 实验 4：词边界分块修复后重摄入（7.5） |
| `tmp/retrieval-eval-exp5.json` | 实验 5：新 chunks + reranker（7.6） |
| `/tmp/diag_results.json` | 每 case gold 单元池内排名 |
| `/tmp/fail_cases.json` | 失败分类明细 |
| `/tmp/pool_top60.json` | 每失败 case 池前 60（含文本） |
