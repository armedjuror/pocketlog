"""
auth_service.py — Auth business logic. Transport-agnostic.

All bot plugins call these functions; none of this knows about HTTP or Telegram.
"""

import random
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models import (
    User, BotIdentity, OTPSession, UserSession, BotConversationState,
)

OTP_EXPIRY_MINUTES = 10
SESSION_EXPIRY_DAYS = 30


# ── User lookup / creation ─────────────────────────────────────────────────

def get_user_by_bot(platform: str, chat_id: str, db: Session) -> Optional[User]:
    identity = (
        db.query(BotIdentity)
        .filter_by(platform=platform, chat_id=chat_id)
        .first()
    )
    return identity.user if identity else None


def get_user_by_email(email: str, db: Session) -> Optional[User]:
    return db.query(User).filter_by(email=email.lower().strip()).first()


def get_user_bot_identities(user_id: int, db: Session) -> list[BotIdentity]:
    return db.query(BotIdentity).filter_by(user_id=user_id).all()


def create_user(name: str, email: str, primary_bot: str, db: Session) -> User:
    user = User(name=name, email=email.lower().strip(), primary_bot=primary_bot)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def link_bot_identity(user_id: int, platform: str, chat_id: str, db: Session) -> BotIdentity:
    identity = BotIdentity(user_id=user_id, platform=platform, chat_id=chat_id)
    db.add(identity)
    db.commit()
    return identity


# ── OTP ───────────────────────────────────────────────────────────────────

def generate_otp(user_id: int, db: Session) -> str:
    """Invalidate old OTPs, create a fresh one, return the code."""
    db.query(OTPSession).filter_by(user_id=user_id, used=False).update({"used": True})
    code = f"{random.randint(0, 999999):06d}"
    otp = OTPSession(
        user_id=user_id,
        otp_code=code,
        expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINUTES),
    )
    db.add(otp)
    db.commit()
    return code


def verify_otp(user_id: int, code: str, db: Session) -> Optional[UserSession]:
    """Verify an OTP; on success create and return a new UserSession."""
    otp = (
        db.query(OTPSession)
        .filter_by(user_id=user_id, otp_code=code.strip(), used=False)
        .first()
    )
    if not otp or otp.expires_at < datetime.utcnow():
        return None
    otp.used = True
    token = secrets.token_hex(32)
    session = UserSession(
        user_id=user_id,
        token=token,
        expires_at=datetime.utcnow() + timedelta(days=SESSION_EXPIRY_DAYS),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ── Session ────────────────────────────────────────────────────────────────

def get_active_session_by_bot(platform: str, chat_id: str, db: Session) -> Optional[UserSession]:
    identity = (
        db.query(BotIdentity)
        .filter_by(platform=platform, chat_id=chat_id)
        .first()
    )
    if not identity:
        return None
    return (
        db.query(UserSession)
        .filter(
            UserSession.user_id == identity.user_id,
            UserSession.expires_at > datetime.utcnow(),
        )
        .first()
    )


# ── Conversation state ─────────────────────────────────────────────────────

def get_bot_state(platform: str, chat_id: str, db: Session) -> Optional[BotConversationState]:
    return db.query(BotConversationState).filter_by(platform=platform, chat_id=chat_id).first()


def set_bot_state(
    platform: str,
    chat_id: str,
    state: str,
    db: Session,
    user_id: Optional[int] = None,
    temp_name: Optional[str] = None,
    temp_email: Optional[str] = None,
) -> BotConversationState:
    existing = get_bot_state(platform, chat_id, db)
    if existing:
        existing.state = state
        if user_id is not None:
            existing.user_id = user_id
        if temp_name is not None:
            existing.temp_name = temp_name
        if temp_email is not None:
            existing.temp_email = temp_email
        existing.updated_at = datetime.utcnow()
    else:
        existing = BotConversationState(
            platform=platform,
            chat_id=chat_id,
            state=state,
            user_id=user_id,
            temp_name=temp_name,
            temp_email=temp_email,
        )
        db.add(existing)
    db.commit()
    return existing


def clear_bot_state(platform: str, chat_id: str, db: Session) -> None:
    db.query(BotConversationState).filter_by(platform=platform, chat_id=chat_id).delete()
    db.commit()