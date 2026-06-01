"""
plugins/auth_flow.py — Reusable auth flow mixin for all bot plugins.

Conversation states
-------------------
awaiting_name       New user → collecting name
awaiting_email      New user → collecting email
awaiting_otp        OTP sent to current channel, awaiting entry
awaiting_merge_otp  Email belongs to existing account; OTP sent to their
                    primary bot to prove ownership before merging identities

Commands
--------
/login              Re-trigger OTP to the current channel (session expired)
/login <platform>   Send OTP to a different linked bot (e.g. /login telegram)
/login list         Show which bots are linked to your account

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

        # ── /login command handling ────────────────────────────────────────
        if text.lower().startswith("/login"):
            parts = text.split()
            arg   = parts[1].lower() if len(parts) > 1 else None
            return await self._handle_login_command(platform, chat_id, arg, db)

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

        # ── No in-progress state — check if user is known ──────────────────
        user = auth_service.get_user_by_bot(platform, chat_id, db)

        if user is None:
            await self.send_message(
                chat_id,
                "Welcome to Hisaab!\n\nLet's set up your account. What's your name?",
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

    # ── /login command ─────────────────────────────────────────────────────

    async def _handle_login_command(
        self, platform: str, chat_id: str, arg: Optional[str], db: Session
    ) -> Optional[User]:
        user = auth_service.get_user_by_bot(platform, chat_id, db)

        if user is None:
            # Unknown user — start signup instead
            await self.send_message(chat_id, "Welcome! What's your name?")
            auth_service.set_bot_state(platform, chat_id, "awaiting_name", db)
            return None

        if arg == "list":
            identities = auth_service.get_user_bot_identities(user.id, db)
            platforms  = ", ".join(i.platform for i in identities)
            await self.send_message(
                chat_id,
                f"Your linked bots: *{platforms}*\n\nTo receive OTP on a specific bot: /login <name>\n"
                f"Supported: {', '.join(sorted(SUPPORTED_PLUGINS))}",
            )
            return None

        if arg and arg != platform:
            if arg not in SUPPORTED_PLUGINS:
                await self.send_message(
                    chat_id,
                    f"*{arg}* is not a supported bot.\nSupported: {', '.join(sorted(SUPPORTED_PLUGINS))}",
                )
                return None
            # User wants OTP on a different linked bot
            return await self._send_otp_to_other_platform(user, platform, chat_id, arg, db)

        # Default: send OTP here
        await self._send_otp_here(user, platform, chat_id, db)
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

        # Fresh signup
        name = state_row.temp_name or "User"
        user = auth_service.create_user(name=name, email=email, primary_bot=platform, db=db)
        auth_service.link_bot_identity(user.id, platform, chat_id, db)

        otp = auth_service.generate_otp(user.id, db)
        await self.send_message(
            chat_id,
            f"Account created!\n\nYour login code is:\n\n*{otp}*\n\nIt expires in 10 minutes.",
        )
        auth_service.set_bot_state(platform, chat_id, "awaiting_otp", db, user_id=user.id)
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
        await self.send_message(chat_id, self.welcome_text(user))
        return user

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
            f"*{platform.capitalize()}* linked to *{user.email}*!\n\n" + self.welcome_text(user),
        )
        return user

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

    async def _send_otp_to_other_platform(
        self, user: User, current_platform: str, current_chat_id: str,
        target_platform: str, db: Session
    ) -> Optional[User]:
        """Route OTP to a different linked bot when user explicitly requests it."""
        identities = auth_service.get_user_bot_identities(user.id, db)
        target = next((i for i in identities if i.platform == target_platform), None)

        if target is None:
            linked = ", ".join(i.platform for i in identities)
            await self.send_message(
                current_chat_id,
                f"You don't have *{target_platform}* linked to your account.\n"
                f"Linked bots: *{linked}*",
            )
            return None

        otp  = auth_service.generate_otp(user.id, db)
        sent = await self._deliver_otp_to_identity(target, otp, user.email)

        if not sent:
            await self.send_message(
                current_chat_id,
                f"The *{target_platform}* bot isn't available right now. "
                f"Sending the code here instead.",
            )
            await self._send_otp_here(user, current_platform, current_chat_id, db)
            return None

        await self.send_message(
            current_chat_id,
            f"Code sent to your *{target_platform}* bot. Enter it here to log in.",
        )
        auth_service.set_bot_state(
            current_platform, current_chat_id, "awaiting_otp", db, user_id=user.id
        )
        return None

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
