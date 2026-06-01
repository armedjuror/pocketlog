"""
services.py — Core business logic, completely transport-agnostic.

Every plugin (Telegram, WhatsApp, email, web API…) calls these functions.
Nothing here knows about HTTP, bots, or webhooks.
"""

import calendar
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from models import (
    Account, Category, Transaction, Budget, Lending,
    AccountType, TransactionType, LendingType,
)


# ── Shared result types ────────────────────────────────────────────────────

@dataclass
class ParsedTransaction:
    """What an AI parser returns; not yet saved."""
    amount:       Optional[float]  = None
    description:  Optional[str]    = None
    account_id:   Optional[int]    = None
    category_id:  Optional[int]    = None
    date:         Optional[date]   = None
    type:         str              = "expense"
    note:         Optional[str]    = None
    missing:      list[str]        = field(default_factory=list)
    reply:        str              = ""


@dataclass
class ParsedLending:
    """Result of AI lending parser."""
    intent:       str            = "unknown"   # log | list_owed | list_i_owe | list_all
    lending_type: Optional[str]  = None        # lent | borrowed
    person:       Optional[str]  = None
    amount:       Optional[float]= None
    date:         Optional[date] = None
    note:         Optional[str]  = None
    missing:      list[str]      = field(default_factory=list)
    reply:        str            = ""


@dataclass
class BudgetStatus:
    budget_amount:   float
    spent:           float
    remaining:       float
    daily_budget:    float
    expected_spent:  float
    over_pace:       bool
    over_pace_by:    float
    pct:             float
    category_name:   str


# ── Account services ───────────────────────────────────────────────────────

def get_accounts(db: Session, active_only: bool = True, user_id: Optional[int] = None) -> list[dict]:
    q = db.query(Account).order_by(Account.id)
    if active_only:
        q = q.filter(Account.is_active == True)
    if user_id is not None:
        from sqlalchemy import or_
        q = q.filter(or_(Account.user_id == None, Account.user_id == user_id))
    accounts = q.all()

    # Compute this month's spend per CC account in one query
    today = date.today()
    cc_ids = [a.id for a in accounts if a.type == AccountType.credit_card]
    month_spend_map: dict[int, float] = {}
    if cc_ids:
        rows = (
            db.query(Transaction.account_id, func.sum(Transaction.amount))
            .filter(
                Transaction.account_id.in_(cc_ids),
                Transaction.type == TransactionType.expense,
                extract("year",  Transaction.date) == today.year,
                extract("month", Transaction.date) == today.month,
            )
            .group_by(Transaction.account_id)
            .all()
        )
        month_spend_map = {row[0]: round(row[1], 2) for row in rows}

    result = []
    for a in accounts:
        d = _account_dict(a)
        if a.type == AccountType.credit_card:
            d["month_spend"] = month_spend_map.get(a.id, 0.0)
        result.append(d)
    return result


def _normalize_cc_balance(kwargs: dict) -> dict:
    """For credit cards, balance is stored negative (outstanding = -balance). Flip if positive."""
    acc_type = kwargs.get("type")
    type_val = acc_type.value if hasattr(acc_type, "value") else str(acc_type)
    if type_val == "credit_card" and "balance" in kwargs and kwargs["balance"] > 0:
        kwargs["balance"] = -kwargs["balance"]
    return kwargs


def create_account(db: Session, **kwargs) -> dict:
    acc = Account(**_normalize_cc_balance(kwargs))
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return _account_dict(acc)


def update_account(db: Session, account_id: int, **kwargs) -> dict:
    acc = db.query(Account).get(account_id)
    if not acc:
        raise ValueError(f"Account {account_id} not found")
    if "balance" in kwargs and kwargs["balance"] > 0:
        type_val = acc.type.value if hasattr(acc.type, "value") else str(acc.type)
        if type_val == "credit_card":
            kwargs["balance"] = -kwargs["balance"]
    for k, v in kwargs.items():
        setattr(acc, k, v)
    db.commit()
    return _account_dict(acc)


def delete_account(db: Session, account_id: int) -> None:
    acc = db.query(Account).get(account_id)
    if not acc:
        raise ValueError(f"Account {account_id} not found")
    if acc.is_protected:
        raise ValueError("This account is protected and cannot be deleted.")
    acc.is_active = False
    db.commit()


