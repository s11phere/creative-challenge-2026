"""BlobStore port for raw file storage.

Defines the domain Protocol that every blob‑storage adapter must satisfy.
Zero external dependencies — the concrete implementation lives in
``infrastructure``.
"""

from __future__ import annotations

from typing import Protocol


class BlobStore(Protocol):
    """Binary large object storage for raw ingested files.

    Implementations must be:
    - **Key‑addressed**: callers supply the storage key (see
      :func:`~domain.fingerprinting.compute_storage_key`).
    - **Safe**: server‑generated keys prevent path‑traversal attacks.
    - **Idempotent**: storing the same data to the same key a second time
      is a no‑op (overwrite is fine).
    - **Consistent**: after a successful ``store()``, ``exists()`` returns
      ``True`` and ``retrieve()`` returns the exact bytes.
    """

    async def store(self, key: str, data: bytes) -> None:
        """Store *data* at *key*.

        Overwrites any existing blob at the same key.
        """
        ...

    async def retrieve(self, key: str) -> bytes | None:
        """Retrieve data at *key*, or ``None`` if the key does not exist."""
        ...

    async def delete(self, key: str) -> None:
        """Delete the blob at *key*.

        No‑op if the key does not exist.
        """
        ...

    async def exists(self, key: str) -> bool:
        """Return ``True`` if a blob exists at *key*."""
        ...

    async def store_and_verify(self, key: str, data: bytes, expected_hash: str) -> None:
        """Store *data* at *key* and verify its SHA-256 matches *expected_hash*.

        Implementations must raise ``ValueError`` if the hash does not
        match, and must leave the storage untouched on failure.
        """
        ...
