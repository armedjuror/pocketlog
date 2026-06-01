"""
plugins/auth_flow.py — Reusable auth flow mixin for all bot plugins.

Conversation states
-------------------
awaiting_name       New user → collecting name
awaiting_email      New user → collecting email
awaiting_email_otp  New user → verifying email via Resend OTP
awaiting_currency   New user → collecting currency preference
awaiting_otp        OTP sent to current channel, awaiting entry
awaiting_merge_otp  Email belongs to existing account; OTP sent to their
                    primary bot to prove ownership before merging identities

Commands
--------
/channels           List all bots/channels linked to your account

Login codes are sent automatically when your session expires — no manual command needed.

Usage in a plugin::

    class MyPlugin(AuthFlowMixin, BasePlugin):
        async def handle(self, msg: InboundMessage, db) -> None:
            user = await self.get_authenticated_user(msg, db)
            if user is None:
                return          # auth flow is in progress
            msg.user_id = user.id
            # ... normal message handling
"""

from typing import Optional

from sqlalchemy.orm import Session

import auth_service
from models import User
from plugins.base import InboundMessage
from plugins import SUPPORTED_PLUGINS


_TYPE_COLORS = {
    "bank":        "#3b82f6",  # blue
    "credit_card": "#8b5cf6",  # purple
    "cash":        "#22c55e",  # green
    "metro_card":  "#14b8a6",  # teal
    "wallet":      "#f97316",  # orange
    "loan":        "#ef4444",  # red
    "chitty":      "#d97706",  # amber
    "other":       "#6b7280",  # gray
}

def _account_color(acc_type) -> str:
    return _TYPE_COLORS.get(str(acc_type.value if hasattr(acc_type, "value") else acc_type), "#6366f1")


