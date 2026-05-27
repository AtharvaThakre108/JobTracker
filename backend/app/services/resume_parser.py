# app/services/resume_parser.py
# ─────────────────────────────────────────────────────────────────────────────
# Resume text extraction and structured parsing.
#
# PIPELINE:
#   1. Extract raw text  → pdfminer (PDF) or python-docx (DOCX)
#   2. Run spaCy NER     → identify PERSON, ORG, DATE entities
#   3. Pattern matching  → extract email, phone, skills
#   4. Section splitting → split into Experience, Education, Projects
#   5. Return structured dict ready to store in Resume.parsed_data
#
# WHY spaCy over regex alone:
#   Regex finds patterns (email, phone) perfectly.
#   spaCy finds meaning (this word is a person's name, this is an org).
#   We use both — regex for structured fields, spaCy for named entities.
# ─────────────────────────────────────────────────────────────────────────────

import re
import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Skills database ───────────────────────────────────────────────────────────
# Comprehensive list of technical and soft skills to match against.
# Add more as needed — this is a simple O(n) lookup against the resume text.
KNOWN_SKILLS: list[str] = [
    # Languages
    "python", "javascript", "typescript", "java", "c++", "c#", "go", "rust",
    "kotlin", "swift", "ruby", "php", "scala", "r", "matlab", "dart",
    # Web
    "react", "next.js", "vue", "angular", "html", "css", "tailwind",
    "node.js", "express", "fastapi", "flask", "django", "spring boot",
    # Data / ML
    "machine learning", "deep learning", "nlp", "computer vision",
    "tensorflow", "pytorch", "keras", "scikit-learn", "pandas", "numpy",
    "spacy", "hugging face", "bert", "llm", "generative ai",
    # Databases
    "postgresql", "mysql", "mongodb", "redis", "sqlite", "dynamodb",
    "elasticsearch", "cassandra", "firebase",
    # Cloud / DevOps
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "ansible",
    "ci/cd", "github actions", "jenkins", "linux", "bash",
    # Tools
    "git", "github", "jira", "figma", "postman", "graphql", "rest api",
    "microservices", "kafka", "rabbitmq", "celery",
    # Soft skills
    "leadership", "communication", "teamwork", "problem solving",
    "agile", "scrum", "project management",
]

# ── Section header patterns ────────────────────────────────────────────────
# These regex patterns identify section boundaries in a resume.
SECTION_PATTERNS: dict[str, re.Pattern] = {
    "experience":  re.compile(r"(work\s+)?experience|employment|career", re.I),
    "education":   re.compile(r"education|academic|qualification|degree", re.I),
    "skills":      re.compile(r"skills|technologies|tech\s+stack|competencies", re.I),
    "projects":    re.compile(r"projects?|portfolio|work\s+samples?", re.I),
    "summary":     re.compile(r"summary|profile|objective|about\s+me", re.I),
}


