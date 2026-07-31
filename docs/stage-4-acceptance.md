# 阶段 4 Provisional 工程移交记录

> 记录日期：2026-07-31
>
> 结论：Stage 4 Step 0～10 的门禁允许范围内工程契约和回归验证完成。本记录不是阶段 4 正式退出，
> 不宣称 Grounded QA、Citation、Worker 执行或 `knowledge_qa` Skill 已可用。

## 范围与边界

本次只使用合成输入、确定性 fake、内存 Repository 和仓库许可的验证元数据。实现覆盖
GroundedAnswer/Evidence/Citation 契约、查询与上下文、结构化生成和故障语义、内存状态、SSE/API、
Web queued 状态、反馈候选导出、answer evaluator 的 validate-only 门禁，以及唯一 provisional
`GroundedQAApplicationPort` 的完整内存执行路径。

本次完成项不包含、也未验收工作树中已有的 QA ORM/Alembic 草稿；没有读取私有正文、调用真实
回答模型或执行 development/holdout。Stage 3 的默认检索 profile 和真实模型尚未冻结，Stage 4
继续处于未正式启动状态。

## 实际验证

```powershell
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy apps packages
uv run --frozen pytest -q
uv run --frozen python scripts/export_openapi.py
uv run --frozen pytest tests/unit/test_openapi.py -q
uv run --frozen python scripts/evaluate_answers.py --validate-only
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
git diff --check
```

结果：Ruff format（151 files）和 check 通过；mypy 覆盖 77 个源文件；后端 pytest 为
`585 passed, 41 skipped`；OpenAPI 导出后一致性测试通过。answer evaluator 验证 development 127、
holdout 149，`formal_run_eligible=false`，未产生模型调用或回答执行。前端 lint、typecheck、Vitest
`15 passed` 和 production build 通过，`git diff --check` 通过。Windows 沙箱无法创建 `.pytest_cache`
的既有警告不影响结果。

补充：`knowledge-qa-v0/cases.jsonl` 是未提交的 `internal_team_only` 数据，完整 `--validate-only`
只能在批准的内部环境运行。公开 CI 会显式跳过该数据依赖测试；CLI 仍会在读取 dataset 前拒绝
`formal_runs_enabled=false` 的任何执行请求。

继续实现补充（2026-07-31）：`GroundedQAApplicationPort` 已通过合成 SearchService、Citation target、
结构化 Chat fake 和内存 Repository 验证幂等提交、Evidence 保存、回答原子发布、检索失败与显式取消；
定向 QA service/persistence/API 测试为 `15 passed`。截至该次记录，该服务尚未接入 API/Worker，
也没有新增受正式门禁约束的数据库实现。

可用 provisional 补充（2026-07-31）：按用户明确方向，现有 QA API 已在 API 进程内接入该唯一
Application Port，并复用真实 PostgreSQL SearchService 和新增 Citation target adapter。隔离 Compose
空卷完成迁移、摄入 manifest 允许的 `omnistudio/README.md`（上传前 SHA-256 与 manifest 一致）后，
QA Run 从 queued 到 completed，返回回答及绑定同一 Space 的 source/document/version/chunk 和
`lines 120-126` locator。默认模型仍为确定性抽取 fake；未运行 development 或 holdout。

持久化补充（2026-07-31）：新增前向 Alembic revision 和 PostgreSQL Repository/Event Store，
持久保存 Conversation、Message、Run、append-only Attempt、Evidence、Citation、Feedback 与
`qa-sse-v1` 事件。隔离 PostgreSQL 已验证空库 upgrade、downgrade、单一 head、终态数据跨 Repository
实例读取，以及中断 attempt 重排队和未发布 Evidence 清理。隔离 Compose 中已完成 Run 在 API 重启后
仍保持 completed，回答、1 条 Citation 和 accepted/completed 事件均可读取/重放。执行器仍在 API
进程内，未接入 Worker，未实现引用原文 API。

本次补充验证：Ruff format/check、mypy（84 个源文件）、后端全量 pytest（`602 passed, 43
skipped`）、OpenAPI 一致性、Web lint/typecheck/Vitest（`16 passed`）与 production build 均通过。
PostgreSQL 持久化集成测试在同一保留卷连续运行两次，均为 `2 passed`；Windows 沙箱既有
`.pytest_cache` 写权限警告不影响结果。

