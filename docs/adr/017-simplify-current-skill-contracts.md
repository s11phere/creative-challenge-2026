# ADR-017：收敛当前 Skill 契约

## 背景

仓库同时保留多个历史 Skill、Prompt、Assistant API 模式和 Skill 生命周期接口，导致运行时选择、
恢复和 Web 交互存在不必要的分支。当前产品已经确定 Assistant 主路径和最新 Skill 实现，继续维护
旧兼容层会增加决策错误和测试负担。

## 决定

- 每个受信 Skill 只保留当前目录和版本 `1.0.0`；未激活的学习 Workflow 契约也遵循此规则，且不进入 Assistant 调用目录。
- 知识请求固定使用 `knowledge_agent 1.0.0`，持久化新 Run 固定使用 Router `assistant-agent-loop-v1`
  和 Prompt `assistant-base-prompt-v7`。
- 删除旧 `knowledge_qa` Skill/适配器、旧 Prompt 产物、Assistant v1 Web 入口和 Skill 激活、回滚、清理 API。
- `/api/v1/skills` 仅提供当前安装 Skill 的只读目录；版本变更通过代码和部署发布完成。

## 后果

新部署路径更短，模型和 Web 不再面对兼容模式选择。历史旧身份 Run 不再由当前 Worker 恢复，升级时应
重新提交未完成请求；这是有意的不兼容变更。阶段文档中的旧版本只作为历史记录保留。

## 重新评估

只有在需要并行发布 Skill 或跨版本数据迁移时，才重新引入版本生命周期，并为新契约增加显式版本号和迁移。
