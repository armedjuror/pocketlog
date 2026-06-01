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
    /channels  — list linked bots/channels
    /help      — show all commands
"""

import asyncio
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
        return f"👋 Welcome, *{user.name}*!\nLogged in as `{user.email}`\n\n" + self._help_text()

    @staticmethod
    def _help_text() -> str:
        return (
            "*Expenses & income* — text or 🎙️ voice note:\n"
            "  `spent 450 on lunch`\n"
            "  `paid 1200 electricity from HDFC`\n"
            "  `got salary 85000`\n\n"
            "*Lending:*\n"
            "  `lent 2000 to Rahul`\n"
            "  `borrowed 5000 from Priya`\n"
            "  `who owes me` — unsettled lent amounts\n"
            "  `what do I owe` — unsettled borrowed amounts\n\n"
            "*Group splits (in group chats):*\n"
            "  `split 1200 lunch` — split equally among all\n"
            "  `split 1200 lunch between Alice, Bob` — named split\n"
            "  `split 1200 lunch 1:2:3 between Alice, Bob, Priya` — ratio split\n"
            "  /groupbalance — show who owes whom\n\n"
            "*Quick queries:*\n"
            "  `my accounts` — balances\n"
            "  `my categories` — list categories\n"
            "  `my budgets` — this month's budget progress\n"
            "  `what did I spend today`\n\n"
            "*Reports:*\n"
            "  /report — this month's summary\n"
            "  /report last — last month\n"
            "  /report jan — January (add year: `jan 2024`)\n"
            "  /report trend — 6-month spending trend\n\n"
            "*Accounts:*\n"
            "  `my accounts` — list balances\n"
            "  `add HDFC Savings as bank account` — create account\n"
            "  `delete all accounts` — remove all your accounts\n\n"
            "*Other:*\n"
            "  /channels — show your linked bots/channels\n"
            "  /apikey — get your API access token\n"
            "  /help — show this message\n"
        )

    async def _get_file_path(self, file_id: str) -> str | None:
        """Resolve a Telegram file_id to a direct download URL."""
        if not self._token:
            return None
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.telegram.org/bot{self._token}/getFile",
                params={"file_id": file_id},
                timeout=10,
            )
            data = r.json()
            if data.get("ok") and data["result"].get("file_path"):
                return f"https://api.telegram.org/file/bot{self._token}/{data['result']['file_path']}"
        return None

    async def transcribe_voice(self, file_id: str) -> str | None:
        """Download a Telegram voice/audio file and transcribe it with Whisper (runs in thread pool)."""
        import asyncio
        import tempfile, os
        url = await self._get_file_path(file_id)
        if not url:
            return None
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=60)
            r.raise_for_status()
        suffix = ".oga" if ".oga" in url else ".mp3" if ".mp3" in url else ".ogg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(r.content)
            tmp_path = f.name
        try:
            from services import transcribe_audio
            return await asyncio.get_event_loop().run_in_executor(None, transcribe_audio, tmp_path)
        finally:
            os.unlink(tmp_path)

    async def ocr_photo(self, file_id: str) -> str | None:
        """Download a Telegram photo and extract text via RapidOCR (runs in thread pool)."""
        import asyncio
        import tempfile, os
        url = await self._get_file_path(file_id)
        if not url:
            return None
        async with httpx.AsyncClient() as client:
            r = await client.get(url, timeout=60)
            r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(r.content)
            tmp_path = f.name
        try:
            from services import ocr_image
            return await asyncio.get_event_loop().run_in_executor(None, ocr_image, tmp_path)
        finally:
            os.unlink(tmp_path)

    async def send_message(self, chat_id: str, text: str) -> int | None:
        """Send a message and return its message_id."""
        if not self._token:
            log.warning("TELEGRAM_BOT_TOKEN not set — skipping send")
            return None
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        data = r.json()
        if not data.get("ok"):
            log.error(
                "Telegram sendMessage failed for chat_id=%s: %s",
                chat_id,
                data.get("description", data),
            )
            raise RuntimeError(data.get("description", "Telegram delivery failed"))
        return data["result"]["message_id"]

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        if not self._token or not message_id:
            return
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{self._token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=10,
            )

    async def send_transient_message(self, chat_id: str, text: str, delay: float = 3.0) -> None:
        """Send a status message and delete it after `delay` seconds."""
        message_id = await self.send_message(chat_id, text)
        async def _delete_later():
            await asyncio.sleep(delay)
            await self.delete_message(chat_id, message_id)
        asyncio.create_task(_delete_later())

    async def handle(self, msg: InboundMessage, db: Session) -> None:
        # Auth gate — handles signup/OTP flow; returns User or None
        user = await self.get_authenticated_user(msg, db)
        if user is None:
            return  # auth flow is in progress

        # Attach authenticated user to message
        msg.user_id = user.id

        text = (msg.text or "").strip()

        # ── /help ─────────────────────────────────────────────────────────
        if text.lower() == "/help":
            await self.send_message(msg.chat_id, self._help_text())
            return

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

        # ── Voice / audio note? Transcribe with Whisper ───────────────────
        if not msg.text and msg.raw:
            message = msg.raw.get("message", {})
            voice = message.get("voice") or message.get("audio")
            if voice:
                await self.send_transient_message(msg.chat_id, "🎙️ Transcribing your voice note…")
                try:
                    transcript = await self.transcribe_voice(voice["file_id"])
                    if transcript:
                        await self.send_transient_message(msg.chat_id, "🎙️ Transcription: \n" + transcript)
                        msg.text = transcript
                        log.info("Voice transcribed: %s", transcript)
                    else:
                        await self.send_message(msg.chat_id, "Couldn't transcribe the audio. Please try again.")
                        return
                except Exception:
                    log.exception("Voice transcription failed")
                    await self.send_message(msg.chat_id, "Voice transcription failed. Send a text message instead.")
                    return

        # ── Photo/invoice image? OCR ──────────────────────────────────────
        # Always OCR photos — even when a caption is present (caption + OCR text both go to AI)
        photo = msg.raw and msg.raw.get("message", {}).get("photo", [])
        if photo:
            await self.send_transient_message(msg.chat_id, "🔍 Reading your invoice…")
            try:
                ocr_text = await self.ocr_photo(photo[-1]["file_id"])
                if ocr_text:
                    caption = msg.raw.get("message", {}).get("caption", "")
                    receipt_block = "RECEIPT:\n" + ocr_text
                    msg.text = (receipt_block + '\n' + caption).strip() if caption else receipt_block
                    log.info("OCR extracted: %s", msg.text[:100])
                else:
                    await self.send_message(msg.chat_id, "Couldn't read the image. Try typing the amount instead.")
                    return
            except Exception as exc:
                log.exception("OCR failed")
                await self.send_message(
                    msg.chat_id,
                    f"Image reading failed: {exc}\n\nTry typing the expense as text instead."
                )
                return

        if not msg.text:
            await self.send_message(msg.chat_id, "Send me a text message, a voice note, or a photo of your receipt.")
            return

        if not msg.text.startswith("RECEIPT"):
            # ── Account management (delete all / create)? ────────────────────
            manage_reply = await self.maybe_manage_accounts(msg, db)
            if manage_reply is not None:
                await self.send_message(msg.chat_id, manage_reply)
                return

            # ── Set / update account balance? ────────────────────────────────
            balance_reply = self.maybe_update_account_balance(msg, db)
            if balance_reply is not None:
                await self.send_message(msg.chat_id, balance_reply)
                return

            # ── Account list? ─────────────────────────────────────────────────
            accounts_reply = self.maybe_list_accounts(msg, db)
            if accounts_reply is not None:
                await self.send_message(msg.chat_id, accounts_reply)
                return

            # ── Category list? ────────────────────────────────────────────────
            categories_reply = self.maybe_list_categories(msg, db)
            if categories_reply is not None:
                await self.send_message(msg.chat_id, categories_reply)
                return

            # ── Budget summary? ───────────────────────────────────────────────
            budget_reply = self.maybe_list_budgets(msg, db)
            if budget_reply is not None:
                await self.send_message(msg.chat_id, budget_reply)
                return

            # ── Today's spends? ───────────────────────────────────────────────
            today_reply = self.maybe_get_today_spends(msg, db)
            if today_reply is not None:
                await self.send_message(msg.chat_id, today_reply)
                return

            # ── Lending? ──────────────────────────────────────────────────────
            lending_reply = await self.maybe_handle_lending(msg, db)
            if lending_reply is not None:
                await self.send_message(msg.chat_id, lending_reply)
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

        # Treat as chat if AI flagged it, or if there's no amount and nothing is missing
        # (AI occasionally misclassifies greetings as transactions with empty missing list)
        if parsed.chat or (parsed.amount is None and not parsed.missing):
            await self.send_message(msg.chat_id, parsed.reply or "Hey! Send me an expense or income to log.")
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

        warning = self.budget_warning(parsed, db, user_id=msg.user_id)
        await self.send_message(msg.chat_id, (parsed.reply or "Logged!") + warning)

    # ── Group / Splitwise handlers ─────────────────────────────────────────

    async def handle_group_message(
        self, msg: InboundMessage, group_payload: dict, db: Session
    ) -> None:
        """Entry point for messages from Telegram group/supergroup chats."""
        import re
        from services import (
            get_or_create_group, get_or_create_member,
            get_user_currency, currency_symbol,
        )
        import auth_service as _auth

        message   = group_payload.get("message", {})
        chat      = message.get("chat", {})
        from_user = message.get("from", {})

        chat_id_str      = str(chat.get("id", ""))
        chat_title       = chat.get("title", "")
        platform_user_id = str(from_user.get("id", ""))
        display_name     = (
            " ".join(filter(None, [from_user.get("first_name"), from_user.get("last_name")]))
            or from_user.get("username", "Unknown")
        )
        username = from_user.get("username")   # without @

        # Get/create the GroupChat record
        group = get_or_create_group("telegram", chat_id_str, chat_title, db)

        # Look up whether this sender has a registered PocketLog account
        linked_user = _auth.get_user_by_bot("telegram", platform_user_id, db)
        user_id = linked_user.id if linked_user else None

        # If not registered, create a guest user so they can be included in splits
        if user_id is None:
            guest = _auth.get_or_create_guest_user(
                "telegram", platform_user_id, display_name, db
            )
            user_id = guest.id

        # Get/create GroupMember
        member = get_or_create_member(group, platform_user_id, display_name, username, user_id, db)

        text = (msg.text or "").strip()

        # Route to group-specific handlers
        handled = await self.maybe_handle_group_split(msg, group, member, db)
        if handled:
            return

        handled = await self.maybe_handle_group_balance(msg, group, db)
        if handled:
            return

        # Ignore other group messages silently (don't reply to every group message)

    async def maybe_handle_group_split(
        self, msg: InboundMessage, group, sender_member, db: Session
    ) -> bool:
        """
        Detect and handle split commands in group chats.
        Returns True if handled.
        """
        import re
        from datetime import date as _date
        from services import (
            parse_group_split_with_ai, create_group_expense,
            get_group_members, get_user_currency, currency_symbol,
        )
        import auth_service as _auth

        text = (msg.text or "").strip()

        # Trigger pattern: starts with "split" or contains "split ... between"
        if not re.search(r'^\s*split\b|\bsplit\b.{0,60}\bbetween\b', text, re.IGNORECASE):
            return False

        # Collect all group members for name resolution
        all_members = get_group_members(group.id, db)
        member_names = [m["display_name"] for m in all_members]
        if not member_names:
            member_names = [sender_member.display_name]

        try:
            parsed = parse_group_split_with_ai(text, member_names)
        except Exception as exc:
            log.exception("Group split AI parse failed")
            await self.send_message(msg.chat_id, f"Couldn't parse the split: {exc}")
            return True

        if parsed.missing or parsed.amount is None:
            await self.send_message(msg.chat_id, "Please include the amount — e.g. `split 1200 lunch`")
            return True

        # Resolve member names to GroupMember records
        def _fuzzy_find(name: str):
            name_lower = name.lower()
            # Exact display_name match first
            for m in all_members:
                if m["display_name"].lower() == name_lower:
                    return m
            # Username match
            for m in all_members:
                if m.get("username") and m["username"].lower() == name_lower.lstrip("@"):
                    return m
            # Partial match
            for m in all_members:
                if name_lower in m["display_name"].lower() or m["display_name"].lower() in name_lower:
                    return m
            return None

        if parsed.members:
            resolved = []
            unresolved = []
            for name in parsed.members:
                m = _fuzzy_find(name)
                if m:
                    resolved.append(m)
                else:
                    unresolved.append(name)

            if unresolved:
                # Create guest users for unresolved @mentions
                for name in unresolved:
                    clean = name.lstrip("@")
                    # Check if it looks like a username
                    guest = _auth.get_or_create_guest_user("telegram", f"guest_{clean}", clean, db)
                    from services import get_or_create_member as _gom
                    gm = _gom(group, f"guest_{clean}", clean, clean, guest.id, db)
                    resolved.append({
                        "id": gm.id, "display_name": gm.display_name,
                        "username": gm.username, "user_id": gm.user_id, "is_guest": True,
                    })
                    await self.send_message(
                        msg.chat_id,
                        f"👋 @{clean} — you've been added to a split! "
                        f"DM me /start to register and see your balance.",
                    )
        else:
            # All members
            resolved = all_members

        if not resolved:
            await self.send_message(msg.chat_id, "No members found to split among.")
            return True

        # Build shares list
        if parsed.ratios and len(parsed.ratios) == len(resolved):
            shares = [{"member_id": m["id"], "ratio": r} for m, r in zip(resolved, parsed.ratios)]
        else:
            shares = [{"member_id": m["id"], "ratio": 1.0} for m in resolved]

        # Ensure sender (payer) is included
        payer_ids = [s["member_id"] for s in shares]
        if sender_member.id not in payer_ids:
            shares.append({"member_id": sender_member.id, "ratio": 1.0})

        try:
            result = create_group_expense(
                group_chat_id=group.id,
                paid_by_member_id=sender_member.id,
                amount=parsed.amount,
                description=parsed.description or "group expense",
                expense_date=_date.today(),
                shares=shares,
                db=db,
            )
        except Exception as exc:
            log.exception("create_group_expense failed")
            await self.send_message(msg.chat_id, f"Failed to save split: {exc}")
            return True

        # Format confirmation
        from services import get_user_currency, currency_symbol
        sym = currency_symbol(get_user_currency(db, sender_member.user_id))
        lines = [f"Split *{sym}{parsed.amount:,.0f}* — _{parsed.description}_"]
        for s in result["shares"]:
            suffix = " (guest)" if not any(
                m["id"] == s["member_id"] and not m.get("is_guest") for m in all_members
            ) else ""
            lines.append(f"  {s['display_name']}{suffix}: {sym}{s['share_amount']:,.0f}")
        await self.send_message(msg.chat_id, "\n".join(lines))
        return True

    async def maybe_handle_group_balance(
        self, msg: InboundMessage, group, db: Session
    ) -> bool:
        """
        Handle /groupbalance or "who owes what" in group chats.
        Returns True if handled.
        """
        import re
        text = (msg.text or "").strip()
        if not re.search(r'^/groupbalance\b|who\s+owes\s+(what|whom)', text, re.IGNORECASE):
            return False

        from services import get_group_balances, get_user_currency, currency_symbol, get_group_members
        # Use first member's currency as group currency (best effort)
        members = get_group_members(group.id, db)
        sym = "₹"  # default; try to get from first registered member
        for m in members:
            if m.get("user_id"):
                sym = currency_symbol(get_user_currency(db, m["user_id"]))
                break

        balances = get_group_balances(group.id, db)
        if not balances:
            await self.send_message(msg.chat_id, "All settled up! 🎉 No outstanding balances.")
            return True

        lines = ["*Group balances:*\n"]
        for b in balances:
            lines.append(f"  {b['from_name']} → {b['to_name']}: {sym}{b['amount']:,.0f}")
        await self.send_message(msg.chat_id, "\n".join(lines))
        return True


# Singleton plugin instance
_plugin = TelegramPlugin()


@router.post("/webhook", include_in_schema=False)
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    message = payload.get("message", {})
    chat    = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    msg_id  = str(message.get("message_id", ""))
    text    = message.get("text", "") or message.get("caption", "")

    if not chat_id:
        return {"ok": False, "reason": "no chat_id"}

    chat_type = chat.get("type", "private")
    is_group  = chat_type in ("group", "supergroup")

    msg = InboundMessage(
        text       = text or None,
        source_ref = msg_id,
        chat_id    = chat_id,
        raw        = payload,
    )

    async def _handle_in_background():
        _db = SessionLocal()
        try:
            if is_group:
                await _plugin.handle_group_message(msg, payload, _db)
            else:
                await _plugin.handle(msg, _db)
        finally:
            _db.close()

    asyncio.create_task(_handle_in_background())
    return {"ok": True}
