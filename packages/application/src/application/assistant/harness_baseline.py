"""Local-only aggregate measurements for Agent Harness migration baselines.

This module intentionally accepts already-captured debug trace events and returns only counts and
byte estimates. It never persists measurements and never returns prompt, answer, Tool argument, or
Tool output text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentHarnessRoundBaseline:
    """One body-free prompt-cost projection for a local Agent decision round."""

    static_prompt_bytes: int
    dynamic_context_bytes: int
    eager_skill_instruction_bytes: int
    cache_read_tokens: int
    cache_write_tokens: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.static_prompt_bytes,
                self.dynamic_context_bytes,
                self.eager_skill_instruction_bytes,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        ):
            raise ValueError("Agent harness round baseline values cannot be negative")

    def as_dict(self) -> dict[str, int]:
        return {
            "static_prompt_bytes": self.static_prompt_bytes,
            "dynamic_context_bytes": self.dynamic_context_bytes,
            "eager_skill_instruction_bytes": self.eager_skill_instruction_bytes,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass(frozen=True)
class AgentHarnessBaseline:
    """Body-free summary of one development-local Agent harness trace."""

    agent_rounds: int
    tool_calls: int
    terminal_generations: int
    long_answer_generations: int
    static_prompt_bytes: int
    dynamic_context_bytes: int
    total_input_bytes: int
    max_round_input_bytes: int
    eager_skill_instruction_bytes: int
    unselected_skill_instruction_bytes: int
    cache_read_tokens: int
    cache_write_tokens: int
    round_metrics: tuple[AgentHarnessRoundBaseline, ...] = ()

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.agent_rounds,
                self.tool_calls,
                self.terminal_generations,
                self.long_answer_generations,
                self.static_prompt_bytes,
                self.dynamic_context_bytes,
                self.total_input_bytes,
                self.max_round_input_bytes,
                self.eager_skill_instruction_bytes,
                self.unselected_skill_instruction_bytes,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        ):
            raise ValueError("Agent harness baseline values cannot be negative")
        if self.max_round_input_bytes > self.total_input_bytes:
            raise ValueError("Agent harness maximum round input cannot exceed total input")

    def as_dict(self) -> dict[str, int | list[dict[str, int]]]:
        """Return counters only; no trace content can escape this projection."""

        return {
            "agent_rounds": self.agent_rounds,
            "tool_calls": self.tool_calls,
            "terminal_generations": self.terminal_generations,
            "long_answer_generations": self.long_answer_generations,
            "static_prompt_bytes": self.static_prompt_bytes,
            "dynamic_context_bytes": self.dynamic_context_bytes,
            "total_input_bytes": self.total_input_bytes,
            "max_round_input_bytes": self.max_round_input_bytes,
            "eager_skill_instruction_bytes": self.eager_skill_instruction_bytes,
            "unselected_skill_instruction_bytes": self.unselected_skill_instruction_bytes,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "round_metrics": [item.as_dict() for item in self.round_metrics],
        }


def summarize_agent_harness_trace(events: Sequence[Mapping[str, object]]) -> AgentHarnessBaseline:
    """Compute a local migration baseline from complete opt-in trace events.

    The v1 trace marks every decision request as ``agent_round``. The static prompt is its first
    system message; remaining request content is dynamic context. Later Harness v2 traces can add
    ``unselected_skill_instruction_bytes`` explicitly, but v1 derives no such value and reports 0.
    """

    rounds = 0
    tool_calls = 0
    terminal_generations = 0
    long_answer_generations = 0
    static_prompt_bytes = 0
    dynamic_context_bytes = 0
    total_input_bytes = 0
    max_round_input_bytes = 0
    eager_skill_instruction_bytes = 0
    unselected_skill_instruction_bytes = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    round_metrics: list[AgentHarnessRoundBaseline] = []

    for event in events:
        event_type = event.get("event")
        if event_type == "tool_call":
            tool_calls += 1
            continue
        if event_type != "agent_round":
            continue
        rounds += 1
        phase = event.get("phase")
        if phase == "long_answer":
            long_answer_generations += 1
        elif phase == "decision":
            terminal_generations += _terminal_generation_count(event.get("output"))
        elif phase == "native_tool_use":
            terminal_generations += _native_terminal_generation_count(event.get("output"))
        unselected_skill_instruction_bytes += _non_negative_int(
            event.get("unselected_skill_instruction_bytes")
        )
        static, dynamic, eager = _input_bytes(event.get("input"))
        cache_read, cache_write = _cache_tokens(event.get("output"))
        static_prompt_bytes += static
        dynamic_context_bytes += dynamic
        eager_skill_instruction_bytes += eager
        cache_read_tokens += cache_read
        cache_write_tokens += cache_write
        round_total = static + dynamic
        total_input_bytes += round_total
        max_round_input_bytes = max(max_round_input_bytes, round_total)
        round_metrics.append(
            AgentHarnessRoundBaseline(
                static_prompt_bytes=static,
                dynamic_context_bytes=dynamic,
                eager_skill_instruction_bytes=eager,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
        )

    return AgentHarnessBaseline(
        agent_rounds=rounds,
        tool_calls=tool_calls,
        terminal_generations=terminal_generations,
        long_answer_generations=long_answer_generations,
        static_prompt_bytes=static_prompt_bytes,
        dynamic_context_bytes=dynamic_context_bytes,
        total_input_bytes=total_input_bytes,
        max_round_input_bytes=max_round_input_bytes,
        eager_skill_instruction_bytes=eager_skill_instruction_bytes,
        unselected_skill_instruction_bytes=unselected_skill_instruction_bytes,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        round_metrics=tuple(round_metrics),
    )


def _input_bytes(value: object) -> tuple[int, int, int]:
    if not isinstance(value, Mapping):
        return 0, 0, 0
    if all(
        isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
        for key in (
            "static_prompt_bytes",
            "dynamic_context_bytes",
            "eager_skill_instruction_bytes",
        )
    ):
        return (
            _non_negative_int(value.get("static_prompt_bytes")),
            _non_negative_int(value.get("dynamic_context_bytes")),
            _non_negative_int(value.get("eager_skill_instruction_bytes")),
        )
    messages = value.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        return 0, 0, 0
    static = 0
    dynamic = 0
    eager_skill_instructions = 0
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        size = len(content.encode("utf-8"))
        if index == 0 and message.get("role") == "system":
            static += size
            eager_skill_instructions += _eager_skill_instruction_bytes(content)
        else:
            dynamic += size
    return static, dynamic, eager_skill_instructions


def _terminal_generation_count(value: object) -> int:
    if not isinstance(value, Mapping):
        return 0
    text = value.get("text")
    return int(isinstance(text, str) and '"action":"complete"' in text.replace(" ", ""))


def _native_terminal_generation_count(value: object) -> int:
    if not isinstance(value, Mapping):
        return 0
    return int(value.get("tool_calls") == 0 and value.get("finish_reason") == "stop")


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _eager_skill_instruction_bytes(system_prompt: str) -> int:
    """Measure v1 full Skill blocks without returning their sensitive text."""

    total = 0
    cursor = 0
    while True:
        start = system_prompt.find("<active_skill ", cursor)
        if start < 0:
            return total
        end = system_prompt.find("</active_skill>", start)
        if end < 0:
            return total
        end += len("</active_skill>")
        total += len(system_prompt[start:end].encode("utf-8"))
        cursor = end


def _cache_tokens(value: object) -> tuple[int, int]:
    if not isinstance(value, Mapping):
        return 0, 0
    usage = value.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0
    cache_read = usage.get("cache_read_tokens", usage.get("cached_input_tokens"))
    cache_write = usage.get("cache_write_tokens", usage.get("cache_write_input_tokens"))
    return _non_negative_int(cache_read), _non_negative_int(cache_write)


__all__ = [
    "AgentHarnessBaseline",
    "AgentHarnessRoundBaseline",
    "summarize_agent_harness_trace",
]
