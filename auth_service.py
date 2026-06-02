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


def create_user(name: str, email: str, primary_bot: str, db: Session, currency: str = "INR") -> User:
    user = User(name=name, email=email.lower().strip(), primary_bot=primary_bot, currency=currency)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_or_create_guest_user(
    platform: str, platform_user_id: str, display_name: str, db: Session
) -> User:
    """
    Find or create a guest User for a group member who hasn't registered.
    Uses BotIdentity to link platform_user_id → User.
    When a guest later completes signup with an email, call merge_guest_user().
    """
    identity = db.query(BotIdentity).filter_by(platform=platform, chat_id=platform_user_id).first()
    if identity:
        return identity.user

    guest = User(name=display_name, email=None, is_guest=True, primary_bot=platform)
    db.add(guest)
    db.commit()
    db.refresh(guest)
    link_bot_identity(guest.id, platform, platform_user_id, db)
    return guest


def merge_username_guest_to_numeric(
    platform: str, numeric_id: str, username: str, db: Session
) -> Optional[User]:
    """
    If a @username-based guest identity exists, re-anchor it to the real numeric
    chat_id and update all GroupMember records so their shares remain intact.
    Returns the guest User if found and updated, None otherwise.
    """
    from models import GroupMember
    at_id = f"@{username}"
    identity = db.query(BotIdentity).filter_by(platform=platform, chat_id=at_id).first()
    if not identity or not identity.user or not identity.user.is_guest:
        return None

    identity.chat_id = numeric_id
    db.query(GroupMember).filter_by(platform_user_id=at_id).update(
        {"platform_user_id": numeric_id}, synchronize_session=False
    )
    db.commit()
    return identity.user


def merge_guest_user(user: User, name: str, email: str, currency: str, db: Session) -> User:
    """
    Upgrade a guest User to a full account after they complete signup with an email.
    All existing GroupExpenseShare and Transaction rows linked to guest.id are preserved.
    """
    user.name = name
    user.email = email.lower().strip()
    user.currency = currency
    user.is_guest = False
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

def create_session(user_id: int, db: Session) -> UserSession:
    """Create a session directly — no OTP required (used for bot signups)."""
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
    temp_currency: Optional[str] = None,
    temp_otp: Optional[str] = None,
    temp_otp_expires_at: Optional[datetime] = None,
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
        if temp_currency is not None:
            existing.temp_currency = temp_currency
        if temp_otp is not None:
            existing.temp_otp = temp_otp
        if temp_otp_expires_at is not None:
            existing.temp_otp_expires_at = temp_otp_expires_at
        existing.updated_at = datetime.utcnow()
    else:
        existing = BotConversationState(
            platform=platform,
            chat_id=chat_id,
            state=state,
            user_id=user_id,
            temp_name=temp_name,
            temp_email=temp_email,
            temp_currency=temp_currency,
            temp_otp=temp_otp,
            temp_otp_expires_at=temp_otp_expires_at,
        )
        db.add(existing)
    db.commit()
    return existing


def clear_bot_state(platform: str, chat_id: str, db: Session) -> None:
    db.query(BotConversationState).filter_by(platform=platform, chat_id=chat_id).delete()
    db.commit()