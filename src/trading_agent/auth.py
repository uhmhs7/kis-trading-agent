"""Minimal Google OAuth 2.0 (authorization-code) helpers.

Kept dependency-light: only `requests` (already used by the KIS client). Session
state lives in a signed cookie via Starlette's SessionMiddleware, so nothing is
persisted server-side (safe on Render's ephemeral filesystem).
"""

from __future__ import annotations

import secrets
from typing import Optional
from urllib.parse import urlencode

import requests

from .config import Settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def new_state() -> str:
    """Opaque anti-CSRF token stored in the session before the redirect."""
    return secrets.token_urlsafe(24)


def build_auth_url(settings: Settings, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(settings: Settings, code: str, redirect_uri: str) -> Optional[dict]:
    """Exchange the auth code for tokens, then fetch the user's profile (email)."""
    try:
        token_resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        if token_resp.status_code != 200:
            return None
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return None
        info_resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if info_resp.status_code != 200:
            return None
        return info_resp.json()  # {"email": ..., "email_verified": ..., "name": ...}
    except requests.RequestException:
        return None


def email_allowed(settings: Settings, email: str) -> bool:
    """Allowlist check. With no ALLOWED_EMAILS set, any Google account passes."""
    if not email:
        return False
    allow = settings.allowed_emails
    if not allow:
        return True
    return email.strip().lower() in {e.lower() for e in allow}
