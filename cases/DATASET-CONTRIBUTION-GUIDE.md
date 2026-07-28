# 数据集构建指南

## 概述

本项目构建一个本地优先、来源可追溯的个人知识工作台。评测数据集用于验证系统的检索和问答质量，包含两大部分：

1. **评测语料库 (Corpus)** — 系统检索的知识来源文档
2. **评测数据集 (Dataset)** — 结构化问答评测用例

本文档说明数据集的结构、格式要求以及贡献方式，供合作者参考。

---

## 目录结构

```
cases/
├── evals/
│   ├── corpus/
│   │   └── v0/                    # 语料库版本
│   │       ├── manifest.yaml      # 语料清单（唯一入口）
│   │       └── fixtures/          # 公开测试 fixture
│   └── datasets/
│       └── knowledge-qa-v0/       # 评测数据集
│           ├── schema.json        # JSON Schema 定义
│           ├── cases.jsonl        # 评测用例（逐行 JSON）
│           └── README.md          # 数据集说明
```

---

## 第一部分：评测语料库 (Corpus)

语料库由 `manifest.yaml` 统一管理。**所有来源必须在 manifest 中显式列出，严禁递归扫描整个目录。**

### 知识空间 (Space)

知识按 **Space（知识空间）** 隔离。每个 Space 对应一个独立的知识领域，在 `manifest.yaml` 的 `spaces` 列表中定义。

> 如需新增 Space，在 manifest.yaml 的 `spaces` 列表中添加即可。

### manifest.yaml 格式

```yaml
schema_version: '1.0'
corpus_version: v0
status: draft_pending_license_review     # 新建版本的初始状态；当前 v0 已 frozen/internal_team_only
path_base: cases_root                    # 路径基准
description: 语料描述。
hashing:
  source: sha256 of raw file bytes               # 来源文件哈希算法
  excerpt: sha256 of UTF-8 text after Unicode NFKC and whitespace collapsing  # 摘录哈希算法
spaces:
  - id: your_space                     # Space 唯一 ID
    name: 知识空间名称                   # Space 显示名称
    description: 说明文字。              # Space 描述
    sources:
      - source_key: your_space/doc-name   # 来源唯一标识（space_id/名称）
        path: path/to/file.pdf          # 相对于 cases/ 的路径
        format: pdf                      # 文件格式
        language: en                     # 语言代码
        license: undetermined            # 许可证
        sensitivity: private_local       # 敏感度
        allowed_uses:                    # 允许的用途列表
          - local_development
          - local_evaluation
        redistribution: review_required  # 再分发策略
        content_sha256: a1b2c3d4e5f6...  # 文件的 SHA-256（必填）
```

### Source 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `source_key` | 是 | 全局唯一标识，格式: `{space_id}/{描述性名称}` |
| `path` | 是 | 相对于 `cases/` 目录的文件路径 |
| `format` | 是 | 文件格式（见下方列表） |
| `language` | 是 | 语言代码：`en` / `zh` / `mixed` |
| `license` | 是 | 许可证 |
| `sensitivity` | 是 | 敏感度：`private_local` / `public_demo` / `restricted` |
| `allowed_uses` | 是 | 允许用途列表 |
| `redistribution` | 是 | 再分发策略 |
| `content_sha256` | 是 | 文件原始字节的 SHA-256 校验和 |
| `version` | 否 | 文档版本号（仅版本化文档需要） |
| `document_key` | 否 | 逻辑文档标识（同一文档多版本时使用） |
| `lifecycle_status` | 否 | 生命周期状态（`current` / `superseded`） |
| `supersedes` | 否 | 取代的版本号 |
| `trust_level` | 否 | 信任级别（`unverified` / `untrusted_adversarial_fixture`） |

### 支持的格式

| format | 说明 |
|---|---|
| `pdf` | PDF 文档 |
| `markdown` | Markdown 文件 |
| `text` | 纯文本文件 |
| `code_python` | Python 代码 |
| `code_cpp` | C++ 代码 |
| `code_c` | C 代码 |
| `code_sql` | SQL 代码 |
| `notebook` | Jupyter Notebook；属于 P1，P0 摄入验收不得视为已支持 |

### 敏感度与许可证策略

- `private_local` — 默认值；仅限本地开发与评测，不得进入 Git 或公开演示包
- `public_demo` — 可进入公开演示与仓库 fixture
- `restricted` — 不得处理；只有书面授权和已记录例外才能纳入
- 个人笔记使用 `private_personal_notes` 许可证
- 公开 fixture 使用 `cc0-1.0` 许可证
- 开源项目使用其自身许可证（如 `gplv3`）

### 版本化管理

同一逻辑文档的多个版本可通过以下字段管理：

```yaml
- source_key: your_space/doc-v2
  document_key: your_space/doc         # 同一 document_key 关联不同版本
  version: '2.0'
  lifecycle_status: current
  supersedes: '1.0'                    # 取代哪个版本
```

