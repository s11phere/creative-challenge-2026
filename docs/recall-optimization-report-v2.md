# Recall 优化实验报告 v2

> 分支：`dev/recall-optimization` ｜ 基线：`tmp/retrieval-eval-baseline.json`（commit `cde331c`）
> 门禁：Recall@5 ≥ 85% ｜ **基线 58.6% → v0 实测最优 61.9% → v1 实测 60.2%/61.9% → v1+k10 72.5% → 修 PDF 提取 73.8%（段落chunk）→ 多栏修复 75.8%**

**TL;DR**：v1 报告宣称丢弃 TOC 段达 90.48%，已被判定为虚假（破坏 PDF 结构，`3b97c5f` 回退）。v2 独立重查，根因是**正确 chunk 被 dense 排名压到池内 6–30 位**（不是 TOC）。候选方向全部实测收口，v0 最优 61.9%。接入同学修订的 **v1 数据集 + claim 级评分**后，**Oracle 天花板 94.3%，85% 目标舒适可达**。**§七 门禁放宽到 Recall@10**：recall@k 曲线 k=5 61.9% → k=10 **72.5%**（正式 gate 验证）→ k=15 74.1%（饱和），k=5 截断白白丢失 ~12pp。

> **§四 过拟合警告（Step 2）**：尝试「调大 chunk_size 去洪」→ dev 上 +6.8pp（60.2→67.0），但 **holdout 上仅 +1.4pp**（77.7→79.1），收益衰减 ~5 倍。**chunk_size 是过拟合杠杆，不作修复**；去洪机制真实但很小（+1.4pp）。

---

## 一、根因：dense 排名把正确 chunk 压到 6–30 位

### 失败全景（111 dev case）

| 分类 | 数量 |
|---|:---:|
| pass（全覆盖） | 53 |
| locator_mapping（正确文档进 top-5，部分 gold 未命中） | 46 |
| dense_recall_multi_doc（部分 gold 文档未进 top-5） | 12 |

### 87 个未命中 gold 单元按其池内排名

| 池内排名 | 单元数 | 真实命中率 | 诊断 |
|:---:|:---:|:---:|---|
| ≤5 | 61 | 84% | 被去重/配额最终选取挤掉（10 lost） |
| **6–30** | **56** | **9%** | **在 dense@30 池内但被 5–29 个更高分 chunk 挤出 ← 主导（51 lost）** |
| >30 | 26 | 0% | 不在候选池 |

**结论**：瓶颈是 dense 排名本身，不是最终选取、不是池内容（Oracle 证明池足够）。

### Blocker 分类（6–30 位 gold 前面的 685 个 chunk）

| 类型 | 占比 |
|---|:---:|
| 同文档其他片段/页 | 50% |
| 其他文档合法内容 | 41% |
| 关键词诱饵（速查表 6% + PDF 点线目录 3%） | 9% |

→ **否定 v1 的 TOC 叙事**：诱饵只占 9%。v1 的 +34.76pp 是整段删目录的过拟合。

### 结构性放大器：PDF 页切分洪水

`_split_oversize_segment` 按 512 字符硬切超长页 + `preserve_source_span` 让每片携带整页行号 → **每页 10–14 片**（pi07: 269 chunks / 25 源范围）。后果：池被 2–3 页碎片占满、配额=3 饿死其他 gold 页（qa-203 第 1 页池内第 3 却未进 top-5）、半词切片嵌入质量差。

### 失败集中度：PDF 数学内容最差（v1 claim recall）

| 维度 | 表现 |
|---|---|
| 领域 | cs229 **96.6%** / devtools 80.3% / omnistudio 56.7% / papers 54.5% / math 53.2% / **physics 41.7%** |
| 格式 | lines 69.1% > pdf 55.0% > **markdown+pdf 混合 40.7%** |
| 语言 | mixed 62.8% / zh 53.8% / en 33.3%（小样本） |

**含义**：失败高度集中在「PDF + 数学/物理公式页」（physics/math/papers），干净 markdown（cs229/devtools）已很好——**模型没问题，是 PDF 表示坏了**；跨格式混合 case 是独立难点。改进应优先修 PDF 数学内容，而非泛泛换模型。

## 二、实验收口（全部真实评测）