class AuthFlowMixin:
    """
    Mixin for BasePlugin subclasses.
    Requires: self.name (str) and self.send_message(chat_id, text).
    """

    async def get_authenticated_user(
        self, msg: InboundMessage, db: Session
    ) -> Optional[User]:
        """
        Drive the auth conversation.
        Returns the authenticated User on success, None while the flow is ongoing.
        """
        platform = self.name
        chat_id  = msg.chat_id
        text     = (msg.text or "").strip()

        # ── /channels command ─────────────────────────────────────────────
        if text.lower() == "/channels":
            return await self._handle_channels_command(platform, chat_id, db)

        # ── In-progress conversation state ─────────────────────────────────
        state_row = auth_service.get_bot_state(platform, chat_id, db)
        if state_row:
            if state_row.state == "awaiting_name":
                return await self._handle_name(msg, db, state_row)
            if state_row.state == "awaiting_email":
                return await self._handle_email(msg, db, state_row)
            if state_row.state == "awaiting_otp":
                return await self._handle_otp(msg, db, state_row)
            if state_row.state == "awaiting_merge_otp":
                return await self._handle_merge_otp(msg, db, state_row)
            if state_row.state == "awaiting_email_otp":
                return await self._handle_email_otp(msg, db, state_row)
            if state_row.state == "awaiting_currency":
                return await self._handle_currency(msg, db, state_row)
            if state_row.state == "awaiting_accounts_setup":
                return await self._handle_accounts_setup(msg, db, state_row)

        # ── No in-progress state — check if user is known ──────────────────
        user = auth_service.get_user_by_bot(platform, chat_id, db)

        if user is None:
            await self.send_message(
                chat_id,
                "Welcome to PocketLog!\n\nLet's set up your account. What's your name?",
            )
            auth_service.set_bot_state(platform, chat_id, "awaiting_name", db)
            return None

        # Known user — check session
        session = auth_service.get_active_session_by_bot(platform, chat_id, db)
        if session:
            return user

        # Session expired — re-auth on the current channel
        await self._send_otp_here(user, platform, chat_id, db)
        return None

    # ── /channels command ──────────────────────────────────────────────────

    async def _handle_channels_command(
        self, platform: str, chat_id: str, db: Session
    ) -> Optional[User]:
        user = auth_service.get_user_by_bot(platform, chat_id, db)
        if user is None:
            await self.send_message(chat_id, "You're not signed up yet. What's your name?")
            auth_service.set_bot_state(platform, chat_id, "awaiting_name", db)
            return None
        identities = auth_service.get_user_bot_identities(user.id, db)
        lines = "\n".join(f"  • {i.platform.capitalize()}" for i in identities)
        await self.send_message(
            chat_id,
            f"*Your linked channels:*\n{lines}\n\n"
            f"Available: {', '.join(sorted(SUPPORTED_PLUGINS))}",
        )
        return None

    # ── Signup step handlers ───────────────────────────────────────────────

    async def _handle_name(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        platform = self.name
        chat_id  = msg.chat_id
        name     = (msg.text or "").strip()

        if len(name) < 2:
            await self.send_message(chat_id, "Please enter a valid name (at least 2 characters):")
            return None

        auth_service.set_bot_state(platform, chat_id, "awaiting_email", db, temp_name=name)
        await self.send_message(
            chat_id,
            f"Nice to meet you, {name}!\n\nNow please share your email address:",
        )
        return None

    async def _handle_email(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        platform = self.name
        chat_id  = msg.chat_id
        email    = (msg.text or "").strip().lower()

        if "@" not in email or "." not in email.split("@")[-1]:
            await self.send_message(chat_id, "That doesn't look like a valid email. Please try again:")
            return None

        existing_user = auth_service.get_user_by_email(email, db)

        if existing_user:
            # ── Email collision — verify ownership via existing bot ────────
            return await self._initiate_merge(
                existing_user, email, platform, chat_id, state_row, db
            )

        # Fresh signup — send email verification OTP
        import random
        from datetime import datetime, timedelta
        from email_service import send_email_otp

        otp     = f"{random.randint(0, 999999):06d}"
        expires = datetime.utcnow() + timedelta(minutes=10)
        name    = state_row.temp_name or "there"

        sent = send_email_otp(to_email=email, name=name, otp=otp)

        auth_service.set_bot_state(
            platform, chat_id, "awaiting_email_otp", db,
            temp_name=state_row.temp_name, temp_email=email,
            temp_otp=otp, temp_otp_expires_at=expires,
        )

        if sent:
            await self.send_message(
                chat_id,
                f"We sent a 6-digit verification code to *{email}*.\n\n"
                "Enter it here to continue:",
            )
        else:
            await self.send_message(
                chat_id,
                f"Enter the 6-digit code sent to *{email}*:\n\n"
                "_(Email delivery unavailable — check server logs)_",
            )
        return None

    async def _handle_email_otp(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        platform = self.name
        chat_id  = msg.chat_id
        code     = (msg.text or "").strip()

        from datetime import datetime
        if not state_row.temp_otp or not state_row.temp_otp_expires_at:
            await self.send_message(chat_id, "Something went wrong. Please start over by sending your email again.")
            auth_service.clear_bot_state(platform, chat_id, db)
            return None

        if datetime.utcnow() > state_row.temp_otp_expires_at:
            await self.send_message(
                chat_id,
                "That code has expired. Send your email again to get a new one.",
            )
            auth_service.set_bot_state(platform, chat_id, "awaiting_email", db,
                                       temp_name=state_row.temp_name)
            return None

        if code != state_row.temp_otp:
            await self.send_message(chat_id, "Invalid code. Please try again:")
            return None

        # Code verified — proceed to currency
        auth_service.set_bot_state(
            platform, chat_id, "awaiting_currency", db,
            temp_name=state_row.temp_name, temp_email=state_row.temp_email,
        )
        await self.send_message(
            chat_id,
            "✅ Email verified!\n\n"
            "What currency do you use? Enter a 3-letter code, e.g.:\n"
            "  *INR* — Indian Rupee\n"
            "  *USD* — US Dollar\n"
            "  *EUR* — Euro\n"
            "  *GBP* — British Pound\n"
            "  *AED* — UAE Dirham\n\n"
            "Find your code at xe.com/symbols",
        )
        return None

    async def _handle_currency(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        platform = self.name
        chat_id  = msg.chat_id
        code     = (msg.text or "").strip().upper()

        if len(code) != 3 or not code.isalpha():
            await self.send_message(
                chat_id,
                "Please enter a valid 3-letter currency code (e.g. INR, USD, EUR):",
            )
            return None

        name     = state_row.temp_name or "User"
        email    = state_row.temp_email

        # Check if this chat_id is already a guest identity — if so, merge instead of create
        existing_identity = db.query(
            __import__("models", fromlist=["BotIdentity"]).BotIdentity
        ).filter_by(platform=platform, chat_id=chat_id).first()

        if existing_identity and existing_identity.user and existing_identity.user.is_guest:
            user = auth_service.merge_guest_user(existing_identity.user, name, email, code, db)
        else:
            user = auth_service.create_user(name=name, email=email, primary_bot=platform, db=db, currency=code)
            auth_service.link_bot_identity(user.id, platform, chat_id, db)
        auth_service.create_session(user.id, db)
        from services import create_account
        from models import AccountType
        create_account(db, name="Cash", type=AccountType.cash, user_id=user.id,
                       color=_account_color(AccountType.cash), is_protected=True)
        auth_service.set_bot_state(platform, chat_id, "awaiting_accounts_setup", db, user_id=user.id)
        await self.send_message(
            chat_id,
            f"✅ Welcome, *{user.name}*! Currency set to *{code}*.\n\n"
            "Tell me what other accounts you use — one at a time.\n"
            "Examples: *HDFC Savings bank*, *HDFC Credit Card*, *Amazon Pay wallet*\n\n"
            "Say *done* when finished.",
        )
        return None

    async def _handle_otp(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        platform = self.name
        chat_id  = msg.chat_id
        code     = (msg.text or "").strip()

        session = auth_service.verify_otp(state_row.user_id, code, db)
        if not session:
            await self.send_message(
                chat_id,
                "Invalid or expired code. Try again, or send /login to get a new one.",
            )
            return None

        auth_service.clear_bot_state(platform, chat_id, db)
        user = db.query(User).get(state_row.user_id)
        await self.send_message(chat_id, f"✅ Logged in as *{user.name}*. What would you like to log?")
        return None

    async def _handle_accounts_setup(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        """Collect the user's spending accounts during onboarding, one per message."""
        platform = self.name
        chat_id  = msg.chat_id
        text     = (msg.text or "").strip()

        DONE_WORDS = {"done", "skip", "no", "nothing", "finish", "ok", "okay", "that's all", "thats all", "stop"}
        if text.lower() in DONE_WORDS:
            user = auth_service.get_user_by_bot(platform, chat_id, db)
            auth_service.clear_bot_state(platform, chat_id, db)
            await self.send_message(chat_id, self.welcome_text(user))
            return None

        from services import parse_account_with_ai, create_account
        from models import AccountType
        parsed = parse_account_with_ai(text)
        if not parsed.valid:
            await self.send_message(
                chat_id,
                "I didn't quite get that. Try something like *HDFC Savings bank* or *HDFC Credit Card*.\n"
                "Say *done* when finished.",
            )
            return None

        try:
            acc_type = AccountType(parsed.type)
        except ValueError:
            await self.send_message(
                chat_id,
                f"Unknown type *{parsed.type}*. Try: bank, credit card, cash, wallet, metro card, loan.\n"
                "Say *done* when finished.",
            )
            return None

        user = auth_service.get_user_by_bot(platform, chat_id, db)
        create_account(db, name=parsed.name, type=acc_type, user_id=user.id,
                       color=_account_color(acc_type))
        type_label = parsed.type.replace("_", " ").title()
        await self.send_message(
            chat_id,
            f"✅ Added *{parsed.name}* ({type_label}).\n\nAnything else? Say *done* when finished.",
        )
        return None

    # ── Merge / cross-plugin verification ─────────────────────────────────

    async def _initiate_merge(
        self, existing_user: User, email: str, platform: str, chat_id: str, state_row, db: Session
    ) -> Optional[User]:
        """
        Email already exists under another account.
        Send OTP to the existing user's primary bot to prove ownership.
        If verified → link this new bot identity and log in.
        """
        primary_bot = existing_user.primary_bot
        identities  = auth_service.get_user_bot_identities(existing_user.id, db)

        # Prefer the user's primary bot if it's supported; fall back to any supported identity
        primary_identity = next(
            (i for i in identities
             if i.platform == primary_bot and i.platform in SUPPORTED_PLUGINS),
            None,
        )
        if primary_identity is None:
            primary_identity = next(
                (i for i in identities if i.platform in SUPPORTED_PLUGINS), None
            )

        if primary_identity is None:
            # No bot identity found for that user — shouldn't happen, but be safe
            await self.send_message(
                chat_id,
                "An account with this email already exists but we couldn't verify it. "
                "Please use a different email or contact support.",
            )
            return None

        # Check if this same (platform, chat_id) is already linked to that user
        if primary_identity.platform == platform and primary_identity.chat_id == chat_id:
            # Same identity — user is just re-authing
            otp = auth_service.generate_otp(existing_user.id, db)
            await self.send_message(
                chat_id,
                f"Welcome back! Your login code is:\n\n*{otp}*\n\nIt expires in 10 minutes.",
            )
            auth_service.set_bot_state(platform, chat_id, "awaiting_otp", db, user_id=existing_user.id)
            return None

        otp = auth_service.generate_otp(existing_user.id, db)

        # Deliver OTP to the existing user's registered bot
        sent = await self._deliver_otp_to_identity(primary_identity, otp, email)

        if sent:
            await self.send_message(
                chat_id,
                f"An account with *{email}* already exists.\n\n"
                f"We sent a verification code to your *{primary_identity.platform}* bot. "
                f"Enter it here to link this bot and log in.",
            )
        else:
            # Registry doesn't have that plugin running — tell user
            await self.send_message(
                chat_id,
                f"An account with *{email}* already exists, but the "
                f"{primary_identity.platform} bot needed to verify you isn't available right now.\n\n"
                f"Please log in from your {primary_identity.platform} account, or use a different email.",
            )
            return None

        auth_service.set_bot_state(
            platform, chat_id, "awaiting_merge_otp", db,
            user_id=existing_user.id,
            temp_email=email,
        )
        return None

    async def _handle_merge_otp(
        self, msg: InboundMessage, db: Session, state_row
    ) -> Optional[User]:
        """Verify OTP for a cross-plugin identity merge."""
        platform = self.name
        chat_id  = msg.chat_id
        code     = (msg.text or "").strip()

        session = auth_service.verify_otp(state_row.user_id, code, db)
        if not session:
            email = state_row.temp_email or "that email"
            await self.send_message(
                chat_id,
                f"Invalid or expired code.\n\n"
                f"If *{email}* is not your account, please use a different email — "
                f"send your email address to start over.\n\n"
                f"Otherwise, send /login to request a new code to your primary bot.",
            )
            return None

        # Ownership confirmed — link this bot identity to the existing user
        auth_service.link_bot_identity(state_row.user_id, platform, chat_id, db)
        auth_service.clear_bot_state(platform, chat_id, db)

        user = db.query(User).get(state_row.user_id)
        await self.send_message(
            chat_id,
            f"✅ *{platform.capitalize()}* linked to *{user.email}*! Logged in as *{user.name}*.",
        )
        return None

    # ── OTP delivery helpers ───────────────────────────────────────────────

    async def _send_otp_here(
        self, user: User, platform: str, chat_id: str, db: Session
    ) -> None:
        """Send OTP to the current channel and set awaiting_otp state."""
        otp = auth_service.generate_otp(user.id, db)
        await self.send_message(
            chat_id,
            f"Your login code is:\n\n*{otp}*\n\nIt expires in 10 minutes.",
        )
        auth_service.set_bot_state(platform, chat_id, "awaiting_otp", db, user_id=user.id)

    async def _deliver_otp_to_identity(self, identity, otp: str, email: str) -> bool:
        """
        Send OTP message via the plugin registered for identity.platform.
        Returns True if the plugin was found and message was dispatched.
        """
        from plugins.registry import get as get_plugin
        plugin = get_plugin(identity.platform)
        if plugin is None:
            return False
        await plugin.send_message(
            identity.chat_id,
            f"Login verification code for *{email}*:\n\n*{otp}*\n\nExpires in 10 minutes.",
        )
        return True
