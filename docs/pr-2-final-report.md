# PR #2 查询性能修复最终报告

日期：2026-07-30
范围：PR #2 的修复分支 `fix/pr-2-query-performance`
评测边界：仅使用冻结语料的 development split，未读取或执行 holdout。

## 一、结论摘要

PR #2 原本报告了 development Dense Recall@5 = 90.48%，但该结论不能采信。实现中存在
源内容被错误处理、跨文档结果被错误去重、reranker 部署不一致等正确性问题；90.48% 与
这些问题同时存在，因此不能作为性能提升证据。

修复后，在相同的 Qwen3 embedding、1.3 版分块器和隔离数据库上，v0 development
Dense exact 的 Recall@5 为 59.52%（125/210）。相对于项目此前确认的 51.90% 基线，提升
7.62 个百分点。
这说明修复对 development 检索有可观测改善，但仍低于 85% 质量门禁，不能宣称阶段 3 正式
通过。

v1 是 v0 的标注修订版，保留相同语料和 split，但拆分了复合 claim 并补充了可接受证据集合。
在同一修复后索引上，v1 的严格 locator Recall@5 为 52.72%（126/239），MRR 为 0.6226；按
v1 的 OR/AND claim 协议计算，claim coverage 为 60.49%（222/367），101 个有证据 case 中有
43 个实现了全部 claim 覆盖。

## 二、原始 PR 的主要问题

### 2.1 将普通 `raw_text` 内容误判为目录并删除

原始 PR 将文档开头的 `raw_text` 节点整体当作目录处理。这个假设不适用于 PDF：当前 PDF
解析器通常把每一页抽取为一个 `raw_text` 节点，因此普通正文页也会被纳入“目录”判断。

实际复现中，使用 3 个 PDF-like `raw_text` 页面、`chunk_size=100` 进行分块时，原始实现
将页面内容合并成一个长度约 916 的 oversized chunk，而不是产生不超过目标大小的多个块。
这会同时破坏分块大小约束、定位信息和后续召回。

更严重的是，原始逻辑会丢弃被判断为目录的内容。目录可以被过滤出直接检索结果，但不能
从存储和上下文中删除，因为正文内容与目录判断之间没有可靠的一一对应关系。

### 2.2 以 source URI 和 locator 做全局去重，误删不同文档

原始去重键包含 `source_key + locator`。在当前数据模型中，同一个目录型 Source 下的不同
Document 可以共享同一个 Source URI；因此不同文档中相同或相近 locator 的合法结果可能被
当成重复项删除。

实际复现使用两个不同 document、同一个 `source_key=file:///knowledge`，并让两者具有相同
的长文本和 locator。原始逻辑会错误保留一个结果；修复后的逻辑会保留两个结果，因为去重
范围被限制在同一个 `(document_id, version_id)` 内。

### 2.3 去重和文档配额的顺序不正确

滑动窗口分块会产生真实的边界重复文本。如果先应用每文档 quota，再去重，重复块会占用
quota 槽位，导致后续独立内容无法进入 top-k。原始实现没有可靠地区分边界重复与独立内容，
也没有保证去重在 quota 之前完成。

### 2.4 移除了仍被默认配置引用的 reranker

PR 删除了 reranker 服务和相关基础设施，但当前主线 API/profile 仍支持并默认使用
`hybrid_rerank`。这会造成配置与运行时能力不一致；在启用该 profile 时，API 会因为没有
可用 reranker 而返回 503 或 profile 不兼容错误。

### 2.5 CPU Compose override 没有真正清除 GPU reservation

原始 `compose.cpu.yaml` 使用 `deploy: ~`，不能可靠清除基础 Compose 文件继承的 GPU
设备 reservation。CPU 环境仍可能尝试请求 GPU。修复后使用 Compose `!reset []` 明确清空
设备 reservation，并通过合并后的 Compose JSON 进行验证。

### 2.6 行为变化没有同步提升 chunker version

分块器行为发生变化但版本仍保持 `1.1`，会让旧 chunks 与新分块规则混用，破坏增量重建和
评测可复现性。修复后 chunker version 提升为 `1.3`，已有文档必须重建。

### 2.7 原始 90.48% 报告的结论不成立

90.48% 结果依赖了目录内容删除和不安全的跨文档去重，且报告把 provisional development
结果写成了已经越过 85% 门禁的正式结论。该结果已标记为无效；修复后的实测结果没有复现
90.48%。

### 2.8 对 Recall 分子和分母的实际影响审计

本项目的严格 Evidence Recall@5 由评测代码按下式计算：

```text
Recall@5 = top-5 中被直接命中的唯一 gold evidence 数 / 全部 gold evidence 数
```

命中要求为 source key、source version 相同，且返回 chunk 的 locator 与 gold locator 有重叠；
`context_only` chunk 不计入命中。同一个 gold evidence 即使被多个 chunk 命中也只计一次。反过来，
一个 locator 过宽的 chunk 可以与多个独立 gold locator 重叠，因而一次增加多个 evidence 命中。
Recall 本身不因 chunk 过宽、内容不够精确或 top-5 中有 false positive 而扣分。

