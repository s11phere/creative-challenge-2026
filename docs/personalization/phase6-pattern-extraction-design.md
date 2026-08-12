# Phase 6：自动提取工作模式（Path B）

日期：2026-08-12
依赖：Phase 1（eval 门禁）、Phase 2（usage_patterns）、Phase 4（creator 机制）
产出：模式挖掘任务 + 候选生成 + 历史验证 + 用户审批

## 目标

系统从使用痕迹自动识别用户**重复工作模式**，提议个人 skill。走 `creator 定稿 + eval 门禁 + 用户审批`，**绝不自动激活**。

## 范围

**做**：模式挖掘（复用 Phase 2 usage_patterns）→ 候选生成（复用 Phase 4 creator 机制）→ 用历史 exemplar runs 验证候选 eval → draft + 证据展示 → 用户审批。
**不做**：无监督自动激活；跳过 eval 门禁；一次性行为被提为 skill（有过拟合防护）。

## 交付物

1. **模式挖掘任务**（worker）：按重复阈值（如 ≥N 次/时段）识别强模式；(skill, 任务类别, 工具序列, 输入类型) 聚类；产出候选 + **证据**（哪些 run、频率、exemplars）。
2. **候选生成**：对每个强模式，调 Phase 4 creator 机制草拟候选 skill（prompt / workflow / schema + 从 exemplar 提炼的 eval cases）。
3. **验证**：候选 skill 的 eval cases 跑**历史 exemplar runs**——验证"能否复现观察到的行为"；并跑 Phase 1 eval 门禁。双闸未过不出 draft。
4. **draft + 证据**：候选进入 SkillsPanel 草稿区，展示证据（频率 / 示例 / 来源 run，可追溯）；用户审批 → 激活；拒绝可反馈原因（写入 traces 供后续学习）。
5. **安全阀**：重复阈值下限、无自动激活、每次生成走 draft。

## 验收

- 对有明显重复的工作流能提出候选 skill（有证据、可追溯、来源 run 可点查）。
- 候选必须过 eval 门禁 + 历史验证才允许 draft；用户激活前不生效。
- 全量门禁通过；测试覆盖：挖掘阈值、候选生成、历史验证、审批流、过拟合防护。
