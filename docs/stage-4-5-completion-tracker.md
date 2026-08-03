# 阶段 4/5 收尾看板

> 对应收尾计划：[`post-stage-3-stage-4-5-completion-plan.md`](post-stage-3-stage-4-5-completion-plan.md)
>
> 看板版本：v1
>
> 建立日期：2026-08-03
>
> 状态规则：`已完成` 仅表示对应工程/契约已有证据；`provisional` 表示可用于后续开发或内部
> 验证，但不能写入正式质量结论；`待执行` 表示尚无实际验收证据；`阻塞` 表示存在前置门禁或
> 事实源缺口。

## 1. 当前阶段结论

| 阶段 | 工程状态 | 正式质量状态 | 当前决策依据 |
| --- | --- | --- | --- |
| 阶段 3 | 工程 Step 0-10 已完成；v1 development 复核已执行 | 仍未通过，未冻结，未运行 holdout | [ADR-010](adr/010-stage-3-termination-and-evaluation-boundary.md)；[v1 development 记录](stage-3-reopen-development-v1.md)：v1 修复标注但未 materially improve coverage/representativeness |
| 阶段 4 | QA Domain/Application、PostgreSQL、Worker、SSE、API、Web、Citation 和 `qa-continuation-v1` provisional 配置已具备 | 未退出 | [stage-4-acceptance.md](stage-4-acceptance.md)：Playwright、真实回答质量、正式默认配置冻结、answer holdout 和完整反馈旅程未完成 |
| 阶段 5 | Runtime/Registry、active pointer、`knowledge_qa` 及知识整理 Skill provisional 子集已具备 | 未退出 | [stage-5-acceptance.md](stage-5-acceptance.md)：正式 Eval、跨进程故障注入、认证/审批正式验收和最终移交未完成 |

本看板不批准任何正式 holdout，不改变 `retrieval-v1.yaml`、`qa-v1.yaml` 或现有 Skill 的状态，
也不改变阶段 0 的 `internal_team_only` 分发边界。

### 1.1 Step 1 结果与继续策略

Step 1 的正式质量前置门未通过，但当前 GPU development 结果满足 [ADR-011](adr/011-provisional-stage-4-5-continuation-gate.md)
定义的 provisional continuation gate。因此可以进入 Step 2 的 provisional QA 工程工作；正式
retrieval/answer holdout、正式质量结论和阶段退出仍被阻断。

### 1.2 Step 3 provisional E2E 结果

第 3 步的隔离 HTTP 旅程已执行并记录在 [`stage-3-4-5-step3-e2e.md`](stage-3-4-5-step3-e2e.md)：
健康检查、会话创建、异步 QA Run、回答、SSE 事件和反馈幂等均有证据。真实 fake 旅程的
Citation 解析为 `invalid`（无正文返回），所以 Citation 质量门禁仍未通过。期间发现并修复
SSE `id` 与 `Last-Event-ID` 类型不一致（改为严格递增 sequence）；12 个 API/SSE 单测通过。
隔离 API 镜像受 Docker buildx 权限限制尚未重建，网络层的 sequence 重验待后续正常构建完成。
该结果仅允许继续 provisional Step 4，不改变正式质量门禁、holdout 禁止或 internal-only 边界。

## 2. 版本矩阵

所有“目标版本”均为待创建或待冻结的版本；完成前不得将候选版本标记为正式。

