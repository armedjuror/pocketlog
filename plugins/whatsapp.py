"""
plugins/whatsapp.py — WhatsApp channel adapter (skeleton / template).

Uses the WhatsApp Business Cloud API (Meta).
Required env vars:
    WHATSAPP_TOKEN           — your permanent access token
    WHATSAPP_PHONE_NUMBER_ID — the sender phone number id
    WHATSAPP_VERIFY_TOKEN    — any string you choose for webhook verification
    WHATSAPP_ALLOWED_NUMBER  — your personal WhatsApp number (e.g. 919876543210)

To activate, add to plugins/__init__.py:
    from plugins.whatsapp import router as whatsapp_router
    routers = [..., whatsapp_router]

Then register the webhook in Meta developer console:
    Callback URL: https://yourdomain.com/plugins/whatsapp/webhook
    Verify token: whatever you set in WHATSAPP_VERIFY_TOKEN
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from models import SessionLocal
from plugins.base import BasePlugin, InboundMessage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/plugins/whatsapp", tags=["whatsapp"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class WhatsAppPlugin(BasePlugin):
    """
    WhatsApp Business Cloud API adapter.
    Inherits parse(), save(), budget_warning() from BasePlugin — zero business logic here.
    """

    def __init__(self):
        self._token      = os.getenv("WHATSAPP_TOKEN", "")
        self._phone_id   = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self._allowed    = os.getenv("WHATSAPP_ALLOWED_NUMBER", "")

    @property
    def name(self) -> str:
        return "whatsapp"

    async def send_message(self, chat_id: str, text: str) -> None:
        """chat_id here is the recipient's phone number (e.g. '919876543210')."""
        if not self._token or not self._phone_id:
            log.warning("WhatsApp credentials not set — skipping send")
            return
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://graph.facebook.com/v19.0/{self._phone_id}/messages",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": chat_id,
                    "type": "text",
                    "text": {"body": text},
                },
                timeout=10,
            )

    async def handle(self, msg: InboundMessage, db: Session) -> None:
        # Identical flow to Telegram — parse → check missing → save → reply
        if not msg.text:
            await self.send_message(msg.chat_id, "Send me a text describing your expense.")
            return
        try:
            parsed = self.parse(msg, db)
        except Exception as exc:
            log.exception("AI parse failed")
            await self.send_message(msg.chat_id, f"❌ Couldn't parse: {exc}")
            return

        if parsed.missing:
            await self.send_message(msg.chat_id, self.missing_prompt(parsed))
            return

        try:
            self.save(parsed, db, msg)
        except Exception as exc:
            log.exception("Save failed")
            await self.send_message(msg.chat_id, f"❌ Couldn't save: {exc}")
            return

        warning = self.budget_warning(parsed, db)
        await self.send_message(msg.chat_id, (parsed.reply or "✅ Logged!") + warning)


_plugin = WhatsAppPlugin()


# ── Webhook verification (GET) ─────────────────────────────────────────────

@router.get("/webhook")
async def verify_webhook(
    hub_mode:       str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge:  str = Query(None, alias="hub.challenge"),
):
    """Meta calls this once to verify your webhook URL."""
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if hub_mode == "subscribe" and hub_verify_token == expected:
        return int(hub_challenge)
    return {"error": "invalid verify token"}, 403


# ── Inbound messages (POST) ────────────────────────────────────────────────

@router.post("/webhook")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    try:
        entry   = payload["entry"][0]["changes"][0]["value"]
        message = entry["messages"][0]
        from_   = message["from"]                      # sender's phone number
        msg_id  = message["id"]
        text    = message.get("text", {}).get("body")
    except (KeyError, IndexError):
        # Could be a status update or delivery receipt — ignore
        return {"ok": True}

    # Security: only accept from your own number
    if _plugin._allowed and from_ != _plugin._allowed:
        return {"ok": False, "reason": "unauthorized"}

    msg = InboundMessage(
        text       = text,
        source_ref = msg_id,
        chat_id    = from_,
        raw        = payload,
    )
    await _plugin.handle(msg, db)
    return {"ok": True}
