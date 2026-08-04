# 阶段 5 Provisional 工程验收记录

## 2026-08-04 Engineering Completion Update

Runtime checkpoint persistence, approval and derived-knowledge exactly-once behavior, active Skill
pointer CAS/rollback, reference-aware cleanup, the three knowledge-organization Skills, their Web
entry points, Web feedback, and the durable feedback review lifecycle are implemented.
`scripts/export_feedback_candidates.py`
writes only a new development JSONL file and refuses frozen, manifest, or holdout paths; it never
copies question, answer, note, prompt, provider, or excerpt text.

The Stage 5 engineering implementation is complete for the current contracts, but the stage remains
provisional: no formal Skill evaluation or Stage 3/4 holdout result is claimed. Playwright/browser
coverage is unavailable and is recorded as not executed.

> 记录日期：2026-07-31
>
> 结论：Stage 5 当前可用子集已完成 provisional 工程验收；阶段 5 未正式退出。

## 验收范围

本次验收覆盖 ADR-006 约束的单 Agent Runtime、Tool/Skill Registry、受信包加载、固定摘要、
预算/权限/审计、事务式 reload/回滚、PostgreSQL active pointer，以及当前五个 Skill（`knowledge_agent
0.2.0`，保留 `0.1.0` 回滚包；其余四个为 `0.1.0`）：

- `knowledge_agent` 通过真实 `fast_chat` 决策节点在同一 QA Run 中调用受限只读 `grounded_qa` Tool。
- `knowledge_qa` 复用唯一 Grounded QA Application Port 和现有 QA Web/API/Worker/SSE。
- `summarize_document` 固定一个 current published DocumentVersion 并返回带引用摘要。
- `compare_sources` 固定至少两个同 Space 来源；回答缺少两个来源的 Citation 时拒答。
- `create_review_cards` 在审批前只生成带引用预览并返回 `side_effects=0`；审批通过后通过现有
  Derived Knowledge Port exactly-once 写入，并返回 `side_effects=1`。

知识整理 Skill 使用现有 QA Run 作为持久执行身份，提交时保存 Source/Document/DocumentVersion
范围，Worker 执行时通过 SearchService 再次校验精确 current published 版本。没有新增第二套
Runtime Run、Worker、SSE、取消、检索、问答或 Citation 协议。

`knowledge_agent` 只接受严格 `call_tool/complete/refuse` JSON，并由 Runtime 强制 Tool 白名单、最大
轮数、Token/Tool 预算、权限、Space 和 schema。当前 Tool 输出给模型的内容仅含状态、Citation 数和
结果类型；回答和原文不进入外层模型上下文，写 Tool 在持久审批/幂等事实源完成前保持禁止。
真实 Provider 增量还验证了 `fast_chat` 外部路由与 fake Embedding/Reranker 能力级隔离，以及
`grounded-answer-v1` schema 指令对真实模型结构化输出的约束。隔离 Compose Run 完成并发布 3 条
Citation，3 条均可解析为 `valid` 且版本一致；API/Worker 重启后终态、Citation 和幂等重放保持一致。

## 实际验证

```powershell
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe apps packages
.venv\Scripts\python.exe -m pytest
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
.venv\Scripts\python.exe scripts/export_openapi.py
.venv\Scripts\python.exe -m pytest tests/unit/test_openapi.py
git diff --check
```

结果：Ruff format/check 通过；mypy 覆盖 102 个源文件；后端全量 pytest 为
`698 passed, 48 skipped`；Web lint/typecheck、Vitest `28 passed` 和 production build 通过；
OpenAPI 重导出及一致性测试通过。Windows 沙箱不能写 `.pytest_cache` 的既有警告不影响结果。

一次性隔离 PostgreSQL 已验证迁移 `upgrade head -> downgrade 29d0e1f2a3b4 -> upgrade head`、
单一 head 和 QA/Runtime/Skill persistence 集成测试 `6 passed`，测试数据库均已清理。此前 Step 5～7 的隔离
Compose 记录已覆盖 Worker 停止期间 queued、重启接管、租约、重复投递不重复发布、API 重启后
终态/SSE/Citation 保留，以及 active pointer revision CAS 和排队 Run 固定旧摘要。

