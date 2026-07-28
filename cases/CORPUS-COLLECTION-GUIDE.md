# 语料采集指南 v1.0

> 本文档供人类协作者使用——告诉你应该找什么样的材料来扩充评测数据集。
> Agent 负责后续的入库、出题、校验。你只负责找到合适的材料。

---

## 我们的目标场景

构建一个面向**大学生考前复习**的知识问答评测集。Agent 需要在多份课程资料中检索、比对、综合，回答学生的问题。

典型使用场景：
- 学生上传了课件 PDF、自己的笔记、学长整理的复习提纲、历年真题
- 学生问："第三章的 XXX 公式和第四章的 YYY 定理之间是什么关系？"
- Agent 需要跨多份文档找到答案，并区分哪些来源可信（教材）、哪些可能有误（同学笔记）

---

## 什么样的材料是"好的语料"？

### 硬性条件

| 条件 | 标准 |
|------|------|
| **许可证** | CC0 / CC-BY / CC-BY-SA / Public Domain / MIT / Apache 2.0 / BSD / 开源项目文档 |
| **可公开发布** | 不含个人隐私、不涉密、不侵犯第三方权利 |
| **非敏感内容** | 不是内部考试答案、不是付费课程内容 |

### 软性条件（越好越容易出题）

| 条件 | 为什么重要 |
|------|-----------|
| **同一主题 3-8 份文档** | 太少无法做跨文档综合，太多 Agent 上下文装不下 |
| **格式多样**：PDF + Markdown + 代码文件 + 纯文本 | 测试 Agent 的多格式检索能力 |
| **有"版本差异"**：同一份笔记的旧版和新版 | 支撑 version_or_conflict（版本冲突）题型 |
| **有"来源矛盾"**：A 文档说 X，B 文档说 Y 且不同 | 支撑 cross_document_synthesis（跨文档综合）题型 |
| **有"中英混合"**：英文术语 + 中文解释 | 支撑 bilingual（双语）题型 |
| **有代码示例** | 支撑 code_and_nl（代码与自然语言）题型 |
| **可明确界定知识边界** | 支撑 no_answer（拒答）题型——知道什么不该答 |

---

## 推荐的语料来源

### Tier 1：最推荐（许可证明确、质量高）

| 来源 | 例子 | 许可证 | 适合领域 |
|------|------|--------|---------|
| **MIT OpenCourseWare** | 6.006 算法、6.004 组成原理、6.033 操作系统 | CC BY-NC-SA 4.0 | 计算机全领域 |
| **Stanford 公开课** | CS231n (CV)、CS224n (NLP)、CS229 (ML) | 课程网站公开，教育用途 | AI/ML |
| **Berkeley CS 课程** | CS61A (SICP)、CS61B (数据结构)、CS162 (OS) | CC BY-NC-SA | 编程基础 |
| **Python 官方文档** | docs.python.org Tutorial、Language Reference | PSF License (BSD-like) | 编程语言 |
| **Rust 官方文档** | The Rust Book、Rust by Example | MIT / Apache 2.0 | 编程语言 |
| **Go 官方文档** | Go Tour、Effective Go、Go Blog | BSD-style | 编程语言 |
| **React 官方文档** | react.dev | CC BY 4.0 | 前端框架 |
| **Django 文档** | docs.djangoproject.com | BSD | Web 框架 |
| **Dive into Deep Learning** | d2l.ai (李沐) | Apache 2.0 | 深度学习 |
| **IETF RFC** | RFC 2616 (HTTP/1.1)、RFC 7230 系列 | IETF Trust (允许复制) | 计算机网络 |
| **Wikipedia 技术条目** | en.wikipedia.org 计算机相关条目 | CC BY-SA 3.0 | 通用 |

### Tier 2：可以用但需注意

| 来源 | 注意事项 |
|------|---------|
| **GitHub 项目 README/Wiki** | 注意项目本身的 LICENSE |
| **ArXiv 论文** | 作者保留版权，大部分允许教育引用，但不能全文复制 |
| **技术博客** | 需确认是否标注 CC 许可（如 Julia Evans 的博客通常是 CC BY） |
| **Stack Overflow** | CC BY-SA 4.0，需注明出处 |
| **Medium 技术文章** | 默认 All Rights Reserved，不可直接使用 |

