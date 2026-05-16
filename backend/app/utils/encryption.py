# app/utils/encryption.py
# ─────────────────────────────────────────────────────────────────────────────
# Symmetric encryption for sensitive DB fields.
#
# WHAT gets encrypted:
#   - User.totp_secret        (2FA seed — needed to verify codes)
#   - User.linkedin_access_token  (OAuth token — needed to call LinkedIn API)
#   - CalendarConnection.access_token / refresh_token
#
# WHY not just hash these?
#   Hashing is one-way. We need to READ these values back to use them.
#   Encryption is two-way — we can recover the original value with the key.
#
# HOW Fernet works:
#   - AES-128-CBC encryption + HMAC-SHA256 authentication in one
#   - Same key encrypts and decrypts
#   - Key lives ONLY in .env — never in DB, never in code
#   - If the key is lost, all encrypted values are permanently unreadable
#     → Back up ENCRYPTION_KEY somewhere safe (e.g. a password manager)
#
# Generate a key:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# ─────────────────────────────────────────────────────────────────────────────

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _get_cipher() -> Fernet:
    """
    Build a Fernet cipher using the key from app config.

    Returns:
        Fernet: Ready-to-use cipher instance.

    Raises:
        RuntimeError: If ENCRYPTION_KEY is missing or invalid.
    """
    key: str = current_app.config.get("ENCRYPTION_KEY", "")

    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set in .env. "
            "Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    # Fernet expects bytes
    if isinstance(key, str):
        key = key.encode()

    return Fernet(key)


def encrypt(plain_text: str) -> str:
    """
    Encrypt a plain string and return a base64-encoded ciphertext string.

    Args:
        plain_text: The raw value to encrypt (e.g. a TOTP secret).

    Returns:
        str: Encrypted, base64-encoded string safe to store in the DB.

    Example:
        stored = encrypt("JBSWY3DPEHPK3PXP")
        # → "gAAAAABl..." (different every call due to random IV)
    """
    if not plain_text:
        return plain_text

    cipher: Fernet = _get_cipher()
    return cipher.encrypt(plain_text.encode()).decode()


def decrypt(cipher_text: str) -> str:
    """
    Decrypt a previously encrypted string back to its original value.

    Args:
        cipher_text: The encrypted base64 string from the DB.

    Returns:
        str: The original plain text value.

    Raises:
        ValueError: If the ciphertext is tampered with or the key is wrong.

    Example:
        original = decrypt("gAAAAABl...")
        # → "JBSWY3DPEHPK3PXP"
    """
    if not cipher_text:
        return cipher_text

    cipher: Fernet = _get_cipher()

    try:
        return cipher.decrypt(cipher_text.encode()).decode()
    except InvalidToken:
        # Raised if ciphertext was tampered with OR wrong key is being used
        raise ValueError(
            "Decryption failed — ciphertext is invalid or the ENCRYPTION_KEY has changed."
        )