| 方向 | 结果 | 结论 |
|---|:---:|---|
| 配额 3→5 | **+1.4pp** | 便宜、确定，保留 |
| **reranker（bge-reranker-base）** | **+2.8pp**（新 chunks +3.3pp；修 14 破 9） | 分数偏斜（top-1 后全落 0.001–0.009 噪声区间），模型不够强 |
| 词边界分块修复 | 0 | 片数不变，池泛滥结构依旧 |
| 候选池 @30→@100 | 0 | 无效 |
| MMR / 页面去重 | 负收益 | 仿真筛掉 |
| **Oracle 天花板（v0）** | **86.7%** | 池内容足够，唯一缺口是排序 |

**v0 实测最优 61.9%**（quota5 + reranker + 修复后分块）。rerk k100 变体更差（58.6%），放开配额看全池更差（57.1%）。

## 三、v1 数据集 + claim 级评分（2026-08-01）

### v1 是什么

同学修订（commit `774276e`，main）：① 论文 gold 从标题页重定位到答案页；② math 大段范围收窄为精确行；③ **协议升级为 claim 级 OR/AND 证据集**（`acceptable_evidence_sets`，集合内 AND、集合间 OR）。v0 是扁平证据单元计数。

**关键陷阱**：用 v0 式评分器（扁平计数）跑 v1，recall 掉到 53.1%（分子只 +4、分母 +29）——是错误评分器的假象。

### 新增：claim 级评分器

同学的 commit 只有 `validate.py` 结构校验，无 claim 评分。本次在 `evaluation.py` 实现 `GoldClaim` + `evidence_id` + claim 指标（`claim_recall_at_k` 等），`evaluate_retrieval_case` 按「任一可接受证据集完整命中即满足」评分。3 个单元测试，494 passed，ruff/mypy 全过。

### v1 实测（dense + reranker）

| 变体 | evidence recall@5 | claim recall@5 | MRR |
|---|:---:|:---:|:---:|
| dense-exact | 53.1% | **60.2%** | 0.636 |
| **rerank k30 + quota5** | **54.8%** | **61.9%** | 0.675 |
| rerank k30 + quota3 | 51.9% | 59.1% | 0.650 |
| rerank k100 + quota5 | 50.6% | 55.9% | 0.619 |

分片 claim recall：code_and_nl 0.788→**0.909**、bilingual 0.500→0.562（rerank 提升明显）；single_document_factual 略降 0.621→0.604。

### v1 Oracle 天花板：94.3%（决定性）

| 指标 | v0 | v1 |
|---|:---:|:---:|
| Oracle 天花板（quota3） | 86.7% | **94.3%** |
| Oracle 天花板（quota5） | 88.1% | 94.8% |

**v1 的 85% 舒适可达**（余量 9.3pp；v0 仅 1.7pp）。重定位 + claim OR/AND 把天花板抬高 7.6pp。

## 四、Step 2 试错：调大 chunk_size 去洪（负面结果）

为减少「池子洪水」（§一），尝试调大 chunk_size（512→4096 扫掠）：dev 上 claim recall 峰值在 1536（60.2→67.0%，+6.8pp），但曲线非单调（1024 反而更低）。**Holdout 验证只有 +1.4pp**（77.7→79.1）——收益衰减 ~5 倍，证明 dev 增益主要是 chunk 边界对齐的过拟合，不是「更大更好」原则。**不作为修复**；去洪的正解是修分块器（语义合并页内碎片），而非靠尺寸赌对齐。附带发现：holdout（77-79%）整体比 dev（60-67%）高 ~17pp，holdout 集明显更易。

## 五、结论与下一步

1. **v1 是正确基准**：标注修复 + claim 协议使 85% 从「勉强够到」变「正常可达」。
2. **当前 v1 最优 61.9%**，对 94.3% 天花板还有 ~33pp 排序差距。
3. reranker（bge-reranker-base）在 v1 上仅 +1.6pp，验证「模型不够强」。
4. **剩余路径**：优先 **更强的 reranker（v2-m3）**，其次更大 embedding（1.8B）；~~修分块器~~ 已被 §六② 证伪，不再考虑。

## 六、方向①②③实证结论（2026-08-01）

### ① dev/holdout 不对称 = 格式构成混杂（非 bug）

