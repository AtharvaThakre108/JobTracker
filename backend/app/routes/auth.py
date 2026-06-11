# app/routes/auth.py
# ─────────────────────────────────────────────────────────────────────────────
# Authentication routes.
#
# ENDPOINTS:
#   POST   /api/auth/register           — create account
#   POST   /api/auth/login              — email + password login
#   POST   /api/auth/refresh            — get new access token
#   POST   /api/auth/logout             — revoke current token
#   GET    /api/auth/me                 — get current user profile
#   GET    /api/auth/verify/<token>     — verify email address
#   POST   /api/auth/2fa/setup          — generate QR code
#   POST   /api/auth/2fa/confirm        — activate 2FA + get backup codes
#   POST   /api/auth/2fa/verify         — complete login when 2FA is enabled
#   POST   /api/auth/2fa/disable        — turn off 2FA
#   GET    /api/auth/linkedin           — start LinkedIn OAuth flow
#   GET    /api/auth/linkedin/callback  — LinkedIn OAuth callback
# ─────────────────────────────────────────────────────────────────────────────

import io
import base64
from datetime import datetime, timezone
from typing import Optional

import bcrypt
import pyotp
import qrcode
import requests

from flask import Blueprint, request, redirect, current_app
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
)

from app.extensions import db, limiter
from app.models import User, UserSettings, BackupCode
from app.utils import success, error, encrypt, decrypt, log_action
from app.utils.tokens import generate_token, generate_inbound_token, generate_backup_codes


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


# ─────────────────────────────────────────────────────────────────────────────
#  Token blocklist — in-memory for dev, swap for Redis in production
# ─────────────────────────────────────────────────────────────────────────────
#  WHY: JWT tokens can't be invalidated server-side by default (they're
#  stateless). We maintain a blocklist of revoked JTIs (JWT IDs) so
#  logout actually works. In production this set lives in Redis so it
#  persists across server restarts and multiple instances.

_blocklist: set = set()


def block_token(jti: str) -> None:
    """Add a token JTI to the blocklist — called on logout."""
    _blocklist.add(jti)


