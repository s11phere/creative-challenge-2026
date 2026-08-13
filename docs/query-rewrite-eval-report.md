# LLM Query 改写评测与优化实验报告

> 日期：2026-08-08 · 分支：`main` · 环境：本地 Compose（DeepSeek v4 flash fast_chat + 本地 TEI embedding/reranker）· 方法：`scripts/evaluate_query_rewrite.py` 配对 A/B（rewrite on/off）+ context coverage@32 指标
>
> **状态：provisional engineering**，非 Stage 0 / holdout 正式验收记录。

## 1. 背景与目标

知识问答的检索阶段已用 LLM（fast_chat）对用户问题做多路查询改写（`QueryPlanner` → `LlmQueryRewriter` → `QASearchCoordinator`，`qa_execution.py:526` 已启用 `rewrite_enabled=true, max_subqueries=4`）。需要回答三个问题：

1. 这套改写到底给 QA 带来了多少收益？（量化基线）
2. 是否有值得做的改进？（检索侧杠杆 / prompt 强化 / 生成策略）
3. 用什么数据、怎么测才算公平？（评测方法）

## 2. 评测方法（新增 harness）

### 2.1 为什么不能用现有检索评测

`scripts/evaluate_retrieval.py` 直接 `SearchService.search(case.question)`（`evaluate_retrieval.py:689`），**不经过 QueryPlanner 改写**，且用 recall@10——top-10 截断会掩盖改写收益（改写命中的证据常落在 rank 11–32）。因此新增 `scripts/evaluate_query_rewrite.py`。

### 2.2 配对 A/B 设计

对每条 case 跑两条**完全相同的 QA 检索链路**，唯一差别是 `rewrite_enabled`：

```
case.question
  ├─ rewrite OFF：原问题 → 1 条检索
  └─ rewrite ON ：LlmQueryRewriter(DeepSeek) 生成子查询 → 合并检索
      两条都走 QueryPlanner → QASearchCoordinator → SearchService（生产 QA 同款）
```

- **指标**：context coverage@32（`limit=max_evidence_items`，QA 真实上下文宽度）——即 gold 证据进没进前 32 条合并结果；同时给 claim recall@32、full coverage、MRR。
- **配对统计**：逐 case 对比 evidence recall@32 的改善 / 回退 / 不变，以及 off 未全覆盖 → on 全覆盖的个数。
- **防泄漏（R4-04）**：只喂 `case.question`，重写器 question-only，绝不喂 gold claims/答案。
- **隐私**：报告经 `_privacy_scan`，不含 question/原文/evidence 文本。

### 2.3 数据与环境

| 项 | 值 |
|---|---|
| 语料 | `cases/evals/corpus/v0`（frozen，90 源 / 12 space，internal_team_only） |
| 数据集 | `cases/evals/datasets/knowledge-qa-v1`（276 = dev 127 + holdout 149） |
| 实际评测 | dev split，格式过滤后 **111 例**（16 例引用 code/notebook 等排除格式） |
| 检索 | `dense_rerank`，production `RetrievalProfileV1` 默认（final_k=5, rerank_k=10, dense_k=30） |
| embedding | Qwen3-Embedding-0.6B，`qwen3-knowledge-qa-v1` 前后缀（`.env` 已配置） |
| reranker | bge-reranker-v2-m3（TEI） |
| 改写 LLM | DeepSeek v4 flash（fast_chat，temperature 0） |
| 隔离库 | `eval-pg` 容器（:5433，db=`evaluation`），语料 5454 chunks |

### 2.4 复现命令

```bash
# 首次：摄入语料到隔离库（~5 分钟）
EVALUATION_DATABASE_ISOLATED=1 POSTGRES_PORT=5433 POSTGRES_DB=evaluation POSTGRES_USER=app POSTGRES_PASSWORD=eval_only_pw \
EMBEDDING_ENDPOINT=http://localhost:8080 RERANKER_ENDPOINT=http://localhost:8081 \
.venv/Scripts/python.exe scripts/evaluate_query_rewrite.py \
  --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml --prepare-corpus --limit-cases 5 --output tmp/smoke.json --quiet

# 全量 dev A/B（~5 分钟，语料已缓存则无需 --prepare-corpus）
EVALUATION_DATABASE_ISOLATED=1 POSTGRES_PORT=5433 POSTGRES_DB=evaluation POSTGRES_USER=app POSTGRES_PASSWORD=eval_only_pw \
EMBEDDING_ENDPOINT=http://localhost:8080 RERANKER_ENDPOINT=http://localhost:8081 \
.venv/Scripts/python.exe scripts/evaluate_query_rewrite.py \
  --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml --output tmp/query-rewrite-dev.json --quiet
```

可调参数：`--max-subqueries`、`--rewrite-timeout`、`--mode`、`--limit`、`--limit-cases`。holdout 被 formal gate 阻断（`formal_runs_enabled: false`），不应跑。

## 3. 实验与结果（dev 111 例）

### 3.1 基线：现有改写器（通用 prompt + msq=4）

| 指标 | rewrite off | rewrite on | Δ |
|---|---|---|---|
| Evidence recall@32 | 52.3% | **72.8%** | **+20.5pp** |
| Claim recall@32 | 59.1% | **78.5%** | **+19.3pp** |
| 完整证据覆盖率 | 45.5% | 62.4% | +16.8pp |
| MRR | 0.670 | 0.702 | +0.033 |
| P95 延迟 | 0.2s | 2.0s | ~10x |

