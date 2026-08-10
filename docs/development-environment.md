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

Checked on 2026-08-04:

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
| Model endpoint | Pinned TEI GPU embedding and reranker profiles verified on 2026-08-04 | Fake remains the default; real models are opt-in |

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
docker compose -f deploy/compose.yaml --env-file .env up --build --detach --wait
```

The single Compose command builds and starts PostgreSQL/pgvector, Redis, the migration gate, API,
Worker and Web. Open `http://127.0.0.1:5173`; nginx forwards same-origin `/api` requests to the API.

When the Web QA or `knowledge_agent` flow needs the complete local GPU retrieval path, start both
model profiles as well. A base-stack start without the embedding profile leaves `http://tei:80`
unavailable; without the reranker profile, the default `dense_rerank` path can only use the explicit
fake fallback:

```powershell
docker compose -f deploy/compose.yaml --env-file .env `
  --profile embedding --profile reranker up --build --detach --wait
```

If the configured host ports are already occupied, set `WEB_PORT`, `API_PORT`, `POSTGRES_PORT`, and
`REDIS_PORT` in the ignored `.env` and pass the same file to every Compose command. Do not use
`down --volumes` to resolve a port conflict; it permanently deletes the local database and Redis
volumes.

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

Stage 1 defaults to `MODEL_PROVIDER=fake`. A Web QA or `knowledge_agent` deployment with an
OpenAI-compatible Chat endpoint must use `MODEL_PROVIDER=openai-compatible`; route the local TEI
embedding capability separately with `EMBEDDING_PROVIDER=text-embeddings-inference`. A pure
retrieval-only process may use `MODEL_PROVIDER=text-embeddings-inference`, but that provider cannot
serve `fast_chat`. The optional Compose `embedding` profile provides a pinned Text Embeddings
Inference GPU image and `Qwen/Qwen3-Embedding-0.6B` revision. Configure
`EMBEDDING_ENDPOINT=http://tei:80`,
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

## Assistant Routing Development Metrics

Validate the pinned Assistant routing dataset without executing a model or reading the controlled
corpus:

```powershell
uv run --frozen python scripts/evaluate_assistant_routing.py --validate-only
```

The command accepts only `synthetic_only` development cases with hash-checked schema and case files.
To aggregate predictions, provide body-free metadata containing actions, safe Skill names, counts,
usage, latency, and termination reasons. Its JSON report is permanently marked
`quality_status=provisional` and `dataset_split=development`; it is not a retrieval, answer, or Skill
holdout and cannot be enabled as one. Do not write user text, prompts, document text, Provider
responses, or internal resource IDs to prediction files or operational metric logs.

### Web QA provider split

`text-embeddings-inference` is an embedding/reranking adapter, not a Chat adapter. Do not set it as
the sole `MODEL_PROVIDER` when using `knowledge_qa` or `knowledge_agent`; that leaves `fast_chat`
unavailable. The supported local-development split is:

```dotenv
MODEL_PROVIDER=openai-compatible
MODEL_ALLOW_EXTERNAL=true
FAST_CHAT_ENDPOINT=https://api.example.com/v1
FAST_CHAT_API_KEY=<ignored-local-secret>
FAST_CHAT_MODEL=<chat-model>

EMBEDDING_PROVIDER=text-embeddings-inference
EMBEDDING_ENDPOINT=http://tei:80
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_PROTOCOL=tei

RERANKER_PROVIDER=inherit
RERANKER_ENDPOINT=http://tei-reranker:80
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

Replace the Chat endpoint, model, and key with values approved for the deployment. Enabling
`MODEL_ALLOW_EXTERNAL=true` means retrieved document snippets and questions may be sent to that
external provider; private or restricted sources require an explicit policy decision. After changing
these variables, recreate both `api` and `worker` so both processes load the same configuration:

```powershell
docker compose -f deploy/compose.yaml --env-file .env `
  --profile embedding --profile reranker up --detach --force-recreate api worker
```

