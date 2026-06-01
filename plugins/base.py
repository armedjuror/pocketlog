"""
plugins/base.py — Abstract base for all messaging plugins.

A plugin is a thin adapter between an external channel (Telegram, WhatsApp,
email…) and the core services layer. It must:

  1. Receive an inbound message (text / image / voice / document).
  2. Optionally call services.parse_message_with_ai() to extract transaction data.
  3. Call the appropriate service function (create_transaction, etc.).
  4. Send a reply back through its own channel.

Plugins must NOT contain any business logic — that all lives in services.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING


@dataclass
class InboundMessage:
    """Normalised message from any channel."""
    text:        Optional[str]   = None   # plain text or voice transcription
    source_ref:  Optional[str]   = None   # channel-native message id
    chat_id:     Optional[str]   = None   # channel-native user/chat id
    media_url:   Optional[str]   = None   # image / invoice URL if any
    raw:         Optional[dict]  = None   # original payload for debugging
    user_id:     Optional[int]   = None   # set after authentication


class BasePlugin(ABC):
    """
    Subclass this for every new channel.

    Minimal implementation::

        class MyPlugin(BasePlugin):
            @property
            def name(self) -> str:
                return "myplugin"

            async def send_message(self, chat_id: str, text: str) -> None:
                ...   # channel-specific send

            async def handle(self, msg: InboundMessage, db) -> None:
                parsed = self.parse(msg, db)   # calls AI parser via services
                if parsed.missing:
                    await self.send_message(msg.chat_id, self.missing_prompt(parsed))
                    return
                self.save(parsed, db, msg)     # calls services.create_transaction
                status = self.budget_warning(parsed, db)
                await self.send_message(msg.chat_id, parsed.reply + status)
    """

    # ── Must override ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique slug stored in Transaction.source_plugin, e.g. 'telegram'."""
        ...

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a text reply back through this channel."""
        ...

    @abstractmethod
    async def handle(self, msg: InboundMessage, db) -> None:
        """Entry point called by the transport layer for each inbound message."""
        ...

    # ── Helpers available to all subclasses ───────────────────────────────

    def parse(self, msg: InboundMessage, db) -> "services.ParsedTransaction":  # noqa: F821
        from services import parse_message_with_ai, get_accounts, get_categories
        accounts   = get_accounts(db, user_id=msg.user_id)
        categories = get_categories(db)
        return parse_message_with_ai(msg.text or "", accounts, categories)

    def save(self, parsed: "services.ParsedTransaction", db, msg: InboundMessage) -> dict:  # noqa: F821
        from services import create_transaction
        return create_transaction(
            db,
            amount       = parsed.amount,
            description  = parsed.description,
            date         = parsed.date,
            account_id   = parsed.account_id,
            type         = parsed.type,
            category_id  = parsed.category_id,
            note         = parsed.note,
            source_plugin= self.name,
            source_ref   = msg.source_ref,
            user_id      = msg.user_id,
        )

    def budget_warning(self, parsed: "services.ParsedTransaction", db) -> str:  # noqa: F821
        """Returns a warning string if the category is over daily pace, else ''."""
        from datetime import date
        from services import get_budget_status
        if not parsed.category_id:
            return ""
        today = date.today()
        status = get_budget_status(db, parsed.category_id, today.month, today.year)
        if status and status.over_pace:
            return (
                f"\n\n⚠️ You're ₹{status.over_pace_by:,.0f} over daily pace"
                f" in *{status.category_name}* this month."
            )
        return ""

    async def maybe_get_report(self, msg: InboundMessage, db) -> Optional[str]:
        """
        Check whether the message is asking for an analytics report.
        If yes, generates and returns the formatted report string.
        If no, returns None so the caller can proceed with expense parsing.

        Available to every plugin automatically — no extra implementation needed.
        """
        from services import parse_report_request, generate_report
        period = parse_report_request(msg.text or "")
        if period is None:
            return None
        return generate_report(db, period, user_id=msg.user_id)

    def welcome_text(self, user: "models.User") -> str:  # noqa: F821
        """
        Message sent to a user immediately after a successful login.
        Override in each plugin to use platform-native formatting and
        list the commands that plugin specifically supports.
        """
        return (
            f"Logged in as {user.email}!\n\n"
            "Just tell me what you spent to log an expense.\n\n"
            "Commands:\n"
            "  /report          — this month's summary\n"
            "  /report <period> — e.g. last month, jan, trend\n"
            "  /login           — re-authenticate\n"
            "  /apikey          — get your API access token\n"
        )

    @staticmethod
    def missing_prompt(parsed: "services.ParsedTransaction") -> str:  # noqa: F821
        fields = ", ".join(parsed.missing)
        return (
            f"🤔 Almost there! I couldn't figure out: *{fields}*.\n"
            "Reply with the missing details and I'll log it."
        )
