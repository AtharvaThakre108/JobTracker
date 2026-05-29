# app/ml/job_matcher.py
# ─────────────────────────────────────────────────────────────────────────────
# Resume ↔ Job Description matching and skill gap detection.
#
# SCORING STRATEGY (weighted blend):
#   60% — BERT semantic similarity (meaning-level match)
#   40% — Skill overlap ratio (exact skill keyword match)
#
# WHY blend both:
#   BERT alone scores "I build web apps" vs "React developer" as high
#   even if the resume has zero React. Skill overlap catches this.
#   Skill overlap alone misses semantic matches like "ML" vs "machine learning".
#   Blending gives the most accurate real-world score.
# ─────────────────────────────────────────────────────────────────────────────

import re
import logging
import numpy as np
from typing import Optional

from app.ml.embeddings import embed, cosine_similarity

logger = logging.getLogger(__name__)

# Weight of each scoring component
BERT_WEIGHT:  float = 0.60
SKILL_WEIGHT: float = 0.40


def score_match(
    resume_text: str,
    resume_skills: list[str],
    job_description: str,
) -> float:
    """
    Score how well a resume matches a job description.

    Args:
        resume_text:   Full resume text (from parsed_data["raw_text"]).
        resume_skills: List of skills extracted from resume.
        job_description: Full job description text.

    Returns:
        float: Match score between 0.0 and 1.0.
               0.75+ = strong match (above typical auto-apply threshold)
               0.50+ = moderate match
               below 0.50 = weak match
    """
    if not resume_text.strip() or not job_description.strip():
        return 0.0

    # ── Component 1: BERT semantic similarity ─────────────────────────────────
    try:
        resume_vec: np.ndarray = embed(resume_text[:2000])   # Cap for speed
        jd_vec:     np.ndarray = embed(job_description[:2000])
        bert_score: float = max(0.0, cosine_similarity(resume_vec, jd_vec))
    except Exception as e:
        logger.warning(f"BERT scoring failed, using 0: {e}")
        bert_score = 0.0

    # ── Component 2: Skill keyword overlap ────────────────────────────────────
    jd_skills:     set[str] = _extract_skills_from_text(job_description)
    resume_skill_set: set[str] = {s.lower() for s in resume_skills}

    if jd_skills:
        matched: set[str] = resume_skill_set & jd_skills
        skill_score: float = len(matched) / len(jd_skills)
        skill_score = min(skill_score, 1.0)   # Cap at 1.0
    else:
        # No skills found in JD — fall back to BERT only
        skill_score = bert_score

    # ── Weighted blend ────────────────────────────────────────────────────────
    final_score: float = (BERT_WEIGHT * bert_score) + (SKILL_WEIGHT * skill_score)

    return round(min(final_score, 1.0), 4)


def rank_jobs(
    resume_text: str,
    resume_skills: list[str],
    jobs: list[dict],
) -> list[dict]:
    """
    Score and rank a list of job listings against a resume.

    Args:
        resume_text:   Full resume text.
        resume_skills: Extracted skills list.
        jobs:          List of job dicts, each with at least a
                       "description" or "title" + "company" key.

    Returns:
        List of jobs sorted by match_score descending,
        with "match_score" added to each job dict.

    Example:
        ranked = rank_jobs(resume_text, skills, scraped_jobs)
        top_matches = [j for j in ranked if j["match_score"] >= 0.75]
    """
    if not jobs:
        return []

    # Pre-embed the resume once — reuse for all job comparisons
    try:
        resume_vec: np.ndarray = embed(resume_text[:2000])
    except Exception:
        resume_vec = np.zeros(384)

    scored_jobs: list[dict] = []

    for job in jobs:
        jd_text: str = job.get("description", "") or (
            f"{job.get('title', '')} {job.get('company', '')} "
            f"{job.get('location', '')}"
        )

        try:
            jd_vec: np.ndarray = embed(jd_text[:2000])
            bert_score: float  = max(0.0, cosine_similarity(resume_vec, jd_vec))

            jd_skills          = _extract_skills_from_text(jd_text)
            resume_skill_set   = {s.lower() for s in resume_skills}
            skill_score: float = (
                len(resume_skill_set & jd_skills) / len(jd_skills)
                if jd_skills else bert_score
            )

            match_score: float = round(
                (BERT_WEIGHT * bert_score) + (SKILL_WEIGHT * min(skill_score, 1.0)),
                4,
            )
        except Exception as e:
            logger.warning(f"Scoring failed for job {job.get('title')}: {e}")
            match_score = 0.0

        scored_jobs.append({**job, "match_score": match_score})

    # Sort highest score first
    return sorted(scored_jobs, key=lambda j: j["match_score"], reverse=True)


def get_skill_gap(
    resume_skills: list[str],
    job_description: str,
) -> dict:
    """
    Identify which skills from the job description are missing from the resume.

    Args:
        resume_skills:   Skills extracted from the resume.
        job_description: Full job description text.

    Returns:
        dict with keys:
            present  — skills in both resume and JD
            missing  — skills in JD but not in resume
            extra    — skills in resume not mentioned in JD
            coverage — % of JD skills covered by resume (0–100)

    Example:
        gap = get_skill_gap(["python", "flask"], "We need React, Python, Docker")
        # → {"present": ["python"], "missing": ["react", "docker"], ...}
    """
    jd_skills:          set[str] = _extract_skills_from_text(job_description)
    resume_skill_set:   set[str] = {s.lower() for s in resume_skills}

    if not jd_skills:
        return {
            "present":  list(resume_skill_set),
            "missing":  [],
            "extra":    [],
            "coverage": 100.0,
        }

    present: set[str] = resume_skill_set & jd_skills
    missing: set[str] = jd_skills - resume_skill_set
    extra:   set[str] = resume_skill_set - jd_skills

    coverage: float = round(len(present) / len(jd_skills) * 100, 1)

    return {
        "present":  sorted(present),
        "missing":  sorted(missing),
        "extra":    sorted(extra),
        "coverage": coverage,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_skills_from_text(text: str) -> set[str]:
    """
    Extract known skills from any text using the same list as the resume parser.
    Returns a set of lowercase skill strings.
    """
    from app.services.resume_parser import KNOWN_SKILLS

    text_lower: str   = text.lower()
    found: set[str]   = set()

    for skill in KNOWN_SKILLS:
        if len(skill.split()) == 1:
            pattern = re.compile(r"\b" + re.escape(skill) + r"\b", re.I)
        else:
            pattern = re.compile(re.escape(skill), re.I)

        if pattern.search(text_lower):
            found.add(skill.lower())

    return found