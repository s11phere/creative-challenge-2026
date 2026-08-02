# Recall 优化实验报告 v2

> 分支：`dev/recall-optimization` ｜ 门禁：Recall@10 ≥ 85%（原 Recall@5）
> **最终状态（2026-08-02 收口）：dev claim@10 = 75.8%（MRR 0.676），holdout 87.0%。检索侧（B/C/D/E）全部杠杆实测收口，75.8% 是检索侧天花板；剩余 recall 空间在 A 环节（中文查询↔英文/math 页的语义桥接），归 Stage 4 查询改写（R4-04，pilot 证据 +2.8pp 固定 / +4.3pp oracle）。**
> **模型栈**：embedding = **Qwen3-Embedding-0.6B**（768d，`qwen3-knowledge-qa-v1` 前后缀）；reranker = **bge-reranker-v2-m3**（rerank k30 → top10）；chunk = StructureChunker 段落原子（max_segment 4096）+ pdf dict 多栏修复；融合 = dense@30（exact）+ keyword@1 → RRF(α=0.5)。

**TL;DR**
1. **根因**：正确 chunk 被 dense 排名压到池内 6–30 位——不是 TOC、不是候选池不足（Oracle 94.3% 证明池够），瓶颈在 dense 排序本身。
2. **有效改进**：PDF 段落结构 + 段落边界 chunk（dev 73.0→73.8）+ 多栏布局修复（73.8→75.8）；门禁放宽到 k=10（61.9→72.5）。
3. **收口实验（2026-08-02）**：quota k=10 扫掠确认 **q5 最优**（问题关闭）；**PDF 乱码量化 0%**——提取没坏、真瓶颈是跨语言语义匹配，**Nougat 证伪**。
4. **结论**：检索侧已耗尽，下一批 recall 收益归 Stage 4 查询改写，不在检索分支重复建机制。

---

## 一、根因：dense 排名把正确 chunk 压到 6–30 位

### 失败全景（111 dev case，v0 扁平证据）

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

→ **否定 v1 报告的 TOC 叙事**（诱饵只占 9%，v1 的 +34.76pp 是整段删目录的过拟合，已回退）。

### 结构性放大器：PDF 页切分洪水（§六 的动机）

旧 chunker 按 512 字符硬切超长页 + 每片携带整页行号 → **每页 10–14 片**（pi07: 269 chunks / 25 源范围）→ 池被 2–3 页碎片占满、半词切片嵌入质量差。去洪的正解是修分块器（§六），而非靠调大 chunk_size（§四证伪）。

### 失败集中度：PDF 数学内容最差（v1 claim recall）

| 维度 | 表现 |
|---|---|
| 领域 | cs229 **96.6%** / devtools 80.3% / omnistudio 56.7% / papers 54.5% / math 53.2% / **physics 41.7%** |
| 格式 | lines 69.1% > pdf 55.0% > **markdown+pdf 混合 40.7%** |
| 语言 | mixed 62.8% / zh 53.8% / en 33.3%（小样本） |

**含义**：失败高度集中在「PDF + 数学/物理公式页」（physics/math/papers），干净 markdown 已很好——模型没问题，问题在 PDF 的表示与检索匹配（§七② 定量确认）。

## 二、v0 检索管线实验收口（reranker/配额/候选池）

| 方向 | 结果 | 结论 |
|---|:---:|---|
| 配额 3→5 | **+1.4pp**（k=5 时代） | 便宜、确定，保留 |
| **reranker（bge-reranker-base）** | **+2.8pp**（新 chunks +3.3pp；修 14 破 9） | 分数偏斜（top-1 后全落 0.001–0.009 噪声区间），模型不够强 |
| 词边界分块修复 | 0 | 片数不变，池泛滥结构依旧 |
| 候选池 @30→@100 | 0 | 无效 |
| MMR / 页面去重 | 负收益 | 仿真筛掉 |
| **Oracle 天花板（v0）** | **86.7%** | 池内容足够，唯一缺口是排序 |

v0 实测最优 61.9%（quota5 + reranker + 修复后分块）。

### reranker 换型实测：天花板 ~73%，到顶（2026-08-01）

| reranker | recall@10 | MRR | evidence@10 | 修好 case |
|---|---|---|---|---|
| bge-base | 72.5% | 0.683 | 64.9% | — |
| **bge-v2-m3** | **73.0%** | 0.653 | 65.3% | 58 |
| **Qwen3-0.6B** | **73.0%** | **0.667** | **65.7%** | 58（同批） |

