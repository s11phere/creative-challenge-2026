"""Local filesystem BlobStore adapter.

Stores raw ingested file bytes under a configurable root directory.
Storage keys are entirely server‑generated (see
:func:`domain.fingerprinting.compute_storage_key`) so path‑traversal
attacks via caller‑supplied file names are structurally impossible.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from infrastructure.config import settings

logger = logging.getLogger(__name__)


class LocalFileBlobStore:
    """Stores blobs on the local filesystem.

    Layout under *base_path*::

        {base_path}/{source_id}/{blob_hash[:2]}/{blob_hash}

    The base path is read from ``Settings.blob_store_path`` at construction
    time unless explicitly overridden.
    """

    def __init__(self, base_path: str | Path | None = None) -> None:
        self._root = Path(base_path or settings.blob_store_path).resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(self, key: str, data: bytes) -> None:
        """Write *data* to *key*, creating parent directories as needed."""
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def store_and_verify(self, key: str, data: bytes, expected_hash: str) -> None:
        """Write *data* to *key* and verify the SHA-256 matches *expected_hash*.

        Raises ``ValueError`` if the hash does not match, leaving the
        filesystem untouched.
        """
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_hash:
            raise ValueError(
                f"Hash mismatch for key={key!r}: expected {expected_hash}, got {actual}"
            )
        await self.store(key, data)

    async def retrieve(self, key: str) -> bytes | None:
        """Return the bytes at *key*, or ``None`` if missing."""
        path = self._resolve(key)
        if not path.exists():
            return None
        if path.is_dir():
            logger.warning(
                "Blob key %r resolved to directory %s; treating as missing",
                key,
                path,
            )
            return None
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        """Remove the blob at *key*.  No‑op if it does not exist."""
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    async def exists(self, key: str) -> bool:
        """Return ``True`` if a blob exists at *key*."""
        return self._resolve(key).exists()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve(self, key: str) -> Path:
        """Resolve a storage key to an absolute filesystem path.

        Uses ``resolve()`` to detect and reject path‑traversal attempts:
        a key that resolves outside the root directory raises
        ``ValueError``.
        """
        candidate = (self._root / key).resolve()
        if not str(candidate).startswith(str(self._root)):
            raise ValueError(f"Path traversal detected in key={key!r}")
        return candidate

    @property
    def root(self) -> Path:
        """Return the root directory (read‑only)."""
        return self._root
