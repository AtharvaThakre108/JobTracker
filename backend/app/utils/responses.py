# app/utils/responses.py
# ─────────────────────────────────────────────────────────────────────────────
# Standardised API response helpers.
#
# Every response from this API follows the same shape:
#   {
#     "success": true | false,
#     "message": "Human readable string",
#     "data":    { ... } | null       ← only on success
#     "errors":  { ... }              ← only on validation failure
#   }
#
# Usage:
#   return success({"user": user_dict})
#   return success({"user": user_dict}, "Account created.", 201)
#   return error("Email already exists.", 409)
#   return error("Validation failed.", 422, {"email": "Invalid format."})
# ─────────────────────────────────────────────────────────────────────────────

from typing import Any, Optional
from flask import jsonify


def success(
    data: Optional[Any] = None,
    message: str = "Success",
    status_code: int = 200
):
    """
    Return a successful JSON response.

    Args:
        data:        The payload to return (dict, list, or None).
        message:     Human-readable success message.
        status_code: HTTP status code (200, 201, etc.).

    Returns:
        Flask Response object with JSON body + status code.
    """
    body: dict = {
        "success": True,
        "message": message,
        "data": data,
    }
    return jsonify(body), status_code


def error(
    message: str = "An error occurred",
    status_code: int = 400,
    errors: Optional[dict] = None
):
    """
    Return a failed JSON response.

    Args:
        message:     Human-readable error description.
        status_code: HTTP status code (400, 401, 404, 422, 500, etc.).
        errors:      Optional dict of field-level validation errors.
                     e.g. {"email": "Required.", "password": "Too short."}

    Returns:
        Flask Response object with JSON body + status code.
    """
    body: dict = {
        "success": False,
        "message": message,
    }

    # Only include "errors" key if there are field-level errors to show
    if errors:
        body["errors"] = errors

    return jsonify(body), status_code