配对：改善 **33** / 回退 **0** / 不变 68 / off→full **17**；改写 111/111 生效，4 query/例，0 fallback。

**结论：现有改写器在 dev 上 +20.5pp evidence recall@32、+19.3pp claim recall@32，且 0 回退。**

### 3.2 A1：query 指令前缀（none-v1 → qwen3-knowledge-qa-v1）

**发现：`.env` 本来就已经是 `qwen3-knowledge-qa-v1`（query + document 双侧）**，不是 `config.py` 代码默认的 `none-v1`。A1 的 env override 为 no-op（A1 报告与基线报告 `embedding_identity` 逐位相同）。**无需切换**，基线本身就是 qwen3 指令下的结果。

> 教训：判断生效配置要看 `.env` 实际值，不能只读代码默认值。

### 3.3 B1：强化 prompt（双语双版本 + 同义词 + 实体逐字）

针对报告 §七② 诊断出的双语瓶颈（zh 查询 → en 页），把通用 prompt 强化为「关键实体逐字保留、同一实体中英各发一版、覆盖同义词」。

| 指标（on） | 基线 | B1 | Δ |
|---|---|---|---|
| Evidence recall@32 | 72.8% | 69.9% | **-2.9pp** |
| 完整覆盖率 | 62.4% | 56.4% | **-5.9pp** |
| MRR | 0.702 | 0.673 | **-0.029** |

- 分类级：bilingual 63.8→61.7（**没救回**），single_document_factual -4.3pp，全部分类持平或略降。
- 配对：improved 29（基线 33），off→full 11（基线 17）。

**结论：强化 prompt 是净负收益，已回退。**

### 3.4 B2：按问题类型特化 prompt（COMPARISON/PROCEDURAL/SYNTHESIS）

把 `classify_question` 结果传进改写 prompt，让「对比 A 和 B」拆成 A/B/指标 子查询。

| 指标（on） | 基线 | B2 | Δ |
|---|---|---|---|
| Evidence recall@32 | 72.8% | 72.0% | **-0.8pp** |
| 完整覆盖率 | 62.4% | 60.4% | **-2.0pp** |
| MRR | 0.702 | 0.677 | **-0.026** |

- 分类级：cross_document_synthesis 67.7→64.6（-3.1pp），其余持平。
- 配对：improved 32 / off→full 15。

**结论：按类型特化的 prompt 同样净负，已回退。**

### 3.5 D1：max_subqueries 扫掠（2/3/4/6）

| msq | ev recall@32 | Δ vs off | claim@32 | 全覆盖率 | MRR | 配对改善 | P95 |
|---|---|---|---|---|---|---|---|
| 2 | 56.5% | +4.2pp | 62.7% | 46.5% | 0.659 | 7 | — |
| 3 | 64.9% | +12.6pp | 69.8% | 54.5% | 0.678 | 22 | — |
| **4** | **72.8%** | **+20.5pp** | **78.5%** | 62.4% | **0.702** | 33 | 2.0s |
| 6 | 78.7% | **+26.4pp** | 83.9% | **65.3%** | **0.633** ⚠️ | **42** | 2.8s |

**关键发现**：
- recall/覆盖率随 msq 单调上升（2→6 全程 +4.2→+26.4pp），**没有饱和**。
- **MRR 在 msq=4 见顶（0.702），msq=6 崩到 0.633——低于 rewrite off 的 0.670**。子查询越多，合并池被噪声候选稀释越严重（同报告 §四「往 rerank 池里加候选」的效应），第一个 gold 反而排得更靠后。
- 全程 0 回退。

**结论：msq=4 是甜点（MRR 最优 + recall 强 + 延迟可控），产线默认正是 4。** msq=6 只在「追求回答完整度、可接受首个命中变晚 + 40% 延迟」时考虑，但 MRR 跌穿不改写的基线是硬伤。

## 4. 总体结论

1. **现产线的 LLM 查询改写器已接近最优**：通用 prompt + msq=4 + qwen3 指令下，+20.5pp evidence recall@32、+19.3pp claim recall@32、**0 回退**。
2. **给改写 prompt 加任何指令约束都会变差**（B1 双语强化 -2.9pp、B2 类型特化 -0.8pp，双证）——通用 prompt 是经验最优，**不要再往 prompt 里加料**。
3. **msq=4 是平衡点**：更多子查询能多捞 recall（msq=6 +26.4pp）但稀释首位命中（MRR 崩穿基线），4 是 recall/MRR/延迟的最佳折中。
4. **A1 已是现状**（`.env` 早就是 qwen3 指令），无需改动。
5. **检索侧已收口**（与 recall-optimization-report-v2 §八 一致），剩余 recall 空间属于「zh→en 语义桥接」，改写已兑现其价值。

## 5. 产物

| 文件 | 内容 |
|---|---|
| `scripts/evaluate_query_rewrite.py` | 新增评测执行器（配对 A/B + context coverage@32） |
| 本报告 | 完整实验总结 |
| `tmp/query-rewrite-dev.json` / `-d1-msq*.json` / `-b1-*.json` / `-b2-*.json` | 各轮原始报告（本地工作区，gitignored 未入库） |

> 原始 JSON 产物按仓库约定留 `tmp/`（gitignored），不入库；正文数值即全部数据。
