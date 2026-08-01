# 阶段 5 实现审查记录

> 审查日期：2026-07-19
>
> 结论：阶段 5 通用工程基础通过审查；阶段 5 整体未达到退出条件。

## 审查结论

本次审查确认 Step 0～4 及 Step 9 中不依赖前序业务模型的通用部分已经实现，并与
ADR-003、ADR-006 和阶段 5 实施计划一致：领域状态与 Port、Tool/Skill Registry、受信包加载、
固定版本、确定性有限状态执行、预算、权限、审计、事务式 reload 和内存活动版本回滚均有
单元或契约测试。

这不是阶段 5 整体验收。阶段 5 的目标是封装阶段 2～4 已验证的知识能力。当前仓库已有摄入闭环、
RetrievalStore、GroundedAnswer/Citation Application 用例、QA Conversation/Run/Evidence 持久化及
ADR-007 协议；`knowledge_qa 0.1.0` 现已通过既有 QA Web/API/Worker 成为 active provisional Skill。
三个知识整理业务 Skill 已提供 provisional 只读版本；仍没有派生知识写入/持久确认、通用 Runtime
API、PostgreSQL Runtime Checkpoint 恢复或正式质量验收。

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
| Step 5 | 部分完成 | PostgreSQL QA Run/Attempt、Worker lease/heartbeat、重复投递和重启恢复已完成；Runtime Checkpoint、QA Worker resume、lease-loss 取消、审批/派生写入 Adapter 及 QA API 控制入口子集已完成，跨进程故障注入验收与清理仍待实现 |
| Step 6 | active provisional | 声明式包固定名称/版本/摘要，由 Worker 对同一 QA Run 调用唯一 QA Port；正式质量仍待阶段 3/4 门禁 |
| Step 7 | provisional 可用子集 | 现有 QA API/Web/Worker 已执行固定 Skill，并提供真实检索、持久 Run、引用原文和重启恢复；通用 Runtime/Skill 管理入口未实现 |
| Step 8 | provisional 只读完成 | 三个 Skill 固定来源并复用既有 QA Run/Worker/SSE；派生知识写入 Application Port 与持久确认仍阻塞 |
| Step 9 持久化部分 | 阻塞 | Step 5 提供运行引用查询、保留和清理事实源 |
| Step 10 | 阻塞 | Step 0～9 全部交付，阶段 0 数据门禁关闭 |

上述阻塞项不得用平行 ORM、临时回答 schema、fake API 或私有语料绕过。项目当前主推进顺序
仍是阶段 2 摄入闭环，然后阶段 3 检索和阶段 4 引用问答，再回到阶段 5 业务接入。

2026-07-31 补充审查：阶段 4 provisional 契约落地后，Step 5 已在不新增业务表的边界内继续。
`RuntimeStateStore` 复用共享 AgentRun 身份，内存事务替身原子保存运行状态与带摘要的下一安全
检查点；执行器恢复不会重放已完成节点，并拒绝跨 Space、摘要篡改、序号间隙和预算回退。该结论
仍不覆盖 PostgreSQL、Worker 重启或重复副作用验收。

2026-07-31 Step 6 补充审查：新增的 `skills/_provisional/knowledge_qa` 不参与批量 reload，
`KnowledgeQASkillAdapter` 只调用唯一 provisional QA Port。服务端 Run 上下文提供调用者、Space 和
幂等身份；测试验证回答、拒答、依赖故障以及客户端伪造安全字段。该实现没有生产注册、HTTP/Web
入口、真实 Citation Adapter 或质量结论，仍不构成业务 Skill 可用性。

2026-07-31 Step 7 再次复核：当前 OpenAPI 没有 `/api/v1/skills` 或 `/api/v1/runs`，Web 也没有
Skill 入口；这与门禁一致，不是遗漏。只有在 Step 5/6 提供持久 Run、Worker 完成/恢复和活动
`knowledge_qa` 后，才能基于 ADR-007 的既有 `qa-sse-v1` 接入三入口，不能先发布进程内临时 API。

2026-07-31 用户随后明确接受当前表现不足，并要求先交付真实可用版本。基于该方向，Step 7 增加
不改变正式门禁的 provisional 子集：复用现有 QA API、`qa-sse-v1` 和 Web 问答入口，在 API 进程
内调用唯一 QA Application Port、真实 PostgreSQL SearchService 与 Citation target adapter。
默认 fake Chat 返回确定性证据摘录，终态 API/Web 展示回答或拒答及引用身份。未新增第二套 Runtime
API/SSE，未激活 `_provisional/knowledge_qa`；该时间点 QA 状态重启丢失、无 Worker、无原文跳转，故不能把
该可用子集记为 Step 7 或阶段 5 正式完成。

