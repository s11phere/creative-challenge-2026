"""Content fingerprinting: stable_key, blob_hash, content_hash.

All functions are pure, deterministic, and have zero external dependencies.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

STABLE_KEY_NORMALIZER_VERSION = "1.0"
"""Version of the stable_key normalisation algorithm.

Bump this when the normalisation rules change so that the same URI produces
a different stable key, forcing re-identity checks.
"""

CONTENT_HASHER_VERSION = "1.0"
"""Version of the content hash algorithm.

Bump this when the content hash input changes (e.g. different normalisation
applied before hashing) so that existing hashes are not confused with new
ones.
"""

# ---------------------------------------------------------------------------
# Stable-key normalisation
# ---------------------------------------------------------------------------

# Characters that are valid in a stable key (alphanumeric plus a few safe
# punctuation characters).  Everything else is replaced with underscores.
_SAFE_KEY_PATTERN = re.compile(r"[^a-zA-Z0-9._~/-]")


def normalize_stable_key(uri: str) -> str:
    """Normalise a source URI into a stable, comparable key.

    Rules
    -----
    - Lowercases the entire URI.
    - Collapses consecutive slashes (``//`` → ``/``).
    - Strips leading and trailing whitespace *and* slashes.
    - Replaces every character outside ``[a-zA-Z0-9._~-]`` with ``_``.
    - Collapses consecutive underscores.
    - Strips leading and trailing underscores.

    The result is deterministic and survives cosmetic URI differences
    (trailing slashes, mixed case, doubled slashes, URL‑encoded characters).
    """
    key = uri.strip().lower()
    # Collapse multiple slashes
    key = re.sub(r"/{2,}", "/", key)
    # Strip leading/trailing slashes
    key = key.strip("/")
    # Replace unsafe characters
    key = _SAFE_KEY_PATTERN.sub("_", key)
    # Collapse consecutive underscores
    key = re.sub(r"_+", "_", key)
    # Strip leading/trailing underscores
    key = key.strip("_")
    return key


def normalize_stable_key_from_parts(
    source_type: str,
    file_name: str,
    relative_path: str | None = None,
) -> str:
    """Build and normalise a stable key from logical parts.

    Convenience helper for callers that have structured metadata rather
    than a raw URI.
    """
    parts = [source_type, file_name]
    if relative_path:
        parts.append(relative_path)
    return normalize_stable_key("/".join(parts))


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------

_NORMALIZER_SEPARATOR = b"\x00"


def compute_content_hash(
    normalized_text: str,
    hasher_version: str = CONTENT_HASHER_VERSION,
) -> str:
    """Return the lowercase hex SHA-256 of normalised text content.

    The hash input is ``version + NUL + normalized_text`` so that a change
    to the hasher algorithm (bumping *hasher_version*) produces a different
    hash even for the same text, preventing silent cross‑version reuse.

    Parameters
    ----------
    normalized_text:
        The text *after* NORMALIZE processing (not the raw file bytes).
    hasher_version:
        Version of the hasher algorithm.  Change this when the hashing
        input changes, not when the normaliser changes — the normaliser
        version is tracked separately in ``DocumentVersion.normalizer_version``.
    """
    raw = hasher_version.encode("utf-8") + _NORMALIZER_SEPARATOR + normalized_text.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Blob-storage key derivation
# ---------------------------------------------------------------------------


def compute_storage_key(source_id: UUID, blob_hash: str) -> str:
    """Generate a server‑side storage key for a blob.

    Format: ``{source_id}/{blob_hash[:2]}/{blob_hash}``

    Using the source ID as the top‑level directory prevents one source from
    seeing another's files.  The two‑character prefix distributes blobs
    across sub‑directories so no single directory holds more than ~65k
    entries even with many millions of blobs.  The key is entirely
    server‑generated — caller‑supplied file names are never used so that
    path‑traversal attacks are structurally impossible.
    """
    return f"{source_id}/{blob_hash[:2]}/{blob_hash}"
