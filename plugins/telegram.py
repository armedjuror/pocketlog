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
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from models import SessionLocal
from plugins.auth_flow import AuthFlowMixin
from plugins.base import BasePlugin, InboundMessage
from plugins.registry import register as register_plugin

log = logging.getLogger(__name__)

# Pending transaction context keyed by chat_id — survives until resolved or replaced
@dataclass
class _PendingTx:
    original_text: str
    parsed: object   # services.ParsedTransaction

_pending: dict[str, _PendingTx] = {}

# Pending group expense awaiting split instructions, keyed by chat_id
@dataclass
class _PendingGroupExpense:
    amount: float
    description: str

_pending_group: dict[str, _PendingGroupExpense] = {}

# Pending group split waiting for @tag resolution, keyed by telegram chat_id_str
@dataclass
class _PendingGroupSplit:
    text: str
    parsed: object          # ParsedGroupSplit
    resolved: list          # member dicts already matched
    unresolved: list        # name strings still to resolve (pop front as each is resolved)
    sender_member_id: int

_pending_group_splits: dict[str, _PendingGroupSplit] = {}


def _group_sym(group_id: int, db) -> str:
    """Return the currency symbol for the first registered (non-guest) member of a group."""
    from services import get_group_members, get_user_currency, currency_symbol
    members = get_group_members(group_id, db)
    # Prefer a fully registered member
    for m in members:
        if m.get("user_id") and not m.get("is_guest"):
            return currency_symbol(get_user_currency(db, m["user_id"]))
    # Fall back to any member with a user_id
    for m in members:
        if m.get("user_id"):
            return currency_symbol(get_user_currency(db, m["user_id"]))
    return "₹"