### Tier 3：避免使用

| 来源 | 原因 |
|------|------|
| 知乎 / CSDN / 掘金 | 版权模糊，多数不标注许可证 |
| 付费课程平台内容 | 明确禁止再分发 |
| 教科书的扫描版 / 整章复制 | 版权风险 |
| 内部讲义 / 未公开课件 | 即使内容好，公开发布有风险 |
| GitHub Private 仓库内容 | 许可证不适用于公开分发 |

---

## 采集流程

### Step 1: 确定主题
选一个计算机专业课主题（如"数据结构"），列出你需要的子主题。

### Step 2: 收集材料
从 Tier 1 来源中寻找 3-8 份文档。一份理想的 Space 包含：

```
主题名称/
├── lecture-*.pdf              ← 2-3 份课件 PDF
├── textbook-notes.md          ← 1 份中文学习笔记
├── review-v1.md               ← 1 份旧版复习提纲
├── review-v2.md               ← 1 份新版复习提纲（有内容更新和矛盾）
├── example-code.py            ← 1-2 份代码文件
├── exam-questions.md          ← 1 份历年真题/模拟题
└── common-mistakes.md         ← 1 份"常见错误"（可选，用于对抗文档）
```

### Step 3: 放入 cases 目录
在 `cases/` 下创建新目录，把所有文件放进去。目录名用简短的英文名（如 `data-structures`）。

### Step 4: 告诉 Agent
在对话中说："我在 `cases/<目录名>/` 下放了一批语料，帮我入库"。

Agent 会自动：
1. 计算所有文件的 SHA-256
2. 写入 manifest.yaml
3. 分析语料特征，建议可覆盖的题型和难度
4. 逐批生成评测用例草案供你审核

---

## 推荐的首批采集目标

按出题潜力和采集难度排序：

| 优先级 | 主题 | 推荐来源 | 预估出题量 |
|--------|------|---------|-----------|
| ⭐1 | 数据结构与算法 | MIT 6.006 课件 + Python 官方教程算法部分 + Wikipedia | 40-60 |
| ⭐2 | Python 编程语言 | Python 官方 Tutorial + Language Reference + HOWTO 指南 | 35-50 |
| ⭐3 | 操作系统 | MIT 6.033/6.828 课件 + Linux Kernel 文档 | 35-50 |
| ⭐4 | 计算机网络 | IETF RFC 精选 + Wikipedia + 开源网络编程指南 | 25-40 |
| ⭐5 | 数据库系统 | PostgreSQL 官方文档 + SQLite 文档 + CMU 15-445 课件 | 30-45 |
| ⭐6 | 机器学习基础 | CS229 已有 + 补充 Stanford CS231n 或 fast.ai 资料 | 35-50 |
| ⭐7 | 软件工程 | 《Clean Code》公开摘要 + 设计模式 Wiki + 开源项目架构文档 | 25-35 |

---

## 常见问题

### Q: 我只找到 2 份文档，不够怎么办？
不够 3 份也可以放入。Agent 可以在入库后帮你设计 CC0 fixture（虚构的补充文档）来填补题型缺口。真实文档 + fixture 的组合比纯 fixture 更好。

### Q: PDF 是扫描版（图片），不是文字版，能用吗？
不太适合。评测数据集的 evidence 需要可检索的文本。如果只有扫描版 PDF，可以：
- 用 OCR 工具先转成文字
- 或者自己手打一份文字版（标记为 CC0）

### Q: 我不确定某份文档的许可证能不能用怎么办？
把文档的来源链接发给 Agent，Agent 可以帮你查许可证信息。

### Q: 可以用中文文档吗？
当然可以。中英文档混合更好——能支撑 bilingual 题型。中文社区的技术文档要注意许可证问题（Tier 3）。

### Q: 一份文档多大合适？
单份 50-500 行比较合适。太短出不了几道题（如 10 行），太长 Agent 处理效率低（如 1000+ 行 PDF 可能包含太多不相关内容）。
