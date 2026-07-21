# 阶段 3 Step 0 启动基线与评测协议

> 状态：临时工程基线，非阶段 3 正式评测基线  
> 核对日期：2026-07-21

## 门禁结论

- 阶段 0 尚未关闭：corpus manifest 状态为 `draft_pending_license_review`。
- 仓库中不存在阶段 2 Step 9 正式验收记录。
- `cases/evals/corpus/v0/manifest.yaml` 当前列出 43 个来源；本地核对时文件全部存在且原始字节
  SHA-256 全部匹配。43 个来源均允许 `local_development` 和 `local_evaluation`，其中 9 个允许
  `repository_fixture`；34 个标记为 `private_local`，9 个标记为 `public_demo`。
- 因上述门禁未关闭，当前配置固定为 `provisional` 且 `formal_runs_enabled: false`。它只能用于
  本地工程验证，不能生成正式质量基线或支持阶段 3 达标声明。

## 数据快照

| 项目 | 当前值 |
| --- | --- |
| Corpus | `v0`，manifest SHA-256 `dd096e1aefc5b612124ed239d88fb035d0c42ee724eb52da7d8c72d12287d8b6` |
| Dataset | `knowledge-qa-v0`，30 例，SHA-256 `e0947b3c028c562ad99ad00c44b78642c2a9dd53542784da7a3dcf7f0bd086c2` |
| Development | 20 例，顺序敏感内容 SHA-256 `4628f7192042a4eea03861026b003d776fda535d633d20e3d0146f22e199874b` |
| Holdout | 10 例，顺序敏感内容 SHA-256 `67b34a4d071a8bc8512281701a2239467efb736e98dc639d93926d3f9d5fb2ff` |
| Dataset schema | SHA-256 `4949f3f4a9212f5fa02c915850f06a64bde501df0568e57fab582057a3b45e16` |

当前切片为：单文档事实 8、跨文档综合 8、版本/冲突 2、无答案 5、恶意文档 1、双语 3、
代码与自然语言 3。数据规模低于总计划建议的 60～100 例；阶段 0 冻结前必须决定发布新的
dataset version，或保留 v0 并正式接受统计限制。不得原地修改已查看的 holdout。

## 评分协议

每个 `evidence[]` 是独立证据单元。Chunk 只有同时满足以下条件才命中该证据：

1. `source_key` 完全相同；
2. `source_version` 与原始字节 SHA-256 完全相同；
3. locator 类型相同，且一基、闭区间页码或行号发生重叠。

主要指标固定为 Evidence Recall@5、MRR、evidence-unit nDCG@5、全证据覆盖率、
must-exclude 违规数、P50/P95 延迟和失败率。无证据 case 不进入 Recall、MRR、nDCG 或全证据
覆盖率分母，单独进入无答案、跨 Space、撤下版本和恶意文档切片。上下文扩展块不得用于
Recall 命中。

聚合 Evidence Recall@5 按全部 gold evidence unit 微平均；MRR、nDCG@5 和全证据覆盖率按
有证据 case 宏平均。must-exclude 违规数汇总全部 case。

`evidence-unit nDCG@K` 定义为每个证据单元首次命中排名的折损值
`1 / log2(rank + 1)` 的均值，K 内未命中记 0。该口径避免重叠 Chunk 重复累计同一证据。

## 临时运行条件

- 目标：Windows 10.0.26200，Intel64 family 6 model 183，32 logical processors。
- 并发：1；预热查询：3；采样：完整单次遍历。
- 临时 `retrieval_p95_budget_ms`：1000 ms。正式冻结前需在目标部署环境复核。
- holdout 只允许在 development 配置正式冻结后执行一次正式评测；当前 Step 0 不执行检索。

配置位于 `cases/evals/configs/retrieval-v1.yaml`，其 JSON Schema 位于同目录。当前仅支持：

```powershell
uv run python scripts/evaluate_retrieval.py `
  --config cases/evals/configs/retrieval-v1.yaml `
  --split development `
  --validate-only
```

命令只输出 ID、版本、hash、计数、协议和门禁原因，不输出问题、文档正文、引用片段或向量。
