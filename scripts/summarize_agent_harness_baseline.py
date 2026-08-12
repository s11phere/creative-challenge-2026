"""Print body-free aggregate counters for one local Agent harness debug trace."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import UUID

from application.assistant import summarize_agent_harness_trace
from infrastructure.config import settings


def _trace_path(run_id: UUID, directory: Path) -> Path:
    root = directory.resolve()
    path = (root / f"{run_id}.jsonl").resolve()
    if path.parent != root:
        raise ValueError("trace path is outside the configured QA debug directory")
    return path


def _events(path: Path) -> Iterable[dict[str, Any]]:
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        if not block.strip():
            continue
        value = json.loads(block)
        if not isinstance(value, dict):
            raise ValueError("QA debug trace contains a non-object event")
        yield value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=UUID, required=True)
    args = parser.parse_args()
    trace_path = _trace_path(args.run_id, Path(settings.qa_debug_trace_path))
    if not trace_path.is_file():
        raise FileNotFoundError(f"QA debug trace was not found: {trace_path}")
    baseline = summarize_agent_harness_trace(tuple(_events(trace_path)))
    print(json.dumps(baseline.as_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