def parse_resume(file_bytes: bytes, filename: str) -> dict:
    """
    Main entry point. Extract and structure all data from a resume file.

    Args:
        file_bytes: Raw file bytes (from the uploaded file).
        filename:   Original filename — used to detect PDF vs DOCX.

    Returns:
        dict: Structured resume data with keys:
              name, email, phone, summary, skills,
              experience, education, projects, raw_text
    """
    # ── Step 1: Extract raw text ──────────────────────────────────────────────
    ext: str = filename.lower().split(".")[-1]

    if ext == "pdf":
        raw_text: str = _extract_pdf(file_bytes)
    elif ext in ("doc", "docx"):
        raw_text: str = _extract_docx(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Use PDF or DOCX.")

    if not raw_text.strip():
        raise ValueError("Could not extract any text from the resume file.")

    # ── Step 2: Extract contact info via regex ────────────────────────────────
    email:  Optional[str] = _extract_email(raw_text)
    phone:  Optional[str] = _extract_phone(raw_text)

    # ── Step 3: Extract name via spaCy ────────────────────────────────────────
    name: Optional[str] = _extract_name(raw_text)

    # ── Step 4: Extract skills ────────────────────────────────────────────────
    skills: list[str] = _extract_skills(raw_text)

    # ── Step 5: Split into sections ───────────────────────────────────────────
    sections: dict = _split_sections(raw_text)

    # ── Step 6: Parse each section ────────────────────────────────────────────
    summary:    str        = _parse_summary(sections.get("summary", ""))
    experience: list[dict] = _parse_experience(sections.get("experience", ""))
    education:  list[dict] = _parse_education(sections.get("education", ""))
    projects:   list[dict] = _parse_projects(sections.get("projects", ""))

    return {
        "name":       name,
        "email":      email,
        "phone":      phone,
        "summary":    summary,
        "skills":     skills,
        "experience": experience,
        "education":  education,
        "projects":   projects,
        "raw_text":   raw_text[:5000],   # Store first 5000 chars for ML use
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Text extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdf(file_bytes: bytes) -> str:
    """
    Extract all text from a PDF using pdfminer.six.
    Handles multi-page, text-based PDFs.
    Does NOT handle scanned/image PDFs (OCR not in scope for Phase 8).
    """
    from pdfminer.high_level import extract_text as pdf_extract

    try:
        buf = io.BytesIO(file_bytes)
        text: str = pdf_extract(buf)
        return text or ""
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


def _extract_docx(file_bytes: bytes) -> str:
    """
    Extract all text from a DOCX file using python-docx.
    Reads all paragraphs in order.
    """
    import docx

    try:
        buf = io.BytesIO(file_bytes)
        doc = docx.Document(buf)
        lines: list[str] = [para.text for para in doc.paragraphs if para.text.strip()]
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"DOCX extraction failed: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Contact info extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_email(text: str) -> Optional[str]:
    """Extract the first email address found in the text."""
    pattern = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
    match = pattern.search(text)
    return match.group(0).lower() if match else None


def _extract_phone(text: str) -> Optional[str]:
    """
    Extract an Indian or international phone number.
    Handles formats: +91-9876543210, 9876543210, (91) 98765 43210
    """
    pattern = re.compile(
        r"(\+?91[\s\-]?)?[6-9]\d{9}"          # Indian mobile
        r"|(\+?[1-9]\d{0,3}[\s\-]?)?"          # International prefix
        r"\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}" # Standard 10-digit
    )
    match = pattern.search(text)
    if match:
        # Clean the number — remove spaces, dashes, brackets
        return re.sub(r"[\s\-\(\)]", "", match.group(0))
    return None


def _extract_name(text: str) -> Optional[str]:
    """
    Use spaCy NER to find the candidate's name.
    Strategy: find the first PERSON entity in the first 500 characters
    (names almost always appear at the top of a resume).
    """
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm")
        # Only process the top of the resume for speed
        doc = nlp(text[:500])
        for ent in doc.ents:
            if ent.label_ == "PERSON":
                return ent.text.strip()
    except Exception as e:
        logger.warning(f"spaCy name extraction failed: {e}")

    # Fallback: assume first non-empty line is the name
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        first_line: str = lines[0]
        # Only use if it looks like a name (2-4 words, no special chars)
        words = first_line.split()
        if 1 < len(words) <= 4 and all(w.isalpha() for w in words):
            return first_line

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Skills extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_skills(text: str) -> list[str]:
    """
    Match the resume text against our KNOWN_SKILLS list.
    Case-insensitive whole-word matching.

    Returns deduplicated list of matched skills, sorted alphabetically.
    """
    text_lower: str = text.lower()
    found: list[str] = []

    for skill in KNOWN_SKILLS:
        # Use word boundary for single-word skills to avoid partial matches
        # e.g. "r" should not match inside "react"
        if len(skill.split()) == 1:
            pattern = re.compile(r"\b" + re.escape(skill) + r"\b", re.I)
        else:
            pattern = re.compile(re.escape(skill), re.I)

        if pattern.search(text_lower):
            # Store in original casing from our list
            found.append(skill)

    return sorted(set(found))


# ─────────────────────────────────────────────────────────────────────────────
#  Section splitting
# ─────────────────────────────────────────────────────────────────────────────

def _split_sections(text: str) -> dict[str, str]:
    """
    Split raw resume text into labelled sections.

    Strategy:
        1. Walk through lines
        2. When a line matches a section header pattern, start a new section
        3. Accumulate lines until the next section header

    Returns dict like:
        {"experience": "...", "education": "...", "skills": "...", ...}
    """
    lines: list[str] = text.split("\n")
    sections: dict[str, list[str]] = defaultdict(list)
    current_section: str = "header"

    for line in lines:
        stripped: str = line.strip()
        if not stripped:
            continue

        # Check if this line is a section header
        matched_section: Optional[str] = None
        for section_name, pattern in SECTION_PATTERNS.items():
            # Only treat short lines as headers (headers are rarely > 5 words)
            if pattern.search(stripped) and len(stripped.split()) <= 5:
                matched_section = section_name
                break

        if matched_section:
            current_section = matched_section
        else:
            sections[current_section].append(stripped)

    # Join each section's lines back into a single string
    return {k: "\n".join(v) for k, v in sections.items()}


# ─────────────────────────────────────────────────────────────────────────────
#  Section parsers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_summary(text: str) -> str:
    """Return the summary/objective section as a cleaned string."""
    return " ".join(text.split())[:1000]   # Clean whitespace, cap at 1000 chars


def _parse_experience(text: str) -> list[dict]:
    """
    Parse the experience section into a list of job entries.

    Each entry: {title, company, duration, description}

    Strategy: Split on date patterns which typically start a new job entry.
    """
    if not text.strip():
        return []

    entries: list[dict] = []

    # Split on lines that look like job titles or date ranges
    date_pattern = re.compile(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|"
        r"march|april|june|july|august|september|october|november|december)"
        r"[\s,]*\d{4}", re.I
    )

    lines: list[str] = [l.strip() for l in text.split("\n") if l.strip()]
    current: dict = {}
    description_lines: list[str] = []

    for line in lines:
        if date_pattern.search(line):
            # Save previous entry
            if current:
                current["description"] = " ".join(description_lines)[:500]
                entries.append(current)
            current = {
                "title":       "",
                "company":     "",
                "duration":    line,
                "description": "",
            }
            description_lines = []
        elif current:
            # First line after a date is usually the title
            if not current["title"]:
                current["title"] = line
            elif not current["company"]:
                current["company"] = line
            else:
                description_lines.append(line)
        else:
            # No date found yet — treat as description
            description_lines.append(line)

    # Don't forget the last entry
    if current:
        current["description"] = " ".join(description_lines)[:500]
        entries.append(current)

    return entries[:10]   # Cap at 10 entries


def _parse_education(text: str) -> list[dict]:
    """
    Parse the education section into a list of degree entries.

    Each entry: {degree, institution, year}
    """
    if not text.strip():
        return []

    entries: list[dict] = []
    lines: list[str] = [l.strip() for l in text.split("\n") if l.strip()]

    year_pattern = re.compile(r"\b(19|20)\d{2}\b")
    degree_keywords = re.compile(
        r"b\.?tech|m\.?tech|b\.?e|m\.?e|b\.?sc|m\.?sc|bca|mca|"
        r"bachelor|master|phd|diploma|b\.?com|mba", re.I
    )

    current: dict = {}
    for line in lines:
        if degree_keywords.search(line):
            if current:
                entries.append(current)
            current = {"degree": line, "institution": "", "year": ""}
        elif current:
            year_match = year_pattern.search(line)
            if year_match and not current["year"]:
                current["year"] = year_match.group(0)
            elif not current["institution"]:
                current["institution"] = line

    if current:
        entries.append(current)

    return entries[:5]


def _parse_projects(text: str) -> list[dict]:
    """
    Parse the projects section.

    Each entry: {name, description, tech}
    """
    if not text.strip():
        return []

    entries: list[dict] = []
    lines: list[str] = [l.strip() for l in text.split("\n") if l.strip()]

    current: dict = {}
    description_lines: list[str] = []

    for line in lines:
        # New project if line is short (likely a title) and previous entry exists
        if len(line.split()) <= 6 and current:
            current["description"] = " ".join(description_lines)[:300]
            current["tech"] = _extract_skills(" ".join(description_lines))
            entries.append(current)
            current = {"name": line, "description": "", "tech": []}
            description_lines = []
        elif not current:
            current = {"name": line, "description": "", "tech": []}
            description_lines = []
        else:
            description_lines.append(line)

    if current:
        current["description"] = " ".join(description_lines)[:300]
        current["tech"] = _extract_skills(" ".join(description_lines))
        entries.append(current)

    return entries[:8]