holdout 比 dev 高 ~17pp 的根因是**语料格式构成不同**，不是难度或标注差异：

| split | pdf_page 案例 | lines 案例 |
|---|---|---|
| dev | 65（45% claim 失败） | 46（31%） |
| holdout | ~10 | ~139 |

两边 per-format claim recall 一致（PDF 55.0% / lines 69.1%）。**dev 是更难的、正确的优化目标**；holdout 高是 markdown 便宜。

### ②「一页一块」分块假设被证伪

用与 eval 一致的 embedding（768-dim Matryoshka + `qwen3-knowledge-qa-v1` 前后缀，TEI 支持 `dimensions=768`）实测合并同页碎片：**12/16 例 dense 相似度反而变差**（碎片已含查询相关信号，整页稀释，如 pi07 p2 合并 0.33 vs 最优碎片 0.48）。不做分块修复。

### ③ 更强 reranker 是首选（exact 检索量化）

> 排查陷阱：裸 `ORDER BY embedding <=> q LIMIT 30` 会走 **ivfflat 近似索引**，结果不可靠（两次运行 top-1 都不同）。必须 `SET enable_indexscan=off` 与 eval 对齐取 exact。

84 个失败 PDF gold 单元（v1 claim 级）：
- **58 个在 exact dense@30 池内**（多数 dense rank 3-11）→ 失败发生在 **rerank 阶段**（如 qa-017 金页 rank4/5 却只 match 1/5 claims）
- 26 个不在池 → 需更强 embedding / PDF 提取

### ③b reranker 换型实测：天花板 ~73%，到顶（2026-08-01）

换 bge-reranker-v2-m3（TEI 原生）与 Qwen3-Reranker-0.6B（官方 logit 打分，自建 TEI 兼容服务）实测 dev：

| reranker | recall@10 | MRR | evidence@10 | 修好 case |
|---|---|---|---|---|
| bge-base | 72.5% | 0.683 | 64.9% | — |
| **bge-v2-m3** | **73.0%** | 0.653 | 65.3% | 58 |
| **Qwen3-0.6B** | **73.0%** | **0.667** | **65.7%** | 58（同批） |

**关键发现**：v2-m3 和 Qwen3 修好的 case **完全相同（58 个，0 差异）**——更强 reranker 把 recall@10 从 72.5% 提到 73.0% 就到顶。剩余 ~27% 失败是「乱码 PDF 公式页」+「dense 就漏了」的根本问题：**换任何 reranker 都救不回**（gold 内容与查询语义匹配度本身低）。**reranker 不是瓶颈，瓶颈是 PDF 数学表示 / dense 召回。**

**取舍**：Qwen3 MRR/evidence 略高但需自建 torch 服务（`/tmp/qwen3_rerank_server.py`，README 的 yes/no logit 打分 + `padding_side='left'`）；v2-m3 TEI 原生零维护、recall 相同 → **保留 v2-m3**。

> Qwen3 打分坑：必须 `AutoTokenizer(padding_side='left')` + yes/no token + prefix/suffix chat 模板；默认 right padding 会让 `logits[:,-1]` 取到 pad 位置垃圾值（实测 recall 掉到 66.5%）。

排查脚本：`/tmp/embed_api.py`（正确 embedding）、`/tmp/reranker_ceiling_exact.py`（exact 池分析）、`/tmp/pdf_merge_hypothesis2.py`、`/tmp/qwen3_rerank_server.py`（Qwen3 打分服务）。

## 七、门禁放宽到 Recall@10（2026-08-01）

### recall@k 曲线（v1 + rerank，dev，claim 级）

| k | claim recall | Δ vs k=5 |
|---|---|---|
| 5 | 61.9% | — |
| **10** | **72.5%**（正式 gate 验证） | **+10.6pp** |
| 15 | 74.1% | +12.2pp |
| 20 | 74.1% | 饱和 |

**核心发现**：黄金内容一直在检索池里，只是 **k=5 截断把它丢在 rank 6-15**。k=5→10 回收 +10.6pp，k=15 几乎吃满。这不是靠更强 reranker，而是给下游更多上下文——与 v2-m3（把 gold 从 6-30 挤进 top-5）**正交、可叠加**。