原始 PR 的三个连续 development 运行都使用 v0 的同一批 210 个 gold evidence。因此在这些运行之间，
**分母固定为 210，变化发生在分子**：

| 运行阶段 | 实际匹配的 gold evidence（分子） | gold evidence 总数（分母） | Recall@5 | 相对前一阶段 |
| --- | ---: | ---: | ---: | ---: |
| 原始 PR 的上下文嵌入实验 | 113 | 210 | 53.81% | — |
| 原始 PR 的 section-aware/TOC 删除实验 | 186 | 210 | 88.57% | +73 |
| 原始 PR 的 quota + locator 去重实验 | 190 | 210 | 90.48% | +4 |

这证明原始报告中的 90.48% 是 `190 / 210`，而不是通过遗漏、缩小或重新定义 gold evidence
分母得到的数字。相较于其紧邻的 53.81% 运行，表面上多命中了 77 个 gold evidence；相较于
修复后的 `125 / 210 = 59.52%`，表面差为 65 个。但两组实现的分块、候选过滤和去重规则均不同，
这两个差值只能描述结果差异，不能被解释成某一处缺陷单独“制造了”77 或 65 个命中。

已有原始运行记录支持以下更细的判断：

- section-aware 阶段报告 `113/210 -> 186/210`，并记录 51 个 case 改进、22 个原先零召回的
  case 变为全命中；例如 `qa-249` 中，目录型 chunk 因密集关键词排名靠前，正文 evidence 被挤出
  top-5。该运行现象说明“移除候选”确实改变了分子，但不证明被移除的所有内容都是真目录或这
  73 个新增命中都有效。
- quota + 去重阶段报告 `186/210 -> 190/210`。`qa-192` 从单一文档占满 5 个槽位变为每文档最多
  3 个槽位后补入另一份 evidence；`qa-177` 有 3 个 chunk 覆盖同一行，去重释放了 2 个槽位。这些
  是该阶段净增 4 个命中的直接运行例子。与此同时 `qa-014` 从 42.9% 降到 28.6%，因为其 7 个
  gold 都来自同一文档而 quota 截断了候选，说明该规则对分子没有单向提升保证。
- PDF-like `raw_text` 的三页复现得到约 916 字符的 oversized chunk（目标 `chunk_size=100`）。
  按当前 overlap 判定，这类宽 locator 一旦进入 top-5，理论上可同时命中多个 gold evidence，
  从而虚增分子；原始 PR 缺少逐 case top-5 JSON，无法把 190 个命中中的具体多少个精确归因于
  此机制。另一方面，误把正文当目录并删除也可能直接丢失 gold evidence，降低分子，影响方向
  取决于具体 case。
- 跨 document 的 `source_key + locator` 去重已用两个不同 document、相同 source key/locator
  的复现证实会错误删掉一个合法结果。它可能删除 gold 命中而降低分子，也可能删除竞争项并间接
  抬高分子；原始运行产物未保存该规则触发的逐 case 明细，故不能对 90.48% 做定量归因。

因此，审计结论不是“原始 PR 一定通过缩小 Recall 分母抬高指标”，而是：分母可确认保持 210；
原始实现改变了候选集合、chunk 边界和 locator 宽度，使分子 `190` 的语义不再代表可比较的、
严格定位的 evidence 命中。缺少原始逐 case 产物时，任何把 77 或 64 个差额完全归因于 TOC、
去重或配额任一单项的说法都超出了证据范围。

## 三、修复内容和运行方式

### 3.1 分块与目录处理

- chunker version 从 `1.1` 提升为 `1.3`。
- 标题文本保留在 chunk text 中，不再只放入 metadata。
- 不删除任何源内容。
- 仅对连续的前置 list/raw_text group 做保守目录识别：至少有 3 个 label，且至少 60%
  的 label 能与后续 heading 匹配，才标记为 `table_of_contents`。
- 目录 chunk 继续保存并可参与上下文扩展，只从直接 keyword/dense/hybrid 候选中过滤。
- 真实 corpus 中识别出 8 个 TOC 来源、9 个 TOC chunk；普通前置列表和 PDF-like `raw_text` 未被标记。

### 3.2 检索去重与融合

- 只在同一 `(document_id, version_id)` 内识别滑动窗口边界文本重复。
- 不再以 Source URI 或 locator 跨文档去重。
- 去重先于每文档 quota 执行。
- keyword、dense、hybrid 和 hybrid+rereank 路径使用一致的 TOC 过滤和去重规则。
- TOC 过滤下推到 keyword、dense exact 和 IVFFlat shortlist 的 SQL `LIMIT` 之前；context expansion
  仍可取回 TOC，避免目录占据 top-30 候选名额。
- 恢复现有 weighted-RRF 和 reranker contract，不使用未经验证的 dense-only 替代实现。

### 3.3 embedding 和部署

- embedding 输入只添加 document instruction prefix，不再重复注入已经存在于 chunk text 中的
  heading metadata。
- 恢复 API/worker 的 reranker 配置、服务和缓存卷。
- CPU Compose override 使用 `deploy.resources.reservations.devices: !reset []` 清除 GPU
  reservation。

