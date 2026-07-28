# AGENTS.md — cases 评测数据集构建

## 边界规则（红线）

本目录是从零构建的独立评测数据集，严格遵循 `DATASET-CONTRIBUTION-GUIDE.md`。

- **所有语料内容必须由用户提供或确认。** 不得自行编造文档原文、题目原文或答案文本。
- **每条 evidence 必须可追溯到 manifest 中登记的真实文件位置。** SHA-256 必须通过工具实际计算，不得编造。
- **manifest.yaml 是语料的唯一入口。** 未在 manifest 中登记的文档视为不存在，不得在评测用例中引用。
- **cases.jsonl 每行必须通过 `schema.json` 校验。**
- **所有语料默认 `private_local`**；只有许可证、归属、脱敏和人工复核均有记录，且
  `allowed_uses` 明确包含 `repository_fixture` 时才可提交或公开演示。

## 完整工作流见 WORKFLOW.md

本文件仅列出核心约束。**详细操作步骤、覆盖矩阵、校验清单见 `WORKFLOW.md`。**
每次启动新会话时，Agent 应先阅读 `WORKFLOW.md` + `../DATASET-CONTRIBUTION-GUIDE.md`。

## 目录结构

```
cases/
├── AGENTS.md                       ← 本文件
├── CONTRIBUTING.md
├── WORKFLOW.md
├── CORPUS-COLLECTION-GUIDE.md
├── scripts/
│   ├── validate.py
│   └── excerpt_hash.py
├── <space-dirs>/                   ← 各知识空间语料
└── evals/
    ├── corpus/v0/
    │   ├── manifest.yaml           ← 语料唯一入口
    │   └── fixtures/               ← CC0 系统验证文本
    └── datasets/knowledge-qa-v0/
        ├── schema.json             ← JSON Schema
        ├── cases.jsonl             ← 评测用例
        └── README.md
```

## 工作流程

每一步操作遵循 **草案 → 人工确认 → 落盘** 模式：

1. 生成变更草案（manifest 新增条目、cases.jsonl 新增行、新 fixture 文本等）
2. 展示给用户确认
3. 用户确认后才写入文件

## 关键约束速查

| 约束 | 说明 |
|---|---|
| Space 隔离 | 每个 Space 独立，题目必须指定 `space_id` |
| id 格式 | `qa-NNN`（三位数字），全局唯一 |
| split 分配 | development ≥ 20 条，holdout ≥ 10 条 |
| answer 类型 | 必须有 claims + evidence（至少各1） |
| refuse 类型 | claims 和 evidence 必须为空数组 |
| claim 原子性 | 每个 claim 是不可再分的独立语义断言 |
| excerpt_sha256 | NFKC 归一化 + 空白折叠后的 SHA-256 |
| source_version | manifest 中 content_sha256 的值 |
| 7 种题型 | single_document_factual / cross_document_synthesis / version_or_conflict / no_answer / adversarial_document / bilingual / code_and_nl |
| 5 种格式 | pdf / markdown / text / code_python / code_cpp |

## 代理使用提示

- 启动时 Read 本文件 + `DATASET-CONTRIBUTION-GUIDE.md`
- 每次新增内容前先 Read `manifest.yaml` 和 `cases.jsonl` 获取当前状态
- 完工前运行 `schema.json` 校验
