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

    def maybe_list_accounts(self, msg: InboundMessage, db) -> Optional[str]:  # noqa: F821
        """
        Check whether the message is asking to list accounts.
        Returns a formatted string if yes, None otherwise.
        """
        import re
        text = (msg.text or "").strip().lower()
        if not re.search(
            r"\baccounts?\b|/accounts",
            text,
        ):
            return None

        from services import get_accounts
        accounts = get_accounts(db, user_id=msg.user_id)
        if not accounts:
            return "You have no accounts yet. Add one from the dashboard."

        lines = ["*Your accounts:*\n"]
        for a in accounts:
            lines.append(f"  • *{a['name']}* ({a['type']}) — ₹{a['balance']:,.2f}")
        return "\n".join(lines)

    def maybe_list_categories(self, msg: InboundMessage, db) -> Optional[str]:  # noqa: F821
        """Returns formatted category list if the message asks for categories, else None."""
        import re
        text = (msg.text or "").strip().lower()
        if not re.search(r"\bcategor(y|ies)\b|/categories", text):
            return None

        from services import get_categories
        cats = get_categories(db)
        if not cats:
            return "No categories found. Add some from the dashboard."

        lines = ["*Categories:*\n"]
        for c in cats:
            lines.append(f"  {c['icon']}  {c['name']}")
        return "\n".join(lines)

    def maybe_get_today_spends(self, msg: InboundMessage, db) -> Optional[str]:  # noqa: F821
        """Returns today's transactions if the message asks about today's spending, else None."""
        import re
        from datetime import date
        text = (msg.text or "").strip().lower()
        if "today" not in text:
            return None
        # Require clear query intent — avoid intercepting logging messages like "spent ₹500 today"
        if not re.search(r"\b(what|show|list|how much|how many)\b|\?", text):
            return None

        today = date.today()
        from services import list_transactions
        txns = list_transactions(db, month=today.month, year=today.year, limit=500, user_id=msg.user_id)
        today_txns = [t for t in txns if t["date"] == today.isoformat()]

        if not today_txns:
            return f"Nothing logged for today ({today.strftime('%b %d')}) yet."

        total_expense = sum(t["amount"] for t in today_txns if t["type"] == "expense")
        total_income  = sum(t["amount"] for t in today_txns if t["type"] == "income")

        lines = [f"*Today ({today.strftime('%b %d')}):*\n"]
        for t in today_txns:
            prefix = "+" if t["type"] == "income" else "−"
            cat = f"  _{t['category_name']}_" if t.get("category_name") else ""
            lines.append(f"  {prefix}₹{t['amount']:,.0f} — {t['description']}{cat}")

        summary = []
        if total_expense:
            summary.append(f"Spent: *₹{total_expense:,.0f}*")
        if total_income:
            summary.append(f"Received: *₹{total_income:,.0f}*")
        if summary:
            lines.append("\n" + "  |  ".join(summary))

        return "\n".join(lines)

    async def maybe_handle_lending(self, msg: InboundMessage, db) -> Optional[str]:  # noqa: F821
        """
        Detect and handle lending-related messages (log, list).
        Returns a response string if handled, None if the message is not lending-related.
        """
        import re
        text = (msg.text or "").strip().lower()
        if not re.search(r'\b(lent|loaned|lend|borrowed|borrow|owe|owes|loan|debt|lending)\b', text):
            return None

        from services import parse_lending_with_ai, create_lending, list_lending
        parsed = parse_lending_with_ai(msg.text or "")

        if parsed.intent == "unknown":
            return None

        if parsed.intent == "log":
            if parsed.missing:
                return f"I need a bit more info: *{', '.join(parsed.missing)}*."
            from datetime import date
            create_lending(
                db,
                user_id      = msg.user_id,
                person_name  = parsed.person,
                type         = parsed.lending_type,
                amount       = parsed.amount,
                amount_settled = 0.0,
                date         = parsed.date or date.today(),
                note         = parsed.note,
                is_settled   = False,
            )
            return parsed.reply or (
                f"Logged: {'lent' if parsed.lending_type == 'lent' else 'borrowed'} "
                f"*₹{parsed.amount:,.0f}* {'to' if parsed.lending_type == 'lent' else 'from'} *{parsed.person}*."
            )

        # List intents
        records = list_lending(db, settled=False, user_id=msg.user_id)
        if parsed.intent == "list_owed":
            records = [r for r in records if r["type"] == "lent"]
            header = "*People who owe you:*"
        elif parsed.intent == "list_i_owe":
            records = [r for r in records if r["type"] == "borrowed"]
            header = "*You owe:*"
        else:
            header = "*Unsettled lending:*"

        if not records:
            return "Nothing outstanding."

        lines = [f"{header}\n"]
        total = 0.0
        for r in records:
            arrow = "→" if r["type"] == "lent" else "←"
            lines.append(f"  {arrow} *{r['person_name']}*: ₹{r['outstanding']:,.0f}")
            total += r["outstanding"]
        lines.append(f"\n*Total: ₹{total:,.0f}*")
        return "\n".join(lines)

    def maybe_list_budgets(self, msg: InboundMessage, db) -> Optional[str]:  # noqa: F821
        """Returns this month's budget summary if the message asks about budgets, else None."""
        import re
        from datetime import date
        text = (msg.text or "").strip().lower()
        if not re.search(r'\bbudgets?\b', text):
            return None
        if re.search(r'\b(set|create|add|update|change|put)\b', text):
            return "Budget setting isn't supported here yet — use the dashboard to set budgets."

        today = date.today()
        from services import list_budgets
        budgets = list_budgets(db, today.month, today.year, user_id=msg.user_id)

        if not budgets:
            return f"No budgets set for {today.strftime('%B %Y')}. Add them from the dashboard."

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
                f"     ₹{spent:,.0f} of ₹{total:,.0f}  (₹{remaining:,.0f} left)"
            )
        return "\n".join(lines)

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
