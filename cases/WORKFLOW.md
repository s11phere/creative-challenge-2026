# 评测数据集构建工作流 v1.0

## 快速启动（每次打开项目先做）

```bash
# 1. 确认在 cases 目录下
cd cases

# 2. 角色分工见 CONTRIBUTING.md（人类做什么、Agent 做什么）
# 3. 读一遍本文件和 AGENTS.md（特别是红线部分）
# 4. 检查当前进度
python scripts/validate.py
```

> **角色说明：** `WORKFLOW.md`（本文件）是给 Agent 看的操作流程。
> `CONTRIBUTING.md` 是给人类协作者看的——告诉你需要提供什么材料、Agent 会怎么处理、项目进度怎么看。

## 红线速查

- **只能操作 `cases/` 内的文件。**
- **所有语料内容必须由用户提供或确认。** 不得自行编造文档原文、题目原文或答案。
- **每步走"草案 → 确认 → 落盘"**，不跳过用户确认。
- **manifest.yaml 是语料唯一入口。** 未登记的文档视为不存在。
- **所有语料默认仅本地使用**；公开发布必须有可核验的许可证、归属、脱敏和人工复核记录。

---

## 目录结构

```
cases/
├── WORKFLOW.md                     ← 本文件
├── AGENTS.md                       ← Agent 操作手册
├── CONTRIBUTING.md
├── CORPUS-COLLECTION-GUIDE.md
├── scripts/
│   ├── validate.py                 ← 一键质量校验
│   └── excerpt_hash.py             ← excerpt_sha256 计算工具
├── <space-dirs>/                   ← 各知识空间语料
└── evals/
    ├── corpus/v0/
    │   ├── manifest.yaml           ← 语料清单（唯一入口）
    │   └── fixtures/               ← CC0 系统验证文本
    └── datasets/knowledge-qa-v0/
        ├── schema.json             ← JSON Schema
        ├── cases.jsonl             ← 评测用例
        └── README.md
```

---

## 完整工作流

### 阶段 A：语料入库（新增 Space 或文档）

```
A1. 确认文档可公开发布（许可证检查）
A2. 放入 cases/ 下对应目录
A3. 计算 SHA-256 → sha256sum <file>
A4. 在 manifest.yaml 的 spaces 下新增 source 条目
    - 填写所有必填字段（source_key, path, format, language, license,
      sensitivity, allowed_uses, redistribution, content_sha256）
    - 如有版本关系，填写 document_key, version, supersedes 等
A5. 运行 python scripts/validate.py 确认 SHA-256 一致
A6. 用户确认 manifest 草案 → 落盘
```

### 阶段 B：评测用例编写（新增题目）

```
B1. 确定 Space 和 category（7 种题型之一）
B2. 撰写问题（中/英/双语均可）
B3. 确定 expected_behavior: answer 还是 refuse
B4-answer（answer 类型）:
    - 拆分 answer_claims（每 claim 一句独立语义断言）
    - 在源文件中精确定位 evidence（page 或 lines）
    - 复制原文 quote
    - 计算 excerpt_sha256：python scripts/excerpt_hash.py "quote文本"
    - 填写 supports_claims 关联到 claim ID
    - 编写 forbidden_claims
B4-refuse（refuse 类型）:
    - answer_claims 和 evidence 设空数组
    - 编写 forbidden_claims（系统不该说的话）
    - 可选填写 reference_scope（标注检查过的文档范围）
B5. 选择 difficulty（easy/medium/hard）和 tags
B6. 确定 split（新增用例默认放 development）
B7. 分配 id（取当前最大 ID + 1，格式 qa-NNN）
B8. 追加一行到 cases.jsonl
B9. 运行 python scripts/validate.py 校验
B10. 用户确认 → 落盘
```

### 阶段 C：质量检查

```
C1. 运行 python scripts/validate.py（硬指标全自动检查）
C2. 对照覆盖矩阵（见下文）检查软指标
C3. 用户抽查 2-3 条，审视 claim 原子性和题目清晰度
C4. 发现问题 → 回到对应阶段修补
C5. 全部绿灯 → 当前版本可以交付
```

---

## 覆盖矩阵

### 题型覆盖（目标：每种 ≥2 条）

| category | 最少 | 当前 | 依赖 |
|---|---|---|---|
| single_document_factual | 2 | 74 | 任意文档 |
| cross_document_synthesis | 2 | 51 | 同一 Space 内 ≥2 份文档 |
| version_or_conflict | 2 | 26 | fixtures 中多版本文档 |
| no_answer | 2 | 33 | Space 隔离或知识盲区 |
| adversarial_document | 1 | 5 | untrusted 文档 |
| bilingual | 2 | 41 | 中英混合问法 |
| code_and_nl | 2 | 46 | 代码文件 |

### 格式覆盖（目标：5 种全有）

| format | 已登记 | 已出题 |
|---|---|---|
| markdown | 31 | ✓ |
| code_cpp | 7 | ✓ |
| code_python | 9 | ✓ |
| pdf | 30 | ✓ |
| text | 6 | ✓ |

### 难度分布

| difficulty | 建议占比 | 当前实际 |
|---|---|---|
| easy | 30-40% | 16.3% (45 条) |
| medium | 40-50% | 66.3% (183 条) |
| hard | 10-20% | 17.4% (48 条) |

### Split 数量

| split | 最低要求 | 当前实际 |
|---|---|---|
| development | ≥20 | 127 |
| holdout | ≥10 | 149 |

---

## 校验项清单（validate.py 做的事）

| # | 检查项 | 类型 |
|---|---|---|
| 1 | manifest.yaml 格式正确 | 硬 |
| 2 | 所有 source 文件存在 | 硬 |
| 3 | content_sha256 与实际文件一致 | 硬 |
| 4 | cases.jsonl 每行是合法 JSON | 硬 |
| 5 | 每行通过 schema.json 校验 | 硬 |
| 6 | id 格式正确且唯一 | 硬 |
| 7 | expected_behavior 与 claims/evidence 约束一致 | 硬 |
| 8 | evidence.supports_claims 引用的 claim ID 存在 | 硬 |
| 9 | source_version 与 manifest 中 content_sha256 一致 | 硬 |
| 10 | excerpt_sha256 格式正确（64 位 hex） | 硬 |
| 11 | source_key 在 manifest 中存在 | 硬 |
| 12 | 统计 development/holdout 数量 | 信息 |
| 13 | 统计各 category 数量 | 信息 |
| 14 | 统计各 difficulty 数量 | 信息 |
| 15 | 统计各 format 引用次数 | 信息 |

---

## 常见问题

### Q: 怎么算 excerpt_sha256？
```bash
python scripts/excerpt_hash.py "要计算的原文引用文本"
```
原理：NFKC 归一化 → 空白折叠为单空格 → SHA-256。

### Q: 题目 id 怎么分配？
从 qa-001 开始递增。新增前先看 cases.jsonl 已有最大 ID。id 一旦分配不要修改（否则评测历史无法回溯）。

### Q: 发现已落盘的题有问题怎么办？
直接在 cases.jsonl 中修改对应行，重新跑 validate.py。如果题目已经用于正式评测，需要升级 dataset_version。当前 v0 阶段视为草稿，可直接修改。

### Q: 怎么加新 Space？
1. 在 `cases/` 下新建目录放语料
2. 在 manifest.yaml 的 spaces 列表新增一个 space
3. 填写 source 条目
4. 跑 validate.py

### Q: 怎么加新 fixture？
1. 写文本文件放入 `evals/corpus/v0/fixtures/`
2. 在 manifest.yaml 新增 source（space: system_validation）
3. 跑 validate.py
