# 术语表

> 对应计划阶段 0 产物

| 术语 | 英文对应 | 定义 |
|------|----------|------|
| 知识空间 | Space | 知识隔离的逻辑边界，每个空间有自己的检索配置、文档集和权限 |
| 数据源 | Source | 知识摄入的来源，可以是本地目录、文件夹、上传文件或未来的网页连接器 |
| 文档 | Document | 逻辑文档实体，内容修改不改变 Document ID，产生新的 DocumentVersion |
| 文档版本 | DocumentVersion | 文档的某个不可变快照，由内容哈希和解析器版本唯一标识 |
| 块 | Chunk | 文档的最小检索单元，包含文本、元数据和定位信息（章节、页码、行号） |
| 证据 | Evidence | 检索结果中支持回答的块级引用，包含来源、分数、排名和使用记录 |
| 内容指纹 | Content Fingerprint | 基于规范化内容的 SHA-256 哈希，用于检测变化和保证幂等 |
| 幂等摄入 | Idempotent Ingestion | 相同内容和配置重复提交不会产生重复块和重复索引 |
| 增量更新 | Incremental Update | 只重建发生变化的内容，未变化部分跳过解析和索引 |
| 混合检索 | Hybrid Search | 同时使用向量（dense）和关键词（sparse）召回后融合排序 |
| RRF | Reciprocal Rank Fusion | 混合检索中多路召回结果的排序融合方法 |
| 精排 | Reranking | 在初步召回后使用更精确的模型（通常是 cross-encoder）重新排序 |
| 引用 | Citation | 回答中对证据的结构化引用，包括 claim、source、locator |
| 拒答 | Refusal | 证据不足时模型明确表示无法回答，而非编造 |
| Skill | — | 有版本的工作流包，包含 manifest、workflow、prompts、schemas 和 evals |
| 工具 | Tool | Agent 可调用的外部能力，有唯一名称、输入输出 schema 和权限等级 |
| Agent 运行 | Agent Run | Agent 一次执行的完整记录，包含状态、步骤、工具调用、证据和预算 |
| 检查点 | Checkpoint | Agent 运行中途的可恢复状态快照 |
| 蒸馏 | Distillation | 从成功/失败运行中抽取稳定的步骤、prompt 和规则，优化 Skill 版本 |
| 模型网关 | Model Gateway | 统一封装 LLM/Embedding/Reranker 等模型调用的适配层 |
| 评测集 | Eval Dataset | 带标准答案和证据的结构化评测用例集合 |
| Holdout 集 | Holdout Set | 冻结的、不参与调参的评测用例子集，用于最终验证 |
| Parser | Parser | 将原始文件解析为统一 ParsedDocument 的适配器 |
| Chunker | Chunker | 将 ParsedDocument 切分为检索单元的模块 |
