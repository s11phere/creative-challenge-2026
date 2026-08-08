# knowledge_agent 证据扩池实验报告

> 日期：2026-08-08 · 环境：本地 Compose（omnistudio space，DeepSeek v4 flash + 本地 TEI embedding/reranker）· 方法：真实系统两轮对话 + 对照组 + 逐 run 落库/日志/trace 对比

## 1. 现象与问题

上传 OmniStudio 相关资料后提问，得到一版回复；再要求"感觉不详细，重新总结一下"，会得到更详细的回复，且**参考资料（引用/证据）变多**。

问题：这里"证据变多"是什么原理？

## 2. 关键前提：每一轮都是全新检索

`apps/.../qa/service.py:379` 的注释明确：检索和历史解耦，QA 只收到"任务式问题 + 证据"，**不复用上一轮的证据**。上一轮答案唯一的影响是：路由模型（`assistant/agent.py`）看到整段对话后，把追问**重述成一个自包含的、更宽泛的问题**（`skill_invocation.py:264` 写入 `standalone_request`）。

## 3. 实验 A：完整链路（有路由重述）

| 轮次 | 实际用于检索的问题（`standalone_request`） | 证据池 | 引用 |
|---|---|---|---|
| 第一轮 | `OmniStudio 有哪些核心功能？请总结一下。` | 6（仅 readme） | 5 |
| 追问（路由重述） | `请基于当前 Space 的知识库，详细总结 OmniStudio 的核心功能，包括多模式编辑器、代码与文档增强、知识管理、编程运行与评测、界面与交互等各个方面，尽量全面详细。` | 8（readme 7 + claude 1） | 6 |
| 对照组（新对话同问题） | `OmniStudio 有哪些核心功能？请总结一下。` | 6 | 4 |

- 路由把"感觉不详细，重新总结一下"重述成宽泛问题，**还带上了第一轮答案里的分类名**。
- 对照组证明：同样的简洁问题，换新对话再问，池子稳定 = 6 → **6→8 不是轮次噪音**。
- 第二轮 planning 阶段 1.6s（跑了 LLM 查询改写），对照组也有改写却仍 = 6 → **关键在问题内容变宽，不在改写本身**。

## 4. 实验 B：无路由重述（trace 揭示的真机制）

修 trace 写盘时顺手复现，因第二轮紧跟第一轮提交、路由没读到第一轮答案，**未做重述**——反而成为对照组：**没有路由重述，"感觉不详细"原话照样让证据翻倍**。

| | 首轮 | 追问（无重述，agent 正常工作） |
|---|---|---|
| agent 计划 | 3 条内容查询 + 上限拉满(100000) | 3 条模糊查询 + `max_tokens_per_evidence=2000`、`max_evidence_items=100` |
| 证据池 | 6（仅 readme） | **13（readme 6 + arch 7）** |
| 引用 / claim | 5 / 5 | **10 / 18** |
| 生成上下文 | 3564 字符 | **8944 字符** |

证据内容确认相关：池子覆盖 readme 功能概述 + **arch 架构文档里更细粒度的实现描述**（AI 分析、错题记录、评测面板等），正好支撑"更详细"。

### 机制链（trace 直接揭示）

1. **agent 主动扩池**：看到"重新总结/更详细"意图后，agent 自己把 `max_tokens_per_evidence` 调到 2000 —— **证据块更短 → 同样 token 预算能塞进更多块**（`context_builder.py` 按 token 预算逐块选）。
2. **检索覆盖面变宽**：4 条查询摊开语料，摸到第二个文档（arch）。
3. **意图进生成**："感觉不详细"是"写多点"指令 → 模型写 18 个 claim、引 10 块证据。
4. **100% 引证完整率门禁**（`qa/profile.py:60`）：池子大 → 才能引得多，两者必然同步。

## 5. 完整回答

