"""Export complete local development Agent rounds from a QA debug trace.

The input is a local, opt-in ``QA_DEBUG_TRACE`` JSONL file. It may include prompts,
user content, Tool payloads, and provider responses, so this command reads only from
the configured local trace directory and writes the human-readable export beneath
that directory by default.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import UUID

from infrastructure.config import settings


def _trace_path(run_id: UUID, directory: Path) -> Path:
    root = directory.resolve()
    path = (root / f"{run_id}.jsonl").resolve()
    if path.parent != root:
        raise ValueError("trace path is outside the configured QA debug directory")
    return path


def _events(path: Path) -> Iterable[dict[str, Any]]:
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        if block.strip():
            event = json.loads(block)
            if not isinstance(event, dict):
                raise ValueError("QA debug trace contains a non-object event")
            yield event


def export_harness_trace(path: Path) -> list[dict[str, Any]]:
    """Return complete Agent model and Tool I/O grouped by Runtime iteration."""
    rounds: list[dict[str, Any]] = []
    by_number: dict[int, dict[str, Any]] = {}
    tools_by_key: dict[str, dict[str, Any]] = {}
    for event in _events(path):
        event_type = event.get("event")
        if event_type == "agent_round":
            round_number = event.get("round_number")
            if not isinstance(round_number, int) or round_number < 1:
                raise ValueError("Agent round is missing a valid round number")
            item = by_number.get(round_number)
            if item is None:
                item = {
                    "schema_version": "agent-harness-trace-v1",
                    "run_id": event.get("run_id"),
                    "trace_id": event.get("trace_id"),
                    "round": round_number,
                    "model_calls": [],
                    "tools": [],
                }
                by_number[round_number] = item
                rounds.append(item)
            item["model_calls"].append(
                {
                    "phase": event.get("phase"),
                    "input": event.get("input"),
                    "output": event.get("output"),
                }
            )
        elif event_type == "tool_call":
            if rounds:
                item = {
                    "tool_name": event.get("tool_name"),
                    "tool_version": event.get("tool_version"),
                    "idempotency_key": event.get("idempotency_key"),
                    "input": event.get("arguments"),
                    "output": None,
                    "error": None,
                }
                rounds[-1]["tools"].append(item)
                key = item["idempotency_key"]
                if isinstance(key, str):
                    tools_by_key[key] = item
        elif event_type == "tool_result":
            # Tool calls follow the decision that selected them. Runtime tracing keeps
            # the sequence stable, so append to the latest nonterminal decision round.
            if rounds:
                key = event.get("idempotency_key")
                item = tools_by_key.get(key) if isinstance(key, str) else None
                if item is not None:
                    item["output"] = event.get("output")
                else:
                    rounds[-1]["tools"].append(
                        {
                            "tool_name": event.get("tool_name"),
                            "tool_version": event.get("tool_version"),
                            "idempotency_key": key,
                            "input": None,
                            "output": event.get("output"),
                            "error": None,
                        }
                    )
        elif event_type == "tool_error" and rounds:
            key = event.get("idempotency_key")
            item = tools_by_key.get(key) if isinstance(key, str) else None
            if item is not None:
                item["error"] = event.get("error")
    return rounds


_TEXT_WIDTH = 100


def _quoted_text(value: Any) -> str:
    """Render text as wrapped Markdown blockquotes without interpreting its Markdown."""
    if value is None:
        return "> _(none)_"
    text = str(value).replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    if not text:
        return "> _(empty)_"
    rendered: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph.strip():
            rendered.append(">")
            continue
        rendered.extend(
            f"> {line}"
            for line in textwrap.wrap(
                paragraph,
                width=_TEXT_WIDTH - 2,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    return "\n".join(rendered)


def _wrapped_json(value: Any) -> str:
    """Keep structured payloads recognizable while preventing very long display lines."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    lines: list[str] = []
    for line in encoded.splitlines():
        if len(line) <= _TEXT_WIDTH:
            lines.append(line)
            continue
        lines.extend(
            textwrap.wrap(
                line,
                width=_TEXT_WIDTH,
                replace_whitespace=False,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    return "\n".join(lines)


def _json_section(value: Any) -> str:
    return f"```json\n{_wrapped_json(value)}\n```"


def _render_model_input(value: Any) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return _json_section(value)
    sections: list[str] = []
    for index, message in enumerate(value["messages"], start=1):
        if not isinstance(message, dict):
            sections.extend((f"**Message {index}**", _quoted_text(message)))
            continue
        role = message.get("role", f"message {index}")
        sections.extend((f"**{role}**", _quoted_text(message.get("content", ""))))
    if not sections:
        return "_(no messages)_"
    return "\n\n".join(sections)


def _render_model_output(value: Any) -> str:
    if isinstance(value, dict) and "text" in value:
        sections = [_quoted_text(value.get("text"))]
        metadata = {key: item for key, item in value.items() if key != "text"}
        if metadata:
            sections.extend(("**Response metadata**", _json_section(metadata)))
        return "\n\n".join(sections)
    return _json_section(value)


def _render_tool(tool: dict[str, Any], index: int) -> list[str]:
    name = tool.get("tool_name") or "unknown"
    version = tool.get("tool_version") or "unknown"
    lines = [f"#### {index}. `{name}` ({version})"]
    key = tool.get("idempotency_key")
    if key:
        lines.extend((f"- Idempotency key: `{key}`", ""))
    lines.extend(("**Input**", "", _json_section(tool.get("input")), ""))
    if tool.get("output") is not None:
        lines.extend(("**Output**", "", _json_section(tool["output"]), ""))
    if tool.get("error") is not None:
        lines.extend(("**Error**", "", _json_section(tool["error"]), ""))
    return lines[:-1]


def render_harness_markdown(rounds: list[dict[str, Any]]) -> str:
    """Render exported rounds as a readable Markdown diagnostic report."""
    first = rounds[0] if rounds else {}
    run_id = first.get("run_id", "unknown")
    trace_id = first.get("trace_id", "unknown")
    lines = [
        "# Agent Harness Trace",
        "",
        f"- Run ID: `{run_id}`",
        f"- Trace ID: `{trace_id}`",
        f"- Rounds: {len(rounds)}",
        "",
        "> This file contains sensitive local diagnostic data. Do not commit, upload, or paste it",
        "> into tickets.",
        "",
    ]
    for round_item in rounds:
        round_number = round_item.get("round", "?")
        lines.extend((f"## Round {round_number}", ""))
        model_calls = round_item.get("model_calls", [])
        for call_index, call in enumerate(model_calls, start=1):
            phase = call.get("phase") or "unknown"
            lines.extend(
                (
                    f"### Model Call {call_index}: {phase}",
                    "",
                    "#### Input",
                    "",
                    _render_model_input(call.get("input")),
                    "",
                    "#### Output",
                    "",
                    _render_model_output(call.get("output")),
                    "",
                )
            )
        tools = round_item.get("tools", [])
        if tools:
            lines.extend(("### Tools", ""))
            for tool_index, tool in enumerate(tools, start=1):
                lines.extend(_render_tool(tool, tool_index))
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _default_output_path(run_id: UUID, directory: Path) -> Path:
    return directory / "exports" / f"agent-harness-{run_id}.md"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Markdown output path (default: QA_DEBUG_TRACE_PATH/exports/agent-harness-<run-id>.md)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing output file",
    )
    args = parser.parse_args()
    trace_directory = Path(settings.qa_debug_trace_path)
    path = _trace_path(args.run_id, trace_directory)
    if not path.is_file():
        raise FileNotFoundError(f"QA debug trace was not found: {path}")
    output = (args.output or _default_output_path(args.run_id, trace_directory)).resolve()
    if output.exists() and not args.force:
        raise FileExistsError(f"output already exists; use --force to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_harness_markdown(export_harness_trace(path)), encoding="utf-8")
    print(f"Exported Agent harness trace to {output}")


if __name__ == "__main__":
    main()
