"""Tests for domain fingerprinting: stable_key, content_hash, storage_key."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest
from domain.fingerprinting import (
    CONTENT_HASHER_VERSION,
    compute_content_hash,
    compute_storage_key,
    normalize_stable_key,
    normalize_stable_key_from_parts,
    normalize_stable_key_v1,
)


class TestStableKey:
    """normalize_stable_key — variant cases merged via parametrize."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # lowercasing & slashes
            ("UPLOAD/MyFile.MD", "upload/myfile.md"),
            ("UPLOAD//MY/FILE//", "upload/my/file"),
            ("upload//myfile", "upload/myfile"),
            # trimming
            ("/upload/file", "upload/file"),
            ("upload/file/", "upload/file"),
            ("  upload/file", "upload/file"),
            # whitespace → underscore
            ("my folder/doc 1", "my_folder/doc_1"),
            # underscore collapse
            ("my___file", "my_file"),
            ("__my_file", "my_file"),
            ("my_file__", "my_file"),
            # safe chars preserved
            ("my-file", "my-file"),
            ("my.file.md", "my.file.md"),
            ("my~file", "my~file"),
            ("中文资料.TXT", "中文资料.txt"),
            ("Ｆｕｌｌｗｉｄｔｈ.txt", "fullwidth.txt"),
            # empty / degenerate
            ("", ""),
            ("   ", ""),
            # deterministic across case variance
        ],
    )
    def test_normalization(self, raw: str, expected: str) -> None:
        assert normalize_stable_key(raw) == expected

    def test_deterministic(self) -> None:
        assert normalize_stable_key("Upload/My File.md") == normalize_stable_key(
            "upload/my file.md"
        )

    def test_unsafe_chars_become_underscore(self) -> None:
        result = normalize_stable_key("@#$%^&")
        assert result == "" or all(c == "_" for c in result)

    def test_url_encoded_percent_replaced(self) -> None:
        result = normalize_stable_key("upload/file%20name")
        assert "%" not in result

    def test_legacy_normalizer_remains_available_for_migration(self) -> None:
        assert normalize_stable_key_v1("中文资料.TXT") == ".txt"


class TestStableKeyFromParts:
    def test_source_type_and_filename(self) -> None:
        assert normalize_stable_key_from_parts("upload", "My File.md") == "upload/my_file.md"

    def test_with_relative_path(self) -> None:
        assert (
            normalize_stable_key_from_parts("upload", "file.md", "docs/subdir")
            == "upload/file.md/docs/subdir"
        )

    def test_deterministic(self) -> None:
        assert normalize_stable_key_from_parts(
            "upload", "File.md"
        ) == normalize_stable_key_from_parts("UPLOAD", "file.md")


class TestContentHash:
    def test_known_value_uses_version_prefix(self) -> None:
        text = "Hello, world!"
        expected_input = CONTENT_HASHER_VERSION.encode() + b"\x00" + text.encode()
        expected = hashlib.sha256(expected_input).hexdigest()
        assert compute_content_hash(text) == expected

    def test_different_version_different_hash(self) -> None:
        assert compute_content_hash("same", hasher_version="1.0") != compute_content_hash(
            "same", hasher_version="2.0"
        )

    def test_different_text_different_hash(self) -> None:
        assert compute_content_hash("a") != compute_content_hash("b")

    def test_deterministic(self) -> None:
        assert compute_content_hash("exact") == compute_content_hash("exact")


class TestStorageKey:
    def test_format(self) -> None:
        src = UUID("00000000-0000-0000-0000-000000000001")
        bh = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        key = compute_storage_key(src, bh)
        assert key == f"{src}/ab/{bh}"

    def test_different_source_different_key(self) -> None:
        bh = "a" * 64
        assert compute_storage_key(UUID(int=1), bh) != compute_storage_key(UUID(int=2), bh)

    def test_different_hash_different_key(self) -> None:
        src = UUID(int=1)
        assert compute_storage_key(src, "a" * 64) != compute_storage_key(src, "b" * 64)
