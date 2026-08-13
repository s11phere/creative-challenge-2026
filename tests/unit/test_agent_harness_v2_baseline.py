from __future__ import annotations

import json
from typing import Any

from application.assistant.harness_baseline import summarize_agent_harness_trace

from scripts import evaluate_agent_harness_v2


def test_harness_baseline_contains_counts_and_no_trace_content() -> None:
    events: list[dict[str, Any]] = [
        {
            "event": "agent_round",
            "phase": "decision",
            "input": {
                "messages": [
                    {"role": "system", "content": "base"},
                    {"role": "user", "content": "user"},
                ]
            },
            "output": {"text": '{"action":"complete","reason":"done"}'},
        },
        {"event": "tool_call", "arguments": {"query": "must not escape"}},
        {
            "event": "agent_round",
            "phase": "long_answer",
            "unselected_skill_instruction_bytes": 17,
            "input": {
                "messages": [
                    {
                        "role": "system",
                        "content": "<active_skill name='x'>body</active_skill>",
                    },
                    {"role": "user", "content": "full"},
                ]
            },
            "output": {"text": "must not escape"},
        },
    ]

    summary = summarize_agent_harness_trace(events).as_dict()

    assert summary == {
        "agent_rounds": 2,
        "tool_calls": 1,
        "terminal_generations": 1,
        "long_answer_generations": 1,
        "static_prompt_bytes": 46,
        "dynamic_context_bytes": 8,
        "total_input_bytes": 54,
        "max_round_input_bytes": 46,
        "eager_skill_instruction_bytes": 42,
        "unselected_skill_instruction_bytes": 17,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "round_metrics": [
            {
                "static_prompt_bytes": 4,
                "dynamic_context_bytes": 4,
                "eager_skill_instruction_bytes": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            {
                "static_prompt_bytes": 42,
                "dynamic_context_bytes": 4,
                "eager_skill_instruction_bytes": 42,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        ],
    }
    assert "must not escape" not in json.dumps(summary)


def test_agent_harness_v2_dataset_is_metadata_only_and_provisional(capsys: object) -> None:
    cases = evaluate_agent_harness_v2.validate_dataset()
    assert len(cases) == 10

    result = evaluate_agent_harness_v2.main([])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert result == 0
    assert json.loads(captured.out) == {
        "case_count": 10,
        "content_policy": "synthetic_metadata_only",
        "dataset_split": "development",
        "formal_run_eligible": False,
        "quality_status": "provisional",
        "schema_version": "agent-harness-v2-metrics-report-v1",
    }


def test_harness_baseline_reads_native_v2_safe_size_and_cache_fields() -> None:
    events: list[dict[str, Any]] = [
        {
            "event": "agent_round",
            "phase": "native_tool_use",
            "input": {
                "static_prompt_bytes": 120,
                "dynamic_context_bytes": 240,
                "eager_skill_instruction_bytes": 30,
                "unselected_skill_instruction_bytes": 0,
            },
            "output": {
                "usage": {
                    "cache_read_tokens": 7,
                    "cache_write_tokens": 3,
                },
                "tool_calls": 0,
                "finish_reason": "stop",
            },
        },
        {
            "event": "agent_round",
            "phase": "native_tool_use",
            "input": {
                "static_prompt_bytes": 90,
                "dynamic_context_bytes": 80,
                "eager_skill_instruction_bytes": 0,
                "unselected_skill_instruction_bytes": 0,
            },
            "output": {
                "usage": {
                    "cached_input_tokens": 4,
                    "cache_write_tokens": 1,
                },
                "tool_calls": 1,
                "finish_reason": "tool_calls",
            },
        },
    ]

    summary = summarize_agent_harness_trace(events).as_dict()

    assert summary == {
        "agent_rounds": 2,
        "tool_calls": 0,
        "terminal_generations": 1,
        "long_answer_generations": 0,
        "static_prompt_bytes": 210,
        "dynamic_context_bytes": 320,
        "total_input_bytes": 530,
        "max_round_input_bytes": 360,
        "eager_skill_instruction_bytes": 30,
        "unselected_skill_instruction_bytes": 0,
        "cache_read_tokens": 11,
        "cache_write_tokens": 4,
        "round_metrics": [
            {
                "static_prompt_bytes": 120,
                "dynamic_context_bytes": 240,
                "eager_skill_instruction_bytes": 30,
                "cache_read_tokens": 7,
                "cache_write_tokens": 3,
            },
            {
                "static_prompt_bytes": 90,
                "dynamic_context_bytes": 80,
                "eager_skill_instruction_bytes": 0,
                "cache_read_tokens": 4,
                "cache_write_tokens": 1,
            },
        ],
    }