| 工作线 | 当前候选/事实 | 当前状态 | 下一步正式版本要求 | 数据与外发边界 | 负责人 |
| --- | --- | --- | --- | --- | --- |
| 阶段 3 检索 | `retrieval-v1.yaml` + dataset `knowledge-qa-v0`；另有 `retrieval-v1-knowledge-qa-v1.yaml` + dataset `knowledge-qa-v1` | v1 schema/locator/hash 校验和 GPU development 消融已通过；两者仍 `provisional`，formal runs disabled；正式门未通过但满足 ADR-011 continuation gate | 可在 v1 上继续阶段 4/5 provisional 工程；正式线仍需新 dataset/config、代表性覆盖、claim-aware evaluator、development 达标、配置 hash 冻结后才可一次性运行 retrieval holdout | 仅使用 manifest 允许来源；默认本地；私有语料不得外发 | 待认领 |
| 阶段 4 QA 评测 | `qa-continuation-v1.yaml` + `qa-profile-continuation-v1.yaml`；dataset `knowledge-qa-v0`；prompt `grounded-qa-v1-provisional` | provisional continuation 配置已 pin `retrieval-v1-knowledge-qa-v1`，validate-only 和受影响单测通过；正式配置未冻结 | 在 continuation gate 下继续 QA/E2E 工程；正式线仍需代表性 QA dataset、真实模型 development、answer config hash 和一次性 holdout | 题目、回答、引用原文和 Provider 响应不得写日志/报告；外部 Chat 需显式策略和同意 | 待认领 |
| 阶段 5 Skill 评测 | active `knowledge_qa 0.1.0`；`knowledge_agent 0.2.0`（保留 `0.1.0` 旧包）；`summarize_document 0.1.0`、`compare_sources 0.1.0`、`create_review_cards 0.1.0` | active/provisional；整理 Skill 目前只读预览，写入和正式 Eval 未关闭 | 为每个 Skill 固定 workflow/manifest/prompt/schema/eval 版本和摘要；完成 Runtime 恢复、审批、派生写入、回滚/清理引用检查后再做正式 Skill Eval | 受信根加载；运行固定 Skill identity；派生写入前必须持久审批，所有输入继承来源敏感度 | 待认领 |

### 2.1 版本冻结顺序

```text
新 Stage 3 dataset/config
  -> retrieval development 与默认配置冻结
  -> retrieval holdout
  -> Stage 4 QA dataset/profile/prompt/model 冻结
  -> answer development 与配置冻结
  -> answer holdout
  -> Stage 5 Skill workflow/schema/eval 固定
  -> Skill Runtime/写入/生命周期正式验收
```

任何一项失败都要创建新的版本回到 development，不能修改已冻结输入，也不能用上一阶段 provisional
结果代替本阶段门禁。

## 3. 退出项对照

### 3.1 阶段 4

| 退出项 | 当前状态 | 证据/缺口 | 看板动作 |
| --- | --- | --- | --- |
| 阶段 0/2 记录和 Stage 3 termination boundary | 已完成 | Stage 0/2 acceptance、ADR-010 已存在 | 保持引用，不重写为质量通过 |
| 导入 -> 问答 -> Citation -> 原文 | provisional 已执行 | Compose 最小链路已验证；完整用户旅程未验收 | Step 3/4 补充隔离 E2E 和 Playwright |
| Citation target resolution 100% 与隔离违规 0 | provisional 部分通过 | 解析和版本校验有证据；需正式报告与全量安全切片 | Step 2/3/9 复核 |
| 默认 QA 配置、development 消融、answer holdout | 待执行 | 当前为 fake/validate-only，`formal_runs_enabled=false` | Step 2，依赖新 Stage 3 质量输入 |
| SSE、取消、重试、重连、唯一终态 | provisional 已执行 | 契约和隔离集成测试已有；需真实旅程回归 | Step 3/4 |
| Conversation/Run/Evidence/Feedback 持久化与恢复 | provisional 已执行 | PostgreSQL/Worker/重启记录已有；反馈完整旅程未执行 | Step 3/5 |
| Web 桌面/移动/键盘/失败状态 | 待执行 | Playwright 未执行 | Step 4 |
| 版本可追溯、预算、日志与隐私 | 部分完成 | identity/稳定错误已存在；正式配置和最终扫描未完成 | Step 2/9/10 |

### 3.2 阶段 5

