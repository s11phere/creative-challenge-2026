#!/usr/bin/env python3
"""Validate or exercise the body-free Exam Preparation public demo scenario."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import request

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "cases/evals/datasets/exam-preparation-v1"
DEFAULT_SPACE_ID = "00000000-0000-0000-0000-000000000000"


def validate() -> dict[str, Any]:
    manifest = yaml.safe_load((DATASET / "manifest.yaml").read_text())
    if manifest["schema_version"] != "exam-demo-manifest-v1":
        raise ValueError("unsupported demo manifest")
    if manifest["classification"] != "public_demo" or manifest["origin"] != "repository_fixture":
        raise ValueError("demo corpus privacy classification is invalid")
    for item in manifest["documents"]:
        path = (DATASET / item["path"]).resolve()
        if path.parent != DATASET.resolve():
            raise ValueError("manifest path escapes dataset")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError(f"hash mismatch: {item['path']}")
    return {
        "dataset_id": manifest["dataset_id"],
        "documents": len(manifest["documents"]),
        "valid": True,
        "quality_status": manifest["quality_status"],
    }


def api_json(base_url: str, method: str, path: str, body: object | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode()
    req = request.Request(
        base_url + path, data=payload, method=method, headers={"Content-Type": "application/json"}
    )
    with request.urlopen(req, timeout=30) as response:  # noqa: S310 - explicit local demo URL
        return json.load(response)


def upload_file(base_url: str, source_id: str, path: Path) -> dict[str, Any]:
    boundary = f"exam-demo-{uuid.uuid4().hex}"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{path.name}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode()
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = request.Request(
        f"{base_url}/api/v1/spaces/{DEFAULT_SPACE_ID}/sources/{source_id}/upload",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with request.urlopen(req, timeout=60) as response:  # noqa: S310 - explicit local demo URL
        return json.load(response)


def prepare_space(base_url: str) -> dict[str, Any]:
    source = api_json(
        base_url,
        "POST",
        f"/api/v1/spaces/{DEFAULT_SPACE_ID}/sources",
        {
            "source_type": "upload",
            "uri": "repo://exam-preparation-v1",
            "name": "Exam Preparation v1",
        },
    )
    task_ids: list[str] = []
    for path in sorted(DATASET.glob("*.md")):
        result = upload_file(base_url, source["source_id"], path)
        if result.get("task_id"):
            task_ids.append(result["task_id"])
    statuses: dict[str, str] = {}
    deadline = time.monotonic() + 180
    while task_ids and time.monotonic() < deadline:
        statuses = {
            task_id: api_json(base_url, "GET", f"/api/v1/tasks/{task_id}")["status"]
            for task_id in task_ids
        }
        if all(
            status in {"succeeded", "failed", "cancelled", "dead_letter"}
            for status in statuses.values()
        ):
            break
        time.sleep(1)
    if any(status != "succeeded" for status in statuses.values()):
        raise RuntimeError(f"demo ingestion did not succeed: {statuses}")
    return {
        "source_id": source["source_id"],
        "documents": 5,
        "tasks": len(task_ids),
        "published": len(task_ids),
    }


def run_scenario(base_url: str, conversation_id: str) -> dict[str, Any]:
    sessions = api_json(base_url, "GET", f"/api/v3/conversations/{conversation_id}/exam-sessions")
    if not sessions:
        raise RuntimeError("start /prepare-exam in the conversation before running scenarios")
    session = sessions[-1]
    phases: list[str] = [session["phase"]]
    duplicate_checked = False
    leakage_checked = False
    while session["interaction"].get("next_action"):
        interaction = session["interaction"]
        paper = interaction.get("paper")
        answers: list[dict[str, Any]] = []
        if paper:
            serialized = json.dumps(paper, ensure_ascii=False).casefold()
            if any(
                term in serialized for term in ("correct_options", "reference_answer", "rubric")
            ):
                raise RuntimeError("public paper leaked private grading material")
            leakage_checked = True
            for section in paper["sections"]:
                for question in section["questions"]:
                    options = question.get("options")
                    answers.append(
                        {
                            "question_id": question["question_id"],
                            "selected_options": [options[0]["option_id"]],
                        }
                        if options
                        else {
                            "question_id": question["question_id"],
                            "response_text": "合成场景作答，等待建议评分。",
                        }
                    )
        nonce = f"demo-{uuid.uuid4()}"
        body = {
            "interaction_id": interaction["interaction_id"],
            "action": interaction["next_action"],
            "idempotency_key": nonce,
            "submission_id": nonce if paper else None,
            "paper_id": paper["paper_id"] if paper else None,
            "paper_version": paper["paper_version"] if paper else None,
            "answers": answers,
        }
        session = api_json(
            base_url, "POST", f"/api/v3/exam-sessions/{session['session_id']}/actions", body
        )
        phases.append(session["phase"])
        if not duplicate_checked:
            duplicate = api_json(
                base_url, "POST", f"/api/v3/exam-sessions/{session['session_id']}/actions", body
            )
            if duplicate["revision"] != session["revision"]:
                raise RuntimeError("idempotent action changed the Session")
            duplicate_checked = True
    return {
        "session_count": len(sessions),
        "phases": phases,
        "final_phase": session["phase"],
        "idempotency_verified": duplicate_checked,
        "answer_isolation_verified": leakage_checked,
        "bodies_saved": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--prepare-space", action="store_true")
    parser.add_argument("--run-scenarios", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--conversation-id")
    args = parser.parse_args()
    report = validate()
    if args.prepare_space:
        report["prepare_space"] = prepare_space(args.base_url)
    if args.run_scenarios:
        if not args.conversation_id:
            raise SystemExit("--conversation-id is required with --run-scenarios")
        report["scenario"] = run_scenario(args.base_url, args.conversation_id)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
