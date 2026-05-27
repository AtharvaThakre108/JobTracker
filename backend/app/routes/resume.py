# app/routes/resume.py
# ─────────────────────────────────────────────────────────────────────────────
# Resume management endpoints.
#
# ENDPOINTS:
#   POST   /api/resume/upload        — upload + parse a new resume
#   GET    /api/resume/versions      — list all resume versions
#   GET    /api/resume/active        — get parsed data of active resume
#   PUT    /api/resume/<id>/activate — set a version as active
#   DELETE /api/resume/<id>          — delete a version
# ─────────────────────────────────────────────────────────────────────────────

import os
import uuid
import logging
from typing import Optional

from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename

from app.extensions import db, limiter
from app.models import Resume, User
from app.utils import success, error, log_action

logger = logging.getLogger(__name__)

resume_bp = Blueprint("resume", __name__, url_prefix="/api/resume")

# Allowed file extensions
ALLOWED_EXTENSIONS: set[str] = {"pdf", "docx", "doc"}


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/resume/upload
# ─────────────────────────────────────────────────────────────────────────────

@resume_bp.route("/upload", methods=["POST"])
@jwt_required()
@limiter.limit("20 per day")
def upload_resume():
    """
    Upload a resume file, parse it, and store the structured data.

    Form data:
        file  — PDF or DOCX file (required)
        label — optional version label e.g. "ML Focus Resume"

    Flow:
        1. Validate file type and size
        2. Parse with resume_parser.py
        3. Upload original file to S3 (or local storage in dev)
        4. Save Resume row to DB
        5. If this is the user's first resume, auto-activate it
    """
    user_id: str = get_jwt_identity()

    # ── Validate file presence ────────────────────────────────────────────────
    if "file" not in request.files:
        return error("No file provided. Send the file as form-data with key 'file'.", 400)

    file = request.files["file"]

    if not file.filename:
        return error("File has no name.", 400)

    # ── Validate extension ────────────────────────────────────────────────────
    ext: str = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return error(f"Invalid file type '{ext}'. Only PDF and DOCX are accepted.", 422)

    # ── Validate size ─────────────────────────────────────────────────────────
    file_bytes: bytes = file.read()
    max_bytes: int    = current_app.config.get("MAX_UPLOAD_SIZE_BYTES", 10 * 1024 * 1024)

    if len(file_bytes) > max_bytes:
        return error(f"File too large. Maximum size is {max_bytes // 1024 // 1024}MB.", 413)

    if len(file_bytes) == 0:
        return error("File is empty.", 422)

    # ── Parse the resume ──────────────────────────────────────────────────────
    try:
        from app.services.resume_parser import parse_resume
        parsed: dict = parse_resume(file_bytes, file.filename)
    except ValueError as e:
        return error(str(e), 422)
    except Exception as e:
        logger.error(f"Resume parsing failed for user {user_id}: {e}")
        return error("Failed to parse resume. Please check the file and try again.", 500)

    # ── Store file (S3 or local) ───────────────────────────────────────────────
    safe_name: str  = secure_filename(file.filename)
    unique_name: str = f"{user_id}/{uuid.uuid4()}/{safe_name}"
    file_url: str   = _store_file(file_bytes, unique_name, ext)

    # ── Determine version number ──────────────────────────────────────────────
    existing_count: int = Resume.query.filter_by(user_id=user_id).count()
    version_number: int = existing_count + 1

    # ── Check if this should be auto-activated ────────────────────────────────
    # Auto-activate if it's the user's first resume OR no active resume exists
    has_active: bool = Resume.query.filter_by(
        user_id=user_id, is_active=True
    ).first() is not None

    should_activate: bool = not has_active

    # If activating, deactivate all others first
    if should_activate:
        Resume.query.filter_by(user_id=user_id).update({"is_active": False})

    # ── Save to DB ────────────────────────────────────────────────────────────
    resume = Resume(
        user_id=user_id,
        version_number=version_number,
        label=request.form.get("label", f"Version {version_number}"),
        file_url=file_url,
        file_name=safe_name,
        parsed_data=parsed,
        skills=parsed.get("skills", []),
        is_active=should_activate,
    )
    db.session.add(resume)

    # ── Update user settings with detected skills (for job matching) ──────────
    user: Optional[User] = db.session.get(User, user_id)
    if user and user.settings and parsed.get("skills"):
        # Merge new skills with existing target roles if applicable
        pass   # ML matching uses resume.skills directly — no settings update needed

    db.session.commit()

    log_action(
        "resume.uploaded",
        entity_type="resume",
        entity_id=resume.id,
        user_id=user_id,
        new_value={
            "version": version_number,
            "skills_found": len(parsed.get("skills", [])),
            "is_active": should_activate,
        },
    )

    return success(
        data={"resume": _serialize(resume)},
        message=f"Resume parsed successfully. Found {len(parsed.get('skills', []))} skills.",
        status_code=201,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/resume/versions
# ─────────────────────────────────────────────────────────────────────────────

@resume_bp.route("/versions", methods=["GET"])
@jwt_required()
def list_versions():
    """
    Return all resume versions for the current user, newest first.
    """
    user_id: str = get_jwt_identity()

    resumes = Resume.query\
        .filter_by(user_id=user_id)\
        .order_by(Resume.created_at.desc())\
        .all()

    return success(data={
        "versions": [_serialize(r) for r in resumes],
        "total":    len(resumes),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/resume/active
# ─────────────────────────────────────────────────────────────────────────────

@resume_bp.route("/active", methods=["GET"])
@jwt_required()
def get_active_resume():
    """
    Return the full parsed data of the currently active resume.
    Used by the frontend to show the resume detail view and
    by the ML layer to get skills for matching.
    """
    user_id: str = get_jwt_identity()

    resume: Optional[Resume] = Resume.query.filter_by(
        user_id=user_id,
        is_active=True,
    ).first()

    if not resume:
        return error("No active resume found. Please upload a resume first.", 404)

    return success(data={"resume": _serialize(resume, full=True)})


# ─────────────────────────────────────────────────────────────────────────────
#  PUT /api/resume/<id>/activate
# ─────────────────────────────────────────────────────────────────────────────

@resume_bp.route("/<string:resume_id>/activate", methods=["PUT"])
@jwt_required()
def activate_resume(resume_id: str):
    """
    Set a specific resume version as the active one.
    Deactivates all other versions first.

    The active resume is used for:
        - Job matching scores
        - Auto-fill in the apply bot
        - ATS keyword analysis
    """
    user_id: str = get_jwt_identity()

    resume: Optional[Resume] = Resume.query.filter_by(
        id=resume_id,
        user_id=user_id,
    ).first()

    if not resume:
        return error("Resume not found.", 404)

    if resume.is_active:
        return success(
            data={"resume": _serialize(resume)},
            message="This resume is already active.",
        )

    # Deactivate all, then activate this one
    Resume.query.filter_by(user_id=user_id).update({"is_active": False})
    resume.is_active = True
    db.session.commit()

    log_action(
        "resume.activated",
        entity_type="resume",
        entity_id=resume.id,
        user_id=user_id,
        new_value={"version": resume.version_number, "label": resume.label},
    )

    return success(
        data={"resume": _serialize(resume)},
        message=f"'{resume.label}' is now your active resume.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE /api/resume/<id>
# ─────────────────────────────────────────────────────────────────────────────

@resume_bp.route("/<string:resume_id>", methods=["DELETE"])
@jwt_required()
def delete_resume(resume_id: str):
    """
    Delete a resume version.
    Cannot delete the active resume if it's the only one.
    If deleting the active resume and others exist,
    auto-activates the most recent remaining version.
    """
    user_id: str = get_jwt_identity()

    resume: Optional[Resume] = Resume.query.filter_by(
        id=resume_id,
        user_id=user_id,
    ).first()

    if not resume:
        return error("Resume not found.", 404)

    total: int = Resume.query.filter_by(user_id=user_id).count()

    # Prevent deleting the only resume
    if total == 1:
        return error(
            "Cannot delete your only resume. Upload a new version first.", 400
        )

    was_active: bool = resume.is_active

    db.session.delete(resume)
    db.session.flush()

    # If we deleted the active one, auto-activate the most recent remaining
    if was_active:
        next_resume: Optional[Resume] = Resume.query\
            .filter_by(user_id=user_id)\
            .order_by(Resume.created_at.desc())\
            .first()
        if next_resume:
            next_resume.is_active = True

    db.session.commit()

    log_action(
        "resume.deleted",
        entity_type="resume",
        entity_id=resume_id,
        user_id=user_id,
    )

    return success(message="Resume deleted.")


# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _store_file(file_bytes: bytes, unique_name: str, ext: str) -> str:
    """
    Store the file either in AWS S3 (production) or local temp folder (dev).

    Returns:
        str: URL or path where the file was stored.
    """
    from flask import current_app

    aws_key: str = current_app.config.get("AWS_ACCESS_KEY_ID", "")

    if aws_key:
        # ── S3 storage ────────────────────────────────────────────────────────
        return _upload_to_s3(file_bytes, unique_name)
    else:
        # ── Local storage (dev only) ──────────────────────────────────────────
        # Store in backend/uploads/ — gitignored
        upload_dir: str = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "uploads"
        )
        os.makedirs(upload_dir, exist_ok=True)

        # Flatten the path for local storage
        local_name: str = unique_name.replace("/", "_")
        local_path: str = os.path.join(upload_dir, local_name)

        with open(local_path, "wb") as f:
            f.write(file_bytes)

        return f"local://uploads/{local_name}"


def _upload_to_s3(file_bytes: bytes, key: str) -> str:
    """
    Upload a file to AWS S3 and return its public URL.
    Only called when AWS credentials are present in config.
    """
    import boto3
    from botocore.exceptions import BotoCoreError

    cfg = current_app.config

    try:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"],
            region_name=cfg["AWS_REGION"],
        )
        s3.put_object(
            Bucket=cfg["S3_BUCKET_NAME"],
            Key=key,
            Body=file_bytes,
            ContentType="application/octet-stream",
        )
        return f"https://{cfg['S3_BUCKET_NAME']}.s3.{cfg['AWS_REGION']}.amazonaws.com/{key}"

    except BotoCoreError as e:
        logger.error(f"S3 upload failed: {e}")
        raise RuntimeError("File storage failed.") from e


def _serialize(resume: Resume, full: bool = False) -> dict:
    """
    Convert a Resume model instance to a safe API dict.

    Args:
        resume: The Resume instance.
        full:   If True, includes the full parsed_data JSON.
                If False, only returns metadata (faster for list views).
    """
    data: dict = {
        "id":             resume.id,
        "version_number": resume.version_number,
        "label":          resume.label,
        "file_name":      resume.file_name,
        "is_active":      resume.is_active,
        "skills":         resume.skills or [],
        "skills_count":   len(resume.skills or []),
        "created_at":     resume.created_at.isoformat() if resume.created_at else None,
    }

    if full:
        data["parsed_data"] = resume.parsed_data

    return data