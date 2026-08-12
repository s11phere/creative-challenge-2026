# 个性化路线图：Skill 与 Memory

日期：2026-08-12
状态：设计稿，待执行
范围：6 个 Phase 的总纲与跨阶段决策

## 背景与目标

本地优先、来源可追溯的个人知识工作台。目标：根据用户使用习惯定制"个人 skill"。两条路径：

- **Path A（用户自建）**：用户通过 Agent 引导创建自己的 skill。
- **Path B（自动提取）**：系统从使用痕迹自动识别用户工作模式并提议 skill。

核心反馈回路：`使用 → 痕迹 → 记忆 → 提取 → skill → 更个性化的使用`。

## 现状缺口（已探索确认）

- Skill 只读不可变（trusted_root + content_sha256），无创建/编辑/删除，激活硬编码于 `apps/api/src/api/main.py`。
- 应用层长期记忆不存在；只有会话内滚动摘要（`conversation_context_summaries`）。
- 无用户偏好/行为痕迹记录（仅每会话 effort / workspace）。
- skill eval（`evals/cases.jsonl`）声明但未接入运行时。
- workflow handler 需应用层注册；声明式 YAML 不能引入任意新行为（ADR-006）。

## 六阶段总览

| # | 阶段 | 一句话 | 主要依赖 | 交付形态 |
|---|------|--------|----------|----------|
| 1 | Eval 门禁（报告先行） | 让 skill eval 可执行、可判定、可出报告 | — | CLI 报告 ✅ 2026-08-12 |
| 2 | 使用痕迹记录 | 从 AgentRun 沉淀结构化痕迹，数据随使用积累 | — | 表 + 蒸馏任务 ✅ 2026-08-12 |
| 3 | 个人 Skill 存储 + 信任模型 | 个人 skill 可写、运行时加载、独立信任边界 | — | API + 存储根 + ADR ✅ 2026-08-12 |
| 4 | Skill Creator（Path A） | Agent 引导创建个人 skill，eval 门禁 + draft 审批 | 1、3 | skill 包 + 工具 + UX |
| 5 | 跨会话长期记忆 | agent 跨会话记得用户 | 2 | 记忆表 + 蒸馏 + 注入 |
| 6 | 自动提取（Path B） | 从痕迹识别重复模式，提议候选 skill | 1、2、4 | 挖掘 + 候选 + 审批 |

**依赖**：6 ← 4 ← 3；6 ← 2（1 提供门禁，2 提供数据）；5 ← 2。

## 跨阶段决策（所有 Phase 遵守）

1. **信任模型**：个人 skill 是低信任、可写、**仅组合已有 handler/tool**，不得引入新 Python 行为（扩展 ADR-006/017）。
2. **Eval 即门禁**：任何生成/提取的 skill 必须过 eval 才允许激活；生命周期 `draft → eval → 用户审批 → active`。
3. **确定性优先**：默认 fake 模型 + 结构化断言；LLM judge 留协议缝隙，不在早期实现。
4. **隐私/脱敏**：痕迹与记忆不存完整 Prompt / 私密正文，按 sensitivity 处理（数据门禁文化）。

## 执行约定

- 每 Phase 一份独立 spec（`2026-08-12-phaseN-*-design.md`），按序号交给 Agent 顺序执行。
- 每 Phase 完成后跑全量门禁：`ruff format/check + mypy + pytest`，并更新受影响文档、`export_openapi.py` 保持一致。
- 每 Phase 按项目 commit 规范单独提交，body 记录验证结果。
