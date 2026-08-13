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


def test_export_native_v2_trace_keeps_only_safe_round_and_tool_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "native-run.jsonl"
    path.write_text(
        "\n\n".join(
            (
                _event(
                    event="agent_round",
                    run_id="run-2",
                    trace_id="trace-2",
                    round_number=1,
                    phase="native_tool_use",
                    harness_version="native-tool-use-v2",
                    schema_version="agent-harness-trace-v2",
                    context_digest="sha256:context-digest",
                    cache_mode="unsupported",
                    visible_observation_bytes=41,
                    input={
                        "tool_count": 2,
                        "message_count": 2,
                        "static_prompt_bytes": 100,
                        "dynamic_context_bytes": 200,
                    },
                    output={
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 4,
                            "cache_read_tokens": 0,
                            "cache_write_tokens": 0,
                        },
                        "tool_calls": 1,
                        "finish_reason": "tool_calls",
                    },
                ),
                _event(
                    event="tool_call",
                    harness_version="native-tool-use-v2",
                    schema_version="agent-harness-trace-v2",
                    tool_name="knowledge_retrieve",
                    tool_version="2.0.0",
                    idempotency_key="native-tool-1",
                    input={"summary": "sha256:private-tool-input"},
                ),
                _event(
                    event="tool_result",
                    harness_version="native-tool-use-v2",
                    schema_version="agent-harness-trace-v2",
                    tool_name="knowledge_retrieve",
                    tool_version="2.0.0",
                    idempotency_key="native-tool-1",
                    output={"summary": "sha256:private-tool-output"},
                ),
            )
        )
        + "\n\n",
        encoding="utf-8",
    )

    exported = export_harness_trace(path)

    assert exported[0]["schema_version"] == "agent-harness-trace-v2"
    assert exported[0]["model_calls"][0]["context_digest"] == "sha256:context-digest"
    assert exported[0]["tools"] == [
        {
            "tool_name": "knowledge_retrieve",
            "tool_version": "2.0.0",
            "idempotency_key": "native-tool-1",
            "input": {"summary": "sha256:private-tool-input"},
            "output": {"summary": "sha256:private-tool-output"},
            "error": None,
        }
    ]
    rendered = render_harness_markdown(exported)
    assert "#### Context" in rendered
    assert "sha256:private-tool-input" in rendered
    assert "sha256:private-tool-output" in rendered
    assert "private-question" not in rendered


def test_export_native_v2_can_opt_in_to_raw_provider_bodies(tmp_path: Path) -> None:
    path = tmp_path / "native-raw.jsonl"
    request_event = _event(
        event="llm_request",
        phase="assistant_agent_decision",
        call_id="raw-call-1",
        messages=[
            {"role": "system", "content": "Native system prompt."},
            {"role": "user", "content": "Private v2 question."},
        ],
        tools=[
            {
                "name": "invoke_skill",
                "description": "Select a Skill.",
                "input_schema": {"type": "object"},
            }
        ],
        tool_call_history=[
            {
                "call_id": "call-1",
                "tool_name": "list_skills",
                "arguments": {},
            }
        ],
        tool_results=[
            {
                "call_id": "call-1",
                "tool_name": "list_skills",
                "observation": {"status": "succeeded", "summary": "Listed 1 Skill."},
            }
        ],
    )
    response_event = _event(
        event="llm_response",
        phase="assistant_agent_decision",
        call_id="raw-call-1",
        text="Private v2 answer.",
        finish_reason="stop",
        tool_calls=[
            {
                "call_id": "call-2",
                "tool_name": "knowledge_retrieve",
                "arguments": {"query": "private"},
            }
        ],
    )
    round_event = _event(
        event="agent_round",
        round_number=1,
        phase="native_tool_use",
        harness_version="native-tool-use-v2",
        schema_version="agent-harness-trace-v2",
        input={"message_count": 2},
        output={"usage": {"input_tokens": 1, "output_tokens": 1}, "tool_calls": 0},
    )
    path.write_text(
        "\n\n".join((request_event, response_event, round_event)) + "\n\n",
        encoding="utf-8",
    )

    safe = export_harness_trace(path)
    raw = export_harness_trace(path, include_v2_bodies=True)

    assert "provider_request" not in safe[0]["model_calls"][0]
    assert "provider_response" not in safe[0]["model_calls"][0]
    assert raw[0]["model_calls"][0]["provider_request"] == json.loads(request_event)
    assert raw[0]["model_calls"][0]["provider_response"] == json.loads(response_event)
    rendered = render_harness_markdown(raw)
    assert "## Provider Transcript" in rendered
    assert "### Provider Turn 1" in rendered
    assert "invoke_skill" in rendered
    assert "knowledge_retrieve" in rendered
    assert "Private v2 question." in rendered
    assert "Private v2 answer." in rendered
    assert "Native system prompt." in rendered
    assert "## Provider Transcript" not in render_harness_markdown(safe)


def test_render_native_v2_provider_error_shows_model_and_reasoning_text() -> None:
    markdown = render_harness_markdown(
        [
            {
                "run_id": "run-error",
                "trace_id": "trace-error",
                "round": 1,
                "model_calls": [
                    {
                        "phase": "native_tool_use",
                        "input": {"message_count": 1},
                        "output": {"usage": {"input_tokens": 1}},
                        "provider_request": {
                            "event": "llm_request",
                            "messages": [],
                        },
                        "provider_response": {
                            "event": "llm_error",
                            "error": {
                                "error_code": "MODEL_INVALID_RESPONSE",
                                "provider_response": {
                                    "choices": [
                                        {
                                            "message": {
                                                "content": "",
                                                "reasoning_content": "Private reasoning.",
                                            },
                                            "finish_reason": "length",
                                        }
                                    ]
                                },
                            },
                        },
                    }
                ],
                "tools": [],
            }
        ]
    )

    assert "**Model Text**" in markdown
    assert "**Reasoning Text**" in markdown
    assert "Private reasoning." in markdown
    assert "**Finish Reason**" in markdown
    assert "length" in markdown


def test_export_keeps_a_final_provider_error_without_a_following_round(
    tmp_path: Path,
) -> None:
    path = tmp_path / "final-error.jsonl"
    path.write_text(
        "\n\n".join(
            (
                _event(
                    event="agent_round",
                    sequence=1,
                    round_number=1,
                    phase="native_tool_use",
                    harness_version="native-tool-use-v2",
                    schema_version="agent-harness-trace-v2",
                    input={"message_count": 2},
                    output={"usage": {"input_tokens": 1}, "tool_calls": 1},
                ),
                _event(
                    event="llm_request",
                    sequence=2,
                    phase="assistant_agent_decision",
                    call_id="final-error-call",
                    messages=[{"role": "user", "content": "Final question."}],
                ),
                _event(
                    event="llm_error",
                    sequence=3,
                    phase="assistant_agent_decision",
                    call_id="final-error-call",
                    error={
                        "error_code": "MODEL_INVALID_RESPONSE",
                        "provider_response": {
                            "choices": [
                                {
                                    "message": {
                                        "content": "",
                                        "reasoning_content": "Final private reasoning.",
                                    },
                                    "finish_reason": "length",
                                }
                            ]
                        },
                    },
                ),
            )
        )
        + "\n\n",
        encoding="utf-8",
    )

    exported = export_harness_trace(path, include_v2_bodies=True)
    rendered = render_harness_markdown(exported)

    assert exported[0]["model_calls"][-1]["phase"] == "provider_error"
    assert "Final private reasoning." in rendered
    assert "**Reasoning Text**" in rendered