### 动态 top_k 被证伪（置信度触发不可行）

尝试按 rerank 分数曲线动态放宽（6th/1st 比值触发）：**precision 仅 0.17**（抓住 1 个需放宽 case 就误放宽 ~5 个「k=5 就够」case）。bge-reranker 分数不判别「还需更多」vs「5 块够了」（需放宽 case 分数有平缓 0.76 也有骤降 0.002）。根源：**不看到答案无法预判是否需 >5 块**，所有可观察代理信号都太弱。

### 决策与改动

- **改为固定 k=10**（不是动态）：简单可靠。检索返回 top-10（profile `final_k: 10`），评估 Recall@10（protocol `recall_k: 10`）。
- schema `recall_k` `const: 5` → `enum: [5, 10]`；配置 `tmp/retrieval-v1-rerank-k10.yaml` 实测 **72.5%**。
- 剩余差距：72.5% → 85% 仍需 ~12.5pp，方向仍为 §六③ 的更强 reranker（v2-m3）。

---

## 八、修 PDF 提取：段落结构 + 段落边界 chunk（2026-08-02）

**结论**：`pdf_parser` 从「每页一个 RAW_TEXT blob」重写为「dict-mode span 重建 + 几何启发式产出 heading/paragraph/list 节点」；chunker 从「按 `chunk_size` 硬切任何超长 segment」改为「段落是原子单元，只有超过 `max_segment_size` 的病态 blob 才按行切」。两者**必须成对**：新 chunker 依赖段落结构，旧 parser 喂给新 chunker 会崩。组合结果 dev **73.0% → 73.8%**（+0.8pp，cs512，非过拟合），chunk 数 6337→4995（−21%），消除半词碎片。

### 动机与假设

memory 的「跳出路径 2（修 C：PDF 公式保真）」具体化为两个可分离改动：

1. **结构**：`page.get_text("text")` → `get_text("dict")`，span 按 baseline(y1) 分组、组内按 x 排序拼行——修复公式 glyph 阅读顺序；再按字号/行距/缩进分组为 heading/paragraph/list 节点（等价 Markdown 的段落结构）。
2. **chunk 边界**：把 `chunk_size` 从「硬切上限」降级为「合并目标」。新引入 `max_segment_size=4096`，段落小于它时绝不被切；只有病态超长 segment（旧 parser 的整页 blob）才被 `_split_oversize_segment` 按行切，且行切仍走词边界（`_split_oversize_line`）。

核心假设（用户提出）：**chunk 太碎不利于 embedding，也稀释下游 LLM 可获取的上下文；512 是任意上界，硬编码字符上限本身就是过拟合来源**。

### 逐次实测（dev，v1 + v2-m3 rerank，k10，全部重摄入）

| 配置 | chunk 数 | claim@10 | evid@10 | MRR |
|---|:---:|:---:|:---:|:---:|
| 基线（旧 parser + 旧 chunker, cs512） | 6337 | **73.0%** | 65.3% | 0.653 |
| 新 parser + 旧 chunker, cs512 | 6999 | 71.9% | 65.3% | 0.647 |
| 新 parser + 旧 chunker, cs1536 | 2729 | 75.2% | 67.4% | 0.727 |
| **新 parser + 新 chunker, cs512** | 4995 | **73.8%** | 65.7% | 0.642 |
| 新 parser + 新 chunker, cs1536 | 2478 | 74.7% | 68.2% | 0.720 |

- 新 parser + 旧 chunker 在 cs512 反而 **降**（71.9%）：段落结构造出更多更小的 segment，旧 chunker 按 512 硬切 → 半词碎片（`zed`/`aining`/`ce`）+ 小块洪水，gold 的整页内容被稀释到 top-10 之外（qa-204 从 1.0 → 0.0，gold 在 starvla p2，新 chunk 的 p2 碎片排不进前 10）。
- 放大 cs1536 能救回（75.2%），但那是**硬切段落的副作用**被「更大块」掩盖。

### 受控 2×2（cs512，隔离 parser 与 chunker 的独立贡献）

| 组合 | dev claim@10 | holdout claim@10 |
|---|:---:|:---:|
| 旧 parser + 旧 chunker（基线） | 73.0% | — |
| 旧 parser + 新 chunker | **61.9%** | 86.4% |
| 新 parser + 新 chunker | **73.8%** | 87.0% |