| 退出项 | 当前状态 | 证据/缺口 | 看板动作 |
| --- | --- | --- | --- |
| Runtime/Registry/manifest/权限/预算通用契约 | 已完成（工程） | 阶段 5 review 已审查通过 | 作为基础，不重复实现 |
| 通用 Runtime Checkpoint 跨进程恢复 | provisional | PostgreSQL 快照/adapter 已有；跨进程故障注入和完整 Worker resume 未完成 | Step 6 |
| 持久审批 | provisional | Adapter/API 子集已有；正式身份认证、竞态和跨进程验收未完成 | Step 6 |
| 派生知识写入 | provisional 阻塞 | `create_review_cards` 仍需审批事实源和 exactly-once 验收 | Step 7 |
| Skill Catalog/active pointer/回滚 | provisional 已有 | CAS 和查询已有；清理引用与发布安全仍需最终验收 | Step 8 |
| 三个知识整理 Skill | provisional 只读 | 固定来源和引用预览已有；无专用完整工作流/正式 Eval | Step 7/8 |
| 正式 Skill Eval 与阶段退出 | 未执行 | 不得使用 fake/provisional 结果替代 | Step 9/10 |

## 4. 风险、阻塞和决策

| 编号 | 类型 | 项目 | 解除条件 | 责任步骤 |
| --- | --- | --- | --- | --- |
| B-01 | 正式阻塞 | 当前 Stage 3 评测集代表性不足，旧 development/holdout 不能支持正式结论；不再阻塞 provisional Stage 4/5 工程 | 新 dataset version 完成覆盖、标注、locator、split 和隐私审查 | 正式质量线 |
| B-02 | 正式缺口 | Stage 4 provisional QA continuation config 已完成；正式 retrieval/QA profile、prompt、Chat model 仍未冻结 | 正式冻结仍需 Stage 3 正式输入和 answer development | 正式质量线 |
| B-03 | 阻塞 | 正式 retrieval/answer holdout 尚未运行 | 冻结配置后各运行一次；失败时创建新版本，不回写 holdout | Step 1/2 |
| B-04 | 缺口 | 完整导入到反馈的真实旅程和 Playwright 尚未执行 | 隔离 Compose E2E、桌面/移动截图、键盘和失败状态回归通过 | Step 3/4/5 |
| B-05 | 缺口 | Runtime Checkpoint、lease-loss、审批跨进程事实源仍未完成正式验收 | 故障注入、租约竞态、审批生命周期和幂等副作用测试通过 | Step 6 |
| B-06 | 缺口 | 派生知识写入和 `create_review_cards` 目前保持预览 | Derived Knowledge Application Port、持久审批和 exactly-once 写入通过 | Step 7 |
| B-07 | 缺口 | Skill 旧版本清理、引用保护和最终生命周期验收未关闭 | active/pin/CAS/引用计数/清理 dry-run 和回滚测试通过 | Step 8 |
| B-08 | 约束 | 阶段 0 语料为 `internal_team_only`，私有内容和 Provider 外发受限 | 保持隔离环境、manifest 校验、授权记录和隐私扫描 | 全步骤 |
| B-09 | 约束 | Windows 沙箱可能产生 `.pytest_cache` 写权限警告 | 记录为环境限制，不修改权限或测试语义 | 全步骤 |

当前没有触发新增 ADR：本步只记录版本、状态和依赖，没有改变模块边界、数据模型、公开 API、
事件协议或 Skill 信任模型。若 Step 6/7/8 需要改变这些边界，必须在对应实现前新增或更新 ADR。

## 5. 第 0 步完成检查

- [x] 记录阶段 3 终止事实和禁止事项。
- [x] 记录阶段 4/5 provisional 工程现状与正式缺口。
- [x] 建立 Stage 3/4/5 dataset/config/model/prompt/Skill 版本矩阵。
- [x] 为每个主要缺口指定后续步骤和解除条件。
- [x] 明确 owner 尚未认领，不虚构负责人或时间承诺。
- [x] 确认本步未运行 holdout、未读取私有正文、未调用外部 Provider、未变更数据库或 API。

## 6. 下一步进入条件

Step 1 已完成 development 复核。确认本看板后可进入 Step 2 的 provisional QA 工程路径；不得
直接运行 `cases/evals/configs/retrieval-v1.yaml` 的当前 holdout，也不得仅通过修改
`formal_runs_enabled` 开启正式评测。正式质量线仍需按 ADR-010/011 的后续触发条件重新建立。
