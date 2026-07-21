from __future__ import annotations

import json
import logging

from infrastructure.logging_config import JsonLogFormatter, RedactionFilter


def test_retrieval_log_schema_keeps_metrics_and_excludes_content() -> None:
    record = logging.LogRecord(
        name="api.routers.search",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="retrieval_search_completed",
        args=(),
        exc_info=None,
    )
    record.requested_mode = "hybrid"
    record.executed_mode = "keyword"
    record.profile_version = "retrieval-profile-v1"
    record.keyword_candidates = 3
    record.final_candidates = 2
    record.stage_timings_ms = {"keyword": 1.25, "query_embedding": 2.5}
    record.degraded = True
    record.degradation_reasons = ["RETRIEVAL_EMBEDDING_UNAVAILABLE"]
    record.query = "private-query-marker"
    record.chunk_text = "private-chunk-marker"

    assert RedactionFilter().filter(record) is True
    payload = json.loads(JsonLogFormatter(service="api", environment="test").format(record))

    assert payload["requested_mode"] == "hybrid"
    assert payload["executed_mode"] == "keyword"
    assert payload["keyword_candidates"] == 3
    assert payload["final_candidates"] == 2
    assert payload["stage_timings_ms"]["query_embedding"] == 2.5
    assert payload["degraded"] is True
    assert "query" not in payload
    assert "chunk_text" not in payload
    assert "private-query-marker" not in json.dumps(payload)
    assert "private-chunk-marker" not in json.dumps(payload)
