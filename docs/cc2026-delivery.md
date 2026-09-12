# CC2026 官网接入交付说明

本仓库交付的是“易知”首期精简能力。官网应通过内部网关调用 API；浏览器不得直连
API、数据库、Redis 或模型服务。

面向官网负责人执行接入时，请以[易知官网适配交付手册](cc2026-yizhi-handoff.md)为主，
本文档作为范围和边界摘要。

## 服务边界

- 官网网关在每个请求中验证成员 session，并生成不含姓名、学号等个人信息的
  `X-App-Scoped-User-Id`。
- 网关向 API 发送 `X-Internal-Service-Token`、`X-Request-ID` 和该匿名用户标识。
- API 在 `SERVICE_AUTH_REQUIRED=true` 时拒绝缺少或不匹配凭据的请求（健康检查除外），
  并把匿名标识写入 Space、Conversation 和 Run 的 owner/caller 字段。
- API 不接受浏览器 session token，也不会把内部令牌转发给模型或 Worker。

## 首期公开能力

生产 Compose 使用 `deploy/compose.intranet.yaml` 覆盖文件：项目 API、Worker、
PostgreSQL、Redis、可选 TEI 以及仓库自带的 Vite `web` 均不映射公网端口。正式官网的
Next.js/Nginx 负责唯一公网入口，并通过内部网络访问 API。设置 `PUBLIC_MODE=true` 后，
所有 `/api/v2`、`/api/v3`、`/api/v4`、Skill 管理、考试、审核、反馈和派生知识路径返回
`CAPABILITY_NOT_EXPOSED`；知识来源、检索、问答、引用和任务状态仍沿用现有 API/Worker
闭环。

Space 生命周期接口：

```text
POST   /api/v1/spaces
GET    /api/v1/spaces
DELETE /api/v1/spaces/{space_id}
```

所有 Space、来源、上传、任务、会话、Run、引用和 SSE 读取都会校验匿名用户归属；不属于
当前用户的对象统一返回 404，避免通过 ID 枚举数据。服务重启后的任务恢复和数据库持久化
继续由现有 PostgreSQL/Redis/Worker 负责。

`docs/openapi.json` 中除健康检查外的操作均声明同时需要
`X-Internal-Service-Token` 和 `X-App-Scoped-User-Id`，并记录 401 响应。令牌验证仍只在
API 中执行，官网 session 不得直接转发给项目服务。

## 部署

```bash
docker compose -f deploy/compose.yaml -f deploy/compose.intranet.yaml \
  --env-file .env up --build --detach --wait
```

生产 `.env` 至少设置 `APP_SECRET_KEY`、`POSTGRES_PASSWORD` 和
`INTERNAL_SERVICE_TOKEN`。镜像以 uid/gid `10001` 的非 root 用户运行；日志写 stdout/stderr，
请求关联使用 `X-Request-ID`，不得记录正文、凭据或模型响应。

本仓库不包含官网 Next.js/Convex 实现、R2 签名存储、招生画像、Tongpaper 或 TongMark
业务表。这些属于官网维护侧和对应项目服务的后续交付，不能用本服务的 JSON 或 localStorage
替代。易知首期也不开放本地 Agent、命令执行、自定义 Skill、长期记忆自动提炼或外部
Provider 切换。

## 外部验收前置

以下项目服务之外的事项不能仅在本仓库内闭环：官网 Next.js/Convex session 网关、R2
上传/下载与生命周期、目标生产机的 CPU Compose 资源复测，以及官网页面端到端验收。仓库已
提供稳定的服务认证、租户边界、OpenAPI、CPU Compose 覆盖和可重复测试；部署前仍需由官网
维护人员把这些能力接入并提交现场证据。

## 验收命令

```bash
uv run ruff format --check apps packages
uv run ruff check apps packages
uv run mypy apps packages
uv run pytest tests/unit/test_service_auth.py tests/unit/test_openapi.py -q
uv run python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json
```
