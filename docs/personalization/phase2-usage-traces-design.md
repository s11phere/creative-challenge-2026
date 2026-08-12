# Phase 2：使用痕迹记录与蒸馏

日期：2026-08-12
状态：✅ 已实施（2026-08-12，commit 99528ae / 48ed48b / 收尾提交）
依赖：无
产出：`usage_traces` 表 + 采集钩子 + 蒸馏任务

## 目标

从已有 `AgentRun` / `tool_history` 沉淀结构化"使用痕迹"，数据随使用自然积累，为 Phase 5/6 备料。本阶段**只记录不消费**（不注入 agent、不做模式识别）。

## 范围

**做**：usage_traces 表 + 采集钩子 + 脱敏；周期性蒸馏任务产出"使用模式聚合"。
**不做**：注入 agent、模式识别（Phase 6）、长期记忆（Phase 5）、记忆编辑 UI。

## 交付物

1. **新表 `usage_traces`**（新 Alembic revision）：`run_id`、`conversation_id`、`skill_name`/`command`、`input_summary`(脱敏)、`tools_used`(list)、`outcome`(completed/failed/refused/clarified)、`model`、`sensitivity`、`created_at`。
2. **采集钩子**：worker 中 AgentRun 结束路径（`apps/worker/src/worker/assistant_tasks.py` 的 finalize）→ `UsageTraceService.record`；skill 调用事件一并记录。钩子放 application/worker 层，不动 `agent_runtime` 核心（保持 runtime 无 DB 依赖）。
3. **脱敏**：`input_summary` 截断 + 敏感字段清洗；不存完整 Prompt / 私密正文 / Provider 原始响应（数据门禁）。复用/扩展 sensitivity 语义。
4. **蒸馏任务**（Dramatiq worker，周期或按需）：raw traces → 聚合出 `usage_patterns`：(skill, 任务类别, 工具序列, 输入类型) 的频率与重复模式。Phase 6 的原料，本阶段只产出不消费。
5. **只读查看**：CLI 或只读 API 查 traces（调试用，可选）。

## 验收

- 每次 skill 调用落一条 trace，outcome 正确。
- 蒸馏任务产出可查的模式聚合；脱敏生效（无完整正文落库）。
- 全量门禁通过；测试覆盖：record 校验、outcome 分类、脱敏逻辑、蒸馏聚合。
