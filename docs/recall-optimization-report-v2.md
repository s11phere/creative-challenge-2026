# Recall 优化实验报告 v2

> 分支：`dev/recall-optimization` ｜ 基线：`tmp/retrieval-eval-baseline.json`（commit `cde331c`）
> 门禁：Recall@5 ≥ 85% ｜ **基线 58.6% → v0 实测最优 61.9% → v1 实测 60.2%/61.9%**

**TL;DR**：v1 报告宣称丢弃 TOC 段达 90.48%，已被判定为虚假（破坏 PDF 结构，`3b97c5f` 回退）。v2 独立重查，根因是**正确 chunk 被 dense 排名压到池内 6–30 位**（不是 TOC）。候选方向全部实测收口，v0 最优 61.9%。接入同学修订的 **v1 数据集 + claim 级评分**后，**Oracle 天花板 94.3%，85% 目标舒适可达**。

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

## 四、结论与下一步

1. **v1 是正确基准**：标注修复 + claim 协议使 85% 从「勉强够到」变「正常可达」。
2. **当前 v1 最优 61.9%**，对 94.3% 天花板还有 ~33pp 排序差距。
3. reranker（bge-reranker-base）在 v1 上仅 +1.6pp，验证「模型不够强」。
4. **剩余路径**（按性价比）：更强的 reranker（v2-m3）/ 更大 embedding（1.8B）；或先修池子洪水（§一）抬高天花板本身。

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
```

### 关键数据文件

| 文件 | 内容 |
|---|:---|
| `tmp/retrieval-eval-baseline.json` | v0 基线 |
| `tmp/retrieval-eval-exp{1,2,4,5}.json` | 实验 1/2/4/5（配额/rerank/分块修复/新chunks+rerank） |
| `tmp/retrieval-eval-v1b.json` / `v1-rerank.json` | v1 dense / v1+rerank（claim 级） |
| `cases/evals/datasets/knowledge-qa-v1/` | 同学修订的 v1 数据集 |
| `tmp/retrieval-v1-*.yaml` | v1 评测配置 |