### Fixture 文件

`fixtures/` 目录存放确定性文本 fixture，用于测试版本冲突、来源冲突和不可信文档等边缘场景。

### 贡献语料库的步骤

1. **确认文件准备就绪**，放到 `cases/` 下适当的目录
2. **计算 SHA-256**：`sha256sum your-file.pdf`（或 PowerShell: `Get-FileHash your-file.pdf -Algorithm SHA256`）
3. **在 `manifest.yaml` 新增 source 条目**，填入所有必填字段
4. **确定敏感度和许可证分类**，选择合适的 `allowed_uses` 和 `redistribution`
5. **如果是已有文档的新版本**，添加 `document_key`、`version`、`supersedes` 等版本字段
6. **PR 提交前**，确保：
   - 文件路径正确，文件确实存在
   - `content_sha256` 与实际文件匹配
   - 许可证信息准确
   - 不包含未脱敏的个人信息

---

## 第二部分：评测数据集 (Dataset)

评测数据集是 JSONL 文件，每行一个 JSON 对象。位于 `evals/datasets/<dataset-name>/cases.jsonl`。

### Schema 定义

完整的 JSON Schema 见对应数据集目录下的 `schema.json`。

### 评测用例结构

```json
{
  "id": "qa-001",
  "dataset_version": "v0",
  "split": "development",
  "category": "single_document_factual",
  "space_id": "your_space",
  "question": "问题文本",
  "expected_behavior": "answer",
  "answer_claims": [
    {"id": "c1", "text": "期望答案的核心断言1"},
    {"id": "c2", "text": "期望答案的核心断言2"}
  ],
  "evidence": [
    {
      "source_key": "your_space/doc-name",
      "source_version": "a1b2c3d4e5f6...",
      "locator": {"type": "pdf_page", "page": 11},
      "quote": "原文引用文本",
      "excerpt_sha256": "摘录文本的 SHA-256",
      "supports_claims": ["c1"]
    }
  ],
  "forbidden_claims": [
    "禁止出现的错误断言"
  ],
  "difficulty": "easy",
  "tags": ["zh"]
}
```

### 字段说明

#### 必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一标识，格式 `qa-NNN`（三位数字） |
| `dataset_version` | string | 数据集版本标识 |
| `split` | string | 数据集划分：`"development"` 或 `"holdout"` |
| `category` | string | 评测分类（见下方分类列表） |
| `space_id` | string | 所属知识空间 ID（与 manifest.yaml 一致） |
| `question` | string | 问题文本 |
| `expected_behavior` | string | 期望行为：`"answer"`（回答）或 `"refuse"`（拒答） |
| `answer_claims` | array | 期望答案的断言列表（`expected_behavior` 为 `"refuse"` 时为空数组） |
| `evidence` | array | 支撑证据列表（`expected_behavior` 为 `"refuse"` 时为空数组） |
| `forbidden_claims` | array | 禁止出现的断言（当系统给出错误结论时用于扣分） |
| `difficulty` | string | 难度：`"easy"` / `"medium"` / `"hard"` |
| `tags` | array | 标签列表（去重） |

#### 可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `reference_scope` | array | 限制参考来源列表 |
| `retrieval_expectations` | object | 检索约束条件 |
| `retrieval_context` | object | 检索上下文参数（如 `{"include_superseded": true}`） |

### 评测分类

| category | 说明 | 场景说明 |
|---|---|---|
| `single_document_factual` | 单文档事实性问答 | 答案可在一份文档中找到 |
| `cross_document_synthesis` | 跨文档综合 | 需要综合多份文档信息 |
| `version_or_conflict` | 版本升级/来源冲突 | 不同版本或来源的信息存在差异 |
| `no_answer` | 拒答场景 | 知识库无法回答的问题 |
| `adversarial_document` | 不可信文档 | 文档含有误导或对抗性内容 |
| `bilingual` | 中英双语问答 | 中英文混合的提问 |
| `code_and_nl` | 代码+自然语言混合 | 分析代码行为或机制 |

### answer_claims 格式

每个 claim 对象：

```json
{
  "id": "c1",          // 格式 cN（N 从 1 开始）
  "text": "断言文本"    // 一句独立的语义断言
}
```

- `expected_behavior` 为 `"answer"` 时，至少 1 个 claim
- `expected_behavior` 为 `"refuse"` 时，claims 必须为空数组

### evidence 格式

每条 evidence 对象：

```json
{
  "source_key": "your_space/doc-name",     // manifest.yaml 中的 source_key
  "source_version": "a1b2c3d4...",         // 来源文件的 SHA-256
  "locator": {"type": "pdf_page", "page": 11}, // 定位信息
  "quote": "原文引用文本",                   // 原文引用
  "excerpt_sha256": "摘录文本的 SHA-256",     // 摘录文本（NFKC 归一化 + 空白折叠后）的 SHA-256
  "supports_claims": ["c1"]                  // 支撑的 claim ID 列表
}
```

