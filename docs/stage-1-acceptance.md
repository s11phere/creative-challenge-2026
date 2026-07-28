# 阶段 1 验收记录

> 验收日期：2026-07-18
>
> 结论（2026-07-18 当时）：阶段 1 工程实现与验收完成，GitHub Actions 由用户确认运行正常；
> 当时项目继续受阶段 0 数据门禁约束。阶段 0 后续已按 `docs/stage-0-acceptance.md` 内部冻结。

## 验收范围

本次只验收工程骨架、运行边界和质量门禁，不验收阶段 2 及后续的摄入、检索、引用、问答、
Skill 或真实语料。

## 规范命令

```powershell
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy apps packages
uv run pytest
corepack pnpm@10.20.0 --dir apps/web install --frozen-lockfile
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
```

## 本地结果

| 检查项 | 结果 |
| --- | --- |
| Ruff format / lint | 通过，41 个 Python 文件格式一致 |
| mypy strict | 通过，23 个源文件无问题 |
| 默认后端测试 | 59 passed，3 个显式集成测试按设计 skipped |
| 真实依赖集成测试 | 3 passed |
| 前端测试 | 6 passed |
| 前端 lint / typecheck / build | 通过 |
| actionlint | v1.7.12 通过 |
| GitHub Actions | 用户于 2026-07-18 确认运行正常 |
| OpenAPI 一致性 | 通过，只包含两个 `/api/v1/health/*` 路径 |
| Compose 冷构建 | API、Worker、Web 构建通过，基础镜像 digest 已核对 |
| Compose 启动 | PostgreSQL、Redis、API、Worker、Web healthy，migrate 成功退出 |
| 重启持久化 | PostgreSQL migration/pgvector 与 Redis AOF 哨兵均保留 |
| 日志与测试值扫描 | 未发现本地测试密钥、私密正文或 Provider 响应进入仓库 |

## 退出条件映射

| 条件 | 结论 |
| --- | --- |
| 阶段状态不越过数据门禁 | 通过：当时明确记录“阶段 1 工程完成，等待阶段 0 数据门禁” |
| 单命令 Compose 启动 | 通过 |
| live/ready 成功与失败语义 | 通过 |
| API/数据库 trace 关联 | 通过 |
| Worker 投递、重试和关联 | 通过；容器 actor 日志差异列为已知问题 |
| 迁移升降级 | 通过 |
| ModelGateway fake/Provider 契约 | 通过，无真实付费调用 |
| Web 只展示真实状态 | 通过 |
| 本地等价 CI 门禁 | 通过 |
| 密钥与正文泄漏检查 | 通过 |

## 外部确认与已知问题

- 当前环境没有读取私有仓库 Actions 的凭据；远端运行结果由用户于 2026-07-18 确认正常，
  不是本机 API 查询结果。
- Worker 容器可确认成功消费、有限重试和死信转移，但 actor started/completed 日志未稳定
  出现在 `docker logs`。Step 3 的非容器验证曾通过，差异记录在故障排查文档中。
- pytest 在当前 Windows 沙箱无法写 `.pytest_cache`，会产生缓存警告，不影响测试结果。

## 移交结论

未参与初始化的开发者可以按根 README 创建 `.env`、单命令启动本地栈并完成 smoke test。
阶段 0 已由 `docs/stage-0-acceptance.md` 以 `internal_team_only` 范围冻结；进入阶段 2 正式质量验收前，
仍须关闭 Worker 容器 actor 日志差异或明确接受其风险。
