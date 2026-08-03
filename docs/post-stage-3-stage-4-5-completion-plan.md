# 阶段 3 终止后的阶段 4/5 收尾计划

> 状态：执行计划 v1
>
> 适用日期：2026-08-03 起
>
> 本计划承接 `docs/adr/010-stage-3-termination-and-evaluation-boundary.md`、阶段 4/5
> provisional 验收记录和现有实现审查。它描述尚未关闭的正式门禁，不把 provisional 工程结果
> 改写为质量通过或阶段退出。

## 1. 目标和当前基线

目标是补齐 P0 闭环：真实文档导入、可恢复的引用问答、可验证的 Web 旅程、反馈到评测候选的
受控回流，以及可版本化、可回滚的 `knowledge_qa` Skill。阶段 4/5 已有的 QA Run/Attempt、
PostgreSQL 持久化、Worker lease/heartbeat、SSE、Citation 原文解析、Skill Registry、active
pointer 和三个知识整理 Skill 作为本计划的实现起点。

当前必须保持的事实：

- 阶段 3 工程实现完成，但质量门禁未通过；当前 retrieval 配置仍是 `provisional`，正式 holdout
  未运行。
- 阶段 4/5 仍是 provisional。fake、合成输入和 `--validate-only` 只能证明契约和安全边界，
  不能证明真实回答质量或正式产品可用性。
- 阶段 0 语料边界仍为 `internal_team_only`。任何真实评测、截图、日志和外部模型调用都必须
  遵守 manifest、SHA-256、授权和隐私策略。

## 2. 执行原则和总门

1. 所有数据集、检索/回答配置、模型、prompt、Skill 和报告均使用新版本号及内容摘要；禁止
   原地修改已记录的 development/holdout 或把当前 Stage 3 分数当作基线。
2. 质量工作只在 development 集调参。holdout 在配置冻结后最多正式运行一次；失败后发布新的
   dataset/config 版本并回到 development。
3. 继续复用唯一 `GroundedQAApplicationPort`、现有 QA Run/Worker/SSE 和 SearchService，
   不建立平行 Runtime Run、检索 SQL、Citation 解析或事件协议。
4. 每一步都必须留下机器可读输出、命令、版本摘要和失败分类；未实际执行的命令不得标记为通过。
5. 任一安全、隐私、Space/版本隔离或引用定位回归，立即阻断后续正式门禁。

## 3. 分步计划

### Step 0：建立版本与退出看板

**目的**：把当前 provisional 状态转成可追踪的收尾工作项。

**工作**：

- 建立阶段 3 新评测线、阶段 4 QA 评测线和阶段 5 Skill 评测线的 dataset/config/model/prompt
  版本矩阵，记录 owner、输入语料分类、是否允许外发和目标退出条件。
- 对照 ADR-010、阶段 4/5 acceptance/review，逐项标记“已完成、需真实依赖、需产品决定、阻塞”。
- 为任何改变数据模型、Runtime 恢复、审批或 Skill 信任边界的方案补 ADR；先审 ADR 再编码。

**交付与验收**：版本矩阵、风险/阻塞清单、更新后的实施看板；审查确认没有把旧 holdout、私有
正文或 provisional 指标纳入正式结论。

### Step 1：重新开启阶段 3 的检索质量工作（前置门）

**目的**：获得可用于阶段 4 的正式检索输入；不复用已终止工作的质量结论。

**工作**：

- 创建新的 dataset version，补充覆盖面、标注规范、证据 locator 和 development/holdout split
  决策；完成 Stage 0 隐私、授权和人工抽检。
- 创建新的 retrieval config/model/profile version，先跑 Keyword、Dense、Hybrid、Reranker
  消融和逐 case 诊断，定位召回、版本过滤、上下文扩展和延迟问题。
- 仅在 development 达标、配置 hash 冻结且所有 Space/tombstone/version 安全测试通过后运行一次
  正式 retrieval holdout。

**退出门**：生成可复现的 retrieval report；达到阶段 0 的 Recall/延迟/违规阈值并接受新的
  ADR/验收记录。未达标时保持阶段 3 未关闭，不能进入阶段 4 正式回答门禁。

### Step 2：冻结阶段 4 QA 契约和真实配置

**目的**：把 GroundedAnswer/Citation/Refusal/SSE 契约绑定到可评测版本。

**工作**：

- 基于通过的检索版本冻结 `QAProfileV1`、query/context 参数、prompt、Chat model capability
  identity、token/超时预算和 config hash。
