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


def export_harness_trace(path: Path, *, include_v2_bodies: bool = False) -> list[dict[str, Any]]:
    """Return complete Agent model and Tool I/O grouped by Runtime iteration."""
    rounds: list[dict[str, Any]] = []
    by_number: dict[int, dict[str, Any]] = {}
    tools_by_key: dict[str, dict[str, Any]] = {}
    events = tuple(_events(path))
    provider_pairs = _provider_pairs(events) if include_v2_bodies else ()
    provider_index = 0
    for event in events:
        event_type = event.get("event")
        if event_type == "agent_round":
            round_number = event.get("round_number")
            native = _is_native_event(event)
            if not isinstance(round_number, int) or (round_number < 1 and not native):
                raise ValueError("Agent round is missing a valid round number")
            item = by_number.get(round_number)
            if item is None:
                item = {
                    "schema_version": (
                        "agent-harness-trace-v2" if native else "agent-harness-trace-v1"
                    ),
                    "run_id": event.get("run_id"),
                    "trace_id": event.get("trace_id"),
                    "round": round_number,
                    "model_calls": [],
                    "tools": [],
                }
                by_number[round_number] = item
                rounds.append(item)
            model_call = {
                "phase": event.get("phase"),
                "input": event.get("input"),
                "output": event.get("output"),
                **(
                    {
                        "cache_mode": event.get("cache_mode"),
                        "context_digest": event.get("context_digest"),
                        "visible_observation_bytes": event.get("visible_observation_bytes"),
                    }
                    if native
                    else {}
                ),
            }
            if native and provider_index < len(provider_pairs):
                provider_request, provider_response = provider_pairs[provider_index]
                response_sequence = provider_response.get("sequence")
                round_sequence = event.get("sequence")
                if (
                    not isinstance(response_sequence, int)
                    or not isinstance(round_sequence, int)
                    or response_sequence < round_sequence
                ):
                    provider_index += 1
                    model_call["provider_request"] = provider_request
                    model_call["provider_response"] = provider_response
            item["model_calls"].append(model_call)
        elif event_type == "tool_call":
            if rounds:
                native = _is_native_event(event)
                item = {
                    "tool_name": event.get("tool_name"),
                    "tool_version": event.get("tool_version"),
                    "idempotency_key": event.get("idempotency_key"),
                    "input": event.get("input") if native else event.get("arguments"),
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
    while provider_index < len(provider_pairs):
        provider_request, provider_response = provider_pairs[provider_index]
        provider_index += 1
        if not rounds:
            round_number = 1
            rounds.append(
                {
                    "schema_version": "agent-harness-trace-v2",
                    "run_id": provider_request.get("run_id"),
                    "trace_id": provider_request.get("trace_id"),
                    "round": round_number,
                    "model_calls": [],
                    "tools": [],
                }
            )
            by_number[round_number] = rounds[-1]
        rounds[-1]["model_calls"].append(
            {
                "phase": "provider_error",
                "input": {"status": "provider_error"},
                "output": {"status": "provider_error"},
                "provider_request": provider_request,
                "provider_response": provider_response,
            }
        )
    return rounds


def _is_native_event(event: dict[str, Any]) -> bool:
    return (
        event.get("schema_version") == "agent-harness-trace-v2"
        or event.get("harness_version") == "native-tool-use-v2"
    )


def _provider_pairs(
    events: Iterable[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair native Assistant provider requests with their local trace responses."""
    pending: dict[str, dict[str, Any]] = {}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for event in events:
        if event.get("phase") != "assistant_agent_decision":
            continue
        call_id = event.get("call_id")
        if not isinstance(call_id, str):
            continue
        event_type = event.get("event")
        if event_type == "llm_request":
            pending[call_id] = event
        elif event_type in {"llm_response", "llm_error"} and call_id in pending:
            pairs.append((pending.pop(call_id), event))
    return pairs


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
    metadata = _provider_request_metadata(value)
    if metadata:
        sections.extend(("**Request metadata**", _json_section(metadata)))
    if not sections:
        return "_(no messages)_"
    return "\n\n".join(sections)


def _provider_request_metadata(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("event") != "llm_request":
        return {key: item for key, item in value.items() if key != "messages"}
    fields = {
        "cache_key",
        "max_tokens",
        "temperature",
        "tool_call_history",
        "tool_results",
        "tools",
    }
    return {key: value[key] for key in fields if key in value}


def _render_model_output(value: Any) -> str:
    if isinstance(value, dict) and "text" in value:
        sections = [_quoted_text(value.get("text"))]
        metadata = {key: item for key, item in value.items() if key != "text"}
        if metadata:
            sections.extend(("**Response metadata**", _json_section(metadata)))
        return "\n\n".join(sections)
    if isinstance(value, dict) and "error" in value:
        error = value.get("error")
        sections: list[str] = []
        if isinstance(error, dict):
            choice = _provider_error_choice(error)
            message = choice.get("message") if isinstance(choice, dict) else None
            sections.extend(
                (
                    "**Provider Error**",
                    _json_section(
                        {key: item for key, item in error.items() if key != "provider_response"}
                    ),
                    "",
                )
            )
            if message is not None:
                sections.extend(
                    (
                        "**Model Text**",
                        _quoted_text(message.get("content", "")),
                        "",
                    )
                )
                if "reasoning_content" in message:
                    sections.extend(
                        (
                            "**Reasoning Text**",
                            _quoted_text(message.get("reasoning_content", "")),
                            "",
                        )
                    )
                if message.get("tool_calls"):
                    sections.extend(
                        (
                            "**Tool Calls**",
                            _json_section(message.get("tool_calls")),
                            "",
                        )
                    )
                finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
                if finish_reason is not None:
                    sections.extend(
                        (
                            "**Finish Reason**",
                            _json_section(finish_reason),
                            "",
                        )
                    )
        metadata = {key: item for key, item in value.items() if key not in {"error"}}
        if metadata:
            sections.extend(("**Response metadata**", _json_section(metadata), ""))
        return "\n\n".join(sections).rstrip() or _json_section(value)
    return _json_section(value)


def _provider_error_choice(error: dict[str, Any]) -> dict[str, Any] | None:
    provider_response = error.get("provider_response")
    if not isinstance(provider_response, dict):
        return None
    choices = provider_response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    return first


def _render_model_context(call: dict[str, Any]) -> str | None:
    metadata = {
        key: call[key]
        for key in ("cache_mode", "context_digest", "visible_observation_bytes")
        if key in call and call[key] is not None
    }
    if not metadata:
        return None
    return _json_section(metadata)


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
    raw_turns = [
        (round_item.get("round", "?"), call)
        for round_item in rounds
        for call in round_item.get("model_calls", [])
        if "provider_request" in call or "provider_response" in call
    ]
    if raw_turns:
        lines.extend(("## Provider Transcript", ""))
        for turn_index, (round_number, call) in enumerate(raw_turns, start=1):
            lines.extend(
                (
                    f"### Provider Turn {turn_index}",
                    "",
                    "#### Request",
                    "",
                    _render_model_input(call.get("provider_request")),
                    "",
                )
            )
            if call.get("provider_response") is not None:
                lines.extend(
                    (
                        "#### Response",
                        "",
                        _render_model_output(call["provider_response"]),
                        "",
                    )
                )
            else:
                lines.extend(("#### Response", "", "_(no response recorded)_", ""))
            lines.extend((f"_Round: {round_number}_", ""))
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
                )
            )
            context = _render_model_context(call)
            if context is not None:
                lines.extend(("#### Context", "", context, ""))
            lines.extend(
                (
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
    parser.add_argument(
        "--include-v2-bodies",
        action="store_true",
        help="Include raw native v2 provider requests/responses in the local-only report",
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
    output.write_text(
        render_harness_markdown(
            export_harness_trace(path, include_v2_bodies=args.include_v2_bodies)
        ),
        encoding="utf-8",
    )
    print(f"Exported Agent harness trace to {output}")


if __name__ == "__main__":
    main()
