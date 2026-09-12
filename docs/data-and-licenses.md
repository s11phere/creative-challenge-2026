# 数据与许可说明

本文件对应《CC2026 获奖作品官网接入与技术交付规范》第 8 节“服务通用交付要求”的最后一项：
**说明第三方模型、数据、字体和素材许可**。它是 `docs/` 下的交付文档之一，与
[易知官网适配交付手册](cc2026-yizhi-handoff.md)、[CC2026 官网接入交付说明](cc2026-delivery.md)
配合使用。

> 本文件是工程与合规事实的记录，不是法律意见。标注为「待维护者确认」的条目必须由项目维护者
> 或法务做出决定后才能对外声明。

## 1. 范围与口径

### 1.1 覆盖范围

| 范围 | 是否覆盖 | 说明 |
| --- | --- | --- |
| 第三方模型权重与推理镜像 | 是 | 见第 2 节 |
| Python / JS 运行时依赖 | 是 | 见第 3 节 |
| 容器与基础设施镜像 | 是 | 见第 3.4 节 |
| 评测语料与数据边界 | 是 | 见第 4 节 |
| 字体、图标、图片与素材 | 是 | 见第 5 节 |
| 项目自身代码的开源许可 | **否（无许可可述）** | 仓库当前没有 `LICENSE` 文件，见第 7.1 节 |
| 运维、备份、升级与回滚 | 否 | 属规范第 8 节 `docs/operations.md` 的范畴，由该文件覆盖，本文件不重复 |

### 1.2 结论口径

本文件所有结论按下表分级，不混用：

| 标记 | 含义 |
| --- | --- |
| **已核实（仓库）** | 可在本仓库文件的具体行号上直接读到 |
| **已核实（官方）** | 已通过上游官方许可文本或官方仓库确认，链接见第 8 节 |
| **已核实（仓库 + 官方）** | 两处证据一致 |
| **未核实** | 本次调查无法取得可靠证据，已登记在第 7 节待确认 |

调查基线：仓库工作树 `/mnt/f/project/creative-challenge-2026`（`git rev-parse HEAD` 为
`e69ce8d` 之后的本地工作状态）；依赖版本取自 `uv.lock`、`apps/web/pnpm-lock.yaml` 和已安装的
`.venv/`、`apps/web/node_modules/`。**本环境无法访问 `huggingface.co`**，模型卡信息通过
`hf-mirror.com` 镜像读取，属“官方镜像”而非“官方原站”，已在第 7 节登记。

> **行号会漂移。** 本文件的 `路径:行号` 引用对应上述基线快照。调查期间 `.env.example`
> 已被并发修改过一次（模型配置段落整体下移 4 行）。若引用处内容对不上，请按符号名
> （如 `MODEL_PROVIDER`、`--revision`、`distribution_scope`）重新定位，而不是依赖行号。
> 依赖版本、许可证结论和列举的取值不因行号漂移而失效。

## 2. 第三方模型

### 2.1 模型清单