#### 定位器 (locator) 格式

支持两种定位方式：

**PDF 页面定位：**
```json
{"type": "pdf_page", "page": 11}
```

**文本行定位：**
```json
{"type": "lines", "start": 1336, "end": 1359}
```

### forbidden_claims 示例

用于描述系统**不该说什么**，尤其适用于：
- 拒答场景：防止系统故作合理但错误的回答
- 对抗场景：防止受不可信文档诱导
- 混淆场景：防止混淆相似概念

```json
"forbidden_claims": [
  "避免出现的错误结论1",
  "避免出现的错误结论2"
]
```

### 数据集划分

- **development 集**（至少 20 条）：开发调参使用
- **holdout 集**（至少 10 条）：冻结不参与调参，仅用于最终评估

新增用例应优先补充到 development 集。holdout 集仅在发版前根据 coverage 缺口补充。

### 约束规则

1. **expected_behavior 为 answer 时**：必须提供至少 1 个 `answer_claims` 和至少 1 条 `evidence`
2. **expected_behavior 为 refuse 时**：`answer_claims` 和 `evidence` 必须为空数组
3. **id 必须唯一**，格式 `qa-NNN`
4. **claim 引用一致性**：`evidence[].supports_claims` 引用的 claim ID 必须在 `answer_claims` 中存在
5. **SHA-256 准确性**：`source_version` 必须与 manifest 中的 `content_sha256` 一致；`excerpt_sha256` 须对 quote 文本按规则计算（NFKC 归一化 + 空白折叠后取 SHA-256）

### 难度分级参考

| 难度 | 特征 |
|---|---|
| `easy` | 单文档、直接引用、答案在原文中明确出现 |
| `medium` | 需综合同一 Space 内 2-3 份文档、或有一定推理 |
| `hard` | 跨 Space 约束、版本冲突、对抗性文档、需严格推理或拒答 |

### 贡献评测用例的步骤

1. **确定要覆盖的 Space 和场景类型**，选择对应 category
2. **撰写问题**（支持中英文或双语）
3. **确定期望行为**：知识库能否回答
4. **编写 answer_claims**：将期望答案拆分为独立的语义断言
5. **精确定位 evidence**：从 manifest 中的源文件查找原文引用
   - PDF：标注页码
   - Markdown/TXT：标注行号范围
6. **计算 excerpt_sha256**：
   - 对 quote 文本做 Unicode NFKC 归一化
   - 折叠连续空白（含换行）为单个空格
   - 取 UTF-8 字节的 SHA-256
7. **编写 forbidden_claims**：描述系统不应犯的错误
8. **选择难度和标签**
9. **确定 split**：新用例默认放入 `development`
10. **验证**：用 `schema.json` 校验格式，确认 `source_key` 在 `manifest.yaml` 中存在

---

## 第三部分：哈希计算指南

### source SHA-256（文件校验）

```bash
# Linux/Mac
sha256sum your-file.pdf

# Windows (PowerShell)
Get-FileHash your-file.pdf -Algorithm SHA256
```

### excerpt SHA-256（摘录文本校验）

摘录文本的 SHA-256 计算流程：

1. 取原始文本
2. 做 Unicode NFKC 归一化
3. 折叠所有连续空白字符（包括换行、Tab 等）为单个空格
4. 取 UTF-8 编码字节的 SHA-256

Python 参考实现：

```python
import hashlib, unicodedata, re

def excerpt_sha256(text: str) -> str:
    normalized = unicodedata.normalize('NFKC', text)
    collapsed = re.sub(r'\s+', ' ', normalized).strip()
    return hashlib.sha256(collapsed.encode('utf-8')).hexdigest()
```

---

## 第四部分：质量要求

### 语料库

- 每个 source 必须有准确的 `content_sha256`，与实际文件一致
- 文件路径正确，文件确实存在于 `cases/` 下
- 许可证和敏感度分类准确
- 个人笔记不得进入 `public_demo` 空间

### 评测用例

- **来源可追溯**：每条 evidence 的 quote 必须能在对应源文件中定位到
- **断言原子性**：每个 claim 应是不可再分的独立语义单元
- **无歧义**：问题应清晰明确，不依赖外部常识猜测
- **覆盖多样性**：尽可能覆盖不同 Space、格式、分类和难度
- **对抗覆盖**：包括拒答、版本冲突、不可信文档等边缘场景
- **双语支持**：中文 Space 可混入英文问题以测试双语检索能力

---

## 第五部分：提交流程

1. 语料文件放入 `cases/` 下对应目录
2. 更新 `manifest.yaml`（新增 source）
3. 评测用例追加到 `cases.jsonl`
4. 用 `schema.json` 校验新增用例
5. 提交 PR，在描述中说明：
   - 新增/变更的数据内容
   - 覆盖的评测分类
   - 数据来源和许可证情况