def delete_all_user_accounts(db: Session, user_id: int) -> int:
    """Deactivate all non-protected accounts owned by the user. Returns count deleted."""
    accs = db.query(Account).filter(
        Account.user_id == user_id,
        Account.is_active == True,
        Account.is_protected == False,
    ).all()
    for acc in accs:
        acc.is_active = False
    db.commit()
    return len(accs)


@dataclass
class ParsedAccount:
    name:  str
    type:  str   # matches AccountType enum values
    valid: bool = True
    error: str  = ""


def parse_account_with_ai(text: str) -> ParsedAccount:
    """Extract account name and type from a natural-language message."""
    import json
    from litellm import completion

    types = "bank, credit_card, cash, metro_card, wallet, loan, chitty, other"
    prompt = f"""Extract account details from the user's message.
Return ONLY a JSON object:
{{"name": "<account name>", "type": "<one of: {types}>", "valid": true}}
If you cannot determine name or type, return {{"valid": false, "error": "<reason>"}}.

User message: {text}"""

    resp = completion(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=100,
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    if not data.get("valid", True):
        return ParsedAccount(name="", type="", valid=False, error=data.get("error", ""))
    return ParsedAccount(name=data["name"], type=data["type"])


def _account_dict(a: Account) -> dict:
    # For credit cards: outstanding = amount owed (balance goes negative when spending)
    outstanding     = round(max(0.0, -a.balance), 2) if a.type == AccountType.credit_card else None
    available_credit = (
        round(a.credit_limit - outstanding, 2)
        if a.credit_limit is not None and outstanding is not None
        else None
    )
    return {
        "id": a.id, "name": a.name, "type": a.type,
        "balance": a.balance, "currency": a.currency,
        "color": a.color, "total_amount": a.total_amount,
        "monthly_emi": a.monthly_emi, "due_date": a.due_date,
        "notes": a.notes, "is_active": a.is_active,
        "created_at": str(a.created_at),
        # Credit card fields
        "credit_limit":             a.credit_limit,
        "shared_limit_account_id":  a.shared_limit_account_id,
        "outstanding":              outstanding,
        "available_credit":         available_credit,
    }


# ── Category services ──────────────────────────────────────────────────────

def get_categories(db: Session) -> list[dict]:
    return [{"id": c.id, "name": c.name, "icon": c.icon, "color": c.color}
            for c in db.query(Category).all()]


def create_category(db: Session, name: str, icon: str = "💰", color: str = "#6366f1") -> dict:
    cat = Category(name=name, icon=icon, color=color)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return {"id": cat.id, "name": cat.name, "icon": cat.icon, "color": cat.color}


# ── Transaction services ───────────────────────────────────────────────────

def list_transactions(
    db: Session,
    month: Optional[int] = None,
    year: Optional[int] = None,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    user_id: Optional[int] = None,
) -> dict:
    q = db.query(Transaction)
    if user_id is not None:
        q = q.filter(Transaction.user_id == user_id)
    if month and year:
        q = q.filter(
            extract("month", Transaction.date) == month,
            extract("year",  Transaction.date) == year,
        )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if category_id:
        q = q.filter(Transaction.category_id == category_id)
    q = q.order_by(Transaction.date.desc(), Transaction.created_at.desc())
    total = q.count()
    items = [_txn_dict(t) for t in q.offset(offset).limit(limit).all()]
    return {"total": total, "items": items}


def create_transaction(
    db: Session,
    amount: float,
    description: str,
    date: date,
    account_id: int,
    type: str = "expense",
    category_id: Optional[int] = None,
    to_account_id: Optional[int] = None,
    note: Optional[str] = None,
    source_plugin: Optional[str] = None,
    source_ref: Optional[str] = None,
    user_id: Optional[int] = None,
) -> dict:
    txn = Transaction(
        amount=amount, description=description, date=date,
        account_id=account_id, type=TransactionType(type),
        category_id=category_id, to_account_id=to_account_id,
        note=note, source_plugin=source_plugin, source_ref=source_ref,
        user_id=user_id,
    )
    db.add(txn)
    _apply_balance(db, txn, reverse=False)
    db.commit()
    db.refresh(txn)
    return _txn_dict(txn)


def update_transaction(
    db: Session,
    transaction_id: int,
    amount: float,
    description: str,
    date,
    account_id: int,
    type: str = "expense",
    category_id: Optional[int] = None,
    to_account_id: Optional[int] = None,
    note: Optional[str] = None,
) -> dict:
    txn = db.query(Transaction).get(transaction_id)
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found")
    # Reverse old balance effect, apply new one
    _apply_balance(db, txn, reverse=True)
    txn.amount = amount
    txn.description = description
    txn.date = date
    txn.account_id = account_id
    txn.type = TransactionType(type)
    txn.category_id = category_id
    txn.to_account_id = to_account_id
    txn.note = note
    _apply_balance(db, txn, reverse=False)
    db.commit()
    db.refresh(txn)
    return _txn_dict(txn)


def delete_transaction(db: Session, transaction_id: int) -> None:
    txn = db.query(Transaction).get(transaction_id)
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found")
    _apply_balance(db, txn, reverse=True)
    db.delete(txn)
    db.commit()


def _apply_balance(db: Session, txn: Transaction, reverse: bool):
    sign = -1 if reverse else 1
    acc = db.query(Account).get(txn.account_id)
    if acc:
        if txn.type == TransactionType.expense:
            acc.balance -= sign * txn.amount
        elif txn.type == TransactionType.income:
            acc.balance += sign * txn.amount
        elif txn.type == TransactionType.transfer and txn.to_account_id:
            acc.balance -= sign * txn.amount
            to_acc = db.query(Account).get(txn.to_account_id)
            if to_acc:
                to_acc.balance += sign * txn.amount


def _txn_dict(t: Transaction) -> dict:
    return {
        "id": t.id, "amount": t.amount, "type": t.type,
        "description": t.description, "note": t.note,
        "date": str(t.date), "created_at": str(t.created_at),
        "account_id": t.account_id,
        "account_name":  t.account.name  if t.account  else None,
        "account_color": t.account.color if t.account  else None,
        "category_id":   t.category_id,
        "category_name": t.category.name if t.category else None,
        "category_icon": t.category.icon if t.category else None,
        "category_color":t.category.color if t.category else None,
        "source_plugin": t.source_plugin,
    }


# ── Budget services ────────────────────────────────────────────────────────

def list_budgets(db: Session, month: int, year: int, user_id: Optional[int] = None) -> list[dict]:
    q = db.query(Budget).filter(Budget.month == month, Budget.year == year)
    if user_id is not None:
        q = q.filter(Budget.user_id == user_id)
    return [_budget_status(db, b, month, year, user_id=user_id) for b in q.all()]


def upsert_budget(
    db: Session, category_id: int, month: int, year: int, amount: float,
    user_id: Optional[int] = None,
) -> dict:
    existing = db.query(Budget).filter(
        Budget.user_id == user_id,
        Budget.category_id == category_id,
        Budget.month == month, Budget.year == year,
    ).first()
    if existing:
        existing.amount = amount
    else:
        db.add(Budget(user_id=user_id, category_id=category_id, month=month, year=year, amount=amount))
    db.commit()
    return {"ok": True}


def get_budget_status(
    db: Session, category_id: int, month: int, year: int, user_id: Optional[int] = None,
) -> Optional[BudgetStatus]:
    q = db.query(Budget).filter(
        Budget.user_id == user_id,
        Budget.category_id == category_id,
        Budget.month == month, Budget.year == year,
    )
    budget = q.first()
    if not budget:
        return None
    d = _budget_status(db, budget, month, year, user_id=user_id)
    return BudgetStatus(**{k: d[k] for k in BudgetStatus.__dataclass_fields__})


def _budget_status(db: Session, b: Budget, month: int, year: int, user_id: Optional[int] = None) -> dict:
    q = db.query(func.sum(Transaction.amount)).filter(
        Transaction.category_id == b.category_id,
        Transaction.type == TransactionType.expense,
        extract("month", Transaction.date) == month,
        extract("year",  Transaction.date) == year,
    )
    if user_id is not None:
        q = q.filter(Transaction.user_id == user_id)
    spent = q.scalar() or 0.0
    days_in_month  = calendar.monthrange(year, month)[1]
    today          = date.today()
    days_elapsed   = today.day if (today.month == month and today.year == year) else days_in_month
    daily_budget   = b.amount / days_in_month
    expected_spent = daily_budget * days_elapsed
    over_pace      = spent > expected_spent
    return {
        "id": b.id, "category_id": b.category_id,
        "category_name": b.category.name if b.category else None,
        "category_icon": b.category.icon if b.category else None,
        "month": b.month, "year": b.year,
        "budget_amount":  round(b.amount, 2),
        "amount":         round(b.amount, 2),  # alias kept for frontend compat
        "spent":          round(spent, 2),
        "remaining":      round(b.amount - spent, 2),
        "daily_budget":   round(daily_budget, 2),
        "expected_spent": round(expected_spent, 2),
        "over_pace":      over_pace,
        "over_pace_by":   round(max(spent - expected_spent, 0), 2),
        "pct":            round((spent / b.amount) * 100, 1) if b.amount else 0,
        "category_name":  b.category.name if b.category else None,
    }


# ── Lending services ───────────────────────────────────────────────────────

def list_lending(db: Session, settled: Optional[bool] = None, user_id: Optional[int] = None) -> list[dict]:
    q = db.query(Lending)
    if user_id is not None:
        q = q.filter(Lending.user_id == user_id)
    if settled is not None:
        q = q.filter(Lending.is_settled == settled)
    return [_lending_dict(l) for l in q.order_by(Lending.date.desc()).all()]


def create_lending(db: Session, user_id: Optional[int] = None, **kwargs) -> dict:
    l = Lending(user_id=user_id, **kwargs)
    db.add(l)
    db.commit()
    db.refresh(l)
    return _lending_dict(l)


def settle_lending(db: Session, lending_id: int, amount: float) -> dict:
    l = db.query(Lending).get(lending_id)
    if not l:
        raise ValueError(f"Lending {lending_id} not found")
    l.amount_settled = min(l.amount_settled + amount, l.amount)
    l.is_settled     = l.amount_settled >= l.amount
    db.commit()
    return _lending_dict(l)


def delete_lending(db: Session, lending_id: int) -> None:
    l = db.query(Lending).get(lending_id)
    if not l:
        raise ValueError(f"Lending {lending_id} not found")
    db.delete(l)
    db.commit()


def _lending_dict(l: Lending) -> dict:
    return {
        "id": l.id, "person_name": l.person_name, "type": l.type,
        "amount": l.amount, "amount_settled": l.amount_settled,
        "outstanding": round(l.amount - l.amount_settled, 2),
        "date": str(l.date),
        "due_date": str(l.due_date) if l.due_date else None,
        "note": l.note, "is_settled": l.is_settled,
    }


# ── Analytics services ─────────────────────────────────────────────────────

def monthly_summary(db: Session, month: int, year: int, user_id: Optional[int] = None) -> dict:
    base = db.query(Transaction).filter(
        extract("month", Transaction.date) == month,
        extract("year",  Transaction.date) == year,
    )
    if user_id is not None:
        base = base.filter(Transaction.user_id == user_id)

    total_expense = base.filter(Transaction.type == TransactionType.expense) \
        .with_entities(func.sum(Transaction.amount)).scalar() or 0
    total_income = base.filter(Transaction.type == TransactionType.income) \
        .with_entities(func.sum(Transaction.amount)).scalar() or 0

    by_cat_q = db.query(
        Category.name, Category.icon, Category.color,
        func.sum(Transaction.amount).label("total")
    ).join(Transaction, Transaction.category_id == Category.id) \
     .filter(
        Transaction.type == TransactionType.expense,
        extract("month", Transaction.date) == month,
        extract("year",  Transaction.date) == year,
     )
    if user_id is not None:
        by_cat_q = by_cat_q.filter(Transaction.user_id == user_id)
    by_cat = by_cat_q.group_by(Category.id).order_by(func.sum(Transaction.amount).desc()).all()

    daily_q = db.query(
        Transaction.date, func.sum(Transaction.amount).label("total")
    ).filter(
        Transaction.type == TransactionType.expense,
        extract("month", Transaction.date) == month,
        extract("year",  Transaction.date) == year,
    )
    if user_id is not None:
        daily_q = daily_q.filter(Transaction.user_id == user_id)
    daily = daily_q.group_by(Transaction.date).order_by(Transaction.date).all()

    from sqlalchemy import or_
    nw_q = db.query(func.sum(Account.balance)).filter(Account.is_active == True)
    if user_id is not None:
        nw_q = nw_q.filter(or_(Account.user_id == None, Account.user_id == user_id))
    net_worth = nw_q.scalar() or 0

    # ── Credit card breakdown ───────────────────────────────────────────────
    cc_q = db.query(Account).filter(
        Account.type == AccountType.credit_card,
        Account.is_active == True,
    )
    if user_id is not None:
        cc_q = cc_q.filter(or_(Account.user_id == None, Account.user_id == user_id))
    all_cc = cc_q.all()

    # Add-on cards (those sharing another card's limit) are merged into their primary
    addon_ids = {cc.id for cc in all_cc if cc.shared_limit_account_id}
    cc_summary = []
    for cc in all_cc:
        if cc.id in addon_ids:
            continue   # shown under primary

        addons        = [c for c in all_cc if c.shared_limit_account_id == cc.id]
        all_in_group  = [cc] + addons
        outstanding   = round(sum(max(0.0, -c.balance) for c in all_in_group), 2)
        limit         = cc.credit_limit
        available     = round(limit - outstanding, 2) if limit is not None else None
        util_pct      = round(outstanding / limit * 100, 1) if limit else None

        # This month's spend on all cards in the group
        month_spend_q = (
            db.query(func.sum(Transaction.amount))
            .filter(
                Transaction.account_id.in_([c.id for c in all_in_group]),
                Transaction.type == TransactionType.expense,
                extract("month", Transaction.date) == month,
                extract("year",  Transaction.date) == year,
            )
        )
        month_spend = round(month_spend_q.scalar() or 0, 2)

        cc_summary.append({
            "name":         cc.name,
            "cards":        [c.name for c in all_in_group],
            "outstanding":  outstanding,
            "month_spend":  month_spend,
            "credit_limit": limit,
            "available":    available,
            "util_pct":     util_pct,
        })

    return {
        "total_expense":  round(total_expense, 2),
        "total_income":   round(total_income, 2),
        "net":            round(total_income - total_expense, 2),
        "net_worth":      round(net_worth, 2),
        "by_category":    [{"name": r[0], "icon": r[1], "color": r[2], "total": round(r[3], 2)} for r in by_cat],
        "daily":          [{"date": str(r[0]), "total": round(r[1], 2)} for r in daily],
        "credit_cards":   cc_summary,
    }


def spending_trend(db: Session, months: int = 6, user_id: Optional[int] = None) -> list[dict]:
    today  = date.today()
    result = []
    for i in range(months - 1, -1, -1):
        m, y = today.month - i, today.year
        while m <= 0:
            m += 12; y -= 1
        base_filters = [
            extract("month", Transaction.date) == m,
            extract("year",  Transaction.date) == y,
        ]
        if user_id is not None:
            base_filters.append(Transaction.user_id == user_id)
        expense = db.query(func.sum(Transaction.amount)).filter(
            Transaction.type == TransactionType.expense, *base_filters,
        ).scalar() or 0
        income = db.query(func.sum(Transaction.amount)).filter(
            Transaction.type == TransactionType.income, *base_filters,
        ).scalar() or 0
        result.append({
            "month":   f"{calendar.month_abbr[m]} {y}",
            "expense": round(expense, 2),
            "income":  round(income, 2),
        })
    return result


# ── CSV Export service ─────────────────────────────────────────────────────

def export_csv(db: Session, user_id: Optional[int] = None) -> str:
    """
    Returns a UTF-8 CSV string with four sheets encoded as sections,
    each prefixed with a '## SECTION' header row so it can be split
    into separate tabs by the consumer (or imported as-is into a spreadsheet
    that accepts multi-table CSVs).

    Sections: transactions, accounts, budgets, lending
    """
    out = io.StringIO()

    # ── Transactions ────────────────────────────────────────────────────
    out.write("## TRANSACTIONS\n")
    w = csv.writer(out)
    w.writerow([
        "id", "date", "type", "amount", "description", "note",
        "account", "account_type", "category", "source", "created_at",
    ])
    txn_q = db.query(Transaction).order_by(Transaction.date.desc())
    if user_id is not None:
        txn_q = txn_q.filter(Transaction.user_id == user_id)
    for t in txn_q.all():
        w.writerow([
            t.id, t.date, t.type.value, t.amount, t.description, t.note or "",
            t.account.name if t.account else "",
            t.account.type.value if t.account else "",
            t.category.name if t.category else "",
            t.source_plugin or "web",
            t.created_at,
        ])

    out.write("\n## ACCOUNTS\n")
    w.writerow([
        "id", "name", "type", "balance", "currency",
        "total_amount", "monthly_emi", "due_date", "notes", "is_active",
    ])
    acc_q = db.query(Account)
    if user_id is not None:
        from sqlalchemy import or_
        acc_q = acc_q.filter(or_(Account.user_id == None, Account.user_id == user_id))
    for a in acc_q.all():
        w.writerow([
            a.id, a.name, a.type.value, a.balance, a.currency,
            a.total_amount or "", a.monthly_emi or "", a.due_date or "",
            a.notes or "", a.is_active,
        ])

    out.write("\n## BUDGETS\n")
    w.writerow(["id", "category", "month", "year", "amount"])
    bgt_q = db.query(Budget).order_by(Budget.year.desc(), Budget.month.desc())
    if user_id is not None:
        bgt_q = bgt_q.filter(Budget.user_id == user_id)
    for b in bgt_q.all():
        w.writerow([
            b.id, b.category.name if b.category else "", b.month, b.year, b.amount,
        ])

    out.write("\n## LENDING\n")
    w.writerow([
        "id", "person", "type", "amount", "amount_settled",
        "outstanding", "date", "due_date", "is_settled", "note",
    ])
    lend_q = db.query(Lending).order_by(Lending.date.desc())
    if user_id is not None:
        lend_q = lend_q.filter(Lending.user_id == user_id)
    for l in lend_q.all():
        w.writerow([
            l.id, l.person_name, l.type.value, l.amount, l.amount_settled,
            round(l.amount - l.amount_settled, 2),
            l.date, l.due_date or "", l.is_settled, l.note or "",
        ])

    return out.getvalue()


# ── Audio transcription ───────────────────────────────────────────────────

_whisper_model = None

def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file using faster-whisper (base model, lazy-loaded)."""
    global _whisper_model
    from faster_whisper import WhisperModel
    if _whisper_model is None:
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = _whisper_model.transcribe(file_path)
    return " ".join(s.text for s in segments).strip()


def ocr_image(file_path: str) -> str:
    """Extract text from a receipt/invoice image using local Tesseract OCR."""
    import pytesseract
    from PIL import Image
    img = Image.open(file_path)
    # PSM 6 = assume a single uniform block of text (good for receipts)
    return pytesseract.image_to_string(img, config="--psm 6").strip()


# ── AI parsing (shared across all plugins) ────────────────────────────────

def parse_message_with_ai(
    text: str,
    accounts: list[dict],
    categories: list[dict],
    today: Optional[date] = None,
) -> ParsedTransaction:
    """
    Calls LiteLLM → Anthropic to parse a free-text expense description.
    Returns a ParsedTransaction; caller decides what to do with missing fields.
    This function has zero knowledge of Telegram, HTTP, etc.
    """
    import json
    from litellm import completion

    today_str      = (today or date.today()).isoformat()
    accounts_str   = ", ".join(f"{a['id']}:{a['name']}({a['type']})" for a in accounts)
    categories_str = ", ".join(f"{c['id']}:{c['name']}" for c in categories)

    prompt = f"""You are an expense parser. Extract transaction details from the user message.
Today: {today_str}
Accounts  (id:name:type): {accounts_str}
Categories (id:name):     {categories_str}

Return ONLY a JSON object — no markdown fences:
{{
  "amount":      <number or null>,
  "description": <string>,
  "account_id":  <int or null>,
  "category_id": <int or null>,
  "date":        "<YYYY-MM-DD>",
  "type":        "expense" | "income" | "transfer",
  "note":        <string or null>,
  "missing":     ["amount"|"account_id"|"category_id"],
  "reply":       "<friendly 1-2 line confirmation>"
}}

Rules:
- Set a field to null and add it to missing[] if you cannot confidently infer it.
- Prefer the most specific matching account/category.
- Default date to today if not mentioned.

User message: {text}"""

    resp = completion(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())

    return ParsedTransaction(
        amount      = data.get("amount"),
        description = data.get("description", ""),
        account_id  = data.get("account_id"),
        category_id = data.get("category_id"),
        date        = date.fromisoformat(data["date"]) if data.get("date") else (today or date.today()),
        type        = data.get("type", "expense"),
        note        = data.get("note"),
        missing     = data.get("missing", []),
        reply       = data.get("reply", ""),
    )


def parse_lending_with_ai(text: str, today: Optional[date] = None) -> ParsedLending:
    """
    Detect and parse lending-related messages.
    Returns a ParsedLending with intent=unknown if the message is not about lending.
    """
    import json
    from litellm import completion

    today_str = (today or date.today()).isoformat()

    prompt = f"""You are a lending/loan tracker. Classify the user's message and extract details.

Intents:
- "log"         — user is recording that they lent or borrowed money
- "list_owed"   — user wants to see who owes them money
- "list_i_owe"  — user wants to see what they owe others
- "list_all"    — user wants to see all lending records
- "unknown"     — not related to lending/loans

Return ONLY a JSON object:
{{
  "intent":       "log" | "list_owed" | "list_i_owe" | "list_all" | "unknown",
  "lending_type": "lent" | "borrowed" | null,
  "person":       <string or null>,
  "amount":       <number or null>,
  "date":         "<YYYY-MM-DD>" or null,
  "note":         <string or null>,
  "missing":      ["person"|"amount"|"lending_type"],
  "reply":        "<friendly 1-line confirmation, or null if not a log>"
}}

Rules:
- "lent/loaned/gave X to Y" → intent=log, lending_type=lent
- "borrowed/took X from Y" → intent=log, lending_type=borrowed
- "who owes me / what's due to me" → intent=list_owed
- "what do I owe / my debts / whom do I owe" → intent=list_i_owe
- "show all lending / loans" → intent=list_all
- Default date to today if not mentioned.

Today: {today_str}
User message: {text}"""

    resp = completion(
        model="anthropic/claude-haiku-4-5",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())

    return ParsedLending(
        intent       = data.get("intent", "unknown"),
        lending_type = data.get("lending_type"),
        person       = data.get("person"),
        amount       = data.get("amount"),
        date         = date.fromisoformat(data["date"]) if data.get("date") else (today or date.today()),
        note         = data.get("note"),
        missing      = data.get("missing", []),
        reply        = data.get("reply") or "",
    )


# ── Analytics report helpers ───────────────────────────────────────────────

_MONTH_MAP: dict[str, int] = {
    name.lower(): i
    for i, name in enumerate(calendar.month_name)
    if i
} | {
    name.lower(): i
    for i, name in enumerate(calendar.month_abbr)
    if i
}

# Phrases that signal a report request rather than an expense entry
_REPORT_TRIGGERS = re.compile(
    r"""
    ^\s*/report | ^\s*/stats | ^\s*/summary | ^\s*/analytics  # explicit commands
    | \bhow\s+much\b                                           # "how much did I spend"
    | \bshow\s+(me\s+)?(my\s+)?(report|summary|expense|spending|analytics)\b
    | \b(expense|spending)\s+(report|summary)\b
    | \bmonthly\s+(report|summary|stats)\b
    | \bwhat\s+.{0,20}(spent|spend|expense|income)\b
    | \b(give|send)\s+me\s+(a\s+)?(report|summary|analytics)\b
    """,
    re.VERBOSE | re.IGNORECASE,
)


@dataclass
class ReportPeriod:
    """Describes the time window for an analytics report."""
    period_type: str      # "month" | "trend"
    month:       Optional[int]  = None
    year:        Optional[int]  = None
    months:      int            = 6   # for trend


def parse_report_request(text: str, today: Optional[date] = None) -> Optional[ReportPeriod]:
    """
    Detect whether `text` is asking for an analytics report and extract the period.
    Returns None if the message is not a report request.

    Recognised patterns (all case-insensitive):
      /report, /stats, /summary, /analytics
      /report last, /report last month, /report jan, /report january 2025
      /report trend, /report trend 3
      Natural language: "how much did I spend last month?", "show me January report"
    """
    if not _REPORT_TRIGGERS.search(text):
        return None

    today = today or date.today()
    t     = text.lower().strip()

    # ── Trend ────────────────────────────────────────────────────────────
    if "trend" in t:
        m = re.search(r"(\d+)\s*months?", t)
        months = min(int(m.group(1)), 24) if m else 6
        return ReportPeriod(period_type="trend", months=months)

    m = re.search(r"last\s+(\d+)\s+months?", t)
    if m:
        return ReportPeriod(period_type="trend", months=min(int(m.group(1)), 24))

    # ── "last month" / "previous month" ──────────────────────────────────
    if re.search(r"\blast\s+month\b|\bprevious\s+month\b", t):
        first = today.replace(day=1)
        prev  = first - timedelta(days=1)
        return ReportPeriod(period_type="month", month=prev.month, year=prev.year)

    # ── Named month (+ optional year) ────────────────────────────────────
    for name, num in _MONTH_MAP.items():
        if re.search(rf"\b{name}\b", t):
            yr_m = re.search(r"\b(20\d{2})\b", t)
            year = int(yr_m.group(1)) if yr_m else today.year
            return ReportPeriod(period_type="month", month=num, year=year)

    # ── YYYY-MM pattern ───────────────────────────────────────────────────
    m = re.search(r"\b(20\d{2})[/-](\d{1,2})\b", t)
    if m:
        return ReportPeriod(period_type="month", month=int(m.group(2)), year=int(m.group(1)))

    # ── Default: this month ───────────────────────────────────────────────
    return ReportPeriod(period_type="month", month=today.month, year=today.year)


def format_monthly_report(summary: dict, month: int, year: int) -> str:
    """Format a monthly_summary dict into a readable bot message."""
    month_label = f"{calendar.month_name[month]} {year}"
    net         = summary["net"]
    net_sign    = "+" if net >= 0 else ""

    lines = [
        f"📊 *{month_label} Report*\n",
        f"💰 Income:    ₹{summary['total_income']:,.0f}",
        f"💸 Expenses:  ₹{summary['total_expense']:,.0f}",
        f"📈 Net:       {net_sign}₹{net:,.0f}",
        f"🏦 Net worth: ₹{summary['net_worth']:,.0f}",
    ]

    if summary.get("by_category"):
        lines.append("\n*Top spending:*")
        total_exp = summary["total_expense"] or 1
        for cat in summary["by_category"][:5]:
            pct  = round(cat["total"] / total_exp * 100)
            icon = cat.get("icon", "•")
            lines.append(f"  {icon} {cat['name']:<18} ₹{cat['total']:>8,.0f}  ({pct}%)")

    if summary.get("daily"):
        days  = summary["daily"]
        avg   = sum(d["total"] for d in days) / len(days) if days else 0
        peak  = max(days, key=lambda d: d["total"])
        lines.append(f"\n📅 Daily avg:  ₹{avg:,.0f}")
        lines.append(f"📌 Peak day:   {peak['date']}  ₹{peak['total']:,.0f}")

    if summary.get("credit_cards"):
        lines.append("\n💳 *Credit cards:*")
        for cc in summary["credit_cards"]:
            card_label = " + ".join(cc["cards"]) if len(cc["cards"]) > 1 else cc["name"]
            lines.append(f"  *{card_label}*")
            lines.append(f"    Spent this month: ₹{cc['month_spend']:,.0f}")
            lines.append(f"    Outstanding:      ₹{cc['outstanding']:,.0f}")
            if cc.get("credit_limit"):
                util = f"  ({cc['util_pct']}% used)" if cc.get("util_pct") is not None else ""
                lines.append(f"    Limit:            ₹{cc['credit_limit']:,.0f}{util}")
                lines.append(f"    Available:        ₹{cc['available']:,.0f}")

    return "\n".join(lines)


def format_trend_report(trend: list[dict], months: int) -> str:
    """Format a spending_trend list into a readable bot message."""
    if not trend:
        return "No data found for that period."

    max_exp = max(t["expense"] for t in trend) or 1
    bar_w   = 10

    lines = [f"📈 *Spending Trend — last {months} months*\n"]
    for t in trend:
        filled = round(t["expense"] / max_exp * bar_w)
        bar    = "█" * filled + "░" * (bar_w - filled)
        lines.append(
            f"`{t['month']:<8}` {bar}  ₹{t['expense']:>9,.0f}"
        )

    total = sum(t["expense"] for t in trend)
    avg   = total / len(trend)
    lines.append(f"\nAvg/month: ₹{avg:,.0f}   Total: ₹{total:,.0f}")
    return "\n".join(lines)


def generate_report(
    db: Session,
    period: ReportPeriod,
    user_id: Optional[int] = None,
) -> str:
    """Fetch data and return a formatted report string for the given period."""
    today = date.today()

    if period.period_type == "trend":
        trend = spending_trend(db, months=period.months, user_id=user_id)
        return format_trend_report(trend, period.months)

    month = period.month or today.month
    year  = period.year  or today.year
    summary = monthly_summary(db, month, year, user_id=user_id)
    return format_monthly_report(summary, month, year)