**关键发现**：v2-m3 和 Qwen3 修好的 case **完全相同（58 个，0 差异）**——更强 reranker 到 73.0% 就到顶。剩余 ~27% 失败是「dense 就漏了」的根本问题：**换任何 reranker 都救不回。reranker 不是瓶颈，瓶颈在 dense 召回。** 取舍：Qwen3 MRR/evidence 略高但需自建 torch 服务；v2-m3 TEI 原生零维护、recall 相同 → **保留 v2-m3**。

> 排查陷阱：裸 `ORDER BY embedding <=> q LIMIT 30` 会走 **ivfflat 近似索引**（两次运行 top-1 都不同），必须 `SET enable_indexscan=off` 与 eval 对齐取 exact。

## 三、v1 数据集 + claim 级评分（2026-08-01）

### v1 是什么

同学修订（commit `774276e`，main）：① 论文 gold 从标题页重定位到答案页；② math 大段范围收窄为精确行；③ **协议升级为 claim 级 OR/AND 证据集**（`acceptable_evidence_sets`，集合内 AND、集合间 OR）。v0 是扁平证据单元计数。

**关键陷阱**：用 v0 式扁平评分器跑 v1，recall 掉到 53.1%——是错误评分器的假象。本次在 `evaluation.py` 实现 `GoldClaim` + `evidence_id` + `claim_recall_at_k`，按「任一可接受证据集完整命中即满足」评分（+3 单元测试，494 passed）。

### v1 实测（dense + reranker）

| 变体 | evidence@5 | claim@5 | MRR |
|---|:---:|:---:|:---:|
| dense-exact | 53.1% | **60.2%** | 0.636 |
| **rerank k30 + quota5** | **54.8%** | **61.9%** | 0.675 |
| rerank k30 + quota3 | 51.9% | 59.1% | 0.650 |
| rerank k100 + quota5 | 50.6% | 55.9% | 0.619 |

### v1 Oracle 天花板：94.3%（决定性）

v1 的 85% 舒适可达（余量 9.3pp；v0 仅 1.7pp）。重定位 + claim OR/AND 把天花板抬高 7.6pp。

> **dev/holdout 不对称**：holdout 比 dev 高 ~17pp 的根因是语料格式构成不同（dev 有 65 个 pdf_page 案例、holdout 只有 ~10），不是难度差异。两边 per-format claim recall 一致（PDF 55.0% / lines 69.1%）。**dev 是更难的、正确的优化目标**；holdout 高是 markdown 便宜，不能据此低估 dev 差距。

## 四、已证伪方向与通用教训（防回归清单）

> **通用教训：凡是「往 rerank 池里加候选」的路线都走不通。** v2-m3 reranker 面对扩大后的候选池，会把「看起来相关」的非 gold 候选（keyword 字面匹配、紧挨强命中的邻接页）排进 top-10，挤掉 gold。病根在 reranker 的判别力，不在候选来源。以下全部实测为负或过拟合，**不要重试**。

1. **调大 chunk_size 去洪**（2026-08-01）——dev +6.8pp（60.2→67.0）但 **holdout 仅 +1.4pp**（77.7→79.1），收益衰减 ~5 倍。chunk 边界对齐的过拟合杠杆，不是「更大更好」。去洪正解是修分块器（§六）。
2. **「一页一块」分块**（2026-08-01）——合并同页碎片 **12/16 例 dense 相似度反而变差**（碎片已含查询信号，整页稀释，如 pi07 p2 合并 0.33 vs 最优碎片 0.48）。
3. **动态 top_k**（2026-08-01）——按 rerank 分数置信度触发放宽，**precision 仅 0.17**（抓 1 个需放宽 case 就误放宽 ~5 个）。根源：不看到答案无法预判需多少块，所有可观察代理信号都太弱。
4. **keyword 融合进 rerank 池**（2026-08-02）——7 剂量全负、完全单调：kw5 −1.1pp → kw10 −2.5pp → kw30 −5.5pp → kw30/q10 −10.1pp → kw50 −19.4pp。keyword 确实救回 9–13 个 dense 漏掉的 case，但 v2-m3 把 keyword-only 候选（字面匹配、语义弱）排进 top-10 挤掉 dense gold，砸坏 2 倍以上。配额挪后也救不回。
5. **页邻接扩展**（2026-08-02）——k8/w1 −1.1pp、k12/w1 −2.2pp、k8/w2 −2.5pp。救回 2–5、砸坏 7–10。同根因：reranker 分不清「在强命中附近」和「就是 gold」（qa-179 实证：邻接候选占 rr9/10，gold 从 rr9/10 被挤出）。
6. **页级表示强化（前缀）**——页标题「2. Related Work」太泛，前缀 ~20-40 字符被正文 ~500 字符淹没，gold 页 dense 排名零变化（17→17）。
7. **换更大 embedding**（Jasper 2048 维）——源内排名几乎不变。**单向量稠密检索有秩天花板**（S=UVᵀ，rank≤d），扩大同一范式的容量无效；突破需多向量晚交互（ColBERT/ColPali）范式。
8. **两级检索（文档级→页级）**——文档内 top-20 才一半。
9. **claim 子查询扩展 = 作弊，已撤销**——用 answer_claims 的 text 作子查询 = 用答案反查答案位置，生产不可用；dev/holdout 都涨不能证明真实（泄漏系统性）。**评估必须只用 question，不用任何 gold 信息喂检索。**

