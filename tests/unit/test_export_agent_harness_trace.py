from __future__ import annotations

import json
from pathlib import Path

from scripts.export_agent_harness_trace import export_harness_trace, render_harness_markdown


def _event(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_export_harness_trace_keeps_complete_round_io_and_attaches_tool_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(
        "\n\n".join(
            (
                _event(
                    event="agent_round",
                    run_id="run-1",
                    trace_id="trace-1",
                    round_number=1,
                    phase="decision",
                    input={"messages": [{"role": "user", "content": "private question"}]},
                    output={"text": '{"action":"call_tool"}'},
                ),
                _event(
                    event="tool_call",
                    tool_name="knowledge_search",
                    tool_version="1.1.0",
                    idempotency_key="tool-1",
                    arguments={"query": "private tool input"},
                ),
                _event(
                    event="tool_result",
                    tool_name="knowledge_search",
                    tool_version="1.1.0",
                    idempotency_key="tool-1",
                    output={"result": "private tool output"},
                ),
                _event(
                    event="agent_round",
                    run_id="run-1",
                    trace_id="trace-1",
                    round_number=2,
                    phase="decision",
                    input={"messages": [{"role": "user", "content": "follow up"}]},
                    output={"text": '{"action":"complete"}'},
                ),
            )
        )
        + "\n\n",
        encoding="utf-8",
    )

    exported = export_harness_trace(path)

    assert [item["round"] for item in exported] == [1, 2]
    assert exported[0]["model_calls"] == [
        {
            "phase": "decision",
            "input": {"messages": [{"role": "user", "content": "private question"}]},
            "output": {"text": '{"action":"call_tool"}'},
        }
    ]
    assert exported[0]["tools"] == [
        {
            "tool_name": "knowledge_search",
            "tool_version": "1.1.0",
            "idempotency_key": "tool-1",
            "input": {"query": "private tool input"},
            "output": {"result": "private tool output"},
            "error": None,
        }
    ]
    assert exported[1]["model_calls"][0]["output"]["text"] == '{"action":"complete"}'


def test_export_harness_trace_groups_multiple_model_calls_from_one_round(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(
        "\n\n".join(
            (
                _event(
                    event="agent_round",
                    run_id="run-1",
                    trace_id="trace-1",
                    round_number=1,
                    phase="decision",
                    input={"messages": []},
                    output={"text": "truncated"},
                ),
                _event(
                    event="agent_round",
                    run_id="run-1",
                    trace_id="trace-1",
                    round_number=1,
                    phase="long_answer",
                    input={"messages": []},
                    output={"text": "complete answer"},
                ),
            )
        )
        + "\n\n",
        encoding="utf-8",
    )

    exported = export_harness_trace(path)

    assert len(exported) == 1
    assert [call["phase"] for call in exported[0]["model_calls"]] == [
        "decision",
        "long_answer",
    ]


def test_export_harness_trace_attaches_tool_error_to_the_original_input(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_text(
        "\n\n".join(
            (
                _event(
                    event="agent_round",
                    run_id="run-1",
                    trace_id="trace-1",
                    round_number=1,
                    phase="decision",
                    input={"messages": []},
                    output={"text": '{"action":"call_tool"}'},
                ),
                _event(
                    event="tool_call",
                    tool_name="fs_read",
                    tool_version="1.0.0",
                    idempotency_key="tool-1",
                    arguments={"path": "private.txt"},
                ),
                _event(
                    event="tool_error",
                    tool_name="fs_read",
                    tool_version="1.0.0",
                    idempotency_key="tool-1",
                    error={"error_type": "ToolRegistryError", "message": "read failed"},
                ),
            )
        )
        + "\n\n",
        encoding="utf-8",
    )

    exported = export_harness_trace(path)

    assert exported[0]["tools"][0] == {
        "tool_name": "fs_read",
        "tool_version": "1.0.0",
        "idempotency_key": "tool-1",
        "input": {"path": "private.txt"},
        "output": None,
        "error": {"error_type": "ToolRegistryError", "message": "read failed"},
    }


def test_render_harness_markdown_separates_rounds_model_io_and_tools() -> None:
    markdown = render_harness_markdown(
        [
            {
                "run_id": "run-1",
                "trace_id": "trace-1",
                "round": 1,
                "model_calls": [
                    {
                        "phase": "decision",
                        "input": {
                            "messages": [
                                {"role": "system", "content": "System prompt"},
                                {"role": "user", "content": "A question"},
                            ]
                        },
                        "output": {"text": "A model answer"},
                    }
                ],
                "tools": [
                    {
                        "tool_name": "knowledge_search",
                        "tool_version": "1.1.0",
                        "idempotency_key": "tool-1",
                        "input": {"query": "search"},
                        "output": {"result": "found"},
                        "error": None,
                    }
                ],
            }
        ]
    )

    assert "## Round 1" in markdown
    assert "### Model Call 1: decision" in markdown
    assert "#### Input" in markdown
    assert "**system**" in markdown
    assert "#### Output" in markdown
    assert "### Tools" in markdown
    assert "#### 1. `knowledge_search` (1.1.0)" in markdown
    assert "**Input**" in markdown
    assert "**Output**" in markdown
    assert "```json" in markdown


def test_render_harness_markdown_wraps_long_text() -> None:
    text = "word " * 40
    markdown = render_harness_markdown(
        [
            {
                "run_id": "run-1",
                "trace_id": "trace-1",
                "round": 1,
                "model_calls": [
                    {
                        "phase": "decision",
                        "input": {"messages": [{"role": "user", "content": text}]},
                        "output": {"text": text},
                    }
                ],
                "tools": [],
            }
        ]
    )

    assert max(len(line) for line in markdown.splitlines()) <= 100


def test_render_harness_markdown_expands_serialized_newlines() -> None:
    markdown = render_harness_markdown(
        [
            {
                "run_id": "run-1",
                "trace_id": "trace-1",
                "round": 1,
                "model_calls": [
                    {
                        "phase": "decision",
                        "input": {"messages": [{"role": "user", "content": "first\\nsecond"}]},
                        "output": {"text": "done"},
                    }
                ],
                "tools": [],
            }
        ]
    )

    assert "> first\n> second" in markdown
