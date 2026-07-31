# 阶段 5 Provisional 工程验收记录

> 记录日期：2026-07-31
>
> 结论：Stage 5 当前可用子集已完成 provisional 工程验收；阶段 5 未正式退出。

## 验收范围

本次验收覆盖 ADR-006 约束的单 Agent Runtime、Tool/Skill Registry、受信包加载、固定摘要、
预算/权限/审计、事务式 reload/回滚、PostgreSQL active pointer，以及五个 `0.1.0` Skill：

- `knowledge_agent` 通过真实 `fast_chat` 决策节点在同一 QA Run 中调用受限只读 `grounded_qa` Tool。
- `knowledge_qa` 复用唯一 Grounded QA Application Port 和现有 QA Web/API/Worker/SSE。
- `summarize_document` 固定一个 current published DocumentVersion 并返回带引用摘要。
- `compare_sources` 固定至少两个同 Space 来源；回答缺少两个来源的 Citation 时拒答。
- `create_review_cards` 只生成带引用预览，并返回 `SKILL_WRITE_PORT_UNAVAILABLE`、
  `side_effects=0`。

知识整理 Skill 使用现有 QA Run 作为持久执行身份，提交时保存 Source/Document/DocumentVersion
范围，Worker 执行时通过 SearchService 再次校验精确 current published 版本。没有新增第二套
Runtime Run、Worker、SSE、取消、检索、问答或 Citation 协议。

`knowledge_agent` 只接受严格 `call_tool/complete/refuse` JSON，并由 Runtime 强制 Tool 白名单、最大
轮数、Token/Tool 预算、权限、Space 和 schema。当前 Tool 输出给模型的内容仅含状态、Citation 数和
结果类型；回答和原文不进入外层模型上下文，写 Tool 在持久审批/幂等事实源完成前保持禁止。

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

结果：Ruff format/check 通过；mypy 覆盖 96 个源文件；后端全量 pytest 为
`634 passed, 45 skipped`；Web lint/typecheck、Vitest `16 passed` 和 production build 通过；
OpenAPI 重导出及一致性测试通过。Windows 沙箱不能写 `.pytest_cache` 的既有警告不影响结果。

一次性隔离 PostgreSQL 已验证迁移 `upgrade head -> downgrade 29d0e1f2a3b4 -> upgrade head`、
单一 head 和 QA persistence 集成测试 `3 passed`，测试数据库均已清理。此前 Step 5～7 的隔离
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
| Tool 权限、危险写入和审批 | 通用契约通过 | 未授权 Tool、权限扩张和无审批写入均拒绝；生产持久审批仍未实现 |
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
| 三个知识整理 Skill | provisional 只读完成 | HTTP/Worker/契约可用；尚无专用 Web 工作流 |
| 通用 AgentRun/Checkpoint PostgreSQL 持久化 | 未完成 | 仅 QA 业务运行持久化，通用 Runtime Checkpoint 仍在内存 |
| 持久审批与派生知识幂等写入 | 未完成 | 复习卡明确零写入副作用 |
| Skill 管理 Web 与旧版本受控清理 | 未完成 | 只有 Catalog 和 CAS API；无完整引用事实源时禁止删除旧包 |
| Stage 3/4 正式质量门禁 | 未完成 | 默认检索/回答配置、真实模型门禁和正式 holdout 未关闭 |
| Stage 5 正式 Eval 与阶段退出 | 未执行 | 不使用 provisional/fake 结果替代正式质量结论 |

## 验收决定

接受“Stage 5 当前可用子集完成 provisional 工程验收”，不接受“Stage 5 正式完成”。当前版本可用于
后续阶段的接口开发和本地真实流程验证；涉及持久写入、通用检查点、版本清理或质量基线的能力仍须
等待对应事实源和前序阶段门禁。
