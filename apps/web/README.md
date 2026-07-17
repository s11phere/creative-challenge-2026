# Web 工作台

阶段 1 的 Web 应用是一个本地系统状态工作台，使用 React、TypeScript、Vite 和
TanStack Query。当前只调用以下版本化接口，不展示伪造的文档、会话或摄入数据：

- `GET /api/v1/health/live`
- `GET /api/v1/health/ready`

## 本地运行

先在仓库根目录启动 FastAPI（默认 `http://127.0.0.1:8000`），再启动前端：

```powershell
corepack pnpm@10.20.0 --dir apps/web dev
```

Vite 开发服务器默认将 `/api` 代理到本地 FastAPI。若 API 位于其他地址，构建或启动前
设置 `VITE_API_BASE_URL`，例如 `http://127.0.0.1:9000`。

## 质量检查

```powershell
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
```

健康请求有 8 秒上限，失败后显示稳定错误码和手动重试入口；页面每 30 秒自动刷新一次，
切到后台时不会继续轮询。