_bot_username: str | None = None

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
            "*Log expenses & income* — text or 🎙️ voice note:\n"
            "  `spent 450 on lunch`\n"
            "  `paid 1200 electricity from HDFC`\n"
            "  `got salary 85000`\n"
            "  `Spent 500 from OneCard for groceries`\n\n"
            "*Lending:*\n"
            "  `lent 2000 to Rahul`\n"
            "  `borrowed 5000 from Priya`\n"
            "  `Rahul settled 2000`\n\n"
            "*Commands:*\n"
            "  /accounts — your account balances\n"
            "  /transactions — this month's transactions\n"
            "  /categories — available categories\n"
            "  /lendings — unsettled lending records\n"
            "  /budget — this month's budget status\n"
            "  /report — this month's summary\n"
            "  /report last — last month\n"
            "  /report jan — January (add year: `jan 2024`)\n"
            "  /report trend — 6-month spending trend\n"
            "  /groups — your groups\n"
            "  /channels — linked bots/channels\n"
            "  /apikey — your API access token\n"
            "  /resend — resend OTP\n"
            "  /help — show this message\n\n"
            "*Group splits (in group chats):*\n"
            "  `split 1200 lunch` — split equally among all\n"
            "  `split 1200 lunch between Alice, Bob` — named split\n"
            "  `split 1200 lunch 1:2:3 between Alice, Bob, Priya` — ratio split\n"
            "  /groupbalance — show who owes whom\n"
            "  /simplify — minimise number of payments\n\n"
            "*Natural queries:*\n"
            "  `what did I spend today`\n"
            "  `show my expenses this month`\n"
            "  `who owes me` / `what do I owe`\n"
            "  `my budgets`\n"
        )

    async def _get_bot_username(self) -> str:
        global _bot_username
        if _bot_username:
            return _bot_username
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.telegram.org/bot{self._token}/getMe", timeout=10
            )
            data = r.json()
            _bot_username = data["result"]["username"]
        return _bot_username

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
        cmd  = text.lower().split()[0] if text.startswith("/") else None

        # ── /help ─────────────────────────────────────────────────────────
        if cmd == "/help":
            await self.send_message(msg.chat_id, self._help_text())
            return

        # ── /apikey ───────────────────────────────────────────────────────
        if cmd == "/apikey":
            import oauth_service as _oas
            session = _oas.get_or_create_session(user.id, db)
            await self.send_message(
                msg.chat_id,
                f"Your Personal Access Token:\n\n`{session.token}`\n\n"
                f"Use it as:\n`Authorization: Bearer {session.token}`\n\n"
                f"Valid for 30 days. Keep it secret.",
            )
            return

        # ── /accounts ─────────────────────────────────────────────────────
        if cmd in ("/accounts", "/account"):
            reply = self.maybe_list_accounts(msg, db)
            await self.send_message(msg.chat_id, reply or "No accounts found.")
            return

        # ── /categories ───────────────────────────────────────────────────
        if cmd in ("/categories", "/category"):
            from services import get_categories
            cats = get_categories(db)
            if not cats:
                await self.send_message(msg.chat_id, "No categories found.")
            else:
                lines = ["*Categories:*\n"] + [f"  {c['icon']}  {c['name']}" for c in cats]
                await self.send_message(msg.chat_id, "\n".join(lines))
            return

        # ── /transactions ─────────────────────────────────────────────────
        if cmd in ("/transactions", "/transaction"):
            from datetime import date as _date
            from services import list_transactions, format_transactions_list_message, \
                get_user_currency, currency_symbol
            today = _date.today()
            result = list_transactions(db, month=today.month, year=today.year, limit=100, user_id=msg.user_id)
            currency = get_user_currency(db, msg.user_id)
            reply = format_transactions_list_message(
                result["items"], currency=currency,
                start_date=today.replace(day=1), end_date=today,
            )
            await self.send_message(msg.chat_id, reply)
            return

        # ── /lendings ─────────────────────────────────────────────────────
        if cmd in ("/lendings", "/lending", "/loans"):
            from services import list_lending, get_user_currency, currency_symbol
            sym = currency_symbol(get_user_currency(db, msg.user_id))
            records = list_lending(db, settled=False, user_id=msg.user_id)
            if not records:
                await self.send_message(msg.chat_id, "No outstanding lending records.")
                return
            lines = ["*Unsettled Lending:*\n"]
            for r in records:
                arrow = "→" if r["type"] == "lent" else "←"
                lines.append(f"  {arrow} *{r['person_name']}*: {sym}{r['outstanding']:,.0f}")
            await self.send_message(msg.chat_id, "\n".join(lines))
            return

        # ── /budget / /budgets ────────────────────────────────────────────
        if cmd in ("/budget", "/budgets"):
            from datetime import date as _date
            from services import list_budgets, get_user_currency, currency_symbol
            today = _date.today()
            budgets = list_budgets(db, today.month, today.year, user_id=msg.user_id)
            if not budgets:
                await self.send_message(msg.chat_id, f"No budgets set for {today.strftime('%B %Y')}. Add them from the dashboard.")
                return
            sym   = currency_symbol(get_user_currency(db, msg.user_id))
            lines = [f"*Budgets — {today.strftime('%B %Y')}:*\n"]
            for b in budgets:
                icon    = b.get("category_icon", "💰")
                name    = b.get("category_name", "")
                spent   = b.get("spent", 0) or 0
                total   = b.get("budget_amount") or b.get("amount") or 0
                remaining = b.get("remaining", total - spent)
                pct     = round(spent / total * 100) if total else 0
                filled  = min(10, pct // 10)
                bar     = "█" * filled + "░" * (10 - filled)
                status  = "⚠️" if pct >= 90 else ("🔶" if pct >= 70 else "✅")
                lines.append(
                    f"  {status} {icon} *{name}*\n"
                    f"     {bar} {pct}%\n"
                    f"     {sym}{spent:,.0f} of {sym}{total:,.0f}  ({sym}{remaining:,.0f} left)"
                )
            await self.send_message(msg.chat_id, "\n".join(lines))
            return

        # ── /report [period] ──────────────────────────────────────────────
        if cmd == "/report":
            from services import parse_report_request, generate_report
            period_text = text[len("/report"):].strip() or "/report"
            period = parse_report_request(period_text) or parse_report_request("/report")
            report = generate_report(db, period, user_id=msg.user_id)
            await self.send_message(msg.chat_id, report or "No report data.")
            return

        # ── /groups ───────────────────────────────────────────────────────
        if cmd == "/groups":
            from services import get_user_groups
            groups = get_user_groups(user.id, db)
            if not groups:
                await self.send_message(msg.chat_id, "You're not in any groups yet.")
            else:
                lines = ["*Your groups:*\n"] + [
                    f"  • *{g['name']}*{' (closed)' if g.get('is_closed') else ''}"
                    for g in groups
                ]
                await self.send_message(msg.chat_id, "\n".join(lines))
            return

        # ── /resend — re-send OTP to current session ─────────────────────
        if cmd == "/resend":
            await self._send_otp_here(user, self.name, msg.chat_id, db)
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

        # ── Pending transaction takes priority over all other handlers ─────
        # When the user is answering our "I need: account, category" prompt,
        # skip all the maybe_* dispatch and resolve inline.
        pending = _pending.get(msg.chat_id)
        if pending:
            import difflib
            from services import get_categories, get_accounts

            if pending.parsed.missing:
                user_text = (msg.text or "").strip().lower()
                resolved_any = False

                def _fuzzy_match(name_lower: str, candidates: list[str]) -> Optional[str]:
                    """Exact → substring → fuzzy."""
                    if name_lower in candidates:
                        return name_lower
                    for c in candidates:
                        if c in name_lower or name_lower in c:
                            return c
                    close = difflib.get_close_matches(name_lower, candidates, n=1, cutoff=0.6)
                    return close[0] if close else None

                if "category_id" in pending.parsed.missing:
                    cats = get_categories(db)
                    cat_names = [c["name"].lower() for c in cats]
                    matched = _fuzzy_match(user_text, cat_names)
                    if matched:
                        for c in cats:
                            if c["name"].lower() == matched:
                                pending.parsed.category_id = c["id"]
                                pending.parsed.missing = [f for f in pending.parsed.missing if f != "category_id"]
                                resolved_any = True
                                break

                if "account_id" in pending.parsed.missing:
                    accs = get_accounts(db, user_id=msg.user_id)
                    acc_names = [a["name"].lower() for a in accs]
                    matched = _fuzzy_match(user_text, acc_names)
                    if matched:
                        for a in accs:
                            if a["name"].lower() == matched:
                                pending.parsed.account_id = a["id"]
                                pending.parsed.missing = [f for f in pending.parsed.missing if f != "account_id"]
                                resolved_any = True
                                break

                if resolved_any and not pending.parsed.missing:
                    _pending.pop(msg.chat_id, None)
                    try:
                        self.save(pending.parsed, db, msg)
                    except Exception as exc:
                        log.exception("Save failed after local resolution")
                        await self.send_message(msg.chat_id, f"Couldn't save: {exc}")
                        return
                    warning = self.budget_warning(pending.parsed, db, user_id=msg.user_id)
                    await self.send_message(msg.chat_id, (pending.parsed.reply or "Logged!") + warning)
                    return
                elif resolved_any:
                    await self.send_message(msg.chat_id, self.missing_prompt(pending.parsed, db=db, user_id=msg.user_id))
                    return

            # Local resolution didn't fully resolve — ask AI with explicit context
            missing_labels = ", ".join(pending.parsed.missing) if pending.parsed.missing else "unknown"
            parse_text = (
                f"{pending.original_text}\n"
                f"[Pending transaction — still need: {missing_labels}. "
                f"User's reply: {msg.text}]"
            )
            try:
                parse_msg = InboundMessage(
                    text=parse_text,
                    source_ref=msg.source_ref,
                    chat_id=msg.chat_id,
                    media_url=msg.media_url,
                    raw=msg.raw,
                    user_id=msg.user_id,
                )
                parsed = self.parse(parse_msg, db)
            except Exception as exc:
                log.exception("AI parse failed for pending context")
                await self.send_message(msg.chat_id, f"Couldn't parse that: {exc}")
                return

            # If AI still can't resolve it, re-ask rather than dropping context
            if parsed.action == "chat" or (parsed.chat and parsed.action not in (
                "transaction", "list_transactions", "list_accounts",
                "list_categories", "list_budgets", "list_lending", "report"
            )):
                await self.send_message(msg.chat_id, self.missing_prompt(pending.parsed, db=db, user_id=msg.user_id))
                return

            if parsed.missing:
                _pending[msg.chat_id] = _PendingTx(
                    original_text=pending.original_text,
                    parsed=parsed,
                )
                await self.send_message(msg.chat_id, self.missing_prompt(parsed, db=db, user_id=msg.user_id))
                return

            if parsed.amount is None and not parsed.missing:
                await self.send_message(msg.chat_id, self.missing_prompt(pending.parsed, db=db, user_id=msg.user_id))
                return

            _pending.pop(msg.chat_id, None)
            try:
                self.save(parsed, db, msg)
            except Exception as exc:
                log.exception("Save failed after AI pending resolution")
                await self.send_message(msg.chat_id, f"Couldn't save: {exc}")
                return
            warning = self.budget_warning(parsed, db, user_id=msg.user_id)
            await self.send_message(msg.chat_id, (parsed.reply or "Logged!") + warning)
            return

        # ── No pending — run normal maybe_* handlers ─────────────────────
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

            # ── Transaction list? ─────────────────────────────────────────────
            txn_list_reply = self.maybe_list_transactions(msg, db)
            if txn_list_reply is not None:
                await self.send_message(msg.chat_id, txn_list_reply)
                return

            # ── Report request? ───────────────────────────────────────────────
            report = await self.maybe_get_report(msg, db)
            if report is not None:
                await self.send_message(msg.chat_id, report)
                return

        # ── Fresh expense / income entry ──────────────────────────────────
        parse_text = msg.text or ""
        try:
            parse_msg = InboundMessage(
                text=parse_text,
                source_ref=msg.source_ref,
                chat_id=msg.chat_id,
                media_url=msg.media_url,
                raw=msg.raw,
                user_id=msg.user_id,
            )
            parsed = self.parse(parse_msg, db)
        except Exception as exc:
            log.exception("AI parse failed")
            await self.send_message(msg.chat_id, f"Couldn't parse that: {exc}")
            return

        # ── Handle non-transaction actions returned by AI ─────────────────
        if parsed.action == "chat" or (parsed.chat and parsed.action not in (
            "transaction", "list_transactions", "list_accounts",
            "list_categories", "list_budgets", "list_lending", "report"
        )):
            await self.send_message(msg.chat_id, parsed.reply or "Hey! Send me an expense or income to log.")
            return

        if parsed.action == "list_transactions":
            from datetime import date as _date
            from services import list_transactions, format_transactions_list_message, get_user_currency
            today = _date.today()
            start = parsed.start_date or today.replace(day=1)
            end   = parsed.end_date   or today
            result = list_transactions(
                db, month=start.month if start.month == end.month else None,
                year=start.year if start.month == end.month else None,
                limit=100, user_id=msg.user_id,
            )
            items = result["items"]
            if start != today.replace(day=1) or end != today:
                items = [t for t in items if start.isoformat() <= t["date"] <= end.isoformat()]
            reply = format_transactions_list_message(
                items, currency=get_user_currency(db, msg.user_id), start_date=start, end_date=end
            )
            await self.send_message(msg.chat_id, reply)
            return

        if parsed.action == "list_accounts":
            reply = self.maybe_list_accounts(msg, db)
            await self.send_message(msg.chat_id, reply or "No accounts found.")
            return

        if parsed.action == "list_categories":
            from services import get_categories
            cats = get_categories(db)
            lines = ["*Categories:*\n"] + [f"  {c['icon']}  {c['name']}" for c in cats]
            await self.send_message(msg.chat_id, "\n".join(lines) if cats else "No categories found.")
            return

        if parsed.action == "list_budgets":
            reply = self.maybe_list_budgets(msg, db)
            await self.send_message(msg.chat_id, reply or "No budgets found.")
            return

        if parsed.action == "list_lending":
            reply = await self.maybe_handle_lending(msg, db)
            await self.send_message(msg.chat_id, reply or "No lending records found.")
            return

        if parsed.action == "report":
            report = await self.maybe_get_report(msg, db)
            await self.send_message(msg.chat_id, report or "No report data.")
            return

        # Treat as chat if there's no amount and nothing is missing
        if parsed.amount is None and not parsed.missing:
            await self.send_message(msg.chat_id, parsed.reply or "Hey! Send me an expense or income to log.")
            return

        if parsed.missing:
            _pending[msg.chat_id] = _PendingTx(
                original_text=msg.text or "",
                parsed=parsed,
            )
            await self.send_message(msg.chat_id, self.missing_prompt(parsed, db=db, user_id=msg.user_id))
            return

        # All fields resolved — save
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

        # If not registered, create/link a guest user
        if user_id is None:
            # If they were previously added as a @username guest, merge to their real ID
            if username:
                merged = _auth.merge_username_guest_to_numeric("telegram", platform_user_id, username, db)
                if merged:
                    user_id = merged.id

        if user_id is None:
            guest = _auth.get_or_create_guest_user(
                "telegram", platform_user_id, display_name, db
            )
            user_id = guest.id

        # Get/create GroupMember
        member = get_or_create_member(group, platform_user_id, display_name, username, user_id, db)

        text = (msg.text or "").strip()

        # /start in a group → welcome message
        if text.lower().startswith("/start"):
            await self.handle_group_start(msg)
            return

        # If a split is waiting for @tag resolution, handle that first (skip for commands)
        if not text.startswith("/") and msg.chat_id in _pending_group_splits:
            handled = await self.maybe_resolve_pending_group_split(msg, group, db, message)
            if handled:
                return

        # Route to group-specific handlers
        handled = await self.maybe_handle_group_split(msg, group, member, db)
        if handled:
            return

        handled = await self.maybe_handle_group_balance(msg, group, db)
        if handled:
            return

        handled = await self.maybe_handle_group_simplify(msg, group, db)
        if handled:
            return

        # Check if this is a reply to a pending split question
        handled = await self.maybe_handle_pending_group_expense(msg, group, member, db)
        if handled:
            return

        # Detect natural expense messages (e.g. "breakfast 100") and ask how to split
        await self.maybe_detect_group_expense(msg, group, member, db)

    async def handle_group_start(self, msg: InboundMessage) -> None:
        """Send a welcome message in the group and ask members to register."""
        try:
            bot_username = await self._get_bot_username()
            dm_link = f"https://t.me/{bot_username}"
        except Exception:
            dm_link = None

        lines = [
            "👋 *PocketLog is here!* I can track and split group expenses for everyone.",
            "",
            "To get registered, just say *hi* here — send any message and I'll add you.",
            "Then DM me to set up your account and see your personal balances.",
        ]
        if dm_link:
            lines.append(f"\n👉 [Message me privately]({dm_link})")

        await self.send_message(msg.chat_id, "\n".join(lines))

    async def maybe_handle_pending_group_expense(
        self, msg: InboundMessage, group, sender_member, db: Session
    ) -> bool:
        """
        If a natural expense was previously detected and we're waiting for split info,
        treat the current message as the split instruction and execute it.
        Returns True if handled.
        """
        pending = _pending_group.get(msg.chat_id)
        if not pending:
            return False

        text = (msg.text or "").strip()
        if not text:
            return False

        # Combine: synthesise a proper split command from pending + current message
        combined = f"split {pending.amount} {pending.description} {text}"
        del _pending_group[msg.chat_id]
        return await self.maybe_handle_group_split(msg, group, sender_member, db, text_override=combined)

    async def maybe_detect_group_expense(
        self, msg: InboundMessage, group, sender_member, db: Session
    ) -> None:
        """
        Detect natural expense messages (e.g. 'breakfast 100') in group chats.
        If an expense is detected, store it as pending and ask how to split.
        """
        import asyncio
        from services import detect_natural_group_expense

        text = (msg.text or "").strip()
        if not text:
            return

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, detect_natural_group_expense, text
            )
        except Exception:
            return

        if result is None:
            return

        amount, description = result
        _pending_group[msg.chat_id] = _PendingGroupExpense(amount=amount, description=description)
        await self.send_message(
            msg.chat_id,
            f"Got it — *{description}* for *{amount:,.0f}*.\n"
            f"How should this be split? e.g.\n"
            f"  `equally` — split among everyone\n"
            f"  `between me and Ali` — named split\n"
            f"  `1:2 between me and Ali` — ratio split",
        )

    async def maybe_handle_group_split(
        self, msg: InboundMessage, group, sender_member, db: Session,
        text_override: str | None = None,
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

        text = text_override or (msg.text or "").strip()

        # Trigger pattern: starts with "split" or contains "split ... between"
        if not re.search(r'^\s*split\b|\bsplit\b.{0,60}\bbetween\b', text, re.IGNORECASE):
            return False

        # Collect all group members for name resolution
        all_members = get_group_members(group.id, db)
        member_names = [m["display_name"] for m in all_members]
        if not member_names:
            member_names = [sender_member.display_name]

        sender_name = sender_member.display_name

        try:
            parsed = parse_group_split_with_ai(text, member_names, sender_name=sender_name)
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
                # Store pending split and ask the user to @tag each unknown person
                _pending_group_splits[msg.chat_id] = _PendingGroupSplit(
                    text=text,
                    parsed=parsed,
                    resolved=resolved,
                    unresolved=unresolved,
                    sender_member_id=sender_member.id,
                )
                first_name = unresolved[0]
                await self.send_message(
                    msg.chat_id,
                    f"I couldn't find *{first_name}* in this group. "
                    f"Please @tag them so I can identify them.",
                )
                return True
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
        sym = _group_sym(group.id, db)
        lines = [f"Split *{sym}{parsed.amount:,.0f}* — _{parsed.description}_"]
        for s in result["shares"]:
            suffix = " (guest)" if not any(
                m["id"] == s["member_id"] and not m.get("is_guest") for m in all_members
            ) else ""
            lines.append(f"  {s['display_name']}{suffix}: {sym}{s['share_amount']:,.0f}")
        await self.send_message(msg.chat_id, "\n".join(lines))
        return True

    async def maybe_resolve_pending_group_split(
        self, msg: InboundMessage, group, db: Session, raw_message: dict
    ) -> bool:
        """
        If a split is waiting for @tag resolution, try to resolve it from the current message.
        Returns True if the message was consumed.
        """
        pending = _pending_group_splits.get(msg.chat_id)
        if not pending:
            return False

        from services import get_group_members, create_group_expense
        from datetime import date as _date

        # Extract mentions from Telegram entities:
        #   "mention"      → text contains @username (user has a public username)
        #   "text_mention" → entity contains user.id (user has no public username)
        text = msg.text or ""
        entities = raw_message.get("entities", [])
        mention_usernames: list[str] = []   # @handle without @
        mention_tg_ids: list[str] = []       # numeric Telegram user IDs

        for e in entities:
            if e.get("type") == "mention":
                mention_usernames.append(
                    text[e["offset"]: e["offset"] + e["length"]].lstrip("@")
                )
            elif e.get("type") == "text_mention":
                tid = str(e.get("user", {}).get("id", ""))
                if tid:
                    mention_tg_ids.append(tid)

        all_members = get_group_members(group.id, db)

        def _find_tagged() -> Optional[dict]:
            # text_mention: match by real Telegram ID stored as platform_user_id
            for tid in mention_tg_ids:
                for m in all_members:
                    if m.get("platform_user_id") == tid:
                        return m
            # mention: match by @username
            for uname in mention_usernames:
                ul = uname.lower()
                for m in all_members:
                    if m.get("username") and m["username"].lower() == ul:
                        return m
                # Fallback: display name match
                for m in all_members:
                    if m["display_name"].lower() == ul:
                        return m
            return None

        no_tag = not mention_usernames and not mention_tg_ids

        if no_tag:
            # No Telegram tag detected — ask the unknown person to send a message
            await self.send_message(
                msg.chat_id,
                f"I still need to identify *{pending.unresolved[0]}*. "
                f"Please ask them to send any message in this group, "
                f"and I'll add them automatically.",
            )
            return True

        matched = _find_tagged()

        if not matched:
            # Tagged user hasn't messaged in the group yet
            await self.send_message(
                msg.chat_id,
                f"The tagged user hasn't sent a message in this group yet. "
                f"Please ask *{pending.unresolved[0]}* to say something here first, "
                f"then tag them again.",
            )
            return True

        # Resolve the first pending name
        pending.resolved.append(matched)
        pending.unresolved.pop(0)

        if pending.unresolved:
            await self.send_message(
                msg.chat_id,
                f"Got it! Now who is *{pending.unresolved[0]}*? Please @tag them.",
            )
            return True

        # All resolved — create the expense
        del _pending_group_splits[msg.chat_id]
        parsed = pending.parsed
        resolved = pending.resolved

        if parsed.ratios and len(parsed.ratios) == len(resolved):
            shares = [{"member_id": m["id"], "ratio": r} for m, r in zip(resolved, parsed.ratios)]
        else:
            shares = [{"member_id": m["id"], "ratio": 1.0} for m in resolved]

        sender_member = next((m for m in all_members if m["id"] == pending.sender_member_id), None)
        if sender_member and sender_member["id"] not in {s["member_id"] for s in shares}:
            shares.append({"member_id": sender_member["id"], "ratio": 1.0})

        try:
            result = create_group_expense(
                group_chat_id=group.id,
                paid_by_member_id=pending.sender_member_id,
                amount=parsed.amount,
                description=parsed.description or "group expense",
                expense_date=_date.today(),
                shares=shares,
                db=db,
            )
        except Exception as exc:
            log.exception("create_group_expense failed in pending resolution")
            await self.send_message(msg.chat_id, f"Failed to save split: {exc}")
            return True

        sym = _group_sym(group.id, db)
        lines = [f"Split *{sym}{parsed.amount:,.0f}* — _{parsed.description}_"]
        for s in result["shares"]:
            lines.append(f"  {s['display_name']}: {sym}{s['share_amount']:,.0f}")
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


    async def maybe_handle_group_simplify(
        self, msg: InboundMessage, group, db: Session
    ) -> bool:
        """Handle /simplify in group chats — show minimised payment plan."""
        text = (msg.text or "").strip()
        if not re.search(r'^/simplify\b', text, re.IGNORECASE):
            return False

        from services import get_simplified_balances, get_user_currency, currency_symbol, get_group_members

        members = get_group_members(group.id, db)
        sym = "₹"
        for m in members:
            if m.get("user_id"):
                sym = currency_symbol(get_user_currency(db, m["user_id"]))
                break

        balances = get_simplified_balances(group.id, db)
        if not balances:
            await self.send_message(msg.chat_id, "All settled up! No payments needed.")
            return True

        lines = ["*Simplified payments:*\n"]
        for b in balances:
            lines.append(f"  {b['from_name']} → {b['to_name']}: {sym}{b['amount']:,.0f}")
        lines.append("\n_These are the minimum transfers to settle all debts._")
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

    chat_type  = chat.get("type", "private")
    is_group   = chat_type in ("group", "supergroup")
    from_user  = message.get("from", {})
    username   = from_user.get("username") or None  # Telegram @handle without @

    msg = InboundMessage(
        text       = text or None,
        source_ref = msg_id,
        chat_id    = chat_id,
        username   = username,
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
