# Development Environment Baseline

This file is the stage 1 source of truth for local tool and runtime support. Project files created in
step 1 will pin installable versions and lock transitive dependencies.

## Supported Baseline

| Capability | Baseline | Version source after step 1 |
| --- | --- | --- |
| Python | CPython 3.12 | `.python-version` and `pyproject.toml` |
| Python package manager | uv 0.11.x | CI setup and contributor documentation |
| Node.js | Node 24 LTS | `apps/web/package.json` engines field and container image |
| Frontend package manager | pnpm 10.20.0 through Corepack | `apps/web/package.json` `packageManager` field |
| Containers | Docker Engine 29+ and Compose 5+ | `deploy/compose.yaml` and pinned images |
| API and Worker base image | `python:3.12-slim-bookworm` | Dockerfile; immutable digest locked in step 7 |
| Web build base image | `node:24.11.0-bookworm-slim` | Dockerfile; immutable digest locked in step 7 |
| Database | PostgreSQL 16+ with pgvector | `deploy/compose.yaml` |
| Queue broker | Redis with Dramatiq | ADR-009 and `deploy/compose.yaml` |

The base image tags above are the selected stage 1 baseline. Their immutable digests, plus the
PostgreSQL/pgvector and Redis service image versions, are locked in the Dockerfiles and Compose file.
The Dockerfiles use the AWS public read-only cache for Docker Official Images because Docker Hub's
token endpoint is unavailable on the current network; the cached manifests were verified to have
the exact same digests as the Docker Hub API responses.

## Current Machine Check

Checked on 2026-07-18:

| Item | Result | Action |
| --- | --- | --- |
| Python 3.12 | 3.12.13 managed environment available | Use `uv sync --frozen` |
| uv | 0.11.1 installed | No installation needed |
| Node.js | 24.11.0 installed | No installation needed |
| pnpm | 10.20.0 installed | Invoke through `corepack pnpm@10.20.0`; do not call `pnpm.ps1` |
| Docker CLI / Compose | 29.4.0 / 5.1.2 installed | Start Docker Desktop or another Docker Engine |
| Docker daemon | Available during Step 7/8 Compose verification | Verify `docker version` before startup |
| PostgreSQL / pgvector | Compose service verified | No host installation needed |
| Redis | Compose service with AOF verified | No host installation needed |
| Model endpoint | Pinned TEI GPU profile verified on 2026-08-03 | Fake remains the default; real model is opt-in |

## Windows Setup

Install the managed Python runtime:

```powershell
uv python install 3.12
uv python pin 3.12
uv python find 3.12
```

The first command downloads Python and therefore needs network access. `uv python pin` is run from the
repository root and creates `.python-version`; this repository file is added in step 1.

Enable the pinned pnpm version declared by the repository:

```powershell
corepack enable
corepack install
corepack pnpm@10.20.0 --version
```

PowerShell on this machine blocks the globally installed `pnpm.ps1`. The canonical commands use
`corepack pnpm@10.20.0`, which both pins the selected version and avoids relying on that script. Do not
weaken the machine-wide execution policy for this project.

Start Docker Desktop, then verify both client and server:

```powershell
docker version
docker compose version
```

`docker version` must include both `Client` and `Server`. PostgreSQL/pgvector and Redis should be run
through the repository Compose file; separate host installations are unnecessary.

## Local Compose Stack

Create an ignored local environment file and replace the two placeholder secrets before startup:

```powershell
Copy-Item .env.example .env
docker compose -f deploy/compose.yaml up --build --detach --wait
```

The single Compose command builds and starts PostgreSQL/pgvector, Redis, the migration gate, API,
Worker and Web. Open `http://127.0.0.1:5173`; nginx forwards same-origin `/api` requests to the API.

Stop containers while keeping PostgreSQL and Redis data:

```powershell
docker compose -f deploy/compose.yaml down
```

Use `down --volumes` only when the local project data should be permanently removed.

## Provider Configuration Boundary

The default model implementation is a deterministic fake and needs no credentials. The first real
adapter uses an OpenAI-compatible HTTP endpoint and is disabled until explicitly configured. Non-secret
variable names are listed in `.env.example`; concrete endpoint URLs, model names, and API keys
belong only in an ignored local `.env` or the process environment.

Private or restricted corpus content must not be sent to an external endpoint. Enabling an external
endpoint requires an explicit deployment setting and a policy check in addition to credentials.

Stage 1 defaults to `MODEL_PROVIDER=fake`. To use a local OpenAI-compatible endpoint, set
`MODEL_PROVIDER=openai-compatible`, `MODEL_ENDPOINT`, `FAST_CHAT_MODEL`, and `EMBEDDING_MODEL`.
Embedding-only operation is supported with `EMBEDDING_ENDPOINT` and `EMBEDDING_MODEL`; the
chat model may be omitted. The optional Compose `embedding` profile provides a pinned
Text Embeddings Inference GPU image and `Qwen/Qwen3-Embedding-0.6B` revision. Configure
`MODEL_PROVIDER=text-embeddings-inference`, `EMBEDDING_ENDPOINT=http://tei:80`,
`EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B`,
`EMBEDDING_MODEL_REVISION=97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`,
`EMBEDDING_QUERY_INSTRUCTION_VERSION=qwen3-knowledge-qa-v1`,
`EMBEDDING_DOCUMENT_INSTRUCTION_VERSION=qwen3-knowledge-qa-v1`, and
`EMBEDDING_NORMALIZATION=l2`, `EMBEDDING_PRECISION=float32`, and
`EMBEDDING_BATCH_SIZE=8` when using that profile. TEI uses `max-batch-tokens=512`,
`max-client-batch-size=8`, and `max-batch-requests=1`. The exact `tei` service name is in the
local endpoint allowlist; other DNS names still require `MODEL_ALLOW_EXTERNAL=true`. A host-run
API/Worker can use the published service at `http://localhost:8080` instead.
`MODEL_API_KEY` is optional for local endpoints and is loaded as a secret value. Public endpoints are
rejected unless `MODEL_ALLOW_EXTERNAL=true` is also set. URL-embedded credentials and endpoint query
parameters are always rejected. Default tests use the fake or a synthetic local HTTP stub and never
call a real or paid model.

## Stage 3 Retrieval Validation

阶段 3 的检索默认使用 PostgreSQL FTS 与 pgvector exact 路径；IVFFlat 只作为对比实验，未通过
正式质量门槛前不会替换 exact 默认值。`MODEL_PROVIDER=fake` 可运行确定性工程测试；需要本地
Embedding 时启用 Compose 的 `embedding` profile，需要本地精排时额外启用 `reranker` profile。
Embedding 使用 `qwen3-knowledge-qa-v1` 的 query/document 指令版本；模型服务使用固定镜像
digest 和固定 embedding model revision，不能改为 `latest` 或通过公网外发。
启用 reranker 时设置 `RERANKER_ENDPOINT=http://tei-reranker:80` 和
`RERANKER_MODEL=BAAI/bge-reranker-v2-m3`。模型切换会产生新的 `embedding_version`，必须通过
受控重建发布新 DocumentVersion；不能让新查询 identity 检索旧向量。

检索 API 和离线评测的完整验收命令、环境、报告摘要和当前门禁见
[`docs/stage-3-acceptance.md`](stage-3-acceptance.md)。评测报告只写入被忽略的 `tmp/`，不得
提交查询正文、文档正文、向量或 Provider 载荷。
