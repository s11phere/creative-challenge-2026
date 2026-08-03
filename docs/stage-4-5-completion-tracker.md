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
| 阶段 3 | 工程 Step 0-10 已完成 | 已终止，质量门禁未通过 | [ADR-010](adr/010-stage-3-termination-and-evaluation-boundary.md)：旧评测集代表性不足，正式 holdout 未运行 |
| 阶段 4 | QA Domain/Application、PostgreSQL、Worker、SSE、API、Web 和 Citation provisional 链路已具备 | 未退出 | [stage-4-acceptance.md](stage-4-acceptance.md)：Playwright、真实回答质量、默认配置冻结、answer holdout 和完整反馈旅程未完成 |
| 阶段 5 | Runtime/Registry、active pointer、`knowledge_qa` 及知识整理 Skill provisional 子集已具备 | 未退出 | [stage-5-acceptance.md](stage-5-acceptance.md)：正式 Eval、跨进程故障注入、认证/审批正式验收和最终移交未完成 |

本看板不批准任何正式 holdout，不改变 `retrieval-v1.yaml`、`qa-v1.yaml` 或现有 Skill 的状态，
也不改变阶段 0 的 `internal_team_only` 分发边界。

## 2. 版本矩阵

所有“目标版本”均为待创建或待冻结的版本；完成前不得将候选版本标记为正式。

| 工作线 | 当前候选/事实 | 当前状态 | 下一步正式版本要求 | 数据与外发边界 | 负责人 |
| --- | --- | --- | --- | --- | --- |
| 阶段 3 检索 | `retrieval-v1.yaml` + dataset `knowledge-qa-v0`；另有 `retrieval-v1-knowledge-qa-v1.yaml` + dataset `knowledge-qa-v1` | 两者均 `provisional`；当前 Stage 3 formal runs disabled | 新 dataset version、新 retrieval config/profile/model identity；完成覆盖/标注/split 审查、development 消融、配置 hash 冻结后才可一次性运行 retrieval holdout | 仅使用 manifest 允许来源；默认本地；私有语料不得外发 | 待认领 |
| 阶段 4 QA 评测 | `qa-v1.yaml`、`qa-profile-v1.yaml`、`grounded-qa-v1-provisional.txt`；dataset `knowledge-qa-v0` | `provisional`；deterministic fake/validate-only 可用 | 新 QA dataset/config/profile/prompt/model identity；必须绑定通过的检索版本，并在 development 选择后冻结 answer config hash | 题目、回答、引用原文和 Provider 响应不得写日志/报告；外部 Chat 需显式策略和同意 | 待认领 |
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
| B-01 | 阻塞 | 当前 Stage 3 评测集代表性不足，旧 development/holdout 不能支持正式结论 | 新 dataset version 完成覆盖、标注、locator、split 和隐私审查 | Step 1 |
| B-02 | 阻塞 | Stage 4 默认 retrieval/QA profile、prompt、Chat model 尚未冻结 | Stage 3 新质量输入通过后完成 development 消融和 config hash 冻结 | Step 1/2 |
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

只有在确认本看板后，才进入 Step 1。Step 1 的第一项动作必须是审查新 dataset version 的覆盖、
标注、证据定位和 Stage 0 数据边界；不得直接运行 `cases/evals/configs/retrieval-v1.yaml` 的
当前 holdout，也不得仅通过修改 `formal_runs_enabled` 开启正式评测。

