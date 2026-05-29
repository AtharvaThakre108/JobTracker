# app/services/ats_optimizer.py
# ─────────────────────────────────────────────────────────────────────────────
# ATS (Applicant Tracking System) keyword scoring.
#
# WHY separate from job_matcher.py:
#   job_matcher uses semantic similarity (meaning).
#   ATS optimizers use EXACT keyword matching (literal strings).
#   Real ATS systems are dumb — they look for "React.js" not "React".
#   This service simulates that literal matching.
#
# SCORING:
#   1. Extract top N keywords from JD using TF-IDF importance weighting
#   2. Check how many appear verbatim in the resume
#   3. Return score 0–100 + lists of present/missing keywords
# ─────────────────────────────────────────────────────────────────────────────

import re
import math
import logging
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)

# Words to ignore when extracting keywords
STOP_WORDS: set[str] = {
    "the", "and", "for", "with", "that", "this", "are", "you", "will",
    "have", "has", "from", "our", "your", "their", "they", "team",
    "work", "working", "ability", "experience", "skills", "required",
    "must", "good", "great", "strong", "knowledge", "understanding",
    "well", "also", "role", "looking", "join", "help", "build", "using",
    "use", "new", "can", "all", "more", "its", "one", "not", "but",
}


def compute_ats_score(
    resume_text: str,
    job_description: str,
    top_n: int = 30,
) -> dict:
    """
    Score how well the resume covers the important keywords in the JD.

    Args:
        resume_text:     Full resume text.
        job_description: Full job description text.
        top_n:           Number of top keywords to extract from JD.

    Returns:
        dict with keys:
            score        — 0–100 ATS score
            present      — keywords found in resume [{"keyword", "importance"}]
            missing      — keywords NOT in resume [{"keyword", "importance"}]
            top_keywords — all extracted JD keywords with importance scores
            verdict      — "Excellent" / "Good" / "Fair" / "Poor"
    """
    if not resume_text or not job_description:
        return _empty_result()

    # ── Extract weighted keywords from JD ─────────────────────────────────────
    jd_keywords: list[dict] = _extract_keywords(job_description, top_n)

    if not jd_keywords:
        return _empty_result()

    resume_lower: str = resume_text.lower()

    # ── Check presence in resume ──────────────────────────────────────────────
    present: list[dict] = []
    missing: list[dict] = []

    for kw in jd_keywords:
        keyword: str = kw["keyword"]

        # Whole-word match for single words, substring for phrases
        if len(keyword.split()) == 1:
            found: bool = bool(re.search(r"\b" + re.escape(keyword) + r"\b",
                                          resume_lower))
        else:
            found: bool = keyword in resume_lower

        if found:
            present.append(kw)
        else:
            missing.append(kw)

    # ── Compute weighted score ────────────────────────────────────────────────
    # Weight by importance — missing a high-importance keyword hurts more
    total_importance:   float = sum(kw["importance"] for kw in jd_keywords)
    covered_importance: float = sum(kw["importance"] for kw in present)

    score: float = (covered_importance / total_importance * 100) if total_importance else 0.0
    score = round(min(score, 100.0), 1)

    return {
        "score":        score,
        "present":      present,
        "missing":      missing,
        "top_keywords": jd_keywords,
        "verdict":      _verdict(score),
        "total_checked": len(jd_keywords),
    }


def _extract_keywords(text: str, top_n: int = 30) -> list[dict]:
    """
    Extract the most important keywords from text using TF-IDF-like scoring.

    Strategy:
        1. Tokenize into words and 2-word phrases
        2. Remove stop words
        3. Score by frequency × length_bonus (longer = more specific = more important)
        4. Return top N sorted by score

    Args:
        text:  Input text to extract keywords from.
        top_n: Maximum number of keywords to return.

    Returns:
        List of {"keyword": str, "importance": float} dicts.
    """
    text_lower: str = text.lower()

    # ── Single word tokens ────────────────────────────────────────────────────
    words: list[str] = re.findall(r"\b[a-z][a-z0-9+#.]*\b", text_lower)
    words = [w for w in words if w not in STOP_WORDS and len(w) > 2]

    word_freq: Counter = Counter(words)

    # ── Two-word phrases ──────────────────────────────────────────────────────
    # Bigrams catch "machine learning", "rest api", "team player" etc.
    bigrams: list[str] = [
        f"{words[i]} {words[i+1]}"
        for i in range(len(words) - 1)
        if words[i] not in STOP_WORDS and words[i+1] not in STOP_WORDS
    ]
    bigram_freq: Counter = Counter(bigrams)

    # ── Score each keyword ────────────────────────────────────────────────────
    scored: dict[str, float] = {}

    # Single words — base score is log(freq) to dampen very common words
    for word, freq in word_freq.items():
        scored[word] = math.log(1 + freq) * 1.0

    # Bigrams get a 1.5x bonus — more specific = more important
    for bigram, freq in bigram_freq.items():
        if freq >= 1:
            scored[bigram] = math.log(1 + freq) * 1.5

    # Normalise scores to 0–1 range
    if scored:
        max_score: float = max(scored.values())
        scored = {k: round(v / max_score, 3) for k, v in scored.items()}

    # Return top N by importance
    sorted_keywords = sorted(scored.items(), key=lambda x: x[1], reverse=True)

    return [
        {"keyword": kw, "importance": score}
        for kw, score in sorted_keywords[:top_n]
    ]


def _verdict(score: float) -> str:
    """Map a numeric score to a human-readable verdict."""
    if score >= 80:
        return "Excellent"
    elif score >= 65:
        return "Good"
    elif score >= 45:
        return "Fair"
    else:
        return "Poor"


def _empty_result() -> dict:
    return {
        "score":         0.0,
        "present":       [],
        "missing":       [],
        "top_keywords":  [],
        "verdict":       "Poor",
        "total_checked": 0,
    }