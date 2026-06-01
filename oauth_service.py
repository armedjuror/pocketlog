"""
oauth_service.py — OAuth 2.0 business logic. Transport-agnostic.

Supports:
  - OAuth client registration (per user)
  - Client Credentials grant  (POST /api/oauth/token)
  - Token verification for both:
      · UserSession tokens  (issued by bot auth — act as Personal Access Tokens)
      · OAuthAccessTokens   (issued via client credentials)
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models import OAuthClient, OAuthAccessToken, User, UserSession

ACCESS_TOKEN_EXPIRY_HOURS = 1
CLIENT_SECRET_BYTES = 32


# ── Helpers ────────────────────────────────────────────────────────────────

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── Client management ──────────────────────────────────────────────────────

def create_client(
    user_id: int, name: str, db: Session, scopes: str = "read write"
) -> tuple[OAuthClient, str]:
    """
    Register a new OAuth client for a user.
    Returns (client, plain_secret) — the plain secret is shown ONCE and not stored.
    """
    client_id_str = secrets.token_hex(16)
    secret_plain  = secrets.token_hex(CLIENT_SECRET_BYTES)
    client = OAuthClient(
        user_id=user_id,
        name=name,
        client_id=client_id_str,
        client_secret_hash=_sha256(secret_plain),
        scopes=scopes,
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return client, secret_plain


def list_clients(user_id: int, db: Session) -> list[OAuthClient]:
    return db.query(OAuthClient).filter_by(user_id=user_id).all()


def delete_client(client_id_str: str, user_id: int, db: Session) -> bool:
    """Delete a client owned by user_id. Returns False if not found or not owned."""
    client = db.query(OAuthClient).filter_by(client_id=client_id_str, user_id=user_id).first()
    if not client:
        return False
    db.delete(client)
    db.commit()
    return True


# ── Token issuance (client_credentials grant) ─────────────────────────────

def issue_access_token(
    client_id_str: str, client_secret_plain: str, db: Session
) -> Optional[OAuthAccessToken]:
    """
    Verify client credentials and issue a new access token.
    Returns None if credentials are invalid.
    """
    client = db.query(OAuthClient).filter_by(client_id=client_id_str).first()
    if not client or client.client_secret_hash != _sha256(client_secret_plain):
        return None

    # Expire old tokens for this client to keep the table tidy
    db.query(OAuthAccessToken).filter(
        OAuthAccessToken.client_id == client.id,
        OAuthAccessToken.expires_at < datetime.utcnow(),
    ).delete()

    token_plain = secrets.token_hex(32)
    token = OAuthAccessToken(
        client_id=client.id,
        user_id=client.user_id,
        token_hash=_sha256(token_plain),
        scopes=client.scopes,
        expires_at=datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRY_HOURS),
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    # Attach plain token temporarily so the caller can return it (not stored)
    token._plain = token_plain
    return token


# ── Token verification ─────────────────────────────────────────────────────

def get_user_from_token(token: str, db: Session) -> Optional[User]:
    """
    Resolve a Bearer token to a User.
    Accepts both UserSession tokens (PATs from bot auth) and OAuthAccessTokens.
    """
    h = _sha256(token)
    now = datetime.utcnow()

    # OAuthAccessToken (hashed)
    oauth_tok = db.query(OAuthAccessToken).filter(
        OAuthAccessToken.token_hash == h,
        OAuthAccessToken.expires_at > now,
    ).first()
    if oauth_tok:
        return db.query(User).get(oauth_tok.user_id)

    # UserSession token (stored plain — bot-issued PATs)
    session = db.query(UserSession).filter(
        UserSession.token == token,
        UserSession.expires_at > now,
    ).first()
    if session:
        return db.query(User).get(session.user_id)

    return None


# ── PAT helpers (for /apikey bot command) ─────────────────────────────────

def get_or_create_session(user_id: int, db: Session) -> UserSession:
    """Return an active UserSession for the user, or create a fresh one."""
    from datetime import timedelta
    session = db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.expires_at > datetime.utcnow(),
    ).first()
    if not session:
        token = secrets.token_hex(32)
        session = UserSession(
            user_id=user_id,
            token=token,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
    return session
