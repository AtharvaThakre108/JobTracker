# app/utils/tokens.py
# ─────────────────────────────────────────────────────────────────────────────
# Token and code generation utilities.
# ─────────────────────────────────────────────────────────────────────────────

import secrets
import string


def generate_token(length: int = 64) -> str:
    """
    Generate a cryptographically secure URL-safe token.
    Used for: email verification links, password reset links.
    """
    return secrets.token_urlsafe(length)


def generate_inbound_token(length: int = 12) -> str:
    """
    Generate a short lowercase alphanumeric token.
    Used as the unique part of a user's inbound email address.
    e.g. "ab3kx9mz1qre" → ab3kx9mz1qre@mail.jobtracker.app
    """
    alphabet: str = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_backup_codes(count: int = 8) -> list[str]:
    """
    Generate one-time 2FA backup codes.
    Returns uppercase hex strings like: ["A1B2C3D4", ...]
    """
    return [secrets.token_hex(4).upper() for _ in range(count)]