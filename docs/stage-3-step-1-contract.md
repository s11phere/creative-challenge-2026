# 阶段 3 Step 1 检索契约与安全边界

> 状态：工程实现完成；等待阶段 0 与阶段 2 Step 9 门禁后进行正式检索验收  
> 完成日期：2026-07-21

## 决策结论

R3-01 已关闭。`packages/domain/src/domain/retrieval.py` 是检索输入、输出、错误和 Port 的唯一
领域契约，不依赖 FastAPI、SQLAlchemy、ORM、模型 SDK 或具体 Provider。

四种模式固定为 `keyword`、`dense`、`hybrid`、`hybrid_rerank`。`SearchFilters` 只允许
`source_ids` 和 `document_ids`，不包含可覆盖请求 `space_id` 的字段；Application 在召回前通过
现有仓库 Port 校验 Space、Source 和 Document 归属。后续 PostgreSQL Adapter 仍必须在 SQL
候选集合边界重复强制 Space、当前发布版本和 tombstone 条件，不能依赖 Application 校验替代。

`RetrievalStore` 只返回 Keyword 或 Dense 原始候选批次，不负责 RRF、精排或扩展。Query
Embedding 和 Reranker 分别通过 `QueryEmbedder`、`Reranker` Port 接入。候选和诊断显式携带
模型、Embedding、索引和 profile 版本；查询向量必须是有限的 768 维值，且模型版本必须与
活动 profile 完全一致。

## 错误与降级

稳定错误码为：

- `RETRIEVAL_SPACE_NOT_FOUND`
- `RETRIEVAL_INVALID_FILTER`
- `RETRIEVAL_EMBEDDING_UNAVAILABLE`
- `RETRIEVAL_EMBEDDING_DIMENSION_MISMATCH`
- `RETRIEVAL_TIMEOUT`
- `RETRIEVAL_RERANKER_UNAVAILABLE`
- `RETRIEVAL_PROFILE_INCOMPATIBLE`
- `RETRIEVAL_PROVIDER_POLICY_DENIED`

Keyword 不调用 Embedding。Dense 的 Embedding 失败始终返回错误。Hybrid 只有在在线上下文且
profile 明确选择 `keyword_fallback` 时，才能对 Embedding unavailable/timeout 降级；Provider
策略拒绝不能降级。Reranker 只有在线上下文且 profile 明确选择 `fused_fallback` 时才能回退。
`offline_evaluation` 对任何降级都返回错误，使该次评测无效。无命中返回成功空结果。

Diagnostics 记录请求模式、实际模式、候选数、各阶段耗时、版本、filter 类型与降级原因。
`SearchHit.safe_summary` 仅含 Chunk hash、字符数和 locator 数，不包含查询或正文，可用于日志；
完整正文仍只在有权限的检索结果对象内传递。

## 排序不变量

加权 RRF 固定为：

```text
(1 - fusion_alpha) / (rrf_k + keyword_rank)
+ fusion_alpha / (rrf_k + dense_rank)
```

以 `chunk_id` 去重，相同文本但不同 Chunk 不合并。排序依次比较 fused score、最佳单路 rank、
稳定 Chunk ID；输入列表顺序不会改变输出。Reranker 必须一一返回输入索引，不允许丢失、重复
或越界。

本步只冻结 RRF 契约与性质测试，R3-04 的上下文扩展、参数范围消融和离线收益验证仍留在
Step 6/9，不能据此宣称 R3-04 已关闭。