## 五、门禁放宽到 Recall@10（2026-08-01）

### recall@k 曲线（v1 + rerank，dev，claim 级）

| k | claim recall | Δ vs k=5 |
|---|---|---|
| 5 | 61.9% | — |
| **10** | **72.5%**（正式 gate 验证） | **+10.6pp** |
| 15 | 74.1% | +12.2pp |
| 20 | 74.1% | 饱和 |

**核心发现**：黄金内容一直在检索池里，只是 **k=5 截断把它丢在 rank 6-15**。k=10 回收 +10.6pp，k=15 几乎吃满——不是靠更强 reranker，而是给下游更多上下文，与 v2-m3 **正交、可叠加**。

**决策**：固定 k=10（简单可靠，profile `final_k: 10`、protocol `recall_k: 10`）；schema `recall_k` `const: 5` → `enum: [5, 10]`。动态 top_k 已证伪（§四③）。

## 六、有效改进：PDF 段落结构 + 段落边界 chunk + 多栏布局修复（2026-08-02）

### 动机与假设（保留）

「修 C：PDF 公式保真」具体化为两个可分离改动：

1. **结构**：`page.get_text("text")` → `get_text("dict")`，span 按 baseline(y1) 分组、组内按 x 排序拼行——修复公式 glyph 阅读顺序；再按字号/行距/缩进分组为 heading/paragraph/list 节点（等价 Markdown 段落结构）。
2. **chunk 边界**：把 `chunk_size` 从「硬切上限」降级为「合并目标」。新引入 `max_segment_size=4096`，段落小于它时绝不被切；只有病态超长 segment（旧 parser 的整页 blob）才按行切，且走词边界。

核心假设（用户提出）：**chunk 太碎不利于 embedding，也稀释下游 LLM 可获取的上下文；512 是任意上界，硬编码字符上限本身就是过拟合来源。**

### 受控 2×2（cs512，隔离 parser 与 chunker 的独立贡献）

| 组合 | dev claim@10 | holdout claim@10 |
|---|:---:|:---:|
| 旧 parser + 旧 chunker（基线） | 73.0% | — |
| 旧 parser + 新 chunker | **61.9%** | 86.4% |
| **新 parser + 新 chunker** | **73.8%** | **87.0%** |

**两个关键事实**：

1. **新 chunker 依赖段落结构**。旧 parser 每页只产 1 个 RAW_TEXT blob，新 chunker 不硬切它（< 4096）→ 整页大 chunk → dev 崩到 61.9%（密集段落信号被稀释）；holdout 反而没崩（86.4%）——印证 dev/holdout 不对称（§三）。**段落结构 + 段落边界 chunk 是一个整体，缺一不可。**
2. **cs512 组合是干净的、dev/holdout 一致的增益**（73.8% / 87.0%）。chunk 数 6337→4995（−21%），半词碎片消失。

### 过拟合复查：cs512 vs cs1536

cs1536 在 dev 上 +0.9pp claim / +2.5pp evid，但 holdout 上 −0.9pp / −1.1pp——与 §四① 完全同构的过拟合模式。**cs512 是干净点，cs1536 的增量不作修复**。chunk 给下游 LLM 更多上下文有好处，但 k=10 检索指标测不到该收益（截断在 rerank 阶段），那是下游 LLM 的增益。