2026-07-31 QA 持久化补充审查：新增前向 Alembic revision、PostgreSQL QA Repository 和 Event
Store，`qa_runs` 保持共享稳定身份，`qa_run_attempts` 记录 append-only attempt，Evidence/Citation/
Feedback 均绑定 attempt。隔离 PostgreSQL 已验证迁移往返、单一 head、终态保留及中断 attempt
重排队；Compose 已验证 completed Run、回答、Citation 和 SSE 事件跨 API 重启可读取。前述“QA
状态重启丢失”限制由此关闭，但 Worker、通用 Runtime Checkpoint、原文跳转和活动 Skill 仍未完成。

2026-07-31 QA Worker 补充审查：API 已移除进程内执行协程，只向既有 Dramatiq broker 投递
`run_id/trace_id/event_version`；Worker 通过新增的 attempt lease/heartbeat 和共享
`GroundedQAExecutor` 调用唯一 QA Application Port。隔离数据库验证 lease 互斥和恢复扫描；Compose
验证 Worker 停止时 Run 保持 queued、重启后自动 completed 并返回 1 条 Citation。completed Run
重复投递前后 Attempt/Citation/Event 计数保持 `1/1/3`。前述 QA Worker 缺口由此关闭；通用 Runtime
Checkpoint、原文跳转、活动 Skill 和正式质量门禁仍未完成。

该补充的完整回归结果为：Ruff format/check、mypy（86 个源文件）、后端 pytest（`605 passed, 44
skipped`）、OpenAPI 一致性、Web lint/typecheck/Vitest（`16 passed`）和 production build 通过；
隔离 PostgreSQL QA persistence/lease 集成测试为 `3 passed`。

2026-07-31 Citation 原文补充审查：新增已发布 Citation Application 用例，HTTP/Web 只以
`run_id + evidence_id` 请求原文，服务端从持久终态取回不可变 Citation 后重新校验 Space、固定版本、
Chunk、locator、Blob 和 excerpt 摘要。真实 Compose 历史 Run 的 Markdown `lines 20-22` 已解析为
`valid` 非空最小片段，伪造 Evidence 返回 404；Web 支持键盘打开、高亮及失败/失效状态。前述
原文跳转缺口由此关闭；活动 Skill、通用 Runtime Checkpoint 和正式质量门禁仍未完成。
本轮回归为后端 pytest `609 passed, 44 skipped`、mypy 87 个源文件、Web Vitest `16 passed`，
Ruff、OpenAPI 一致性、Web lint/typecheck/build 和保留 Compose 全栈健康检查均通过。

2026-07-31 active `knowledge_qa` 补充审查：声明式包已迁移到受信根可扫描目录，API 启动时按配置
显式激活 `0.1.0` 并将名称、semver 和内容 SHA-256 固定到新 QA Run。Worker 不读取当前 active
指针决定已排队 Run，而是按 Run 固定版本重新 pin 并校验摘要，再通过确定性 Runtime handler 执行
同一个现有 Run；不会再次 submit 或建立第二套持久状态。包缺失/摘要不一致投影为
`QA_SKILL_INVALID`。Registry active 指针和通用 Runtime Checkpoint 仍由进程内状态/启动配置重建，
阶段 5 正式退出状态不变。

2026-07-31 Step 7 只读 Catalog 补充审查：新增 `/api/v1/skills` 及版本查询，返回受信包摘要、
manifest 权限/能力/预算和 active 配置版本；QA Run response 返回持久化 Skill identity，Web 工作区
显示 active/fixed 版本。Catalog 没有任意路径、entrypoint、版本、权限或预算写入能力；激活/回滚仍由
启动配置控制，未创建第二套 Runtime Run 或 Checkpoint API。

本子集验证：后端全量 pytest `614 passed, 44 skipped`，Ruff format/check、受影响模块 mypy、
OpenAPI 一致性、Web lint/typecheck/Vitest `16 passed` 和 production build 通过。保留卷 Compose
重建 API/Web 后，API 与 Web 同源代理均返回 1 个 active `knowledge_qa 0.1.0`；版本摘要为 64 位，
manifest `max_steps=4`。新 QA Run completed，response 中 fixed name/version/digest 与 Catalog 一致。

2026-07-31 persistent active pointer 补充审查：新增 `skill_activations` 迁移，以名称、semver、摘要和
revision 保存当前版本。配置只初始化缺失 pointer；Catalog 和新 QA Run 均从 PostgreSQL 同步，受控
activate/rollback 仅能选择受信根已安装版本，并使用 expected revision 防止并发覆盖。Worker 仍按
QA Run 固定 identity 执行，不读取当前 pointer 改写排队工作。该子集未新增第二套 Run/SSE/Checkpoint，
也未提供包安装、任意路径或旧版本删除能力；阶段 5 正式状态不变。