本轮保留卷 Compose 重建 API/Worker 后实际执行三个知识整理入口：摘要固定 1 个版本并以 Citation
完成；比较固定 2 个来源，但 deterministic fake 只形成单来源证据，故按证据门禁拒答；复习卡以
Citation 完成预览且写入副作用为 0。伪造版本和跨 Space 文档分别返回
`SKILL_VERSION_INVALID` 与 `SKILL_DOCUMENT_INVALID`。验证只输出 ID、状态、计数和稳定代码，
未记录问题、回答、原文、prompt 或 Provider 响应。

## 安全矩阵

| 边界 | 状态 | 证据 |
| --- | --- | --- |
| 受信根、路径/链接逃逸、远程 schema | 通过 | Registry 单元测试拒绝父路径、远程 `$ref`、symlink/junction 和可执行 entrypoint |
| 同版本摘要冲突与磁盘篡改 | 通过 | 冲突不覆盖注册版本；恢复和 pin 重校验摘要 |
| Tool 权限、危险写入和审批 | 通用契约与工程实现通过 | 未授权 Tool、权限扩张和无审批写入均拒绝；持久审批/派生知识已实现，独立认证仍不在当前边界 |
| Space、版本、撤下/删除 | provisional 通过 | 提交与 SearchService 双重校验；跨 Space、旧版本和无已发布证据拒绝 |
| 文档 prompt injection | provisional 通过 | 文档仅作为不可信 Evidence；不能改变系统指令、权限或 Tool 白名单 |
| LLM 决策与 Tool 输出边界 | 契约通过 | 非法 JSON、未知 Tool、模型不可见输出、写 Tool、预算耗尽和未终止循环均稳定拒绝 |
| 日志、事件和 Git 泄漏 | 通过 | 仅记录 ID、摘要、计数、耗时与稳定代码；差异扫描无 E2E ID、密钥和正文 |

## 退出矩阵

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| Runtime/Registry/版本/预算/权限通用契约 | 已完成 | 离线确定性 Runtime 和安全边界回归通过 |
| QA Run/Worker/SSE/Citation 重启恢复 | provisional 已完成 | 复用 Stage 4 PostgreSQL QA 事实源，不代表通用 Runtime Checkpoint |
| `knowledge_qa` Web/API/测试同一链路 | provisional 已完成 | 固定 Skill identity 并调用唯一 QA Port |
| `knowledge_agent` LLM/Tool 只读链路 | provisional 已完成 | 真实 Provider 接口已接线；QA Run 仍是结果和恢复权威 |
| 三个知识整理 Skill | 工程实现完成，质量 provisional | HTTP/Worker/契约、统一 `/runs` facade 和复用 QA Run/SSE 可用；正式 Eval 仍未执行 |
| 通用 AgentRun/Checkpoint PostgreSQL 持久化 | 工程实现完成，质量 provisional | 复用 `qa_runs.id` 的 Runtime 快照与 append-only checkpoint、序号/摘要校验、Worker resume、lease-loss 取消和统一 Run 查询/恢复入口已实现 |
| 持久审批与派生知识幂等写入 | 工程实现完成，质量 provisional | PostgreSQL 审批/派生条目 Adapter、Tool 绑定、查询、过期/撤销、Space/citation 校验和 exactly-once 幂等约束已实现 |
| Skill 管理 Web 与旧版本受控清理 | 工程实现完成，质量 provisional | Web/CAS 已存在；清理 API 校验 active、摘要及 QA/Runtime/Checkpoint 引用，只移除进程 Registry，不删除受信磁盘包 |
| Stage 3/4 正式质量门禁 | 未完成 | 默认检索/回答配置、真实模型门禁和正式 holdout 未关闭 |
| Stage 5 正式 Eval 与阶段退出 | 未执行 | 不使用 provisional/fake 结果替代正式质量结论 |

## 验收决定

接受“Stage 5 工程功能完整、质量结论 provisional”。当前版本可直接用于本地真实流程和后续
阶段接口；正式质量结论仍须等待 Stage 3 新版数据集/配置和对应的 development、holdout 门禁，
工程功能本身不再因该质量门禁暂停。
