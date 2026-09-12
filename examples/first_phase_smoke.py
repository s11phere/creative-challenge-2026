#!/usr/bin/env python3
"""First-release smoke test for the website (PUBLIC_MODE) deployment.

Runs against a live API with only the Python standard library so it can be
executed *inside* the API container, where the Compose override publishes no
host ports:

    docker compose -f deploy/compose.yaml -f deploy/compose.intranet.yaml \\
      exec -T api python - < examples/first_phase_smoke.py

Environment:

    SMOKE_BASE_URL   default http://127.0.0.1:8000
    SMOKE_TOKEN      the deployment's INTERNAL_SERVICE_TOKEN (required when the
                     target runs with SERVICE_AUTH_REQUIRED=true)

It exercises the first-release contract end to end: gateway authentication,
tenant isolation, upload -> ingestion -> grounded question -> citation, the
capability boundary of PUBLIC_MODE, input limits, and Space deletion cleanup.
Exit code 0 means every check passed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("SMOKE_TOKEN", "")
TIMEOUT_SECONDS = float(os.environ.get("SMOKE_POLL_TIMEOUT", "180"))

TERMINAL_TASK_STATES = {"succeeded", "failed", "partial_failed", "dead_letter", "cancelled"}
TERMINAL_RUN_STATES = {"completed", "succeeded", "failed", "cancelled", "refused"}

_failures: list[str] = []


def call(
    method: str,
    path: str,
    *,
    user: str | None = None,
    body: dict | None = None,
    upload: tuple[str, bytes] | None = None,
    token: str | None = None,
) -> tuple[int, object]:
    """Issue one gateway request and return ``(status, decoded_body)``."""

    headers = {"X-Request-ID": f"smoke-{uuid.uuid4().hex[:12]}"}
    if token is None:
        token = TOKEN
    if token:
        headers["X-Internal-Service-Token"] = token
    if user:
        headers["X-App-Scoped-User-Id"] = user

    data: bytes | None = None
    if upload is not None:
        filename, content = upload
        boundary = f"----smoke{uuid.uuid4().hex}"
        data = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: text/markdown\r\n\r\n",
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload.decode(errors="replace")


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}", flush=True)
    if not ok:
        _failures.append(name)


def poll(path: str, user: str, terminal: set[str], field: str = "status") -> dict:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    latest: dict = {}
    while time.monotonic() < deadline:
        status, body = call("GET", path, user=user)
        if status == 200 and isinstance(body, dict):
            latest = body
            if latest.get(field) in terminal:
                return latest
        time.sleep(1.0)
    return latest


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    tenant_a = f"member:smoke-a-{suffix}"
    tenant_b = f"member:smoke-b-{suffix}"

    # --- gateway authentication -------------------------------------------
    status, body = call("GET", "/api/v1/health/live", token="")
    check("health/live stays anonymous", status == 200 and body == {"status": "alive"}, status)

    status, _ = call("GET", "/api/v1/config/limits", token="")
    check("missing gateway credentials return 401", status == 401, status)

    status, _ = call("GET", "/api/v1/config/limits", user=tenant_a, token="not-the-token")
    check("wrong internal service token returns 401", status == 401, status)

    status, _ = call("GET", "/api/v1/config/limits", user=tenant_a)
    check("gateway credentials are accepted", status == 200, status)

    # --- tenant isolation --------------------------------------------------
    status, space = call("POST", "/api/v1/spaces", user=tenant_a, body={"name": "smoke-a"})
    check("tenant A creates a Space", status == 201, status)
    space_id = space["id"]  # type: ignore[index]

    status, listed = call("GET", "/api/v1/spaces", user=tenant_b)
    check("tenant B does not see tenant A's Space", status == 200 and listed == [], listed)

    status, _ = call("GET", f"/api/v1/spaces/{space_id}/sources", user=tenant_b)
    check("tenant B cannot read tenant A's Space", status == 404, status)

    # --- upload -> ingestion ----------------------------------------------
    status, source = call(
        "POST",
        f"/api/v1/spaces/{space_id}/sources",
        user=tenant_a,
        body={"source_type": "upload", "uri": "", "name": "notes"},
    )
    check("create an upload source", status == 200, status)
    source_id = source["source_id"]  # type: ignore[index]

    note = "# 操作系统笔记\n\n虚拟内存通过页表把虚拟地址映射到物理页，缺页中断由内核处理。\n"
    status, uploaded = call(
        "POST",
        f"/api/v1/spaces/{space_id}/sources/{source_id}/upload",
        user=tenant_a,
        upload=("os-notes.md", note.encode()),
    )
    check(
        "upload a file (blob volume must be writable)",
        status == 200 and bool(uploaded.get("document_id")),  # type: ignore[union-attr]
        status,
    )
    task_id = uploaded["task_id"]  # type: ignore[index]

    status, ingested = call(
        "POST", f"/api/v1/spaces/{space_id}/sources/{source_id}/ingest", user=tenant_a
    )
    check("trigger ingestion", status in (200, 202), status)
    task_id = (ingested.get("task_ids") or [task_id])[0]  # type: ignore[union-attr]

    task = poll(f"/api/v1/tasks/{task_id}", tenant_a, TERMINAL_TASK_STATES)
    check("ingestion task succeeds", task.get("status") == "succeeded", task.get("status"))

    status, detail = call(
        "GET", f"/api/v1/spaces/{space_id}/sources/{source_id}/detail", user=tenant_a
    )
    check(
        "source shows a published document",
        status == 200 and detail["source"]["available_count"] >= 1,  # type: ignore[index]
        detail["source"]["available_count"] if status == 200 else status,  # type: ignore[index]
    )

    status, _ = call("GET", f"/api/v1/tasks/{task_id}", user=tenant_b)
    check("tenant B cannot read tenant A's task", status == 404, status)

    # --- grounded question and citation ------------------------------------
    status, conversation = call(
        "POST", f"/api/v1/spaces/{space_id}/conversations", user=tenant_a, body={}
    )
    check("create a conversation", status == 201, status)
    conversation_id = conversation["conversation_id"]  # type: ignore[index]

    status, run = call(
        "POST",
        f"/api/v1/conversations/{conversation_id}/questions",
        user=tenant_a,
        body={"question": "虚拟内存是怎么工作的？", "idempotency_key": uuid.uuid4().hex},
    )
    check("submit a question", status == 202, status)
    run_id = run["run_id"]  # type: ignore[index]

    status, _ = call("GET", f"/api/v1/qa/runs/{run_id}", user=tenant_b)
    check("tenant B cannot read tenant A's Run", status == 404, status)

    run = poll(f"/api/v1/qa/runs/{run_id}", tenant_a, TERMINAL_RUN_STATES)
    check(
        "grounded answer is produced",
        run.get("status") in {"completed", "succeeded"},
        f"status={run.get('status')} error={run.get('error_code')}",
    )
    citations = run.get("citations") or []
    check("answer carries citations", len(citations) >= 1, len(citations))

    if citations:
        evidence_id = citations[0]["evidence_id"]
        status, citation = call(
            "GET", f"/api/v1/qa/runs/{run_id}/citations/{evidence_id}", user=tenant_a
        )
        check(
            "citation resolves with a locator and excerpt",
            status == 200 and bool(citation.get("locator")) and bool(citation.get("excerpt")),
            status,
        )

    # --- capability boundary ----------------------------------------------
    for path in ("/api/v1/skills", "/api/v2/commands", f"/api/v3/runs/{uuid.uuid4()}"):
        status, body = call("GET", path, user=tenant_a)
        check(
            f"PUBLIC_MODE hides {path}",
            status == 404
            and isinstance(body, dict)
            and body.get("code") == "CAPABILITY_NOT_EXPOSED",
            status,
        )

    # --- input validation --------------------------------------------------
    status, _ = call(
        "POST",
        f"/api/v1/spaces/{space_id}/sources",
        user=tenant_a,
        body={"source_type": "bogus", "uri": "", "name": "x"},
    )
    check("unknown source_type returns 422", status == 422, status)

    status, _ = call("POST", "/api/v1/spaces", user=tenant_a, body={"name": ""})
    check("empty Space name returns 422", status == 422, status)

    # --- deletion ----------------------------------------------------------
    status, _ = call("DELETE", f"/api/v1/spaces/{space_id}", user=tenant_b)
    check("tenant B cannot delete tenant A's Space", status == 404, status)

    status, _ = call("DELETE", f"/api/v1/spaces/{space_id}", user=tenant_a)
    check("tenant A deletes its own Space", status == 204, status)

    status, _ = call("GET", f"/api/v1/spaces/{space_id}/sources", user=tenant_a)
    check("deleted Space is no longer readable", status == 404, status)

    status, ready = call("GET", "/api/v1/health/ready", token="")
    check("readiness stays healthy", status == 200 and ready.get("status") == "ready", status)  # type: ignore[union-attr]

    summary = f"{len(_failures)} failed check(s)" if _failures else "all checks passed"
    print(f"\n{summary}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