- 扩充并审查 answer dataset：supported claim、citation accuracy/completeness、refusal、
  conflict、model/provider/infrastructure failure 分类；保留隔离 holdout。
- 在 development 上完成模型/profile 消融和失败归因；运行 `--validate-only`，确认版本、分母、
  权限和 formal-run 阻断正确。
- 冻结后正式运行一次 answer holdout，保存 answer-report-v1 及逐 case 诊断摘要（不保存问题、
  prompt、原文或 Provider 响应）。

**退出门**：Supported-claim、引用准确/完整、Refusal、P95、token 和失败率达到冻结阈值，且
   Citation resolution 100%、跨 Space/撤下/错误版本违规为 0。否则发布新版本回到 development。

### Step 3：补齐后端真实端到端旅程

**目的**：证明阶段 4 的持久化链路在真实隔离依赖中可用。

**工作**：

- 在隔离 PostgreSQL/Redis/Compose 项目中执行：manifest 允许来源上传/摄入 -> 等待发布 -> 提问
  -> 读取回答/Evidence/Citation -> `run_id + evidence_id` 解析并打开原文 -> 提交反馈。
- 覆盖单文档、跨文档、证据不足拒答、冲突来源、模型不可用、摄入失败、取消、重试、SSE 断线重连、
  API/Worker 重启和重复投递。
- 验证回答、Citation、SSE 事件、Feedback 的幂等、保留和终态语义；确认所有日志只含 ID、摘要、
  计数和稳定错误码。

**交付与验收**：隔离 Compose E2E 报告、迁移 upgrade/downgrade/单一 head 证据、失败矩阵和
   数据清理记录。不得把共享业务卷或私有正文带入测试产物。

### Step 4：完成 Web 用户旅程和 Playwright 门禁

**目的**：覆盖真实用户可见的 P0/P1 交互，而不以 API 成功替代 Web 验收。

**工作**：

- 实现并测试导入状态、问答 queued/running/completed/refused/failed、取消/重试、SSE 重连、
  Citation 面板按需加载与失效提示、原文最小片段高亮、反馈提交和反馈结果状态。
- 用 Playwright 验证桌面和移动视口、键盘操作、焦点、错误/空状态、长文本和响应式布局；截图只使用
  合成或已批准 fixture，并运行隐私扫描。
- 验证刷新、浏览器重开和 API 重启后的 Run/Skill identity 与状态一致。

**退出门**：Persona S1-S10 中 P0 故事及反馈故事均有自动化证据；无布局遮挡、键盘死路、错误状态
   丢失或引用编号漂移。

### Step 5：关闭反馈到评测候选的闭环

**目的**：让反馈可审查地改进系统，且不污染冻结 holdout。

**工作**：

- 确认赞/踩及可选说明的敏感度继承、脱敏、访问和保留策略；反馈默认进入 `pending_review`。
- 实现人工审核、去重、证据补标、拒绝原因和版本化导出；导出只生成新的 development/candidate
  dataset，不修改 frozen JSONL。
- 为反馈候选增加回归测试：跨 Space、撤下来源、prompt injection、日志泄漏和恶意文本不能改变
  系统权限或进入报告正文。

**退出门**：每个候选可追溯到 feedback/run/evidence/config 版本；审核前不会被评测执行器读取，
   holdout 内容与反馈路径隔离。

### Step 6：完成通用 Runtime 的跨进程恢复和审批事实源

**目的**：把阶段 5 的 provisional Checkpoint/审批实现提升为可运维能力。

**工作**：

- 完成 Worker 对 Runtime Checkpoint 的自动 resume、连续序号、lease-loss 协作取消、超时和
  重复投递幂等；用跨进程故障注入验证 API/Worker 重启和中途崩溃。
- 完成持久审批生命周期（申请、批准、拒绝、过期、撤销）及调用者/Space/Skill/version 绑定；
  写 Tool 没有有效审批必须拒绝。
- 建立 checkpoint、audit、Skill 引用的保留与清理策略；被运行引用的版本不得清理，清理操作可审计、
  可回滚且不删除受信磁盘包。

**退出门**：隔离 PostgreSQL/Redis 集成测试覆盖恢复、租约互斥、审批竞态、重复副作用和清理引用；
   失败时保持只读 Skill 行为。

### Step 7：启用派生知识写入和知识整理 Skill

**目的**：在审批和事实源完备后，安全关闭 `create_review_cards` 的写入阻塞。

**工作**：

- 固定 Derived Knowledge Application Port、目标 Space、输入 Citation 集、幂等键和冲突策略；
  不允许 Skill 直接写 ORM/文件。
