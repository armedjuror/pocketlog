"""
routers/oauth.py — OAuth 2.0 endpoints.

Token endpoint
--------------
POST /api/oauth/token
  grant_type=client_credentials&client_id=...&client_secret=...
  → { access_token, token_type, expires_in, scope }

Client management (requires Bearer token — your PAT or an existing OAuth token)
---------------------------------------------------------------------------
POST   /api/oauth/clients          — register a new app
GET    /api/oauth/clients          — list your apps
DELETE /api/oauth/clients/{id}     — revoke an app
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

import oauth_service
from models import SessionLocal, User

router = APIRouter(prefix="/api/oauth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)


# ── DB dependency ──────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Auth dependency ────────────────────────────────────────────────────────

def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Session = Depends(get_db),
) -> User:
    if not creds:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    user = oauth_service.get_user_from_token(creds.credentials, db)
    if not user:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return user


# ── Schemas ────────────────────────────────────────────────────────────────

class ClientIn(BaseModel):
    name:   str
    scopes: str = "read write"


class ClientOut(BaseModel):
    client_id:  str
    name:       str
    scopes:     str
    created_at: str


# ── Token endpoint ─────────────────────────────────────────────────────────

@router.post(
    "/token",
    summary="Issue an access token (client_credentials grant)",
    response_description="OAuth 2.0 Bearer access token",
)
def token(
    grant_type:    Annotated[str, Form()],
    client_id:     Annotated[str, Form()],
    client_secret: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    """
    Exchange client credentials for a Bearer access token.

    - **grant_type**: must be `client_credentials`
    - **client_id**: from `POST /api/oauth/clients`
    - **client_secret**: shown once at client creation

    Token expires in **1 hour**. Request a new one when it does.
    """
    if grant_type != "client_credentials":
        raise HTTPException(400, "Only client_credentials grant is supported")

    tok = oauth_service.issue_access_token(client_id, client_secret, db)
    if not tok:
        raise HTTPException(401, "Invalid client credentials")

    return {
        "access_token": tok._plain,
        "token_type":   "Bearer",
        "expires_in":   oauth_service.ACCESS_TOKEN_EXPIRY_HOURS * 3600,
        "scope":        tok.scopes,
    }


# ── Client management ──────────────────────────────────────────────────────

@router.post(
    "/clients",
    status_code=201,
    summary="Register a new OAuth client (app)",
)
def create_client(
    data: ClientIn,
    user: User = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """
    Register a new OAuth client tied to your account.

    **Authorization**: Bearer token from your Telegram bot (`/apikey` command).

    The `client_secret` is shown **once** — store it securely.
    """
    client, secret = oauth_service.create_client(user.id, data.name, db, scopes=data.scopes)
    return {
        "client_id":     client.client_id,
        "client_secret": secret,
        "name":          client.name,
        "scopes":        client.scopes,
        "note":          "Store client_secret securely — it will not be shown again.",
    }


@router.get(
    "/clients",
    summary="List your registered OAuth clients",
    response_model=list[ClientOut],
)
def list_clients(
    user: User = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """List all OAuth clients registered to your account."""
    return [
        ClientOut(
            client_id=c.client_id, name=c.name,
            scopes=c.scopes, created_at=str(c.created_at),
        )
        for c in oauth_service.list_clients(user.id, db)
    ]


@router.delete(
    "/clients/{client_id}",
    summary="Revoke an OAuth client",
)
def delete_client(
    client_id: str,
    user: User = Depends(current_user),
    db:   Session = Depends(get_db),
):
    """
    Permanently revoke an OAuth client and all its active tokens.
    Only the owner can revoke their own clients.
    """
    if not oauth_service.delete_client(client_id, user.id, db):
        raise HTTPException(404, "Client not found")
    return {"ok": True}
