"""Tests for LocalFileBlobStore implementation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from infrastructure.blob_store import LocalFileBlobStore


@pytest.fixture
def store(tmp_path: Path) -> LocalFileBlobStore:
    return LocalFileBlobStore(base_path=tmp_path / "blobs")


class TestBasicOps:
    async def test_store_and_retrieve(self, store: LocalFileBlobStore) -> None:
        await store.store("test/ab/key", b"hello")
        assert await store.retrieve("test/ab/key") == b"hello"

    async def test_retrieve_missing(self, store: LocalFileBlobStore) -> None:
        assert await store.retrieve("no/such/key") is None

    async def test_exists(self, store: LocalFileBlobStore) -> None:
        await store.store("k/ab/k", b"x")
        assert await store.exists("k/ab/k") is True
        assert await store.exists("no/such") is False

    async def test_delete(self, store: LocalFileBlobStore) -> None:
        await store.store("k/ab/k", b"x")
        await store.delete("k/ab/k")
        assert await store.exists("k/ab/k") is False

    async def test_delete_missing_is_noop(self, store: LocalFileBlobStore) -> None:
        await store.delete("does/not/exist")  # should not raise

    async def test_overwrite(self, store: LocalFileBlobStore) -> None:
        await store.store("k/ab/k", b"a")
        await store.store("k/ab/k", b"b")
        assert await store.retrieve("k/ab/k") == b"b"


class TestStoreAndVerify:
    async def test_matching_hash(self, store: LocalFileBlobStore) -> None:
        data = b"content"
        h = hashlib.sha256(data).hexdigest()
        await store.store_and_verify("k/ab/k", data, h)
        assert await store.retrieve("k/ab/k") == data

    async def test_mismatched_hash_raises(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Hash mismatch"):
            await store.store_and_verify("k/ab/k", b"x", "0" * 64)

    async def test_mismatched_hash_leaves_no_trace(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError):
            await store.store_and_verify("k/ab/k", b"x", "0" * 64)
        assert await store.exists("k/ab/k") is False


class TestPathTraversal:
    @pytest.mark.parametrize(
        "method_and_args",
        [
            ("store", ("../../etc/passwd", b"evil")),
            ("retrieve", ("a/b/../../../etc/shadow",)),
            ("delete", ("../outside",)),
            ("exists", ("../../etc/hosts",)),
        ],
    )
    async def test_traversal_rejected(self, store: LocalFileBlobStore, method_and_args) -> None:
        method, args = method_and_args
        with pytest.raises(ValueError, match="Path traversal"):
            await getattr(store, method)(*args)

    async def test_absolute_key_rejected(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            await store.exists("/etc/passwd")


class TestEdgeCases:
    async def test_empty_data(self, store: LocalFileBlobStore) -> None:
        await store.store("e/ab/e", b"")
        assert await store.retrieve("e/ab/e") == b""

    async def test_binary_data(self, store: LocalFileBlobStore) -> None:
        data = bytes(range(256))
        await store.store("b/ab/b", data)
        assert await store.retrieve("b/ab/b") == data

    async def test_default_construction_does_not_raise(self) -> None:
        store = LocalFileBlobStore()
        assert store.root is not None