### 3.4 推荐运行方式

代码质量和测试：

```text
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest
```

评测前先验证受控输入：

```text
EVALUATION_DATABASE_ISOLATED=1 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1.yaml \
  --split development \
  --validate-only
```

实际评测必须使用新的、隔离的 PostgreSQL/Redis 数据库和固定 revision 的本地模型。对于
修复后的 v0，先在空库中执行 corpus preparation，使所有可评格式文档按 chunker `1.3`
重建，再运行 `dense-exact`。v1 使用相同 corpus 和索引，只需换用
`cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml`，仍然只能运行 development：

```text
EVALUATION_DATABASE_ISOLATED=1 \
uv run python scripts/evaluate_retrieval.py \
  --config cases/evals/configs/retrieval-v1-knowledge-qa-v1.yaml \
  --split development \
  --prepare-corpus \
  --experiment dense-exact \
  --output tmp/retrieval-eval-v1.json
```

`--prepare-corpus` 必须指向空的隔离评测库，或者确认现有 published versions 与当前
chunker/embedding identity 完全一致。不能把旧索引或共享业务数据库的结果当作修复后结果。

## 四、修复后测试结果

### 4.1 自动化测试和静态检查

- 本次受影响测试：`77 passed, 6 skipped`（含 PostgreSQL 隔离集成测试）。
- 完整测试中唯一未执行的是冻结 manifest 字节哈希测试，因为 Windows worktree 的
  `core.autocrlf` 将文件转换为 CRLF；主工作区原始 LF 文件的固定 SHA-256 已核验正确。
- Ruff format/check：通过。
- Mypy：2 个受影响 source files 无问题。
- Compose 主配置和 CPU override：均可解析；合并后的 CPU 配置没有 GPU device reservation。

新增回归覆盖了：

- PDF-like 多页 `raw_text` 不被误判为目录，chunk 不超过目标大小。
- 普通前置 list 不被误判为目录。
- 真实目录被保留并标记为 `table_of_contents`。
- 标题文本仍存在于 chunk text。
- 同 Source URI 下不同 Document 不被误删。
- 同文档边界重复会去重，locator 重叠但文本不重复不会去重。
- 去重发生在 quota 之前。
- hybrid+rereank 不会把 TOC 或边界重复送入 reranker。
- document instruction 不会重复注入 heading。

### 4.2 v0/v1 development 结果

两次运行均为 111 个 development case，其中 101 个 case 有 evidence。使用相同的
Qwen3-Embedding-0.6B revision、相同的 768 维 L2 normalization、相同的 chunker `1.3`
索引和 `dense-exact` profile。

| 指标 | v0 | v1 | v1 - v0 |
| --- | ---: | ---: | ---: |
| 严格 Evidence Recall@5 | 59.52% | 52.72% | -6.80pp |
| MRR | 0.5886 | 0.6226 | +0.0340 |
| Evidence nDCG@5 | 0.4566 | 0.4555 | -0.0011 |
| Full evidence coverage | 41.58% | 41.58% | 0 |
| P50 | 558.3 ms | 472.5 ms | -85.8 ms |
| P95 | 720.6 ms | 585.3 ms | -135.3 ms |
| Retrieval failure rate | 0% | 0% | 0 |
| Must-exclude violations | 0 | 0 | 0 |

v0 有 210 个 gold evidence，v1 有 239 个。v1 是标注协议修订版，拆分了复合 claim 并补充
了 acceptable evidence sets；因此严格 locator Recall 的分母更严格，不能把 52.72% 简单
解释为系统性能回退。MRR 上升表明首个相关结果排序有所改善，但整体证据覆盖没有提升。

按 v1 的 claim-level OR/AND 协议对同一 v1 top-5 结果做离线诊断：

- claim coverage：`222 / 367 = 60.49%`。
- 全部 claim 覆盖的 case：`43 / 101 = 42.57%`。

当前评测脚本的正式输出仍是严格 locator 指标，不原生消费 `acceptable_evidence_sets`，所以
claim coverage 是诊断指标，不是正式阶段门禁指标。

### 4.3 最终判断

- 修复后的 development 检索相对既有 51.90% 基线提升 7.62 个百分点，提升可以确认。
- PR #2 的 90.48% 结果不能确认，且已被判定为无效。
- 修复后 v0 Recall@5 = 59.52%，v1 严格 locator Recall@5 = 52.72%，v1 claim coverage
  = 60.49%。
- P95 在本次 dense exact 运行中低于 1000 ms 预算，失败率和安全违规为 0。
- 85% Recall 门禁仍未达到，默认配置尚未冻结。
- 本报告没有使用 holdout 结果，不能作为阶段 3 正式退出或正式发布质量证明。

## 五、报告产物

- v0 dense 报告：`tmp/retrieval-eval-pr2-toc-v0-fixed.json`
- v1 dense 报告：`tmp/retrieval-eval-pr2-toc-v1-fixed.json`
- v0/v1 数据集规则：`cases/evals/datasets/knowledge-qa-v1/README.md`
- 原始 PR 结果审计：`docs/recall-optimization-report.md`