Set `RERANKER_PROVIDER=fake` only when deliberately validating the workflow without the optional
GPU reranker. If a prewarmed TEI cache already exists, set `TEI_VOLUME_NAME` in `.env`; otherwise
Compose creates and populates `deploy_teidata`. Verify the resulting capability split before opening
Web:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health/ready | ConvertTo-Json -Depth 8
```

The response should report `fast_chat`, `embedding_zh`, and `reranker_multilingual`. It validates
configuration routing only; make one `dense_rerank` search request before treating the GPU path as
usable.

### Reusing already-running GPU TEI services

On Docker Desktop, do not start a duplicate model pair when compatible GPU TEI containers are
already published on the host. Keep API and Worker in the same capability configuration and point
them at the host-published ports instead:

```dotenv
EMBEDDING_PROVIDER=text-embeddings-inference
EMBEDDING_ENDPOINT=http://host.docker.internal:<embedding-port>
EMBEDDING_PROTOCOL=tei
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_MODEL_REVISION=97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3
EMBEDDING_QUERY_INSTRUCTION_VERSION=qwen3-knowledge-qa-v1
EMBEDDING_DOCUMENT_INSTRUCTION_VERSION=qwen3-knowledge-qa-v1
EMBEDDING_NORMALIZATION=l2
RERANKER_PROVIDER=inherit
RERANKER_ENDPOINT=http://host.docker.internal:<reranker-port>
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

## Assistant Web release controls

The Web build defaults to API v2. Compose passes these Vite variables to `Dockerfile.web` at build
time:

```dotenv
VITE_ASSISTANT_DEFAULT_API_MODE=v2
VITE_ASSISTANT_V1_COMPATIBILITY_UNTIL=2026-09-30T23:59:59Z
```

During the compatibility window, set `VITE_ASSISTANT_DEFAULT_API_MODE=v1` and rebuild only the Web
image to restore the legacy QA entry. This is a reversible entry-point change: it does not remove
v2 data, historical Runs, or Skill packages. The selector is hidden after the deadline and malformed
or expired deadlines fail closed to v2. Deploy fake/local Chat first; an external Chat endpoint still
requires the existing `MODEL_ALLOW_EXTERNAL` and source/deployment/consent policy checks.

## Agent Loop v5 Release Control

The generic Agent Loop is the default synthetic/fake or reviewed local provisional path. API and
Worker default to the following aligned values:

```dotenv
KNOWLEDGE_AGENT_SKILL_VERSION=0.7.0
AGENT_LOOP_V5_ENABLED=true
```

The legacy-named flag remains a fail-closed rollback: `AGENT_LOOP_V5_ENABLED=false` activates
`knowledge_agent 0.3.0` for new Runs. Recreate API/Worker after changing it. This preserves all
persisted Run/Skill pins and keeps v1/v2 API and SSE projections readable. `start-local.ps1` uses
v6 by default; pass `-LegacyKnowledgeAgent` for a local v3 rollback. Do not use an external Provider
without the existing sensitivity, deployment-policy, and visible-consent checks.

Validate the hash-pinned synthetic development fixture without invoking a model or reading the
controlled corpus:

```powershell
uv run --frozen python scripts/evaluate_agent_loop.py --validate-only
```

Predictions, when supplied to the evaluator, may contain only safe action/count/coverage/latency/
token/recovery metadata. Reports are always `development` and `provisional`; this is not a formal
retrieval, answer, or Skill holdout.

`host.docker.internal` is an explicitly allowed local endpoint name. This pattern is specific to
Docker Desktop; use a reviewed reachable host address on other platforms. Recreate `api` and
`worker` after changing either endpoint, then verify the Web proxy and an actual `dense_rerank`
request. Do not mix a new embedding identity with vectors already published under another identity.

## Stage 3 Retrieval Validation

The Search API and QA workflow default to `dense_rerank`: dense-exact candidates are sent directly
to the configured reranker. `hybrid_rerank` is retained only for explicit compatibility requests.
The corrected 2026-08-04 GPU development results remain provisional under ADR-010; do not enable or
run the existing formal holdout.

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