### 决策与改动

- **`pdf_parser.py`**：dict-mode span 重建 + 几何启发式（heading/paragraph/list 节点，局部行号锚定防整页行号洪水）。scanned/corrupt/empty 错误路径保持不变。
- **`chunking.py` / `orchestrator.py` / `structure_chunker.py`**：新增 `max_segment_size=4096`；段落绝不为凑 `chunk_size` 硬切；config hash 纳入新字段。
- **`evaluate_retrieval.py`**：恢复 `--chunk-size` 实验参数。
- **测试**：+9 PDF 结构测试（heading/段落/list/公式 span 顺序/跨页 body 锚定）、+2 chunker 段落原子性、config-hash 新字段。525 passed，ruff/mypy 全过。

### ④ 多栏 PDF 布局修复（追加）

**问题**：§六 提交后 `code_and_nl` 从 90.9% 回落到 75.8%（qa-224/qa-222 掉到 0），逐 case 定位为 gold 页在检索池里**完全消失**（不是 rerank 挤掉，是 dense 捞不到）。

**根因**：`_iter_spans` 把所有 block 的 span 打平、按 baseline 全局排序拼行，**丢掉 PyMuPDF dict 的 block 边界**。对两栏 PDF（physics 5 源几乎全双栏、math/rudin 103 页、23/30 个 PDF 源有双栏页），左右栏 span 在同一 baseline 高度被交错误排成一行 → 语义混乱的巨型段落（pi06 p6 出现 5871 字符交错块）→ embedding 稀释。

**修复**：`_iter_blocks` 按 PyMuPDF block 分组返回 span；`_page_visual_lines` 逐 block 独立重建 visual line，再按 block 顺序拼接。两栏各自保持独立流。

**验证（dev，cs512）**：

| 配置 | claim@10 | evid@10 | MRR | code_and_nl | single_doc |
|---|:---:|:---:|:---:|:---:|:---:|
| §六 提交（segchunk） | 73.8% | 65.7% | 0.642 | 75.8% | 75.8% |
| **+ 多栏修复** | **75.8%** | **67.0%** | **0.676** | **90.9%** | **79.1%** |

- **code_and_nl 完全恢复**（75.8→90.9%），证明诊断正确：是两栏交错混了伪代码/正文，不是段落结构本身。
- claim@10 75.8%（vs 基线 73.0%，**+2.7pp**）；MRR 0.676。
- **过拟合复查（按格式切分）**：dev PDF 案例 69.1→72.4（+3.3pp），holdout PDF 案例 76.1→76.1（持平，**非下降**）——干净的 PDF 改善，非边界对齐巧合。
- **测试**：+1 两栏不交错。526 passed，ruff/mypy 全过。

## 七、收口实验（2026-08-02）

### ① quota 扫掠（k=10）：q5 最优，问题关闭

基线本来就是 q5（profile `dense-rerank-k30-q5-k10`）。dev 扫掠 q3/q5/q8/q30（其余参数全同）：

| quota | claim@10 | evid@10 | MRR | p95(ms) |
|:---:|:---:|:---:|:---:|:---:|
| 3 | 65.9% | 58.6% | 0.644 | 514 |
| **5（基线）** | **75.8%** | 66.9% | **0.676** | 656 |
| 8 | 76.3% | 67.8% | 0.664 | 775 |
| 30（≈无上限） | 74.7% | 66.9% | 0.659 | 913 |

- **q3→q5 +9.8pp**：quota=3 在 k=10 下饿死 cross-document 的第二个 gold（18 跌 2 涨）。
- **q8 是噪声**：逐 case 9 涨 9 跌、类别同分布，MRR 反降。
- **q30（无上限）有害**：rerank 池稀释——§四 通用教训再次复现（MRR 0.659）。

**结论：quota 保持 5，问题关闭，不要重扫。**（扫掠配置/结果为本地产物，未保留）

### ② PDF 乱码量化：0% 乱码，真瓶颈是跨语言语义匹配

方法（`garble_quantify.py`，本地产物未保留）：从 eval-pg 抽取金页文本、TEI 嵌查询、exact dense@30 判池内。39 个失败 case 含 **63 个 PDF gold 单元**：

| 指标 | 数量 |
|---|:---:|
| 乱码（quote 词汇抓不到） | **0 / 63（0%）** |
| token_recall ≥ 0.6（内容在、可读） | **60 / 63** |
| exact 子串命中 | 3 / 63（公式词序/标点差异，非缺内容） |
| 未进 top-10 | 37 = 池外 23（**全可读**）+ 池内排掉 14 |