- 将 `summarize_document`、`compare_sources`、`create_review_cards` 接入同一 QA Run/Worker/SSE，
  验证固定 DocumentVersion、至少两个来源约束和撤下/跨 Space 拒绝。
- 在 Web/API 提供预览 -> 审批 -> 写入 -> 撤销/重试状态，并记录写入结果与审计事件；失败不得产生
  部分派生条目。

**退出门**：审批前 `side_effects=0`；批准后写入 exactly-once、可重放且可查询；来源删除或版本
   变化不会产生悬空派生条目。

### Step 8：完成 Skill 生命周期管理和发布安全

**目的**：让 Skill 可安装、激活、回滚和受控清理，同时保持运行固定版本。

**工作**：

- 完成受信包安装/摘要校验、active pointer CAS、回滚、并发冲突、API/Worker 重启恢复和运行 pin。
- Skill 管理 Web 只暴露受信 Catalog、能力/权限/预算和引用计数；禁止任意路径、entrypoint、权限
  编辑或未经引用检查的删除。
- 对旧版本执行 dry-run、引用检查和可审计清理；保留受信磁盘包的部署运维边界。

**退出门**：旧版本有 AgentRun/Checkpoint/QA Run/审计引用时清理被拒绝；pointer 更新不会改变
   已排队 Run；回滚后新 Run 与 Catalog 摘要一致。

### Step 9：性能、可观测性和隐私最终审查

**目的**：在正式退出前验证运行预算和数据边界。

**工作**：

- 在冻结默认配置下测量检索、回答、SSE、Worker 队列和 Citation 解析 P95、Token、失败率及资源
  使用；确认降级路径显式可见。
- 扫描日志、trace、SSE、评测报告、截图、缓存、Embedding 和模型响应，确认无密钥、私有正文、
  prompt、问题或 Provider 响应泄漏。
- 运行安全回归：Space/版本/tombstone、伪造 locator/evidence、文档 prompt injection、危险 Tool、
  外部 Provider 策略和取消竞态。

**退出门**：预算和隐私门禁全部有实际输出；任何严重回归阻断 release candidate。

### Step 10：阶段 4/5 正式验收、移交和发布决定

**目的**：形成可复核的最终结论。

**工作**：

- 运行后端/前端规范命令、隔离集成测试、OpenAPI 导出一致性、迁移往返、Compose 空卷与保留卷重启、
  Playwright、answer/retrieval report 和安全隐私扫描。
- 更新 `stage-4-acceptance.md`、`stage-5-acceptance.md`、README、architecture、troubleshooting、
  OpenAPI 和相关 ADR；逐项引用实际证据和已知限制。
- 召开退出评审：阶段 4 先关闭，确认唯一 QA Port 和 report/config identity 后再关闭阶段 5；若任一
  门禁失败，明确保持 provisional 并记录下一版输入，不回写成功结论。

**最终退出条件**：阶段 4 的 12 项退出条件全部满足；阶段 5 的 Runtime 恢复、审批、派生写入、
   Skill 生命周期和正式 Eval 均有独立证据；无未披露安全/隐私/质量限制。

## 4. 推荐执行顺序和依赖

```text
Step 0
  -> Step 1（新检索基线）
  -> Step 2（QA 配置与 answer holdout）
  -> Step 3（后端 E2E） -> Step 4（Web/Playwright） -> Step 5（反馈闭环）
  -> Step 6（Runtime/审批） -> Step 7（派生写入） -> Step 8（Skill 生命周期）
  -> Step 9（最终质量/安全） -> Step 10（正式验收）
```

Step 3/4 可在 Step 2 的配置冻结后并行开发，但正式验收必须等待 Step 1/2 的质量门关闭。Step 7
必须等待 Step 6 的审批和恢复事实源；Step 8 的清理必须等待 Step 6 的引用查询。任何步骤均不得
读取未批准语料、启用当前 Stage 3 formal holdout，或把 fake/provisional 结果写入正式报告。

## 5. 每步通用验证清单

- 代码：受影响模块的 Ruff、mypy、pytest/Vitest；共享接口变更同步 OpenAPI。
- 数据：隔离 PostgreSQL/Redis，迁移 `upgrade -> downgrade -> upgrade`、单一 head、数据清理。
- 运行：Compose 空卷启动、保留卷重启、Worker 停止/恢复、重复投递和取消竞态。
- 安全：Space/版本/tombstone、路径逃逸、prompt injection、危险 Tool、密钥和正文泄漏扫描。
- 证据：命令、版本摘要、指标、失败分类、截图/报告位置和未完成项均写入对应验收记录。

