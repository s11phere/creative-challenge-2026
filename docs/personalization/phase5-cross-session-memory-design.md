# Phase 5：跨会话长期记忆

日期：2026-08-12
依赖：Phase 2（使用痕迹/蒸馏管线）
产出：`memory_entries` 表 + 蒸馏任务 + 上下文注入机制

## 目标

agent 跨会话记得用户：从会话摘要与使用痕迹蒸馏持久事实 / 偏好 / 工作模式，新回合按需注入。

## 范围

**做**：memory_entries 表 + 蒸馏任务（复用 Phase 2 管线）+ 注入机制（向量检索 + 相关性 + sensitivity 过滤）。
**不做**：记忆编辑 UI（可后补）、自动提取（Phase 6）、注入无界化。

## 交付物

1. **新表 `memory_entries`**（新 Alembic revision）：`content`、`entry_type`(fact/preference/pattern)、`source_conversation_id`、`embedding`(pgvector)、`sensitivity`、`created_at`、`expires_at`(可选)。沿用 pgvector 迁移与 embedding_zh 能力别名。
2. **蒸馏任务**（worker）：从 `conversation_summaries` + Phase 2 `usage_patterns` → LLM 提炼持久事实/偏好/模式 → 写 memory_entries；含去重与更新策略（同实体更新而非重复插入）。
3. **注入**：新回合组装 `ConversationContextSnapshot`（`packages/application/src/application/assistant/context.py`）时，按当前问题检索相关 memory（向量 + 近因加权），注入 `<memory>` 块；**有界 top-K** + sensitivity 过滤（高危内容不注入或需显式确认）。
4. **可观测**：注入的 memory 进入上下文快照，trace 可见（沿用 assistant trace 机制）。

## 验收

- 跨会话问题能召回相关记忆并改善回答（对照无记忆基线可量化）。
- 注入有界、敏感内容被过滤。
- 全量门禁通过；测试覆盖：蒸馏、去重/更新、检索注入、sensitivity 过滤。
