# 评测数据集 (Eval Cases)

> 提交 PR 前请阅读 `DATASET-CONTRIBUTION-GUIDE.md`，所有格式和流程约束以该文档为准。

## 目录结构

```
cases/
├── README.md                           ← 本文件
├── DATASET-CONTRIBUTION-GUIDE.md        ← 格式规范、字段说明、提交流程（权威参考）
│
├── evals/
│   ├── corpus/v0/                      # 评测语料库 v0
│   │   ├── manifest.yaml               # 语料唯一入口（90 个 source）
│   │   ├── README.md                   # 语料库说明
│   │   ├── AUDIT.md                    # 工程审计结果与正式阻塞项
│   │   └── fixtures/                   # CC0 系统验证文本（版本冲突、对抗、不可信）
│   │       ├── README.md
│   │       ├── quicknotes-changelog-v1.txt
│   │       ├── quicknotes-changelog-v2.txt
│   │       ├── quicknotes-faq.md
│   │       ├── quicknotes-adversarial.md
│   │       └── cs229-adversarial-ml-blog.md
│   │
│   └── datasets/knowledge-qa-v0/       # 评测数据集 v0
│       ├── schema.json                 # JSON Schema（2020-12，含 allOf 条件约束）
│       ├── cases.jsonl                 # 276 条评测用例
│       └── README.md                   # 数据集说明与标注规则
│
├── scripts/                            # 质检工具
│   ├── validate.py                     # 一键校验：manifest + SHA-256 + schema + 引用链 + 覆盖统计
│   └── excerpt_hash.py                 # excerpt_sha256 计算工具
│
├── <space-dirs>/                       # 12 个知识空间语料
│   ├── cs229/        (9 files)         # Stanford CS229 机器学习
│   ├── math/         (9 files)         # 数学分析、线性代数、概率论
│   ├── papers/       (17 files)        # 机器人/ML 论文 (ArXiv)
│   ├── dsa/          (8 files)         # 数据结构与算法
│   ├── cpp/          (7 files)         # C++ 语言
│   ├── os/           (7 files)         # 操作系统
│   ├── net/          (8 files)         # 计算机网络
│   ├── db/           (6 files)         # 数据库
│   ├── omnistudio/   (5 files)         # OmniStudio IDE 项目
│   ├── physics/      (7 files)         # 四大力学笔记 (PDF)
│   ├── devtools/     (3 files)         # Docker / Linux / Git
│   └── quicknotes/   (in fixtures)     # 虚构笔记应用（版本冲突 & 对抗 fixture）
│
├── AGENTS.md                           # 评测数据集构建约束（Agent 操作手册）
├── WORKFLOW.md                         # 完整工作流 & 覆盖矩阵（Agent 操作手册）
├── CONTRIBUTING.md                     # 人类协作者指南（如何贡献语料和题目）
└── CORPUS-COLLECTION-GUIDE.md          # 语料采集指南（推荐来源、许可证分类）
```

## 当前状态

| 维度 | 数据 |
|---|---|
| Corpus 版本 | v0 (`frozen`, `internal_team_only`) |
| 知识空间 | 12 |
| 语料文件 | 90 (markdown 35 / pdf 30 / code 15 / notebook 1 / text 9) |
| 评测用例 | 276 |
| Split | development 127 / holdout 149 |
| 题型 | 7/7 全覆盖 |
| 格式 | 8 种 (md / pdf / txt / py / cpp / c / sql / ipynb，其中 notebook 为 P1) |

## 快速开始

```bash
# 1. 进入数据集目录
cd cases

# 2. 一键校验（每次修改后必跑）
python scripts/validate.py

# 3. 冻结语料应返回零问题；任何修改 manifest、来源或 cases.jsonl 后都必须重新校验。
#    冻结范围仅限组员内部使用，不代表公开再分发授权。
```

## Git 分发边界

原始 Space 语料和 `evals/datasets/knowledge-qa-v0/cases.jsonl` 只保留在本地，不进入 Git。
仓库只版本化 manifest、schema、说明与校验工具、评测配置，以及明确许可的 CC0 小型 fixture；
provisional 配置通过 SHA-256 固定本地 `cases.jsonl`，干净 checkout 允许该受控输入缺失。

## 修改指南

### 新增语料
1. 文件放入对应 Space 目录
2. 计算 SHA-256 并登记到 `evals/corpus/v0/manifest.yaml`
3. 跑 `python scripts/validate.py`

### 新增评测用例
1. 确定 Space、category、difficulty
2. 新增一行到 `evals/datasets/knowledge-qa-v0/cases.jsonl`
3. 跑 `python scripts/validate.py`

详细流程见 `WORKFLOW.md`，格式规范见 `DATASET-CONTRIBUTION-GUIDE.md`。

## 已知改进方向

- **内部冻结例外**: 见 `evals/corpus/v0/AUDIT.md` 与 `docs/stage-0-acceptance.md`；外部授权未确认的来源仍仅限组员本地使用
- **难度分布**: easy 仅 16.3%，建议补充到 25-35%
- **adversarial_document**: 仅 5 条，可扩展
- **reference_scope / retrieval_context**: 尚未使用，可增加跨 Space 约束和检索精度测试
- **断言原子性**: 约 16% claims 含 3 句以上，可后续拆分优化