Worker 补充（2026-07-31）：API 已不再持有 QA 执行协程，只在 Run 提交事务完成后向既有
Redis/Dramatiq 边界投递 `run_id/trace_id/event_version`。独立 Worker 通过 PostgreSQL attempt
lease/heartbeat 领取工作，调用同一个 `GroundedQAApplicationPort`，并在启动时扫描未租用 queued、
cancel_requested 和租约过期运行。隔离 Compose 验证：Worker 停止时新 Run 保持 queued；重启后自动
接管并 completed，返回 answer 和 1 条 Citation。同一 completed Run 重复投递前后均保持
Attempt/Citation/Event 计数 `1/1/3`。新迁移已完成空库 upgrade、downgrade、单一 head 和 lease
互斥集成测试；未实现引用原文 API，未运行 development/holdout。

Worker 补充验证：Ruff format/check、mypy（86 个源文件）、后端全量 pytest（`605 passed, 44
skipped`）、OpenAPI 一致性、Web lint/typecheck/Vitest（`16 passed`）与 production build 均通过；
隔离 PostgreSQL QA persistence/lease 集成测试为 `3 passed`。Windows 沙箱既有 `.pytest_cache`
写权限警告不影响结果。

原文解析补充（2026-07-31）：新增只接受 `run_id + evidence_id` 的已发布 Citation Application
用例和 HTTP 端点，客户端不能提交 Space、版本、Chunk、locator 或 Blob 路径。Resolver 重新校验
固定版本链、Blob SHA-256 和 excerpt SHA-256；Markdown 使用既有 parser 在精确源行范围内重建
规范化 Evidence 文本，TXT 按一基闭区间行号，PDF 按单页解析。Web Citation 按钮支持键盘操作，
按需展示并高亮最小片段，以及加载、失败和不可用状态。保留 Compose 中历史 completed Run 的
`lines 20-22` Citation 已返回 `valid` 非空片段，伪造 evidence ID 返回 404；未输出或记录原文。
本轮回归：后端 pytest `609 passed, 44 skipped`，Ruff format/check、mypy（87 个源文件）、
OpenAPI 一致性、Web lint/typecheck/Vitest（`16 passed`）和 production build 均通过。

## 正式退出矩阵

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 领域、Application、SSE/API、Web 契约回归 | 已完成（provisional） | fake/合成契约与真实 PostgreSQL 链路均已验证 |
| QA PostgreSQL 迁移、upgrade/downgrade、保留语义 | provisional 已执行 | 空库往返迁移、单一 head、终态保留和中断恢复通过 |
| Worker/Dramatiq 执行 | provisional 已执行 | ID-only 消息、lease/heartbeat、启动恢复和重复投递通过 |
| API/Worker 重启恢复 | provisional 已执行 | Worker 停止时 queued，重启接管；终态读取与 SSE 重放通过 |
| 导入到回答、引用和原文的 Compose E2E | provisional 已执行 | 真实摄入/检索/固定版本最小片段解析通过；反馈旅程未执行 |
| Playwright 桌面/移动截图 | 未执行 | 真实回答/Citation 用户旅程不存在 |
| development 消融、默认 QA 配置冻结、正式 holdout | 未执行 | Stage 3 质量门禁及 Stage 4 正式门禁未关闭 |
| Citation target resolution 与回答质量结论 | 部分执行 | PostgreSQL/Blob/locator 解析通过；无质量冻结或正式 answer report |

## 阶段 5 边界

Grounded QA schema、SSE v1、安全边界和唯一 provisional QA Application Port 已可供后续设计；
现有 QA API/Web 也可作为真实检索和引用身份的临时可用入口。QA 状态和事件已有 PostgreSQL
事实源及 API/Worker 重启恢复，执行已进入独立 Worker，固定版本原文片段可按需解析。阶段 5 可以据此继续开发，不得据此宣称活动
`knowledge_qa` Skill 或阶段 4/5 正式完成；正式退出仍须关闭 Stage 3、完整反馈旅程、质量和
holdout 门禁。
