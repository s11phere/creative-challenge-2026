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
PostgreSQL/pgvector and Redis service image versions, are locked after pull and compatibility
verification when the Dockerfiles and Compose file are added in step 7.

## Current Machine Check

Checked on 2026-07-17:

| Item | Result | Action |
| --- | --- | --- |
| Python 3.12 | Missing; Python 3.13 and 3.14 are installed | Install with uv before backend commands |
| uv | 0.11.1 installed | No installation needed |
| Node.js | 24.11.0 installed | No installation needed |
| pnpm | 10.20.0 installed | Invoke through `corepack pnpm`; do not call `pnpm.ps1` |
| Docker CLI / Compose | 29.4.0 / 5.1.2 installed | Start Docker Desktop or another Docker Engine |
| Docker daemon | Not running or not reachable | Start the engine and verify `docker version` shows a Server section |
| PostgreSQL / pgvector | No local service is required yet | Step 7 supplies the Compose service |
| Redis | No local service is required yet | Step 7 supplies the Compose service |
| Model endpoint | Not configured | Optional; fake is the default and external sending stays disabled |

## Windows Setup

Install the managed Python runtime:

```powershell
uv python install 3.12
uv python pin 3.12
uv python find 3.12
```

The first command downloads Python and therefore needs network access. `uv python pin` is run from the
repository root and creates `.python-version`; this repository file is added in step 1.

Enable the pinned pnpm version after step 1 has added the `packageManager` field:

```powershell
corepack enable
corepack install
corepack pnpm --version
```

PowerShell on this machine blocks the globally installed `pnpm.ps1`. The canonical commands use
`corepack pnpm`, which avoids relying on that script. Do not weaken the machine-wide execution policy
for this project.

Start Docker Desktop, then verify both client and server:

```powershell
docker version
docker compose version
```

`docker version` must include both `Client` and `Server`. PostgreSQL/pgvector and Redis should be run
through the repository Compose file once step 7 creates it; separate host installations are unnecessary.

## Provider Configuration Boundary

The default model implementation is a deterministic fake and needs no credentials. The first real
adapter uses an OpenAI-compatible HTTP endpoint and is disabled until explicitly configured. Step 1
will add non-secret variable names to `.env.example`; concrete endpoint URLs, model names, and API keys
belong only in an ignored local `.env` or the process environment.

Private or restricted corpus content must not be sent to an external endpoint. Enabling an external
endpoint requires an explicit deployment setting and a policy check in addition to credentials.
