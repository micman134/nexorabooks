"""Authentication, password hashing and role-based permissions.

Passwords use PBKDF2-HMAC-SHA256 from the Python standard library — no
compiled dependency, so the Windows build stays a single clean executable.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ROLE_ACCOUNTANT, ROLE_ADMIN, ROLE_CLERK, ROLE_VIEWER, User

ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not any(c.isalpha() for c in password):
        problems.append("at least one letter")
    if not any(c.isdigit() for c in password):
        problems.append("at least one number")
    return problems


def new_temp_password() -> str:
    return secrets.token_urlsafe(9)


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------

P_VIEW = "view"          # see records and reports
P_ENTRY = "entry"        # create/edit sales, purchase and cash documents
P_JOURNAL = "journal"    # manual journals, opening balances, reconciliation
P_VOID = "void"          # void or reverse a posted document
P_ADMIN = "admin"        # users, company settings, lock date, year-end, restore

_MATRIX: dict[str, set[str]] = {
    ROLE_ADMIN: {P_VIEW, P_ENTRY, P_JOURNAL, P_VOID, P_ADMIN},
    ROLE_ACCOUNTANT: {P_VIEW, P_ENTRY, P_JOURNAL, P_VOID},
    ROLE_CLERK: {P_VIEW, P_ENTRY},
    ROLE_VIEWER: {P_VIEW},
}

ROLE_LABELS = {
    ROLE_ADMIN: "Administrator",
    ROLE_ACCOUNTANT: "Accountant",
    ROLE_CLERK: "Data entry",
    ROLE_VIEWER: "Viewer",
}


#: Destroying a record for good. Deliberately NOT in the role matrix: no role
#: carries it, and being promoted to administrator does not confer it. It comes
#: only from ``User.is_super_admin``, which one person grants to another by
#: name. Every other permission here describes work; this one describes the
#: ability to make work disappear, and the two should not be granted by the
#: same gesture.
P_DELETE = "delete"


def can(user: User | None, permission: str) -> bool:
    if user is None or not user.is_active:
        return False
    if permission == P_DELETE:
        # Both, always. The flag says "this person is trusted with something
        # irreversible"; the role says "this person already runs the company".
        # Handing the first to somebody without the second would create an
        # account that can destroy records but cannot be held responsible for
        # the settings, the users or the year-end.
        return bool(getattr(user, "is_super_admin", False)) and user.role == ROLE_ADMIN
    return permission in _MATRIX.get(user.role, set())


def is_super_admin(user: User | None) -> bool:
    return can(user, P_DELETE)


# --------------------------------------------------------------------------
# Session helpers (used as FastAPI dependencies)
# --------------------------------------------------------------------------


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def current_user(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    return user


def require(permission: str):
    """Dependency factory: ``Depends(require(P_ADMIN))``."""

    def _dep(request: Request) -> User:
        user = require_user(request)
        if not can(user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Your role ({ROLE_LABELS.get(user.role, user.role)}) does not "
                    "allow this action. Ask an administrator if you need access."
                ),
            )
        return user

    return _dep
