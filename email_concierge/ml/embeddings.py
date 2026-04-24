"""Sentence embeddings with on-disk cache.

Stage 3's classifier runs over sentence-transformers embeddings of
`sender + subject + body_preview`. Embedding the same training row on
every `train` invocation is wasteful; we cache per-text SHA-1 → float32
vector in a SQLite file kept next to the model artifacts.

Graceful degradation: if sentence-transformers isn't installed (the `ml`
extras are optional), `Embedder.available()` returns False. Stage 3
detects this at startup and returns None from `can_handle`, so the
pipeline falls straight through to stage 4.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from email_concierge.log import get_logger

if TYPE_CHECKING:
    import numpy as np

log = get_logger(__name__)

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_EMBEDDING_DIM = 384  # MiniLM-L6 default; sanity check on load


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class EmbeddingCache:
    """SQLite-backed cache: text hash → raw float32 bytes."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash TEXT PRIMARY KEY,
                model     TEXT NOT NULL,
                dim       INTEGER NOT NULL,
                vector    BLOB NOT NULL
            )
            """
        )

    def get(self, text_hash: str, model: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT vector FROM embedding_cache WHERE text_hash = ? AND model = ?",
            (text_hash, model),
        ).fetchone()
        return row[0] if row else None

    def put(self, text_hash: str, model: str, dim: int, vector: bytes) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO embedding_cache (text_hash, model, dim, vector)
            VALUES (?, ?, ?, ?)
            """,
            (text_hash, model, dim, vector),
        )

    def close(self) -> None:
        self._conn.close()


class Embedder:
    """Wraps sentence-transformers with a disk-backed cache.

    Split into `available()` / `encode()` so callers can detect missing
    optional deps without a try/except cluttering the hot path.
    """

    def __init__(
        self,
        *,
        model_name: str = _DEFAULT_MODEL,
        cache_path: Path | None = None,
        model: Any = None,
    ) -> None:
        self._model_name = model_name
        self._model = model  # dependency injection for tests
        self._cache = EmbeddingCache(cache_path) if cache_path else None

    @staticmethod
    def available() -> bool:
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers is not installed. "
                "Install with: pip install -e '.[ml]'"
            ) from e
        log.info("embedding_model_loading", model=self._model_name)
        self._model = SentenceTransformer(self._model_name)
        return self._model

    def encode(self, texts: list[str]) -> np.ndarray:
        """Return an (N, dim) float32 array. Cache hits avoid the model load."""
        import numpy as np

        if not texts:
            return np.zeros((0, _EMBEDDING_DIM), dtype=np.float32)

        results: list[np.ndarray | None] = [None] * len(texts)
        misses: list[tuple[int, str, str]] = []  # (index, text, hash)

        for i, text in enumerate(texts):
            h = _sha1(text)
            if self._cache is not None:
                cached = self._cache.get(h, self._model_name)
                if cached is not None:
                    results[i] = np.frombuffer(cached, dtype=np.float32)
                    continue
            misses.append((i, text, h))

        if misses:
            model = self._ensure_model()
            miss_texts = [t for _, t, _ in misses]
            log.debug("embedding_encode", n=len(miss_texts), cached=len(texts) - len(misses))
            vectors = model.encode(miss_texts, convert_to_numpy=True, show_progress_bar=False)
            # Normalize to float32 regardless of model default for byte-stable caching.
            vectors = np.asarray(vectors, dtype=np.float32)
            for (i, _text, h), vec in zip(misses, vectors, strict=True):
                results[i] = vec
                if self._cache is not None:
                    self._cache.put(h, self._model_name, int(vec.shape[0]), vec.tobytes())

        # Every slot filled by now.
        stacked = np.stack([r for r in results if r is not None])
        return stacked