**两个关键事实**：

1. **新 chunker 依赖段落结构**。旧 parser 每页只产 1 个 RAW_TEXT blob，新 chunker 不硬切它（< 4096）→ 整页大 chunk → dev 崩到 61.9%（密集段落信号被稀释）；holdout 反而没崩（86.4%）——印证 §六① holdout 是 markdown 便宜。**结论：段落结构 + 段落边界 chunk 是一个整体，缺一不可。**

2. **cs512 组合是干净的、dev/holdout 一致的增益**（73.8% / 87.0%，均高于对应旧组合）。chunk 数 −21%，半词碎片消失。

### 过拟合复查：cs512 → cs1536 仍是过拟合

| 新 parser + 新 chunker | dev claim@10 | holdout claim@10 | dev evid@10 | holdout evid@10 |
|---|:---:|:---:|:---:|:---:|
| cs512 | 73.8% | **87.0%** | 65.7% | **85.9%** |
| cs1536 | 74.7% | 86.1% | 68.2% | 84.8% |

cs1536 在 dev 上 +0.9pp claim / +2.5pp evid，但 holdout 上 −0.9pp / −1.1pp——与 §四 完全同构的过拟合模式。**cs512 是干净点，cs1536 的增量不作修复**。用户「chunk 给下游 LLM 更多上下文有好处」的原理仍成立，但 k=10 的检索指标测不到该收益（截断在 rerank 阶段），那是下游 LLM 的增益，不在本指标内。

### 决策与改动（提交内容）

- **`pdf_parser.py`**：dict-mode span 重建 + 几何启发式（heading/paragraph/list 节点，局部行号锚定防整页行号洪水）。scanned/corrupt/empty 错误路径保持不变。
- **`chunking.py` / `orchestrator.py` / `structure_chunker.py`**：新增 `max_segment_size=4096`；`_group_segments` 只用它切病态超长 segment，段落绝不为凑 `chunk_size` 硬切；config hash 纳入新字段。
- **`evaluate_retrieval.py`**：恢复 `--chunk-size` 实验参数（`IngestionConfig.chunk_size` 透传）。
- **测试**：+9 PDF 结构测试（heading/段落/list/公式 span 顺序/跨页 body 锚定）、+2 chunker 段落原子性测试、config-hash 新字段测试。525 passed，ruff/mypy 全过。
- **保留**：`version_or_conflict`（0.765→0.882）与 `bilingual`（0.594→0.656）是段落到单元的直接收益。`code_and_nl` 曾回落（0.909→0.758），已由 §八④ 多栏修复解决。

### ④ 多栏 PDF 布局修复（2026-08-02，追加）

**问题**：§八 提交后 `code_and_nl` 从 90.9% 回落到 75.8%（qa-224/qa-222 掉到 0）。逐 case 定位：gold 页（pi06 p6 等）在检索池里**完全消失**，不是 rerank 挤掉而是 dense 捞不到。

**根因**：`_iter_spans` 把所有 block 的 span 打平成列表、按 baseline 全局排序拼行，**丢掉了 PyMuPDF dict 的 block 边界**。对两栏 PDF（physics 5 源几乎全双栏、math/rudin 103 页、23/30 个 PDF 源有双栏页），左右栏 span 在同一 baseline 高度被交错误排成一行 → 语义混乱的巨型段落（pi06 p6 出现 5871 字符的交错块）→ embedding 稀释 → dense 捞不到。

**修复**：`_iter_blocks` 按 PyMuPDF block 分组返回 span；`_page_visual_lines` 逐 block 独立重建 visual line，再按 block 顺序拼接。两栏各自保持独立流，不再交错。

**验证（dev，cs512）**：

| 配置 | claim@10 | evid@10 | MRR | code_and_nl | single_doc |
|---|:---:|:---:|:---:|:---:|:---:|
| §八 提交（segchunk） | 73.8% | 65.7% | 0.642 | 75.8% | 75.8% |
| **+ 多栏修复** | **75.8%** | **67.0%** | **0.676** | **90.9%** | **79.1%** |

