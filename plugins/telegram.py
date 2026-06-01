"""
plugins/telegram.py — Telegram channel adapter (multi-user).

Registers a FastAPI router at /plugins/telegram/webhook.
Auth (signup + OTP login) is handled by AuthFlowMixin before any expense parsing.
All business logic is delegated to services.py via BasePlugin helpers.

Setup:
    export TELEGRAM_BOT_TOKEN=<your-bot-token>
    # Register webhook once:
    curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://yourdomain.com/plugins/telegram/webhook"
    # Note: router prefix is /telegram, mounted under /plugins → full path: /plugins/telegram/webhook

Commands:
    /login  — request a new OTP (e.g. after session expiry)
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from models import SessionLocal
from plugins.auth_flow import AuthFlowMixin
from plugins.base import BasePlugin, InboundMessage
from plugins.registry import register as register_plugin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class TelegramPlugin(AuthFlowMixin, BasePlugin):
    """Telegram adapter — only knows about HTTP calls to Telegram's Bot API."""

    def __init__(self):
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        register_plugin(self)

    @property
    def name(self) -> str:
        return "telegram"

    def welcome_text(self, user) -> str:
        return (
            f"👋 Welcome, *{user.name}*!\n"
            f"Logged in as `{user.email}`\n\n"
            "Just send me what you spent — in plain language — and I'll log it.\n\n"
            "*Logging examples:*\n"
            "  `spent 450 on lunch`\n"
            "  `paid 1200 electricity bill from HDFC`\n"
            "  `got salary 85000`\n\n"
            "*Reports:*\n"
            "  /report — this month's summary\n"
            "  /report last — last month\n"
            "  /report jan — January (add year for past: `jan 2024`)\n"
            "  /report trend — 6-month spending trend\n"
            "  /report trend 3 — last 3 months\n\n"
            "*Account:*\n"
            "  /login — request a new login code\n"
            "  /login list — show your linked bots\n"
            "  /apikey — get your API access token\n"
        )

    async def send_message(self, chat_id: str, text: str) -> None:
        if not self._token:
            log.warning("TELEGRAM_BOT_TOKEN not set — skipping send")
            return
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )

    async def handle(self, msg: InboundMessage, db: Session) -> None:
        # Auth gate — handles signup/OTP flow; returns User or None
        user = await self.get_authenticated_user(msg, db)
        if user is None:
            return  # auth flow is in progress

        # Attach authenticated user to message
        msg.user_id = user.id

        text = (msg.text or "").strip()

        # ── /apikey — return the user's Personal Access Token ─────────────
        if text.lower() == "/apikey":
            import oauth_service as _oas
            session = _oas.get_or_create_session(user.id, db)
            await self.send_message(
                msg.chat_id,
                f"Your Personal Access Token:\n\n`{session.token}`\n\n"
                f"Use it as:\n`Authorization: Bearer {session.token}`\n\n"
                f"Valid for 30 days. Keep it secret.",
            )
            return

        if not msg.text:
            await self.send_message(msg.chat_id, "Send me a text message describing your expense.")
            return

        # ── Report request? ───────────────────────────────────────────────
        report = await self.maybe_get_report(msg, db)
        if report is not None:
            await self.send_message(msg.chat_id, report)
            return

        # ── Expense / income entry ────────────────────────────────────────
        try:
            parsed = self.parse(msg, db)
        except Exception as exc:
            log.exception("AI parse failed")
            await self.send_message(msg.chat_id, f"Couldn't parse that: {exc}")
            return

        if parsed.missing:
            await self.send_message(msg.chat_id, self.missing_prompt(parsed))
            return

        try:
            self.save(parsed, db, msg)
        except Exception as exc:
            log.exception("Save failed")
            await self.send_message(msg.chat_id, f"Couldn't save: {exc}")
            return

        warning = self.budget_warning(parsed, db)
        await self.send_message(msg.chat_id, (parsed.reply or "Logged!") + warning)


# Singleton plugin instance
_plugin = TelegramPlugin()


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    message = payload.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    msg_id  = str(message.get("message_id", ""))
    text    = message.get("text", "") or message.get("caption", "")

    if not chat_id:
        return {"ok": False, "reason": "no chat_id"}

    msg = InboundMessage(
        text       = text or None,
        source_ref = msg_id,
        chat_id    = chat_id,
        raw        = payload,
    )
    await _plugin.handle(msg, db)
    return {"ok": True}