**分布**：NOT-retrieved 37 = papers 20 / physics 11 / math 6；类别 cross_document_synthesis 14 / single_document_factual 10 / bilingual 8 主导。池外 23 中 papers 15 / physics 6 / math 2。

**样本揭示真瓶颈**：qa-206 查询「世界空间重建 vs 相机空间重建差在哪」vs 金页（en）"Estimating hand motion on world coordinates..."——**答案句子在页里、token 全中，但 768 维稠密向量桥不过 zh→en**。其余样本同类（qa-207/209/211 papers，qa-179/198 Rudin）。

**结论**：
- **Nougat / 数学 OCR 修 recall 是死路（天花板 ≤5%），别建。** 提取没坏，乱码率 0%。
- 真瓶颈是「查询↔页」语义匹配，**且集中在跨语言**（zh 查询 → en/math 页）。
- **活跃杠杆**：① **双语查询扩展**（zh 查询 + 英文关键词再嵌）——就是 Stage 4 查询改写的形态，pilot 已证 +2.8pp，打的正是一批跨语言 case；② **ColPali 视觉页级检索**——token 级 MaxSim 可能桥接 zh/en，重、未证，作为 ① 收益递减后的备选。
- 公式阅读顺序乱只伤下游 LLM 读公式，**不伤 recall@10**（检索指标测不到）。

## 八、结论：检索侧收口

1. **检索侧（B/C/D/E）全部杠杆已实测证伪或到顶**：dense 排名（§一）、reranker 换型（§二）、v1 协议（§三）、证伪清单（§四）、k=10（§五）、PDF 结构/多栏（§六）、quota 扫掠（§七①）、乱码量化（§七②）。
2. **最终基线：dev claim@10 = 75.8%（MRR 0.676），holdout 87.0%。** 门禁 Recall@10。
3. **剩余 recall 空间在 A 环节**（zh→en 查询桥接）：归 **Stage 4 查询改写**（QueryPlanner，`rewrite_enabled` 默认关，R4-04 决策；pilot 证据 +2.8pp 固定 / +4.3pp oracle，非作弊）。**不在检索分支重复建改写机制。**
4. **收口决策**：75.8% 定为检索侧基线。若要继续，先配 LLM（fast_chat）做合法查询扩展，否则接受现状转向生产可用性。

---

## 附录

### 关键复现命令

```bash
# 前置：eval-pg (5433) + 本地 TEI (8080) + reranker (8081)
EVALUATION_DATABASE_ISOLATED=1 POSTGRES_PORT=5433 POSTGRES_DB=evaluation \
POSTGRES_PASSWORD=eval_only_pw EMBEDDING_ENDPOINT=http://localhost:8080 \
uv run python scripts/evaluate_retrieval.py --config <cfg> --split development --output <out>

# cfg: cases/evals/configs/retrieval-v1.yaml（v0 基线）
# rerank 需加 RERANKER_ENDPOINT=http://localhost:8081 RERANKER_MODEL=bge-reranker-v2-m3
# 实验配置（k10 gate / quota 扫掠）与乱码量化脚本均为本地产物，未保留
```

### 关键数据文件

> 各实验原始产物（JSON 结果、实验配置、运行日志）均为本地 `tmp/` 工作区文件，未入库、未保留；本节数据全部体现在正文数值中。

| 文件 | 内容 |
|---|:---|
| `cases/evals/datasets/knowledge-qa-v1/` | v1 数据集（claim 级 OR/AND 协议） |

### 交接：当前运行状态（2026-08-02 收口）

- **分支**：`dev/recall-optimization`（已 squash 为 17 个 commit，工作树干净）。
- **运行容器**：`eval-tei`（embedding，:8080）、`eval-tei-rerank`（bge-reranker-v2-m3，:8081）、`eval-pg`（:5433，隔离评测库）。
- **评测库语料**：cs512（新 parser + 段落边界 chunk + 多栏修复，5403 chunks，v0 manifest）。
- **模型**：Qwen3-Embedding-0.6B（TEI）；bge-reranker-v2-m3（TEI 原生）。无 LLM（fast_chat 未配置）。
- **结论**：检索侧收口于 75.8%。剩余 recall 归 Stage 4 查询改写（证据已备：+2.8pp）。
