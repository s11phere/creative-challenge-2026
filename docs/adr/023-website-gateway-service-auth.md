# ADR-023: Website Gateway Service Authentication

## 背景

CC2026 官网使用浏览器 session 认证，但本仓库的 API/Worker 是独立服务。仅依靠
`/intranet` 页面或隐藏按钮不能保护 API、任务和 Space；浏览器 session 也不应转发给项目服务。

## 决定

API 增加可配置的服务认证中间件。生产部署设置 `SERVICE_AUTH_REQUIRED=true`，要求官网网关
发送 `X-Internal-Service-Token` 和不含个人信息的 `X-App-Scoped-User-Id`。API 将匿名标识
作为 Space/Conversation/Run 的租户边界，并对跨租户读取返回 404。健康检查保持匿名，便于
容器编排探活。`PUBLIC_MODE=true` 时只保留首期知识工作流，阻断全部 `/api/v2`、`/api/v3`、
`/api/v4`、Skill 管理、考试、审核、反馈和派生知识路由；新增生产路由必须显式加入审核后的
公开清单。

开发模式继续兼容原有 `owner_id=local` 客户端，以便本地 smoke test；该兼容路径不应在生产
启用。生产 Compose 覆盖文件撤掉项目服务和独立 Vite UI 的公网端口，镜像使用非 root 用户运行。
Compose 网络不使用 Docker 的 `internal` 标志，以保留受控的模型下载/上游 Provider 出站能力；
私有性由不发布端口和官网网关边界保证。

路径黑名单只是能力开关而不是认证边界，因此启动时增加 `validate_public_mode()` 守卫：
`PUBLIC_MODE=true` 必须同时满足 `SERVICE_AUTH_REQUIRED=true` 和非空
`INTERNAL_SERVICE_TOKEN`，否则进程拒绝启动；`MODEL_ALLOW_EXTERNAL=true` 在公开模式下一律
拒绝，除非显式设置 `PUBLIC_MODE_ALLOW_EXTERNAL_MODEL=true` 记录已完成的数据使用评审。该守卫
在 API lifespan 和 Worker 启动时都会执行，且不受 `APP_ENV` 影响，因为公开模式本身就是面向
公网的部署形态。

非 root 运行带来一个部署约束：Compose 命名卷在容器内路径不存在时会以 `root:root` 创建，
uid 10001 无法写入。因此 API/Worker 镜像必须预建 `/app/data/blobs` 并 chown 给 `app`，
让全新命名卷继承属主；否则上传会在干净部署上返回 500。

## 备选方案

- 只依赖 Next.js layout：不能保护 API，且可被直接请求绕过。
- 把浏览器 session 转发给服务：扩大凭据泄露面，也让项目服务耦合官网认证实现。
- 为每个项目复制一套认证：会造成多个身份边界和不一致的租户校验。

## 后果与复评

官网需要安全保存共享令牌并在请求中注入匿名用户标识；后续若接入 Convex/R2 网关，应保留
同一服务令牌和租户语义，并把短期令牌轮换、审计和配额纳入官网维护侧。若 API 对外开放，
应另行评审 OAuth、限流和密钥轮换，不得把当前共享令牌直接作为公网认证协议。
