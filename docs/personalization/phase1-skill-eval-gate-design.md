# Phase 1：Skill Eval 门禁（报告先行）

日期：2026-08-12
状态：✅ 已实施（2026-08-12，commit 04453ea / 52ad918 / f309758 / f661ba4 / 收尾提交）
依赖：无
产出：eval case schema + 确定性 runner + 判定器 + CLI 报告

## 目标

让 `skills/*/evals/cases.jsonl` 从"声明存在"变成**可执行、可判定、可出报告**。为 Phase 4/6 与最终激活闸提供保险闸。本阶段**只出报告，不阻塞任何流程**。

## 范围

**做**：
- eval case schema + 加载校验
- 确定性探针执行（复用生产 skill 执行路径）
- 结构化判定器 + `SkillEvalJudge` 协议（LLM judge 留缝，不实现）
- `scripts/evaluate_skills.py` CLI + 指标聚合报告
- 覆盖全部 9 个 skill（报告不阻塞，先摸清现状）

**不做**：结果落库、API 端点、激活硬闸、LLM judge 实现、改写现有 9 个 skill 的 case 内容。

## 交付物

1. **`EVAL_CASE_SCHEMA`**（`packages/agent_runtime/src/agent_runtime/skills.py`，与 SKILL_MANIFEST_SCHEMA 同处）。case 标准字段：`case_id` / `input` / `expected` / `checks`(可选) / `fixture`。manifest 加载时一并校验 `evals` 引用的文件。
2. **确定性 check 类型**：`output_matches_schema`（输出符合 output.json）、`output_has_key`、`cites_sources`(min)、`trace_tool_called`(tool, min)、`finalized`（run 以 complete 收尾，非 refuse/clarify/超时）。
3. **`packages/application/src/application/skills/evaluation.py`**：
   - `SkillEvalJudge` Protocol + `StructuralSkillEvalJudge`（确定性实现）。
   - 结果类型对齐 `retrieval/evaluation.py` 命名：`SkillEvalCaseResult` / `SkillEvalSkillReport` / `AggregateSkillEvalReport`。
   - 失败分类枚举：`schema_mismatch / check_failed / trace_missing / case_invalid / case_too_thin / run_error`。
4. **探针执行**：复用 v2 skill-invocation + `AgentLoopExecutor`（生产同路径），默认 fake 模型，`--model` 可覆盖诊断；走预算/超时兜底。
5. **CLI** `scripts/evaluate_skills.py --all | --skills a,b [--model]`，结构照 `scripts/evaluate_retrieval.py`。

## 关键设计点

- 行为标签（如 `expected: "model_directed_retrieval_with_verified_finalization"`）**编码成对 AgentRun trace 的结构化断言**（trace_tool_called / finalized），不靠 LLM 判。
- 无 `checks` 的现有 case：走最低判定（schema 合规 + finalized）+ 如实标注 `case_too_thin`，不误报 pass——这正是"先摸清现状"的产出。

## 验收

- 对任一 skill 能跑出 per-case pass/fail + 证据 + 聚合指标的报告。
- 无 checks 的 case 标注 `case_too_thin`，不误报 pass。
- 全量门禁 ruff/mypy/pytest 通过；新增 ~20–40 个 test case（schema 校验、每类 check、指标聚合、失败分类，parametrize）；集成（RUN_INTEGRATION=1）对合成 fixture skill 跑全流程出报告。