def is_token_blocked(jti: str) -> bool:
    """Called by the app factory on every protected request."""
    return jti in _blocklist


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/register
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["POST"])
@limiter.limit("10 per hour")   # Prevent mass account creation from one IP
def register():
    """
    Create a new user account.

    Body: { "name": str, "email": str, "password": str }

    Returns access + refresh tokens so the user is logged in immediately
    after registering — no separate login step needed.
    """
    data: dict = request.get_json(silent=True) or {}

    name: str     = (data.get("name")     or "").strip()
    email: str    = (data.get("email")    or "").strip().lower()
    password: str = (data.get("password") or "")

    # ── Validate ──────────────────────────────────────────────────────────────
    field_errors: dict = {}

    if not name:
        field_errors["name"] = "Name is required."
    if not email or "@" not in email:
        field_errors["email"] = "A valid email address is required."
    if len(password) < 8:
        field_errors["password"] = "Password must be at least 8 characters."

    if field_errors:
        return error("Validation failed.", 422, field_errors)

    # ── Check duplicate ───────────────────────────────────────────────────────
    if User.query.filter_by(email=email).first():
        return error("An account with this email already exists.", 409)

    # ── Hash password ─────────────────────────────────────────────────────────
    # bcrypt automatically salts the hash — never store plain passwords
    pw_hash: str = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    # ── Create user + default settings ───────────────────────────────────────
    user = User(
        name=name,
        email=email,
        password_hash=pw_hash,
        verification_token=generate_token(),
        inbound_email_token=generate_inbound_token(),
    )
    db.session.add(user)
    db.session.flush()   # flush to get user.id before creating settings

    db.session.add(UserSettings(user_id=user.id))
    db.session.commit()

    log_action("auth.register", entity_type="user", entity_id=user.id, user_id=user.id)

    from app.tasks.email_tasks import send_verification_email
    send_verification_email.delay(user.id)

    return success(
        data={
            "user": _serialize_user(user),
            "access_token": create_access_token(identity=user.id),
            "refresh_token": create_refresh_token(identity=user.id),
        },
        message="Account created successfully.",
        status_code=201,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/login
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
@limiter.limit("20 per hour")   # Brute force protection
def login():
    """
    Authenticate with email + password.

    Body: { "email": str, "password": str }

    If the user has 2FA enabled, returns requires_2fa: true and a
    short-lived pre_token instead of full tokens. The client must then
    call /2fa/verify with that pre_token + TOTP code.
    """
    data: dict = request.get_json(silent=True) or {}

    email: str    = (data.get("email")    or "").strip().lower()
    password: str = (data.get("password") or "")

    user: Optional[User] = User.query.filter_by(email=email).first()

    # ── Validate credentials ──────────────────────────────────────────────────
    # Use the same generic message for both "user not found" and "wrong password"
    # — never reveal which one failed (prevents user enumeration attacks)
    if not user or not user.password_hash:
        log_action("auth.login_failed", new_value={"email": email})
        return error("Invalid email or password.", 401)

    if not bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
        log_action("auth.login_failed", new_value={"email": email})
        return error("Invalid email or password.", 401)

    # ── 2FA gate ──────────────────────────────────────────────────────────────
    if user.totp_enabled:
        # Issue a restricted "pre-auth" token — only valid for /2fa/verify
        # The additional_claims scope flag is checked in the verify endpoint
        pre_token: str = create_access_token(
            identity=user.id,
            additional_claims={"scope": "2fa_pending"},
        )
        return success(
            data={"requires_2fa": True, "pre_token": pre_token},
            message="2FA verification required.",
        )

    # ── Full login ────────────────────────────────────────────────────────────
    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()

    log_action("auth.login", entity_type="user", entity_id=user.id, user_id=user.id)

    return success(
        data={
            "user": _serialize_user(user),
            "access_token": create_access_token(identity=user.id),
            "refresh_token": create_refresh_token(identity=user.id),
        },
        message="Logged in successfully.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/refresh
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)   # Requires the refresh token, not the access token
def refresh():
    """
    Issue a new access token using a valid refresh token.

    The frontend calls this automatically when it gets a 401 token_expired.
    The refresh token itself lives for 7 days (set in config).
    """
    user_id: str = get_jwt_identity()

    return success(
        data={"access_token": create_access_token(identity=user_id)},
        message="Access token refreshed.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/logout
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/logout", methods=["POST"])
@jwt_required()
def logout():
    """
    Revoke the current access token.

    Adds the token's JTI (unique ID) to the blocklist.
    All subsequent requests with this token will get 401 token_revoked.
    """
    jti: str = get_jwt()["jti"]
    block_token(jti)

    log_action("auth.logout", user_id=get_jwt_identity())

    return success(message="Logged out successfully.")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/auth/me
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    """
    Return the currently authenticated user's profile.
    Called by the frontend on app load to restore session state.
    """
    user: User = _get_current_user()
    return success(data={"user": _serialize_user(user)})


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/auth/verify/<token>
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/verify/<string:token>", methods=["GET"])
def verify_email(token: str):
    """
    Mark the user's email as verified.
    Called when user clicks the link in their verification email.
    Redirects to the frontend after verifying.
    """
    user: Optional[User] = User.query.filter_by(verification_token=token).first()

    if not user:
        return error("Invalid or expired verification link.", 400)

    user.is_verified = True
    user.verification_token = None
    db.session.commit()

    log_action("auth.email_verified", entity_type="user",
               entity_id=user.id, user_id=user.id)

    frontend_url: str = current_app.config["FRONTEND_URL"]
    return redirect(f"{frontend_url}/auth/verified")


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/2fa/setup
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/2fa/setup", methods=["POST"])
@jwt_required()
def setup_2fa():
    """
    Generate a TOTP secret + QR code for the user to scan.

    Step 1 of 2 in the 2FA setup flow.
    The QR code is returned as a base64 PNG string — display it in the UI.
    The user then opens Google Authenticator, scans the code, and calls
    /2fa/confirm with the 6-digit code to activate 2FA.
    """
    user: User = _get_current_user()

    if user.totp_enabled:
        return error("2FA is already enabled on this account.", 400)

    # Generate a new TOTP secret and store it encrypted
    # (not activated yet — user must confirm with a valid code first)
    secret: str = pyotp.random_base32()
    user.totp_secret = encrypt(secret)
    db.session.commit()

    # Build the provisioning URI — this is what the QR code encodes
    totp = pyotp.TOTP(secret)
    uri: str = totp.provisioning_uri(
        name=user.email,
        issuer_name=current_app.config["APP_NAME"],
    )

    # Render the URI as a QR code PNG and encode as base64
    # so the frontend can display it as <img src="data:image/png;base64,...">
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64: str = base64.b64encode(buf.getvalue()).decode("utf-8")

    return success(
        data={
            "qr_code": f"data:image/png;base64,{qr_b64}",
            "secret": secret,   # Show once — lets user enter code manually if camera fails
        },
        message="Scan the QR code with your authenticator app, then confirm with the 6-digit code.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/2fa/confirm
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/2fa/confirm", methods=["POST"])
@jwt_required()
def confirm_2fa():
    """
    Verify the first TOTP code and activate 2FA.

    Step 2 of 2 in setup. Returns 8 backup codes — show them once,
    user must save them. Each backup code can be used instead of a
    TOTP code if they lose access to their authenticator app.

    Body: { "code": "123456" }
    """
    user: User = _get_current_user()
    data: dict = request.get_json(silent=True) or {}
    code: str  = (data.get("code") or "").strip()

    if not user.totp_secret:
        return error("Start 2FA setup first by calling /2fa/setup.", 400)

    if user.totp_enabled:
        return error("2FA is already enabled.", 400)

    # Verify the code — valid_window=1 allows 30 seconds of clock drift
    totp = pyotp.TOTP(decrypt(user.totp_secret))
    if not totp.verify(code, valid_window=1):
        return error("Invalid code. Make sure your authenticator app is synced.", 401)

    # Activate 2FA
    user.totp_enabled = True

    # Generate 8 backup codes, store as bcrypt hashes (same as passwords)
    raw_codes: list[str] = generate_backup_codes(8)
    for raw in raw_codes:
        hashed: str = bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        db.session.add(BackupCode(user_id=user.id, code_hash=hashed))

    db.session.commit()

    log_action("auth.2fa_enabled", entity_type="user",
               entity_id=user.id, user_id=user.id)

    return success(
        data={"backup_codes": raw_codes},   # Shown ONCE — user must save these
        message="2FA enabled. Store your backup codes somewhere safe — they won't be shown again.",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/2fa/verify
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/2fa/verify", methods=["POST"])
@jwt_required()
def verify_2fa():
    """
    Complete login for a user who has 2FA enabled.

    Called with the pre_token from /login + a TOTP code (or backup code).
    Returns full access + refresh tokens on success.

    Body: { "code": "123456" }
    """
    claims: dict = get_jwt()

    # Ensure this endpoint is only callable with the restricted pre-auth token
    if claims.get("scope") != "2fa_pending":
        return error("This endpoint requires a 2FA pending token.", 403)

    user: User = _get_current_user()
    data: dict = request.get_json(silent=True) or {}
    code: str  = (data.get("code") or "").strip()

    # ── Try TOTP first ────────────────────────────────────────────────────────
    totp = pyotp.TOTP(decrypt(user.totp_secret))
    if totp.verify(code, valid_window=1):
        return _complete_login(user, method="totp")

    # ── Try backup codes ──────────────────────────────────────────────────────
    unused_backups = BackupCode.query.filter_by(user_id=user.id, used=False).all()

    for backup in unused_backups:
        if bcrypt.checkpw(code.encode("utf-8"), backup.code_hash.encode("utf-8")):
            # Mark this backup code as used — can never be used again
            backup.used = True
            backup.used_at = datetime.now(timezone.utc)
            db.session.commit()
            return _complete_login(user, method="backup_code")

    return error("Invalid code. Try again or use a backup code.", 401)


# ─────────────────────────────────────────────────────────────────────────────
#  POST /api/auth/2fa/disable
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/2fa/disable", methods=["POST"])
@jwt_required()
def disable_2fa():
    """
    Disable 2FA on the account.
    Requires a valid TOTP code to confirm it's the real user — not
    someone who has access to their laptop while they're away.

    Body: { "code": "123456" }
    """
    user: User = _get_current_user()
    data: dict = request.get_json(silent=True) or {}
    code: str  = (data.get("code") or "").strip()

    if not user.totp_enabled:
        return error("2FA is not enabled on this account.", 400)

    totp = pyotp.TOTP(decrypt(user.totp_secret))
    if not totp.verify(code, valid_window=1):
        return error("Invalid code.", 401)

    # Remove all 2FA data
    user.totp_enabled = False
    user.totp_secret  = None
    BackupCode.query.filter_by(user_id=user.id).delete()
    db.session.commit()

    log_action("auth.2fa_disabled", entity_type="user",
               entity_id=user.id, user_id=user.id)

    return success(message="2FA has been disabled.")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/auth/linkedin
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/linkedin", methods=["GET"])
def linkedin_auth():
    """
    Redirect the user to LinkedIn's OAuth consent screen.

    The frontend calls this URL directly (it's a redirect, not an API call).
    LinkedIn shows a permission screen, then redirects to /linkedin/callback.
    """
    cfg = current_app.config

    if not cfg.get("LINKEDIN_CLIENT_ID"):
        return error("LinkedIn OAuth is not configured.", 503)

    # Build the LinkedIn authorization URL
    params: str = (
        f"response_type=code"
        f"&client_id={cfg['LINKEDIN_CLIENT_ID']}"
        f"&redirect_uri={cfg['LINKEDIN_REDIRECT_URI']}"
        f"&scope=openid%20profile%20email"
    )

    return redirect(f"{cfg['LINKEDIN_AUTH_URL']}?{params}")


# ─────────────────────────────────────────────────────────────────────────────
#  GET /api/auth/linkedin/callback
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route("/linkedin/callback", methods=["GET"])
def linkedin_callback():
    """
    Handle LinkedIn's redirect after the user grants permission.

    FLOW:
      1. LinkedIn sends a one-time `code` in the query string
      2. We exchange that code for an access token (server-to-server)
      3. We use the token to fetch the user's profile from LinkedIn
      4. We find or create a User record
      5. We issue JWT tokens and redirect to the frontend
    """
    cfg = current_app.config

    code: Optional[str] = request.args.get("code")
    if not code:
        return error("LinkedIn authorisation failed — no code received.", 400)

    # ── Step 1: Exchange code for access token ─────────────────────────────
    token_resp = requests.post(
        cfg["LINKEDIN_TOKEN_URL"],
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  cfg["LINKEDIN_REDIRECT_URI"],
            "client_id":     cfg["LINKEDIN_CLIENT_ID"],
            "client_secret": cfg["LINKEDIN_CLIENT_SECRET"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )

    if not token_resp.ok:
        return error("Failed to exchange LinkedIn token.", 502)

    access_token: str = token_resp.json().get("access_token", "")

    # ── Step 2: Fetch user profile (OpenID Connect userinfo endpoint) ──────
    profile_resp = requests.get(
        cfg["LINKEDIN_USERINFO_URL"],
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )

    if not profile_resp.ok:
        return error("Failed to fetch LinkedIn profile.", 502)

    profile: dict  = profile_resp.json()
    linkedin_id: str = profile.get("sub", "")
    email: str       = (profile.get("email") or "").lower().strip()
    name: str        = profile.get("name") or profile.get("given_name", "User")
    avatar: str      = profile.get("picture", "")

    if not linkedin_id or not email:
        return error("LinkedIn did not return required profile fields.", 400)

    # ── Step 3: Find or create user ────────────────────────────────────────
    # First try matching by LinkedIn ID, then by email
    user: Optional[User] = (
        User.query.filter_by(linkedin_id=linkedin_id).first()
        or User.query.filter_by(email=email).first()
    )

    if user:
        # Existing user — update LinkedIn fields
        user.linkedin_id           = linkedin_id
        user.linkedin_access_token = encrypt(access_token)
        if avatar and not user.avatar_url:
            user.avatar_url = avatar
    else:
        # New user via LinkedIn — no password needed
        user = User(
            name=name,
            email=email,
            linkedin_id=linkedin_id,
            linkedin_access_token=encrypt(access_token),
            avatar_url=avatar,
            is_verified=True,              # LinkedIn already verified their email
            inbound_email_token=generate_inbound_token(),
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(UserSettings(user_id=user.id))

    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()

    log_action("auth.linkedin_login", entity_type="user",
               entity_id=user.id, user_id=user.id)

    # ── Step 4: Issue JWT tokens + redirect to frontend ───────────────────
    jwt_access:  str = create_access_token(identity=user.id)
    jwt_refresh: str = create_refresh_token(identity=user.id)

    # Redirect to a frontend route that reads tokens from query params
    # and stores them in memory / state
    frontend_url: str = cfg["FRONTEND_URL"]
    return redirect(
        f"{frontend_url}/auth/callback"
        f"?access_token={jwt_access}"
        f"&refresh_token={jwt_refresh}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Private helpers (not exposed as routes)
# ─────────────────────────────────────────────────────────────────────────────

def _get_current_user() -> User:
    """
    Fetch the User row for the currently authenticated JWT identity.
    Aborts with 404 if the user no longer exists.
    """
    user_id: str = get_jwt_identity()
    user: Optional[User] = db.session.get(User, user_id)

    if not user:
        from flask import abort
        abort(404, description="User not found.")

    return user


def _complete_login(user: User, method: str):
    """
    Finalise a successful 2FA verification — update last_login, log, return tokens.

    Args:
        user:   The authenticated User instance.
        method: "totp" or "backup_code" — recorded in the audit log.
    """
    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()

    log_action(
        f"auth.2fa_verified.{method}",
        entity_type="user",
        entity_id=user.id,
        user_id=user.id,
    )

    response_data: dict = {
        "user": _serialize_user(user),
        "access_token":  create_access_token(identity=user.id),
        "refresh_token": create_refresh_token(identity=user.id),
    }

    if method == "backup_code":
        # Warn the user they used a one-time code
        remaining: int = BackupCode.query.filter_by(
            user_id=user.id, used=False
        ).count()
        response_data["warning"] = (
            f"Backup code used. {remaining} backup code(s) remaining."
        )

    return success(data=response_data, message="Login successful.")


def _serialize_user(user: User) -> dict:
    """
    Convert a User model instance to a safe dict for API responses.
    Never includes password_hash, totp_secret, or encrypted tokens.
    """
    settings = user.settings

    return {
        "id":           user.id,
        "name":         user.name,
        "email":        user.email,
        "avatar_url":   user.avatar_url,
        "is_verified":  user.is_verified,
        "totp_enabled": user.totp_enabled,
        "has_linkedin": bool(user.linkedin_id),
        "inbound_email": user.inbound_email,
        "created_at":   user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "settings": {
            "theme":                  settings.theme,
            "target_roles":           settings.target_roles,
            "target_company_types":   settings.target_company_types,
            "target_locations":       settings.target_locations,
            "remote_preference":      settings.remote_preference,
            "salary_min":             settings.salary_min,
            "salary_max":             settings.salary_max,
            "salary_currency":        settings.salary_currency,
            "auto_apply_enabled":     settings.auto_apply_enabled,
            "auto_apply_min_score":   settings.auto_apply_min_score,
            "auto_apply_daily_limit": settings.auto_apply_daily_limit,
            "auto_apply_portals":     settings.auto_apply_portals,
            "notify_status_change":   settings.notify_status_change,
            "notify_interview_remind":settings.notify_interview_remind,
            "notify_weekly_digest":   settings.notify_weekly_digest,
            "onboarding_complete":    settings.onboarding_complete,
            "onboarding_step":        settings.onboarding_step,
        } if settings else {},
    }