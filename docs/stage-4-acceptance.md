# 阶段 4 Provisional 工程移交记录

> 记录日期：2026-07-31
>
> 结论：Stage 4 Step 0～10 的门禁允许范围内工程契约和回归验证完成。本记录不是阶段 4 正式退出，
> 不宣称 Grounded QA、Citation、Worker 执行或 `knowledge_qa` Skill 已可用。

## 范围与边界

本次只使用合成输入、确定性 fake、内存 Repository 和仓库许可的验证元数据。实现覆盖
GroundedAnswer/Evidence/Citation 契约、查询与上下文、结构化生成和故障语义、内存状态、SSE/API、
Web queued 状态、反馈候选导出以及 answer evaluator 的 validate-only 门禁。

没有新增 QA PostgreSQL 表或 Alembic revision；没有读取私有正文、调用真实回答模型或执行
development/holdout。Stage 3 的默认检索 profile 和真实模型尚未冻结，Stage 4 继续处于未正式启动状态。

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

## 正式退出矩阵

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 领域、Application、SSE/API、Web 契约回归 | 已完成（provisional） | 仅内存态与 fake/合成验证 |
| QA PostgreSQL 迁移、upgrade/downgrade、保留语义 | 未执行 | 不新增受门禁限制的业务表 |
| Worker/Dramatiq 执行、取消恢复、API 重启恢复 | 未执行 | 没有 QA Worker 完成链 |
| 导入到引用、原文、反馈的 Compose E2E | 未执行 | 无真实回答与 Citation 发布 |
| Playwright 桌面/移动截图 | 未执行 | 真实回答/Citation 用户旅程不存在 |
| development 消融、默认 QA 配置冻结、正式 holdout | 未执行 | Stage 3 质量门禁及 Stage 4 正式门禁未关闭 |
| Citation target resolution 与回答质量结论 | 未执行 | 不存在真实 Citation 或 answer report |

## 阶段 5 边界

Grounded QA schema、SSE v1 和安全边界已可供后续设计参考，但当前 API/SSE 为进程内 provisional
实现，没有持久执行、终态 Citation 或唯一生产 QA Application Port。阶段 5 不得据此实现、接入或
宣称 `knowledge_qa` Skill；必须先完成阶段 3 正式退出，再按 ADR-007 完成阶段 4 的持久化、Worker、
真实 Citation 和评测门禁。
