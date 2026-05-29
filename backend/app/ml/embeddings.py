# app/ml/embeddings.py
# ─────────────────────────────────────────────────────────────────────────────
# BERT sentence embeddings — converts text into numerical vectors.
#
# WHY sentence-transformers over plain BERT:
#   Plain BERT produces token embeddings (one vector per word).
#   sentence-transformers produces ONE vector per sentence/paragraph —
#   perfect for comparing whole documents like resumes and job descriptions.
#
# MODEL: all-MiniLM-L6-v2
#   - 384 dimensions (small + fast)
#   - Downloaded automatically on first use (~90MB)
#   - Runs on CPU — no GPU needed
#   - Accuracy good enough for resume matching
#
# CACHING:
#   The model is loaded once at module level and reused across all requests.
#   Loading takes ~3 seconds — we never want that on a per-request basis.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

# ── Module-level model cache ──────────────────────────────────────────────────
# None until first call to get_model() — lazy loaded
_model = None
MODEL_NAME: str = "all-MiniLM-L6-v2"


def get_model():
    """
    Return the sentence transformer model, loading it if not yet cached.

    Returns:
        SentenceTransformer: Loaded model ready for encoding.

    Raises:
        RuntimeError: If the model fails to load.
    """
    global _model

    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading sentence transformer model: {MODEL_NAME}")
            _model = SentenceTransformer(MODEL_NAME)
            logger.info("Model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise RuntimeError(f"Embedding model unavailable: {e}") from e

    return _model


def embed(text: str) -> np.ndarray:
    """
    Convert a text string into a 384-dimensional embedding vector.

    Args:
        text: Any text — resume, job description, skill list.

    Returns:
        np.ndarray: Shape (384,) — the embedding vector.

    Example:
        vec = embed("Python backend developer with Flask experience")
        # → array([-0.023,  0.041, ...])  shape: (384,)
    """
    if not text or not text.strip():
        # Return a zero vector for empty text — safe fallback
        return np.zeros(384)

    model = get_model()

    # encode() returns a numpy array by default
    vector: np.ndarray = model.encode(
        text,
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True,   # L2 normalize → cosine similarity = dot product
    )
    return vector


def embed_batch(texts: list[str]) -> np.ndarray:
    """
    Encode multiple texts at once — faster than calling embed() in a loop.

    Args:
        texts: List of strings to encode.

    Returns:
        np.ndarray: Shape (len(texts), 384)

    Example:
        vecs = embed_batch(["Python dev", "Java dev", "React dev"])
        # → array of shape (3, 384)
    """
    if not texts:
        return np.array([])

    model = get_model()

    return model.encode(
        texts,
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True,
        batch_size=32,   # Process 32 texts at a time — memory efficient
    )


def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """
    Compute cosine similarity between two vectors.

    Since we normalize embeddings on encoding, this is just a dot product.
    Returns a value between -1.0 and 1.0:
        1.0  = identical meaning
        0.0  = unrelated
       -1.0  = opposite meaning (rare in practice)

    Args:
        vec_a: First embedding vector.
        vec_b: Second embedding vector.

    Returns:
        float: Similarity score between -1.0 and 1.0.
    """
    if vec_a.shape != vec_b.shape:
        return 0.0

    # dot product of two normalized vectors = cosine similarity
    return float(np.dot(vec_a, vec_b))  