"证据变多" = **agent 主动调参扩池（主要）+ 路由重述宽化问题（上下文可见时的并行路径）+ 生成端"更详细"意图 + 引证门禁**。早期曾误判为"路由重述为主"，实验 B 纠正：agent 的规划能力（尤其是 `max_tokens_per_evidence` 这个杠杆）才是主要驱动。

## 6. 附带发现：agent 循环 flakiness

实验 A 的两次追问，agent 决策循环都失败（一次 `DEPENDENCY_MODEL_TIMEOUT` 挂 114s 被 `fast_chat_timeout=120s` 网关超时杀掉；一次 `TOOL_EXECUTION_FAILED`），靠服务端 fallback（`knowledge_agent.py:151`）兜底完成。实验 B 两轮 agent 循环全部健康 → **间歇性，非结构性**。日志里 DeepSeek 全部 200（无 5xx），是"请求发出后长时间无响应"，属外部模型/网络抖动。

### 根因链

- 工具 handler 全部 `max_retries=0`，一次瞬时模型/工具异常直接打爆 agent 规划，强制走 fallback。
- `ToolInvocation.retry_count` 字段在设计里预留，但 `BoundedLLMAgentNode` **没有接重试循环**——`retryable` 只是被传播，从未真正重试。
- `QADebugTrace` 默认关闭（`QA_DEBUG_TRACE_ENABLED=false`），失败工具的确切异常此前完全不可见。

## 7. 修复与加固（本报告同批落地）

1. **启用 QA debug trace**：`.env` 加 `QA_DEBUG_TRACE_ENABLED=true`（opt-in 设计，仅本地开发）。trace 写 `/data/qa-debug/<run_id>.jsonl`（挂载到 `tmp/qa-debug`），含完整 `llm_request/llm_response/llm_error/tool_call/tool_result/tool_error`。注意：**文件权限 600 root 属主**，host 侧直接读会 Permission Denied，需 `docker exec` 读。
2. **接上工具重试循环**（`packages/agent_runtime/src/agent_runtime/llm_decision.py`）：`_invoke_with_retry` 在工具失败且 `retryable` 时按 `definition.max_retries` 重试（`idempotency_key` 追加 `:retry:N`），保留 agent 选择的计划；重试耗尽后照旧抛 `NodeExecutionError`。
3. **`inspect_retrieval` 开 `max_retries=1`**（`skills/knowledge_agent.py`）：只读、安全，重试能保留 agent 的 additional_queries；`grounded_qa` 保持 0（fallback 已自带一次重试，且部分执行后重试不安全）。

验证：`ruff format/check` 全绿，`mypy apps packages` 0 问题，`pytest` 759 passed / 51 skipped（新增 2 个重试用例）。

## 8. 观测与复现方法

- **池子大小**：`qa_evidence` 按 `run_id` 计数；**引用数**：`qa_citations` 计数 / run 响应 `citations`。
- **agent 计划**：启用 trace 后读 `/data/qa-debug/<run_id>.jsonl` 的 `agent_decision`（`llm_request` 中 `state.history`）和 `tool_call` 参数。
- **复现脚本**：`POST /api/v1/spaces/{space_id}/conversations` 建对话 → `POST /api/v2/conversations/{id}/turns` 发首问 → 发"感觉不详细，重新总结一下" → 轮询 `GET /api/v2/runs/{run_id}` 到终态 → 比对两 run 的池子/引用/agent 计划。

## 9. 局限

- 单一样本对（每实验一组），6→8 与 6→13 方向一致但样本小；对照组（简洁问题×2 均 = 6）降低了噪音解释。
- 实验 B 的 run2 因紧跟 run1 提交，路由未重述，属"无重述"路径；用户真实场景（有等待）走"有重述"路径，二者都验证了证据扩池。
- 决策层模型超时（failure 类型 1）不在本次工具重试覆盖范围；如需覆盖需给 workflow `execute` 节点开 `max_retries`（涉及 skill 版本号变更，另行评估）。