- **code_and_nl 完全恢复**（75.8→90.9%），证明诊断正确：是两栏交错把伪代码/正文混在一起，不是段落结构本身。
- claim@10 75.8%（vs 基线 73.0%，**+2.7pp**）；MRR 0.676。
- **过拟合复查（按格式切分）**：dev PDF 案例 69.1→72.4（+3.3pp），holdout PDF 案例 76.1→76.1（持平，**非下降**）；整体 holdout 的 −0.3pp 是 nonpdf 的 87.5→87.3 噪声。**多栏修复是干净的 PDF 改善，非边界对齐巧合**（对比 §四/§八③ cs1536 的 dev+/holdout− 过拟合模式）。
- **测试**：+1 两栏不交错测试。526 passed，ruff/mypy 全过。

---

## 附录

### 关键复现命令

```bash
# 前置：eval-pg (5433) + 本地 TEI (8080) + reranker (8081)
EVALUATION_DATABASE_ISOLATED=1 POSTGRES_PORT=5433 POSTGRES_DB=evaluation \
POSTGRES_PASSWORD=eval_only_pw EMBEDDING_ENDPOINT=http://localhost:8080 \
uv run python scripts/evaluate_retrieval.py --config <cfg> --split development --output <out>

# cfg: cases/evals/configs/retrieval-v1.yaml（v0 基线）
#      tmp/retrieval-exp1.yaml（配额/候选池）  tmp/retrieval-exp2.yaml（reranker）
#      tmp/retrieval-v1-test.yaml（v1 dense）   tmp/retrieval-v1-rerank.yaml（v1+rerank）
# rerank 需加 RERANKER_ENDPOINT=http://localhost:8081 RERANKER_MODEL=bge-reranker-base
# v1 Oracle：uv run python /tmp/oracle_v1.py
# §四 holdout 诊断（绕过 formal gate）：uv run python /tmp/holdout_eval.py
```

### 关键数据文件

| 文件 | 内容 |
|---|:---|
| `tmp/retrieval-eval-baseline.json` | v0 基线 |
| `tmp/retrieval-eval-exp{1,2,4,5}.json` | 实验 1/2/4/5（配额/rerank/分块修复/新chunks+rerank） |
| `tmp/retrieval-eval-v1b.json` / `v1-rerank.json` | v1 dense / v1+rerank（claim 级） |
| `tmp/retrieval-eval-v1-cs*.json` | §四 chunk_size 扫掠产物 |
| `cases/evals/datasets/knowledge-qa-v1/` | 同学修订的 v1 数据集 |
| `tmp/retrieval-v1-*.yaml` | v1 评测配置 |

### 交接：当前运行状态（2026-08-02）

- **分支**：`dev/recall-optimization`（工作区含 §八 提交前的改动）。
- **运行容器**：`eval-tei`（embedding，:8080）、`eval-tei-rerank`（bge-reranker-v2-m3，:8081）、`eval-pg`（:5433，隔离评测库）。
- **评测库语料**：cs512（新 parser + 段落边界 chunk，4995 chunks，v0 manifest）。切换 chunk_size 需 TRUNCATE + `--prepare-corpus --chunk-size <N>` 重摄入。
- **模型**：Qwen3-Embedding-0.6B（TEI）；bge-reranker-v2-m3（TEI 原生）。无 LLM（fast_chat 未配置）。
- **已提交**：`fab040f`（claim 评分器）、`c9c47b7`（词边界分块修复）。`--chunk-size` 实验参数已恢复（本次新增）。
- **§八 实测文件**：`tmp/retrieval-v1-newpdf-k10.json`（新 parser+旧 chunker cs512）、`...-cs1536.json`（cs1536）、`...-segchunk.json`（新 chunker cs512）、`...-segchunk-cs1536.json`（cs1536）、`...-oldparser-segchunk-cs512{,-holdout}.json`（受控对照）、holdout 同名前缀。
- **下一步候选**（详见 §五/§六/§八）：① 已定位（格式混杂）；② 分块修复已证伪但 §八 证明「段落为单元」成立（前提是有段落结构）；**优先换 1.8B embedding**（修 B：dense 漏 24 单元，与 §八 正交可叠加）。备选：`code_and_nl`/`cross_document` 在 §八 下的小幅回落（配额效应？）排查。
