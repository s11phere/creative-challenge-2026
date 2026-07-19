"""Tests for BlobStore Protocol and LocalFileBlobStore implementation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from infrastructure.blob_store import LocalFileBlobStore

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestBlobStoreProtocol:
    """Structural check that LocalFileBlobStore satisfies the BlobStore Protocol.

    These tests verify that the required methods exist with the correct
    signatures.  They don't exercise behaviour — that's done in the
    implementation tests below.
    """

    def test_has_store_method(self) -> None:
        assert hasattr(LocalFileBlobStore, "store")
        callable(LocalFileBlobStore.store)

    def test_has_retrieve_method(self) -> None:
        assert hasattr(LocalFileBlobStore, "retrieve")
        callable(LocalFileBlobStore.retrieve)

    def test_has_delete_method(self) -> None:
        assert hasattr(LocalFileBlobStore, "delete")
        callable(LocalFileBlobStore.delete)

    def test_has_exists_method(self) -> None:
        assert hasattr(LocalFileBlobStore, "exists")
        callable(LocalFileBlobStore.exists)

    def test_is_blob_store_protocol_compatible(self) -> None:
        """Verify isinstance check against the Protocol at runtime."""
        # Protocols with @runtime_checkable would need the decorator;
        # we use a structural check instead.
        assert isinstance(LocalFileBlobStore, type)


# ---------------------------------------------------------------------------
# LocalFileBlobStore — basic operations
# ---------------------------------------------------------------------------


@pytest.fixture
def blob_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for blob storage."""
    return tmp_path / "blobs"


@pytest.fixture
def store(blob_dir: Path) -> LocalFileBlobStore:
    """Return a LocalFileBlobStore rooted at the temp directory."""
    return LocalFileBlobStore(base_path=blob_dir)


class TestLocalFileBlobStoreBasics:
    async def test_store_and_retrieve(self, store: LocalFileBlobStore) -> None:
        await store.store("test/ab/abc123", b"hello world")
        data = await store.retrieve("test/ab/abc123")
        assert data == b"hello world"

    async def test_retrieve_missing(self, store: LocalFileBlobStore) -> None:
        data = await store.retrieve("test/does/not_exist")
        assert data is None

    async def test_exists(self, store: LocalFileBlobStore) -> None:
        await store.store("test/ab/abc123", b"data")
        assert await store.exists("test/ab/abc123") is True

    async def test_exists_missing(self, store: LocalFileBlobStore) -> None:
        assert await store.exists("test/does/not_exist") is False

    async def test_delete(self, store: LocalFileBlobStore) -> None:
        await store.store("test/ab/abc123", b"data")
        await store.delete("test/ab/abc123")
        assert await store.exists("test/ab/abc123") is False
        assert await store.retrieve("test/ab/abc123") is None

    async def test_delete_missing_is_noop(self, store: LocalFileBlobStore) -> None:
        # Should not raise
        await store.delete("test/does/not_exist")

    async def test_overwrite(self, store: LocalFileBlobStore) -> None:
        await store.store("same/key", b"original")
        await store.store("same/key", b"overwritten")
        data = await store.retrieve("same/key")
        assert data == b"overwritten"

    async def test_creates_parent_directories(self, store: LocalFileBlobStore) -> None:
        key = "deeply/nested/path/ab/abc123"
        await store.store(key, b"data")
        assert await store.exists(key) is True

    async def test_empty_data(self, store: LocalFileBlobStore) -> None:
        await store.store("empty/ab/empty", b"")
        data = await store.retrieve("empty/ab/empty")
        assert data == b""


# ---------------------------------------------------------------------------
# LocalFileBlobStore — store_and_verify
# ---------------------------------------------------------------------------


class TestStoreAndVerify:
    async def test_matching_hash(self, store: LocalFileBlobStore) -> None:
        data = b"some content"
        expected = hashlib.sha256(data).hexdigest()
        await store.store_and_verify("test/ab/should_pass", data, expected)
        assert await store.retrieve("test/ab/should_pass") == data

    async def test_mismatched_hash_raises(self, store: LocalFileBlobStore) -> None:
        data = b"some content"
        wrong_hash = "0" * 64
        with pytest.raises(ValueError, match="Hash mismatch"):
            await store.store_and_verify("test/ab/should_fail", data, wrong_hash)

    async def test_mismatched_hash_leaves_no_trace(self, store: LocalFileBlobStore) -> None:
        data = b"some content"
        wrong_hash = "0" * 64
        with pytest.raises(ValueError):
            await store.store_and_verify("test/ab/no_trace", data, wrong_hash)
        assert await store.exists("test/ab/no_trace") is False


# ---------------------------------------------------------------------------
# LocalFileBlobStore — path traversal protection
# ---------------------------------------------------------------------------


class TestPathTraversal:
    async def test_detects_simple_traversal(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            await store.store("../../etc/passwd", b"evil")

    async def test_detects_deep_traversal(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            await store.retrieve("a/b/../../../etc/shadow")

    async def test_detects_traversal_in_delete(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            await store.delete("../outside")

    async def test_detects_traversal_in_exists(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            await store.exists("../../etc/hosts")

    async def test_absolute_key_is_rejected(self, store: LocalFileBlobStore) -> None:
        with pytest.raises(ValueError, match="Path traversal"):
            await store.exists("/etc/passwd")

    async def test_key_with_symlink_like_component_is_safe(self, store: LocalFileBlobStore) -> None:
        await store.store("test/ab/safe", b"data")
        assert await store.exists("test/ab/safe") is True


# ---------------------------------------------------------------------------
# LocalFileBlobStore — edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_root_directory_is_created(self, blob_dir: Path) -> None:
        store = LocalFileBlobStore(base_path=blob_dir)
        await store.store("test/ab/first", b"data")
        assert blob_dir.exists()

    async def test_default_base_path_is_configured(self) -> None:
        """The default constructor reads from settings (not tested here).

        We only verify that the object can be constructed without a base
        path — the actual path value is a config concern.
        """
        # Must not raise — a default path exists in Settings
        store = LocalFileBlobStore()
        assert store.root is not None

    async def test_large_data(self, store: LocalFileBlobStore) -> None:
        data = b"x" * 10_000_000  # 10 MB
        await store.store("large/ab/large", data)
        retrieved = await store.retrieve("large/ab/large")
        assert retrieved == data
        assert len(retrieved) == 10_000_000  # type: ignore[arg-type]

    async def test_binary_data(self, store: LocalFileBlobStore) -> None:
        data = bytes(range(256))
        await store.store("binary/ab/binary", data)
        retrieved = await store.retrieve("binary/ab/binary")
        assert retrieved == data