本子集验证：Ruff format/check、mypy 92 个源文件、后端全量 pytest
`616 passed, 45 skipped`、OpenAPI 一致性、Web lint/typecheck/Vitest `16 passed` 和 production build
通过；隔离 PostgreSQL pointer 集成测试 `1 passed`。保留卷 Compose 完成迁移单一 head、
downgrade/upgrade 往返和 API 重启恢复；旧 revision 返回 `SKILL_ACTIVATION_CONFLICT`。Worker 停止时
创建的 Run 保持 queued，pointer revision 更新后恢复 Worker，该 Run 仍以原固定 name/version/digest
完成，未被当前 pointer 改写。

2026-07-31 Step 8 补充审查：新增 `summarize_document`、`compare_sources` 和
`create_review_cards 0.1.0` 完整受信包及既有 QA transport 下的三个提交入口。它们复用同一 QA
Run、Worker、`qa-sse-v1`、确定性 Runtime 和唯一 Grounded QA Application Port；未创建第二套
Runtime Run、SSE 或业务问答逻辑。Run 新增固定 Source/Document/DocumentVersion 范围，提交校验
Space、current published 状态，SearchService 在执行时再次要求精确版本一致，避免排队工作跟随新
版本或扩大范围。比较结果必须引用至少两个来源，否则发布为证据不足拒答。

`create_review_cards` 当前只生成带引用预览，HTTP response 与 Runtime Skill output 都返回
`SKILL_WRITE_PORT_UNAVAILABLE`、`side_effects=0`；没有直接写表或文件。故本步骤只记为
provisional 只读完成，派生知识 Application Port、持久审批、幂等写入、通用
Runtime Checkpoint 和正式质量门禁仍未完成。

本子集验证：Ruff format/check、mypy 94 个源文件、后端全量 pytest `624 passed, 45 skipped`、
OpenAPI 一致性、Web lint/typecheck/Vitest `16 passed` 和 production build 通过。一次性隔离
PostgreSQL 完成迁移 `upgrade -> downgrade -> upgrade`、单一 head 和 QA persistence 集成测试
`3 passed`，测试数据库均已清理。保留卷 Compose 重建 API/Worker 后，摘要以 1 个固定版本和
Citation 完成；比较固定 2 个来源，但 deterministic fake 仅给出单来源证据，故按新门禁拒答；
复习卡以 Citation 完成预览且写入副作用为 0。伪造版本和跨 Space 文档分别返回稳定
`SKILL_VERSION_INVALID` 与 `SKILL_DOCUMENT_INVALID`，未读取或记录回答与原文。

本补充验证：Ruff format/check、受影响模块 mypy、后端全量 pytest（`613 passed, 44 skipped`）、
OpenAPI 一致性、Web lint/typecheck/Vitest（`16 passed`）和 production build 通过。保留卷 Compose
重建 API/Worker 后，新 Run 持久化 `knowledge_qa/0.1.0` 与 64 位摘要并 completed；Worker 停止期间
第二个 Run 保持 queued，重启后接管完成，Attempt/Event 为 `1/3`。5 条既有 Citation 在重建后仍为
`valid` 且返回非空原文。新问题的一条 Citation 因既有 excerpt 再校验返回 `invalid`，按 provisional
协议显式展示而未回退到相似文本；未据此形成引用质量结论。

2026-07-31 Skill 管理 Web 补充审查：Web 新增独立技能管理工作区，读取受信 Catalog 和版本详情，
展示版本摘要、权限、能力与预算，并调用已有持久化激活/回滚 API。所有写操作携带当前 revision，
沿用 PostgreSQL compare-and-set 冲突保护；页面不允许安装包、指定路径、编辑权限或删除版本。
该增量关闭 Step 7 的 Skill 管理 Web 缺口，但没有新增通用 Runtime Run/Checkpoint 协议。Web
lint、typecheck、Vitest `19 passed` 和 production build 均通过。

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

2026-07-31 可用 provisional 子集新增验证：

- Ruff format/check 与 mypy 通过；全量 pytest 为 `601 passed, 41 skipped`，skip 均为未启用的真实
  依赖测试或既有平台限制，`.pytest_cache` 权限警告不影响结果。
- Web lint/typecheck、Vitest `16 passed` 和 production build 通过；新增测试覆盖终态回答与引用展示。
- OpenAPI 已重新导出并通过一致性测试；回答、拒答、冲突、Citation 和 locator 均为显式 schema。
- 隔离 Compose project 使用空 PostgreSQL/Redis 命名卷完成构建、迁移与健康启动；上传前校验
  manifest 允许的 `omnistudio/README.md` SHA-256，摄入任务成功，QA Run 返回 completed、回答和
  `lines 120-126` Citation。未读取非允许来源、未调用外部 Provider、未执行 development/holdout。

## 审查决定

接受“阶段 5 通用基础已完成”的工程结论，不接受“阶段 5 已整体完成”或“Skill 业务可用”的
产品结论。后续文档、演示和发布说明必须保留这一边界，直到所有阻塞项和阶段退出条件关闭。
