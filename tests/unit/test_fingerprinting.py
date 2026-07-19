"""Tests for domain fingerprinting: stable_key, content_hash, storage_key."""

from __future__ import annotations

import hashlib
from uuid import UUID

from domain.fingerprinting import (
    CONTENT_HASHER_VERSION,
    STABLE_KEY_NORMALIZER_VERSION,
    compute_content_hash,
    compute_storage_key,
    normalize_stable_key,
    normalize_stable_key_from_parts,
)

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------


class TestVersionConstants:
    def test_normalizer_version_is_string(self) -> None:
        assert isinstance(STABLE_KEY_NORMALIZER_VERSION, str)
        assert len(STABLE_KEY_NORMALIZER_VERSION) > 0

    def test_hasher_version_is_string(self) -> None:
        assert isinstance(CONTENT_HASHER_VERSION, str)
        assert len(CONTENT_HASHER_VERSION) > 0


# ---------------------------------------------------------------------------
# normalize_stable_key
# ---------------------------------------------------------------------------


class TestNormalizeStableKey:
    def test_lowercases(self) -> None:
        assert normalize_stable_key("UPLOAD/MyFile.MD") == "upload/myfile.md"

    def test_collapses_double_slashes(self) -> None:
        assert normalize_stable_key("upload//myfile") == "upload/myfile"

    def test_strips_leading_whitespace(self) -> None:
        assert normalize_stable_key("  upload/file") == "upload/file"

    def test_strips_trailing_whitespace(self) -> None:
        assert normalize_stable_key("upload/file  ") == "upload/file"

    def test_strips_trailing_slash(self) -> None:
        assert normalize_stable_key("upload/file/") == "upload/file"

    def test_strips_leading_slash(self) -> None:
        assert normalize_stable_key("/upload/file") == "upload/file"

    def test_replaces_unsafe_chars_with_underscore(self) -> None:
        assert normalize_stable_key("upload/my file.md") == "upload/my_file.md"

    def test_replaces_spaces(self) -> None:
        assert normalize_stable_key("my folder/doc 1") == "my_folder/doc_1"

    def test_collapses_consecutive_underscores(self) -> None:
        assert normalize_stable_key("my___file") == "my_file"

    def test_strips_leading_underscores(self) -> None:
        assert normalize_stable_key("__my_file") == "my_file"

    def test_strips_trailing_underscores(self) -> None:
        assert normalize_stable_key("my_file__") == "my_file"

    def test_empty_string(self) -> None:
        assert normalize_stable_key("") == ""

    def test_only_whitespace(self) -> None:
        assert normalize_stable_key("   ") == ""

    def test_only_special_chars(self) -> None:
        result = normalize_stable_key("@#$%^&")
        assert result == "" or all(c == "_" for c in result)

    def test_deterministic(self) -> None:
        assert normalize_stable_key("Upload/My File.md") == normalize_stable_key(
            "upload/my file.md"
        )

    def test_url_encoded_round_trip(self) -> None:
        # URL-encoded chars are treated as regular chars
        result = normalize_stable_key("upload/file%20name")
        assert "%" not in result  # the % is replaced with underscore

    def test_relative_path(self) -> None:
        assert normalize_stable_key("./docs/../file.md") == "./docs/../file.md"

    def test_mixed_case_and_slashes(self) -> None:
        assert normalize_stable_key("UPLOAD//MY/FILE//") == "upload/my/file"

    def test_keeps_hyphens(self) -> None:
        assert normalize_stable_key("my-file") == "my-file"

    def test_keeps_dots(self) -> None:
        assert normalize_stable_key("my.file.md") == "my.file.md"

    def test_keeps_tilde(self) -> None:
        assert normalize_stable_key("my~file") == "my~file"


# ---------------------------------------------------------------------------
# normalize_stable_key_from_parts
# ---------------------------------------------------------------------------


class TestNormalizeStableKeyFromParts:
    def test_source_type_and_filename(self) -> None:
        result = normalize_stable_key_from_parts("upload", "My File.md")
        assert result == "upload/my_file.md"

    def test_with_relative_path(self) -> None:
        result = normalize_stable_key_from_parts("upload", "file.md", "docs/subdir")
        assert result == "upload/file.md/docs/subdir"

    def test_deterministic(self) -> None:
        a = normalize_stable_key_from_parts("upload", "File.md")
        b = normalize_stable_key_from_parts("UPLOAD", "file.md")
        assert a == b


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------


class TestComputeContentHash:
    def test_empty_text(self) -> None:
        result = compute_content_hash("")
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 hex

    def test_known_value(self) -> None:
        text = "Hello, world!"
        expected_input = CONTENT_HASHER_VERSION.encode() + b"\x00" + text.encode()
        expected = hashlib.sha256(expected_input).hexdigest()
        assert compute_content_hash(text) == expected

    def test_different_text_different_hash(self) -> None:
        h1 = compute_content_hash("content a")
        h2 = compute_content_hash("content b")
        assert h1 != h2

    def test_different_version_different_hash(self) -> None:
        h1 = compute_content_hash("same text", hasher_version="1.0")
        h2 = compute_content_hash("same text", hasher_version="2.0")
        assert h1 != h2

    def test_same_input_same_hash(self) -> None:
        h1 = compute_content_hash("exact same text")
        h2 = compute_content_hash("exact same text")
        assert h1 == h2

    def test_default_version_is_constant(self) -> None:
        assert compute_content_hash("test") == compute_content_hash(
            "test", hasher_version=CONTENT_HASHER_VERSION
        )

    def test_multiline_text(self) -> None:
        text = "line1\nline2\nline3"
        result = compute_content_hash(text)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_unicode_text(self) -> None:
        text = "你好，世界！"
        result = compute_content_hash(text)
        assert isinstance(result, str)
        assert len(result) == 64


# ---------------------------------------------------------------------------
# compute_storage_key
# ---------------------------------------------------------------------------


class TestComputeStorageKey:
    def test_uses_source_id_and_blob_hash(self) -> None:
        source_id = UUID("00000000-0000-0000-0000-000000000001")
        blob_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        key = compute_storage_key(source_id, blob_hash)
        assert key.startswith("00000000-0000-0000-0000-000000000001/")
        assert blob_hash in key

    def test_includes_two_char_prefix(self) -> None:
        source_id = UUID("00000000-0000-0000-0000-000000000001")
        blob_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        key = compute_storage_key(source_id, blob_hash)
        assert "/ab/" in key  # first two chars of hash

    def test_format(self) -> None:
        source_id = UUID("00000000-0000-0000-0000-000000000001")
        blob_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        key = compute_storage_key(source_id, blob_hash)
        parts = key.split("/")
        assert len(parts) == 3
        assert parts[0] == str(source_id)
        assert parts[1] == blob_hash[:2]
        assert parts[2] == blob_hash

    def test_no_path_traversal(self) -> None:
        source_id = UUID("00000000-0000-0000-0000-000000000001")
        blob_hash = "00abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        key = compute_storage_key(source_id, blob_hash)
        assert ".." not in key
        assert "/" not in key[len(str(source_id)) + 1 : -len(blob_hash) - 1]

    def test_different_source_different_key(self) -> None:
        sid1 = UUID("00000000-0000-0000-0000-000000000001")
        sid2 = UUID("00000000-0000-0000-0000-000000000002")
        bh = "a" * 64
        assert compute_storage_key(sid1, bh) != compute_storage_key(sid2, bh)

    def test_different_hash_different_key(self) -> None:
        sid = UUID("00000000-0000-0000-0000-000000000001")
        assert compute_storage_key(sid, "a" * 64) != compute_storage_key(sid, "b" * 64)