| 服务 | HuggingFace ID | 用途 | 固定 revision | 许可证 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `tei`（embedding profile） | `Qwen/Qwen3-Embedding-0.6B` | 查询/文档向量化，`dense` 召回 | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` | Apache-2.0 | 已核实（仓库 + 官方） |
| `tei-reranker`（reranker profile） | `BAAI/bge-reranker-v2-m3` | 候选精排（`dense_rerank` 默认路径） | **未固定** | Apache-2.0 | 许可证已核实（官方）；revision 未核实，见第 7.3 节 |

### 2.2 仓库证据

| 事实 | 文件与行号 |
| --- | --- |
| 模型清单与 HuggingFace ID | `docs/model-setup.md:9-12` |
| Embedding 预下载命令固定 revision | `docs/model-setup.md:83-84`、`docs/model-setup.md:96-98` |
| 离线覆盖文件固定 `--model-id` 与 `--revision` | `docs/model-setup.md:105-115` |
| `tei` 服务：`--model-id Qwen/Qwen3-Embedding-0.6B` + `--revision 97b0c614...` | `deploy/compose.yaml:273-288` |
| `tei-reranker` 服务：**只有 `--model-id`，没有 `--revision`** | `deploy/compose.yaml:310-317` |
| TEI 镜像按 digest 固定（GPU / CPU） | `deploy/compose.yaml:277`、`deploy/compose.yaml:314`、`deploy/compose.cpu.yaml:17,24` |
| 模型缓存使用命名卷，可复用预热缓存 | `deploy/compose.yaml:293-294,320-321,337-347` |
| 应用侧配置项与默认值 | `.env.example:50-79`、`.env.example:87-116` |
| 能力别名与指令版本绑定 | `docs/development-environment.md:122-135`、`docs/development-environment.md:331-338` |
| 解析器固定 `PyMuPDF==1.28.0` | `docs/architecture.md:468` |

### 2.3 再分发与使用注意

- **权重不随仓库分发（已核实）。** 仓库不包含任何模型权重文件；TEI 容器首次启动时从
  HuggingFace Hub 下载，或由 `--local-dir` 挂载的离线目录提供（`docs/model-setup.md:28-31`、
  `docs/model-setup.md:91-122`）。Docker 卷 `deploy_teidata` / `deploy_rerankerdata` 只是本地缓存。
- **两个模型均为 Apache-2.0（已核实，官方）。** 若以任何形式再分发权重文件，需随附
  Apache-2.0 许可副本、保留版权与归属声明、标注修改，并且不得暗示上游背书（Apache-2.0 第 4、6 条）。
- **默认路径不调用任何第三方模型（已核实）。** `MODEL_PROVIDER=fake` 是默认值
  （`.env.example:46-50`），CI 与本地工程测试使用确定性 fake；真实模型是显式 opt-in。
- **外部 Chat Provider 是独立的数据边界（已核实）。** `MODEL_ALLOW_EXTERNAL=false` 为默认值
  （`.env.example:76`）；工作区文件送入外部模型还需要
  `AGENT_WORKSPACE_MODEL_VISIBILITY_CONSENT=true`（`.env.example:77-79`）。相关决策见
  `docs/adr/004-local-first-data-boundary.md:12-17`。
- **模型 revision 与向量身份绑定（已核实）。** 更换 revision 会产生新的 `embedding_version`，
  必须受控重建，不能让新查询 identity 检索旧向量（`docs/development-environment.md:331-338`）。
  因此“固定 revision”不只是可复现性要求，也是数据一致性要求。
- **镜像源是第三方服务。** `docs/model-setup.md:69-71` 推荐南京大学镜像与 `hf-mirror.com`。
  这些镜像站点本身不受模型许可覆盖，也不在本仓库的信任边界内。

## 3. 第三方软件依赖

### 3.1 许可敏感项（copyleft / 非 OSI）

| 组件 | 版本 | 许可证 | copyleft 义务 | 结论 |
| --- | --- | --- | --- | --- |
| **PyMuPDF** | `1.28.0` | **AGPL-3.0 或 Artifex 商业许可（双许可）** | **强 copyleft** | 已核实（仓库 + 官方） |
| **dramatiq** | `2.2.0` | LGPL-3.0-or-later | 弱 copyleft | 已核实（仓库） |
| **certifi** | `2026.6.17` | MPL-2.0 | 文件级 copyleft | 已核实（仓库） |
| **pathspec** | `1.1.1` | MPL-2.0 | 文件级 copyleft | 已核实（仓库） |
| **redis（容器镜像）** | `redis:7-alpine@sha256:6ab0b6e7…` | 未核实：7.4 及以后为 RSALv2 / SSPLv1，7.2 及以前为 BSD-3-Clause | 取决于实际版本 | **未核实**，见第 7.5 节 |

#### PyMuPDF（唯一需要维护者决策的强 copyleft 项）

| 事实 | 证据 |
| --- | --- |
| 声明为固定版本 `PyMuPDF==1.28.0` | `pyproject.toml:9` |
| 锁文件条目（sdist/wheel 哈希） | `uv.lock:1246-1260` |
| 安装元数据写明 "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial" | `.venv/Lib/site-packages/pymupdf-1.28.0.dist-info/METADATA` |
| 官方 README 的 Licensing 段落：开源为 GNU AGPL v3，闭源需 Artifex 商业许可 | 见第 8 节链接 |
| 代码中的实际调用点 | `packages/infrastructure/src/infrastructure/parsers/pdf_parser.py:21`（`import fitz`） |
| 架构文档记录的解析器职责 | `docs/architecture.md:468` |
| 评测语料的 PDF 提取协议固定为 `PyMuPDF==1.28.0` | `docs/stage-0-acceptance.md:32-37`、`cases/evals/corpus/v0/PDF-EXTRACTION.md` |

**义务说明。** AGPL-3.0 第 13 条要求：若用户通过网络与修改后的程序交互，必须向其提供对应源码。
当前用法是**未经修改的 PyPI 轮子**在服务端被导入调用，不属于“修改 PyMuPDF 源码后分发”，
但“以网络服务形式对外提供含 PyMuPDF 的能力”本身正是 AGPL 第 13 条关注的情形。
**是否触发源码提供义务、以及选择哪条路径，是维护者/法务决定，本文件不作结论。** 可选路径：

1. 保持 AGPL，并准备按 AGPL 向服务使用者提供本项目的完整对应源码（这要求项目自身先有明确许可，见第 7.1 节）；
2. 向 Artifex 购买商业许可，移除 AGPL 义务；
3. 用宽松许可的 PDF 解析器替换 PyMuPDF。注意 `docs/stage-0-acceptance.md:32-37` 把
   `Page.get_text("text")` 的输出固定为语料哈希基线，替换解析器会使 130 条 PDF evidence 的
   `excerpt_sha256` 失效，必须走受控重建。

#### 弱 copyleft 项

- **dramatiq (LGPL-3.0-or-later)** 以库形式被导入（见 `uv.lock`），未修改其源码。LGPL 的
  通常要求是：保留版权与许可声明，并允许接收方替换该库。当前用法（`pip`/`uv` 安装的未修改
  包）满足这一要求。升级或 patch dramatiq 源码时需要重新评估。
- **certifi / pathspec (MPL-2.0)** 为文件级 copyleft：未修改其文件即无额外源码提供义务。

### 3.2 Python 依赖总览

`uv.lock` 共锁定 **82 个包**；其中 80 个已安装到 `.venv/`，未安装的 2 个是根包
`agent-knowledge-repo`（无 dist-info）与平台限定的 `uvloop`。对已安装的 80 个 distribution
逐个读取 `License-Expression`、`License` 与 `Classifier: License` 三个字段后，**73 个第三方包
全部声明了许可证，只有 7 个本仓库 workspace 包没有任何声明**：

| 类别 | 数量 | 代表 | 结论 |
| --- | --- | --- | --- |
| 强 copyleft (AGPL) | 1 | `pymupdf` | 已核实（仓库） |
| 弱/文件级 copyleft | 3 | `dramatiq` (LGPL-3.0+)、`certifi`、`pathspec` (MPL-2.0) | 已核实（仓库） |
| 宽松许可 | 69 | MIT / BSD-2/3-Clause / Apache-2.0 / PSF-2.0 等 | 已核实（仓库，逐包） |
| 无许可声明 | 7 | 本仓库自身的 workspace 包：`agent-runtime`、`api`、`application`、`domain`、`infrastructure`、`model-gateway`、`worker` | 属项目自身代码，见第 7.1 节 |

关键运行时依赖的完整许可值（均从已安装 `METADATA` 读取）：

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| `pymupdf` | 1.28.0 | **AGPL-3.0 或 Artifex 商业许可** |
| `pypdf` | 6.14.2 | BSD-3-Clause（宽松，仅开发依赖，见下） |
| `dramatiq` | 2.2.0 | LGPL-3.0-or-later |
| `certifi` | 2026.6.17 | MPL-2.0 |
| `pgvector`（Python 客户端） | 0.5.0 | MIT |
| `redis`（Python 客户端，注意与容器镜像区分） | 8.0.1 | MIT |
| `fastapi` | 0.139.2 | MIT |
| `starlette` | 1.3.1 | BSD-3-Clause |
| `uvicorn` | 0.51.0 | BSD-3-Clause |
| `pydantic` / `pydantic-settings` | 2.13.4 / 2.14.2 | MIT |
| `SQLAlchemy` | 2.0.51 | MIT |
| `alembic` | 1.18.5 | MIT |
| `asyncpg` | 0.31.0 | Apache-2.0 |
| `PyYAML` | 6.0.3 | MIT |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `protobuf` | 7.35.1 | BSD-3-Clause |
| `requests` | 2.34.2 | Apache-2.0 |
| `markdown-it-py` | 4.2.0 | MIT |
| `jsonschema` | 4.26.0 | MIT |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `websockets` | 16.1 | BSD-3-Clause |
| `watchfiles` | 1.2.0 | MIT |
| `greenlet` | 3.5.3 | MIT AND PSF-2.0 |
| `packaging` | 26.2 | Apache-2.0 OR BSD-2-Clause |

OpenTelemetry 系列（`opentelemetry-api`/`-sdk`/`-proto`/`-instrumentation-*`，1.44.0 / 0.65b0）
全部为 Apache-2.0。如需逐项署名清单，应作为独立任务生成完整的第三方声明文件。

#### pypdf（BSD-3-Clause，仅开发依赖）

| 事实 | 证据 |
| --- | --- |
| 声明在 `[dependency-groups].dev` 中，固定 `pypdf==6.14.2` | `pyproject.toml:75` |
| 锁文件标记为 dev 依赖 | `uv.lock:39,61`（`[package.metadata.requires-dev]`）、`uv.lock:1263-1269` |
| 许可证：BSD-3-Clause | 安装元数据 `License-Expression: BSD-3-Clause`；上游 `LICENSE` 全文（见第 8 节） |
| 结论 | 宽松许可，无 copyleft 义务；**不进入运行时镜像**（`deploy/Dockerfile.api:20` 使用 `uv sync --frozen --no-dev --package api`），因此也不构成再分发义务 |

注意与 PyMuPDF 的区别：两者都用于 PDF，但 `pypdf` 只在开发/测试组，`pymupdf` 是运行时依赖
（`pyproject.toml:9`）。第 3.1 节的 AGPL 义务只来自后者。

### 3.3 JS / 前端依赖

`apps/web/package.json:21-30` 列出的运行时依赖：

| 包 | 版本 | 许可证 | 结论 |
| --- | --- | --- | --- |
| `katex` | `0.16.21` | MIT（**代码**） | 已核实（仓库 + 官方） |
| `katex` 随包字体 | 随 `0.16.21` | **SIL OFL 1.1**（**字体**） | 已核实（仓库 + 官方），见第 5.1 节 |
| `lucide-react` | `1.25.0` | ISC | 已核实（仓库 `LICENSE` 文件） |
| `react` / `react-dom` | `19.2.7` | MIT | 已核实（仓库） |
| `react-markdown` | `10.1.0` | MIT | 已核实（仓库） |
| `rehype-katex` | `7.0.1` | MIT | 已核实（仓库） |
| `remark-gfm` | `4.0.1` | MIT | 已核实（仓库） |
| `remark-math` | `6.0.0` | MIT | 已核实（仓库） |
| `@tanstack/react-query` | `5.101.2` | MIT | 已核实（仓库） |

**使用的实际位置：** `katex` 只在 `apps/web/src/QAWorkspace.tsx:19,23` 被间接引入
（`import rehypeKatex` + `import 'katex/dist/katex.min.css'`）；`lucide-react` 在 6 个组件中
使用（`apps/web/src/AgentRunTimeline.tsx:14`、`apps/web/src/App.tsx:21`、
`apps/web/src/ExamInteractionCard.tsx:1`、`apps/web/src/QAWorkspace.tsx:17`、
`apps/web/src/SkillsPanel.tsx:16`、`apps/web/src/SourcesPanel.tsx:17`）。

**无 AGPL 依赖。** 前端运行时依赖中没有 AGPL 或 GPL 项，MIT / ISC 只要求保留版权与许可声明。

### 3.4 容器与基础设施镜像

| 镜像 | 固定方式 | 许可证 | 结论 |
| --- | --- | --- | --- |
| `ghcr.io/huggingface/text-embeddings-inference:cuda-1.9` | digest | Apache-2.0 | 已核实（官方） |
| `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` | digest | Apache-2.0 | 已核实（官方） |
| `pgvector/pgvector:pg16` | digest | PostgreSQL License | 已核实（官方） |
| `otel/opentelemetry-collector-contrib:0.121.0` | digest | Apache-2.0 | 未逐项核实 |
| `redis:7-alpine` | digest | RSALv2 / SSPLv1（≥7.4）或 BSD-3-Clause（≤7.2） | **未核实**，见第 7.5 节 |
| `python:3.12-slim-bookworm` | digest | PSF-2.0 | 未逐项核实 |
| `node:24.11.0-bookworm-slim` | digest | MIT | 未逐项核实 |
| `nginx:alpine` | digest | BSD-2-Clause | 未逐项核实 |

镜像定义位置：`deploy/compose.yaml:13,31,265,277,314`、`deploy/compose.cpu.yaml:17,24`、
`deploy/Dockerfile.api:1,22`、`deploy/Dockerfile.worker:1,21`、`deploy/Dockerfile.web:1,18`。

**Redis 的特别注意。** 2024-03-20 起，Redis 7.4 及以后版本改为 RSALv2 / SSPLv1 双许可，
不再是 BSD-3-Clause，也不再是 OSI 定义的开源软件。RSALv2 允许内部使用、复制、修改与再分发，
但**不得把该软件作为托管服务提供给第三方，也不得移除许可与版权声明**。本仓库把 Redis 用作
内部任务队列（`deploy/compose.yaml:30-40`，私有网络、`compose.intranet.yaml` 不发布端口），
属于允许用途；但这依赖实际镜像版本，见第 7.5 节。

## 4. 数据与语料

### 4.1 manifest 的权威字段

`cases/evals/corpus/v0/manifest.yaml` 是评测语料的唯一入口（`cases/evals/corpus/v0/README.md:3-5`）。

| 字段 | 值 | 行号 |
| --- | --- | --- |
| `status` | `frozen` | `cases/evals/corpus/v0/manifest.yaml:3` |
| `distribution_scope` | **`internal_team_only`** | `cases/evals/corpus/v0/manifest.yaml:5` |
| `freeze_record` | `docs/stage-0-acceptance.md` | `cases/evals/corpus/v0/manifest.yaml:6` |
| `description` | 内部组间受控评测语料；**不公开发布、不上传原始语料或派生内容到外部服务** | `cases/evals/corpus/v0/manifest.yaml:7` |

逐源字段统计（12 个 Space / 90 个 source，本次统计自 `manifest.yaml`）：

| 维度 | 取值 | 数量 |
| --- | --- | --- |
| `license` | `cc0-1.0` | 59 |
| | `undetermined` | **26** |
| | `gplv3` | 5 |
| `sensitivity` | `public_demo` | 64 |
| | `private_local` | **26** |
| `redistribution` | `permitted_cc0` | 59 |
| | `review_required` | **26** |
| | `permitted_with_gplv3` | 5 |
| `allowed_uses` | `local_development`, `local_evaluation`, `repository_fixture` | 64 |
| | `local_development`, `local_evaluation`（**不含** `repository_fixture`） | **26** |

按 Space 的分布：

| Space | 来源数 | 许可与处置 |
| --- | --- | --- |
| `omnistudio` | 5 | `gplv3` / `public_demo` / `permitted_with_gplv3` |
| `cs229` | 9 | 8 × `undetermined` / `private_local` / `review_required`；1 × `cc0-1.0` |
| `math` | 9 | 8 × `cc0-1.0`；1 × `undetermined`（`math/rudin`） |
| `papers` | 17 | **全部** `undetermined` / `private_local` / `review_required` |
| `quicknotes`、`dsa`、`cpp`、`os`、`net`、`db`、`devtools`、`physics` | 4/8/7/7/8/6/3/7 | 全部 `cc0-1.0` / `public_demo` / `permitted_cc0` |

`AUDIT.md` 与 `docs/stage-0-acceptance.md` 对这批语料的官方定性：

- “The package contains 12 Spaces, 90 manifest sources, and 276 unique evaluation cases.”
  （`cases/evals/corpus/v0/AUDIT.md:13`）
- “This record makes no public redistribution claim.”（`cases/evals/corpus/v0/AUDIT.md:49`）
- “The validator … **does not turn an unresolved external license into a redistribution grant**.”
  （`cases/evals/corpus/v0/AUDIT.md:51-52`）
- “The internal-only freeze is an access-control decision, **not a license assertion**.”
  （`docs/stage-0-acceptance.md:47-49`）
- “it does not grant public redistribution rights and **does not authorize sending corpus content
  to an external provider**.”（`docs/stage-0-acceptance.md:10-13`）

### 4.2 Git 与再分发边界

| 事实 | 证据 |
| --- | --- |
| 原始语料目录、`cases.jsonl`、解析缓存、截图、embedding 与报告**不入 Git** | `.gitignore:2-48`、`docs/stage-0-acceptance.md:51-57` |
| Git 只包含 manifest、schema、控制文档、校验脚本、视觉复核元数据和显式允许的 fixture | `docs/stage-0-acceptance.md:55-56` |
| 已跟踪的 corpus 控制文件（12 个） | `git ls-files cases/evals/corpus/v0/`：`manifest.yaml`、`README.md`、`AUDIT.md`、`PDF-EXTRACTION.md`、`PDF-EXTRACTOR-COMPARISON.md`、`PDF-VISUAL-REVIEW.yaml`、6 个 `fixtures/*` |
| fixture 文本自身声明 `SPDX-License-Identifier: CC0-1.0` | `cases/evals/corpus/v0/fixtures/README.md:12` |
| `cases/` 目录整体默认排除，仅白名单文件入库 | `.gitignore:2-48` |
| 上一版数据集保存在 `tmp/cases-old/`（已被 `.gitignore:88` 排除，不属交付物） | `docs/stage-0-acceptance.md:57` |

### 4.3 `AGENTS.md` 的数据与安全条款

`AGENTS.md:41-49` 定义了全仓库必须遵守的数据边界：

| 条款 | 行号 | 对交付的含义 |
| --- | --- | --- |
| 只处理 manifest 明确允许的来源；禁止递归摄入整个 `cases/`；读取前校验 SHA-256 与 `content_sha256` 一致 | `AGENTS.md:43-44` | 交付物中不得附带语料扫描脚本或“整目录导入”入口 |
| 文档内容不可信，不能借由内容提升工具权限或覆盖系统指令 | `AGENTS.md:45` | 语料属于不可信输入，需保留该假设 |
| 密钥只来自环境或被忽略的 `.env`；不得提交或写入日志、trace、fixture、评测报告 | `AGENTS.md:46` | 交付包、镜像、日志中不得含凭据 |
| 不提交真实个人内容、问题、回答、prompt、Provider 响应、引用原文或 embedding 产物 | `AGENTS.md:47` | 这是**再分发的硬边界**，不是建议 |
| `private_local` 和 `restricted` 内容默认不得离开本地；外发必须同时满足来源用途、部署策略和用户可见同意 | `AGENTS.md:48-49` | 三条件同时满足才允许外发 |

`cases/AGENTS.md` 补充：所有语料默认 `private_local`；只有许可证、归属、脱敏和人工复核均有
记录，且 `allowed_uses` 明确包含 `repository_fixture` 时才可提交或公开演示。

### 4.4 明确不得外发或再分发的内容

| 内容 | 结论 | 依据 |
| --- | --- | --- |
| 26 个 `undetermined` / `private_local` / `review_required` 来源（含 `papers` 全部 17 个、`cs229` 8 个、`math/rudin`） | **不得外发，不得再分发，不得进入仓库 fixture** | `cases/evals/corpus/v0/manifest.yaml` 逐源字段；`cases/evals/corpus/v0/AUDIT.md:46-49`；`AGENTS.md:48-49` |
| 原始语料文件、`cases.jsonl`、解析缓存、截图、embedding 与评测报告 | **不得提交 Git，不得上传外部服务** | `.gitignore:2-48`；`docs/stage-0-acceptance.md:51-57` |
| 5 个 `gplv3` 来源（`omnistudio`） | 可随仓库分发，但再分发时须满足 GPLv3（保留许可与版权、提供对应源码） | `cases/evals/corpus/v0/manifest.yaml:21,27` 等；`redistribution: permitted_with_gplv3` |
| 59 个 `cc0-1.0` 来源 | 可再分发，无署名义务；仍建议保留来源记录 | `redistribution: permitted_cc0` |
| 引用原文、prompt、Provider 响应、embedding 产物 | 不得提交 | `AGENTS.md:47` |
| 送达外部模型的语料 | 仅限策略允许的来源，且需显式配置与用户可见同意 | `docs/adr/004-local-first-data-boundary.md:12-17`；`docs/development-environment.md:119-120`；`.env.example:76-79` |
| 官网首期的模型边界 | 不把 `private_local` 或 `restricted` 内容发送到外部模型 | 交付规范第 7.5 节 |

## 5. 字体与素材

### 5.1 字体

| 字体 | 来源 | 许可 | 是否入库 | 结论 |
| --- | --- | --- | --- | --- |
| KaTeX 数学字体（`KaTeX_*`） | `katex@0.16.21` npm 包 | **SIL OFL 1.1**（保留字体名 KaTeX_*） | 否（构建产物） | 已核实（仓库 + 官方） |
| `Inter` | 仅在 `apps/web/src/index.css:16` 作为 `font-family` 首选名 | 未打包，未使用 `@font-face` | 否 | 已核实（仓库）：**不存在字体再分发** |
| 等宽/系统字体 | `apps/web/src/index.css:17` 的 `'SFMono-Regular', Consolas, 'Liberation Mono', monospace` | 系统字体回退 | 否 | 已核实（仓库） |

**仓库内不存在任何被跟踪的字体文件（已核实）。** 全仓库字体文件只出现在两处，且都不属于源码：

1. `apps/web/dist/assets/` 下的 **59 个** KaTeX 字体文件（20 `.ttf` + 20 `.woff` + 19 `.woff2`，
   占约 2.0 MB 构建产物的大部分），由 `pnpm build` 从 `katex/dist/fonts/` 复制而来；
   `apps/web/dist` 被 `apps/web/.gitignore:11` 排除，**未入库**，但会通过
   `deploy/Dockerfile.web:19` 的 `COPY --from=builder /app/dist` 进入 Web 镜像。
2. `tmp/cases-old/Omni-Studio/resources/fonts/` 下的 20 个 `.woff2`——位于被 `.gitignore:88`
   排除的 `tmp/`，**不属交付物**。

**KaTeX 字体的许可与代码不同（关键结论）。** `katex` npm 包的 `LICENSE` 是 MIT，覆盖的是
JavaScript 代码；字体文件的嵌入元数据写明的是 OFL：

```text
Copyright (c) 2009-2010, Design Science, Inc. (<www.mathjax.org>)
Copyright (c) 2014-2018 Khan Academy (<www.khanacademy.org>),
with Reserved Font Name KaTeX_Main.

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license available with a FAQ at: http://scripts.sil.org/OFL
```

上述文本由本次调查直接从 `apps/web/dist/assets/KaTeX_Main-Regular-*.ttf`、
`KaTeX_AMS-Regular-*.ttf`、`KaTeX_SansSerif-Regular-*.ttf` 的 `name` 表中读出，
并与 KaTeX 官方 issue #339 的维护者答复一致（链接见第 8 节）。

**OFL 1.1 的实际义务：**

- 再分发字体文件时必须随附 OFL 1.1 许可文本（可放在镜像内的第三方声明文件中）；
- 不得单独售卖字体；
- 修改字体后不得继续使用保留字体名（`KaTeX_*`）；
- 纯聚合（本项目只是在网页里引用未修改的字体）不要求对本项目代码开源。

### 5.2 `skills/` 静态资源

| 结论 | 证据 |
| --- | --- |
| `skills/` 下 **没有任何** 图片、字体、图标、PDF 或示例文档 | `find skills -type f` 仅返回 `.md` / `.yaml` / `.json` / `.jsonl` |
| 7 个 Skill 包（`_template`、`assistant_agent`、`course_project_workflow`、`exam_preparation_workflow`、`knowledge_agent`、`research_reading_workflow`、`skill_creator`）只含 manifest、prompt、schema、workflow 与 `cases.jsonl` | 同上 |
| `skill.yaml` 中**没有** `license` 字段 | `skills/knowledge_agent/skill.yaml`（完整读取） |

**结论：`skills/` 不引入第三方素材许可问题（已核实）。** prompt 与 schema 文本的著作权归属
属项目自身代码范畴，见第 7.1 节。

### 5.3 `apps/web/public/` 与 `apps/web/src/assets/`

| 文件 | 是否被引用 | 来源与许可 | 结论 |
| --- | --- | --- | --- |
| `apps/web/public/favicon-branded.svg`（530 B） | **是**，`apps/web/index.html:5` | 项目自制图标（书本造型，自定义配色），2026-08-21 加入 | 已核实（仓库）：自有素材，无第三方许可 |
| `apps/web/public/favicon.svg`（9522 B） | **否** | 初始提交 `e69ce8d` 带入，紫色渐变标识，仓库内无出处记录 | **来源未核实**，见第 7.6 节 |
| `apps/web/public/icons.svg`（5031 B） | **否** | 初始提交 `e69ce8d` 带入，含 Bluesky / X / Discord / GitHub 品牌图标（`symbol id="bluesky-icon"`、`"discord-icon"`、`"github-icon"`、`"x-icon"`、`"documentation-icon"`、`"social-icon"`） | **第三方品牌商标，来源未核实**，见第 7.6 节 |
| `apps/web/src/assets/hero.png`（13 KB，343×361） | **否** | 初始提交 `e69ce8d` 带入，PNG 无 `tEXt`/`iTXt` 元数据，仓库内无出处记录 | **来源未核实**，见第 7.6 节 |
| `apps/web/src/assets/react.svg` | **否** | Vite 脚手架常见素材，`class="iconify iconify--logos"`，来自 Iconify `logos` 图标集 | 来源未核实；品牌商标 |
| `apps/web/src/assets/vite.svg` | **否** | Vite 官方 logo（`<title id="vite-logo-title">Vite</title>`） | Vite 项目 MIT，但 logo 使用受商标政策约束 |

**用户生成内容与运营图片：无。** 仓库内没有上传样例、演示文档、截图或第三方配图
（`find apps packages` 仅命中 `apps/web/src/assets/hero.png`）。

**图标库：已核实。** `lucide-react@1.25.0` 采用 ISC 许可（包内 `LICENSE` 文件），另有部分图标派生自
Feather 项目（同一 `LICENSE` 文件中已列出）。ISC 只要求保留版权与许可声明。

## 6. 再分发与署名义务清单

| # | 对象 | 义务 | 触发条件 | 当前状态 |
| --- | --- | --- | --- | --- |
| 1 | **项目自身代码** | 未授予任何许可 | 任何复制、修改、再分发 | **无 `LICENSE` 文件**，见第 7.1 节 |
| 2 | `Qwen/Qwen3-Embedding-0.6B`（Apache-2.0） | 附许可副本；保留版权/NOTICE；标注修改；不得暗示背书 | 再分发权重 | 仓库不含权重；如打包离线权重需补 |
| 3 | `BAAI/bge-reranker-v2-m3`（Apache-2.0） | 同上 | 再分发权重 | 同上 |
| 4 | Text Embeddings Inference（Apache-2.0） | 附许可副本；保留声明 | 再分发其镜像或二进制 | 仅按 digest 引用官方镜像 |
| 5 | **PyMuPDF（AGPL-3.0）** | AGPL 全文义务，含第 13 条网络服务源码提供；或购买商业许可 | 分发/以网络服务提供 | **未决**，见第 3.1 与第 7.2 节 |
| 6 | `dramatiq`（LGPL-3.0+） | 保留声明；允许替换库 | 分发 | 未修改使用，已满足 |
| 7 | `certifi`、`pathspec`（MPL-2.0） | 保留声明；修改的文件需公开 | 分发/修改 | 未修改使用，已满足 |
| 8 | `katex` 代码（MIT） | 保留版权与许可声明 | 分发构建产物 | 构建产物含 katex 代码，需在声明中保留 |
| 9 | **KaTeX 字体（OFL-1.1）** | **随附 OFL 1.1 全文**；不改保留字体名；不单独售卖字体 | 再分发 Web 镜像 / `dist/` | 字体随 Web 镜像分发；**当前无第三方声明文件**，见第 7.7 节 |
| 10 | `lucide-react`（ISC） | 保留版权与许可声明 | 分发构建产物 | 同上 |
| 11 | React / react-markdown / rehype-katex / remark-* / @tanstack/react-query（MIT） | 保留版权与许可声明 | 分发构建产物 | 同上 |
| 12 | `redis:7-alpine` 镜像 | 若为 ≥7.4：不得作为竞争性托管服务提供，不得移除许可声明 | 对外提供 Redis 服务 | 内部使用；版本未核实，见第 7.5 节 |
| 13 | `favicon.svg`、`icons.svg`、`hero.png`、`react.svg`、`vite.svg` | 来源与许可未明，**不得在未澄清前随交付物对外分发** | 分发 Web 仓库或镜像 | 均未被引用；建议移除，见第 7.6 节 |
| 14 | 语料：26 个 `undetermined` / `private_local` | **禁止再分发** | 任何形式外发 | 已排除在 Git 之外 |
| 15 | 语料：5 个 `gplv3`（`omnistudio`） | 再分发需 GPLv3 合规（保留许可、提供对应源码） | 随仓库/镜像分发 | 已在 Git 中，需保留 GPLv3 声明 |
| 16 | 语料：59 个 `cc0-1.0` | 无署名义务 | 再分发 | 建议保留来源记录 |
| 17 | `cases/evals/corpus/v0/fixtures/*` | 无（CC0-1.0） | 再分发 | 已声明 SPDX 标识 |

## 7. 待维护者确认项

### 7.1 项目自身没有 LICENSE 文件（阻塞性）

**已核实：本仓库不存在 `LICENSE`、`COPYING` 或 `NOTICE` 文件。** 全仓库搜索只在
`tmp/cases-old/`（非交付物）下命中两个语料自带的许可文件。`README.md`、
`docs/cc2026-delivery.md`、`docs/cc2026-yizhi-handoff.md` 中也没有任何许可声明。

后果：

- 在没有许可的情况下，默认著作权规则适用——他人**没有**复制、修改或再分发的权利，
  即使仓库公开可见；
- `uv.lock` 中 7 个 workspace 包（`agent-runtime`、`api`、`application`、`domain`、
  `infrastructure`、`model-gateway`、`worker`）已安装但 `METADATA` 中没有任何许可字段，
  根包 `agent-knowledge-repo` 则没有 dist-info；`uv.lock` 本身也不记录许可证。即仓库内
  **不存在任何指向本项目代码的许可声明**，这与其未提供 `LICENSE` 一致；
- 第 3.1 节的 PyMuPDF AGPL“路径 1”依赖项目自身有明确许可，当前无法成立。

**本项目代码的开源许可必须由维护者决定，本文件不代为选择、不编造。** 在维护者决定之前，
任何关于“本项目是开源项目”的对外表述都不准确。

### 7.2 PyMuPDF 的 AGPL 合规路径未决

见第 3.1 节。需要维护者/法务在“按 AGPL 提供源码”“购买 Artifex 商业许可”“替换解析器”
之间做出选择，并同步更新 `docs/adr/`（AGENTS.md 要求 Provider 或外部数据边界变化时新增或
更新 ADR）。

### 7.3 `BAAI/bge-reranker-v2-m3` 未固定 revision

`deploy/compose.yaml:315-317` 与 `deploy/compose.cpu.yaml:26-27` 只传 `--model-id`，没有
`--revision`；而 embedding 服务在 `deploy/compose.yaml:281-282` 固定了 revision。
`docs/model-setup.md:9-12` 的模型清单也没有给 reranker 列 revision。
这意味着 reranker 权重会随上游更新而漂移，与 `docs/development-environment.md:334-335`
“使用固定镜像 digest 和固定 embedding model revision”的要求不一致。**建议补齐 reranker
revision 并记录到本文第 2.1 节。**

### 7.4 模型卡信息取自镜像站

本环境无法访问 `huggingface.co`（连接超时），Qwen3-Embedding-0.6B 与 bge-reranker-v2-m3 的
Apache-2.0 许可结论取自 `hf-mirror.com` 的模型卡页面。同时在 `docs/model-setup.md:69-71`
被推荐的镜像站还包括南京大学镜像。**建议由可访问官方站点的维护者复核一次模型卡许可字段**，
并确认 revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` 在上游仍然存在。

### 7.5 Redis 镜像实际版本未核实

`deploy/compose.yaml:31` 使用 `redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99`。
`7-alpine` 当前指向 7.4.x，即 RSALv2 / SSPLv1；但本环境无法解析该 digest 对应的具体版本。
**需要在可联网环境执行 `docker buildx imagetools inspect` 或等价命令确认版本与许可**，
并据此更新第 3.1、3.4 节。

### 7.6 未被引用且来源未明的素材

| 文件 | 建议 |
| --- | --- |
| `apps/web/src/assets/hero.png` | 出处无法在仓库内确认；无人引用。移除，或补充来源与许可记录 |
| `apps/web/src/assets/react.svg` | Iconify `logos` 图标集素材，涉及 React 商标；无人引用。建议移除 |
| `apps/web/src/assets/vite.svg` | Vite 官方 logo，涉及商标政策；无人引用。建议移除 |
| `apps/web/public/favicon.svg` | 来源未明；`apps/web/index.html:5` 已改用 `favicon-branded.svg`。建议移除 |
| `apps/web/public/icons.svg` | 含 Bluesky / X / Discord / GitHub 商标；无人引用。若将来启用，需遵守各平台品牌规范 |

### 7.7 第三方声明文件缺失

Web 镜像（`deploy/Dockerfile.web:19`）会分发 KaTeX 字体（OFL-1.1）、KaTeX 代码（MIT）、
`lucide-react`（ISC）、React 等 MIT 依赖。OFL-1.1 明确要求随字体提供许可文本。
**当前仓库没有 `THIRD_PARTY_NOTICES` 或等价文件。** 建议新增一份第三方声明并打进 Web 镜像
（`deploy/Dockerfile.web` 增加一次 `COPY`），内容至少覆盖第 6 节第 8-11 项。

### 7.8 与 `docs/operations.md` 的分工

交付规范第 8 节的目录结构同时列出 `docs/operations.md` 与 `docs/data-and-licenses.md`。
本文件只覆盖后者；运维、持久卷、备份、升级、回滚与临时文件清理由 `docs/operations.md` 覆盖，
本文件不对其内容作完整性判断。两份文件的行号引用口径一致（`路径:行号`），如任一文件被修改，
应同步复核另一份中的交叉引用。

## 8. 参考链接

### 8.1 模型与推理

- [Qwen/Qwen3-Embedding-0.6B 模型卡（许可：Apache-2.0，经 hf-mirror.com 镜像读取）](https://hf-mirror.com/Qwen/Qwen3-Embedding-0.6B)
- [BAAI/bge-reranker-v2-m3 模型卡（许可：Apache-2.0，经 hf-mirror.com 镜像读取）](https://hf-mirror.com/BAAI/bge-reranker-v2-m3)
- [Qwen3-Embedding 官方仓库（模型清单与引用信息）](https://raw.githubusercontent.com/QwenLM/Qwen3-Embedding/main/README.md)
- [FlagEmbedding 官方仓库（BGE 系列模型清单，MIT）](https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/master/README.md)
- [Text Embeddings Inference 许可（Apache-2.0）](https://raw.githubusercontent.com/huggingface/text-embeddings-inference/main/LICENSE)

### 8.2 软件依赖

- [PyMuPDF 官方 README Licensing 段落（AGPL v3 或 Artifex 商业许可）](https://raw.githubusercontent.com/pymupdf/PyMuPDF/main/README.md)
- [GNU Affero General Public License v3.0 全文](https://www.gnu.org/licenses/agpl-3.0.html)
- [Artifex 商业许可说明](https://artifex.com/licensing)
- [pypdf 许可（BSD-3-Clause）](https://raw.githubusercontent.com/py-pdf/pypdf/main/LICENSE)
- [KaTeX 许可（MIT，覆盖代码）](https://raw.githubusercontent.com/KaTeX/KaTeX/main/LICENSE)
- [KaTeX issue #339 “Clarify font license” —— 维护者确认字体为 SIL OFL 1.1](https://github.com/KaTeX/KaTeX/issues/339)
- [SIL Open Font License 1.1](https://openfontlicense.org/open-font-license-official-text/)
- [pgvector 许可（PostgreSQL License）](https://github.com/pgvector/pgvector/blob/master/LICENSE)
- [Redis 双许可公告：7.4 起改为 RSALv2 / SSPLv1](https://redis.io/blog/redis-adopts-dual-source-available-licensing/)
- [Redis Source Available License v2（RSALv2）](https://redis.io/legal/rsalv2-agreement/)
- [Server Side Public License v1（SSPLv1）](https://redis.io/legal/server-side-public-license-sspl/)
- [GNU Lesser General Public License v3.0（dramatiq）](https://www.gnu.org/licenses/lgpl-3.0.html)
- [Mozilla Public License 2.0（certifi / pathspec）](https://www.mozilla.org/en-US/MPL/2.0/)
- [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)

### 8.3 仓库内证据索引

| 主题 | 文件 |
| --- | --- |
| 模型清单与离线部署 | `docs/model-setup.md` |
| 模型与 Provider 能力划分 | `docs/development-environment.md` |
| 服务定义与镜像 digest | `deploy/compose.yaml`、`deploy/compose.cpu.yaml`、`deploy/compose.intranet.yaml` |
| 环境变量默认值 | `.env.example` |
| Python 依赖与锁定 | `pyproject.toml`、`uv.lock` |
| JS 依赖 | `apps/web/package.json`、`apps/web/pnpm-lock.yaml` |
| 语料权威清单 | `cases/evals/corpus/v0/manifest.yaml`、`cases/evals/corpus/v0/AUDIT.md` |
| 语料冻结决定 | `docs/stage-0-acceptance.md` |
| 本地优先数据边界 | `docs/adr/004-local-first-data-boundary.md` |
| 全仓库数据与安全条款 | `AGENTS.md`、`cases/AGENTS.md` |
| 官网接入边界 | `docs/cc2026-delivery.md`、`docs/cc2026-yizhi-handoff.md` |
