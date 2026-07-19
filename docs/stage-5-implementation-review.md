# 阶段 5 实现审查记录

> 审查日期：2026-07-19
>
> 结论：阶段 5 通用工程基础通过审查；阶段 5 整体未达到退出条件。

## 审查结论

本次审查确认 Step 0～4 及 Step 9 中不依赖前序业务模型的通用部分已经实现，并与
ADR-003、ADR-006 和阶段 5 实施计划一致：领域状态与 Port、Tool/Skill Registry、受信包加载、
固定版本、确定性有限状态执行、预算、权限、审计、事务式 reload 和内存活动版本回滚均有
单元或契约测试。

这不是阶段 5 整体验收。阶段 5 的目标是封装阶段 2～4 已验证的知识能力，而当前仓库仍无
完整摄入闭环、RetrievalStore、GroundedAnswer/Citation Application 用例、Conversation/
AgentRun/Evidence 持久化及 ADR-007 协议。因此没有 `knowledge_qa` 或三个知识整理业务 Skill，
没有 Runtime API、Web Skill 入口、PostgreSQL 检查点恢复或三入口端到端旅程。

## 已审查实现

| 范围 | 结果 | 主要实现 |
| --- | --- | --- |
| Step 0 | 通过 | ADR-006、跨阶段契约审计和稳定信任/版本/错误语义 |
| Step 1 | 通过 | `domain.agent_runtime` 纯类型、状态迁移、预算、权限、检查点和 Port |
| Step 2 | 通过（通用部分） | Tool schema、显式 handler、权限/Space/预算/审批前置检查和脱敏调用记录 |
| Step 3 | 通过 | 受信 Skill 包、manifest/schema、摘要、安装/活动版本、固定版本和模板 |
| Step 4 | 通过 | 声明式有限状态执行器、FakeModelGateway、有限重试、取消/超时和审计事件 v1 |
| Step 9 | 通过（通用部分） | 全有或全无 reload、并发保护、原子激活/回滚和旧检查点兼容校验 |

生产代码不会从 manifest 动态导入 Python、执行 shell/SQL 或访问远程 schema。运行时权限同时
受 Skill 声明和服务端调用者授权快照约束；文档、prompt、模型原始响应和密钥不进入 Runtime
审计事件。Registry 没有旧版本删除 API，因为当前没有持久化 AgentRun/Checkpoint 引用事实源。

## 未完成与阻塞

| 范围 | 状态 | 解阻条件 |
| --- | --- | --- |
| Step 5 | 阻塞 | 阶段 4 提供 Conversation、AgentRun、Evidence 身份和持久化语义；再新增迁移与恢复 Adapter |
| Step 6 | 阻塞 | 阶段 2 摄入、阶段 3 检索和阶段 4 引用问答退出条件完成 |
| Step 7 | 阻塞 | Step 5/6 完成，ADR-007 或等价 SSE/后台任务协议接受 |
| Step 8 | 阻塞 | `knowledge_qa` 真实链路和派生知识写入 Application 用例稳定 |
| Step 9 持久化部分 | 阻塞 | Step 5 提供运行引用查询、保留和清理事实源 |
| Step 10 | 阻塞 | Step 0～9 全部交付，阶段 0 数据门禁关闭 |

上述阻塞项不得用平行 ORM、临时回答 schema、fake API 或私有语料绕过。项目当前主推进顺序
仍是阶段 2 摄入闭环，然后阶段 3 检索和阶段 4 引用问答，再回到阶段 5 业务接入。

## 验证记录

本次审查实际运行：

```powershell
uv sync --frozen
uv lock --check
.venv\Scripts\ruff.exe format --check .
.venv\Scripts\ruff.exe check .
.venv\Scripts\mypy.exe apps packages
.venv\Scripts\python.exe -m pytest
corepack pnpm@10.20.0 --dir apps/web lint
corepack pnpm@10.20.0 --dir apps/web typecheck
corepack pnpm@10.20.0 --dir apps/web test
corepack pnpm@10.20.0 --dir apps/web build
```

结果：

- uv 冻结同步和锁文件检查通过，共解析 77 个锁定包。
- 当前 Windows 沙箱无法初始化用户级 uv cache，因此后端质量命令使用冻结同步后的 `.venv`
  可执行文件直接运行；检查内容与仓库规范命令等价，未修改缓存权限或测试语义。
- Ruff format/check 通过；60 个 Python 文件格式一致。
- mypy strict 通过；32 个源文件无问题。
- pytest：`158 passed, 22 skipped`。其中 21 个为未设置 `RUN_INTEGRATION=1` 的真实依赖
  集成测试；1 个为当前 Windows 环境无权限创建符号链接。`.pytest_cache` 写权限警告不影响结果。
- Web lint/typecheck/build 通过；Vitest `6 passed`。Web 未发生阶段 5 功能变化，本次只做基线回归。
- `git diff --check` 在文档更新后再次执行。

## 审查决定

接受“阶段 5 通用基础已完成”的工程结论，不接受“阶段 5 已整体完成”或“Skill 业务可用”的
产品结论。后续文档、演示和发布说明必须保留这一边界，直到所有阻塞项和阶段退出条件关闭。
