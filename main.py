"""
main.py — FastAPI application.

Responsibilities:
  - HTTP routing only (request parsing → service call → response serialisation)
  - Mount plugins (Telegram, future WhatsApp / email routers)
  - Serve the static dashboard
  - NO business logic here — that all lives in services.py
"""

import os
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

import auth_service
import oauth_service
import services
from models import AccountType, TransactionType, LendingType, engine, init_db
from plugins import routers as plugin_routers, SUPPORTED_PLUGINS as supported_plugins
from routers import routers as api_routers

# ── App setup ──────────────────────────────────────────────────────────────

init_db()
SessionLocal = sessionmaker(bind=engine)

app = FastAPI(
    title="PocketLog API",
    description=(
        "Personal finance API — track expenses, budgets, lending and more.\n\n"
        "## Authentication\n\n"
        "All `/api/v1/*` endpoints require a **Bearer token**.\n\n"
        "**Personal Access Token (quickest)**\n"
        "Send `/apikey` to the Telegram bot. Use the returned token as `Authorization: Bearer <token>`.\n\n"
        "**OAuth 2.0 Client Credentials (for integrations)**\n"
        "1. Register a client: `POST /api/oauth/clients` (needs your PAT)\n"
        "2. Exchange credentials: `POST /api/oauth/token`\n"
        "3. Use the returned `access_token` as your Bearer token (valid 1 hour)\n"
    ),
    version="1.0.0",
    redoc_url=None,
    docs_url=None,
    openapi_url="/api/openapi.json",
    openapi_tags=[
        {"name": "auth",         "description": "OAuth 2.0 client management & token issuance"},
        {"name": "v1",           "description": "Full API — all actions, user-scoped"},
    ],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/api/docs", include_in_schema=False)
def api_docs():
    html = """<!DOCTYPE html>
<html>
<head>
<title>PocketLog API</title>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0;padding:0}</style>
</head>
<body>
<redoc spec-url="/api/openapi.json" expand-responses="200,201"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js"></script>
</body>
</html>"""
    return HTMLResponse(html)

# Plugin routers (Telegram webhook, etc.)
for router in plugin_routers:
    app.include_router(router, prefix="/plugins")

# Public API + OAuth routers
for router in api_routers:
    app.include_router(router)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_web_user(request: Request, db: Session = Depends(get_db)):
    """Resolve the session cookie to a User, or return None."""
    token = request.cookies.get("pocketlog_session")
    if not token:
        return None
    return oauth_service.get_user_from_token(token, db)


# ── Pages ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return FileResponse("static/landing.html")


@app.get("/login", include_in_schema=False)
def login_page(request: Request, db: Session = Depends(get_db)):
    # Already logged in → skip to dashboard
    token = request.cookies.get("pocketlog_session")
    if token and oauth_service.get_user_from_token(token, db):
        return RedirectResponse("/app", status_code=302)
    return FileResponse("static/login.html")


@app.get("/app", include_in_schema=False)
def dashboard(web_user=Depends(get_web_user)):
    if not web_user:
        return RedirectResponse("/login", status_code=302)
    return FileResponse("static/index.html")


@app.get("/api/config", include_in_schema=False)
def public_config():
    import os
    return {
        "telegram_bot": os.getenv("TELEGRAM_BOT_USERNAME", ""),
        "supported_plugins": list(supported_plugins),
    }


class WebOTPRequest(BaseModel):
    email:    str
    platform: Optional[str] = None   # specific channel to send OTP to

class WebOTPVerify(BaseModel):
    email: str
    otp:   str


@app.get("/api/auth/web/channels", include_in_schema=False)
def web_user_channels(email: str, db: Session = Depends(get_db)):
    """Return the linked bot platforms for a given email (for channel picker on login page)."""
    user = auth_service.get_user_by_email(email, db)
    if not user:
        return {"platforms": [], "primary": None}
    identities = auth_service.get_user_bot_identities(user.id, db)
    return {"platforms": [i.platform for i in identities], "primary": user.primary_bot}


@app.post("/api/auth/web/request-otp", include_in_schema=False)
async def web_request_otp(data: WebOTPRequest, db: Session = Depends(get_db)):
    user = auth_service.get_user_by_email(data.email, db)
    if not user:
        return {"ok": True, "sent_to": None}

    otp        = auth_service.generate_otp(user.id, db)
    identities = auth_service.get_user_bot_identities(user.id, db)

    # Use explicitly requested platform, else fall back to primary, else first linked
    if data.platform:
        target = next((i for i in identities if i.platform == data.platform), None)
    else:
        target = next((i for i in identities if i.platform == user.primary_bot), None) \
                 or (identities[0] if identities else None)

    if target:
        from plugins.registry import get as get_plugin
        plugin = get_plugin(target.platform)
        if plugin:
            try:
                await plugin.send_message(
                    target.chat_id,
                    f"Your PocketLog web login code:\n\n*{otp}*\n\nExpires in 10 minutes.",
                )
            except Exception as exc:
                import logging as _log
                _log.getLogger(__name__).error("OTP delivery failed: %s", exc)
                raise HTTPException(503, f"Could not deliver OTP: {exc}")
        else:
            raise HTTPException(503, "Bot plugin not available")
    else:
        # No bot identity linked — still return ok so email enumeration isn't possible,
        # but tell the frontend nothing was sent
        return {"ok": True, "sent_to": None}

    return {"ok": True, "sent_to": target.platform}


@app.post("/api/auth/web/verify-otp", include_in_schema=False)
def web_verify_otp(data: WebOTPVerify, response: Response, db: Session = Depends(get_db)):
    user = auth_service.get_user_by_email(data.email, db)
    if not user:
        raise HTTPException(401, "Invalid credentials")

    session = auth_service.verify_otp(user.id, data.otp, db)
    if not session:
        raise HTTPException(401, "Invalid or expired code")

    response.set_cookie(
        key      = "pocketlog_session",
        value    = session.token,
        max_age  = 30 * 24 * 3600,
        httponly = True,
        samesite = "lax",
        path     = "/",
    )
    return {"ok": True, "name": user.name}


@app.get("/api/lookup-user", include_in_schema=False)
def lookup_user(email: str, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    from models import User as _User
    user = db.query(_User).filter_by(email=email.strip().lower()).first()
    if not user:
        return {"found": False}
    return {"found": True, "name": user.name}


@app.get("/api/me", include_in_schema=False)
def get_me(web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    return {"id": web_user.id, "name": web_user.name, "email": web_user.email, "primary_bot": web_user.primary_bot}


class ProfileUpdate(BaseModel):
    name: str
    primary_bot: str


@app.patch("/api/me", include_in_schema=False)
def update_me(data: ProfileUpdate, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    if len(data.name.strip()) < 2:
        raise HTTPException(400, "Name must be at least 2 characters")
    if data.primary_bot not in supported_plugins:
        raise HTTPException(400, f"Unsupported bot: {data.primary_bot}")
    web_user.name = data.name.strip()
    web_user.primary_bot = data.primary_bot
    db.commit()
    return {"ok": True, "name": web_user.name, "primary_bot": web_user.primary_bot}


@app.post("/api/auth/logout", include_in_schema=False)
def logout(response: Response):
    response.delete_cookie("pocketlog_session", path="/")
    return {"ok": True}


@app.delete("/api/auth/account", include_in_schema=False)
def delete_account_permanently(
    response: Response,
    db: Session = Depends(get_db),
    web_user=Depends(get_web_user),
):
    """Permanently delete the authenticated user and all their data."""
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    uid = web_user.id
    from models import (
        BotConversationState, OTPSession, UserSession, BotIdentity,
        OAuthAccessToken, OAuthClient, Transaction, Budget, Lending, Account,
    )
    from sqlalchemy import or_
    # Collect all account IDs owned by this user first
    account_ids = [a.id for a in db.query(Account.id).filter(Account.user_id == uid).all()]
    # Delete all transactions touching those accounts (regardless of transaction.user_id)
    if account_ids:
        db.query(Transaction).filter(
            or_(
                Transaction.account_id.in_(account_ids),
                Transaction.to_account_id.in_(account_ids),
            )
        ).delete(synchronize_session=False)
    # Delete remaining user-scoped records
    db.query(BotConversationState).filter(BotConversationState.user_id == uid).delete()
    db.query(OTPSession).filter(OTPSession.user_id == uid).delete()
    db.query(UserSession).filter(UserSession.user_id == uid).delete()
    db.query(BotIdentity).filter(BotIdentity.user_id == uid).delete()
    db.query(Budget).filter(Budget.user_id == uid).delete()
    db.query(Lending).filter(Lending.user_id == uid).delete()
    # OAuth tokens before clients
    client_ids = [c.id for c in db.query(OAuthClient).filter(OAuthClient.user_id == uid).all()]
    if client_ids:
        db.query(OAuthAccessToken).filter(OAuthAccessToken.client_id.in_(client_ids)).delete(synchronize_session=False)
    db.query(OAuthClient).filter(OAuthClient.user_id == uid).delete()
    # Hard delete all user accounts (not soft-delete)
    db.query(Account).filter(Account.user_id == uid).delete()
    db.query(web_user.__class__).filter(web_user.__class__.id == uid).delete()
    db.commit()
    response.delete_cookie("pocketlog_session", path="/")
    return {"ok": True}


# ── Pydantic schemas ───────────────────────────────────────────────────────

class AccountIn(BaseModel):
    name:                    str
    type:                    AccountType
    balance:                 float = 0.0
    currency:                str   = "INR"
    color:                   str   = "#6366f1"
    total_amount:            Optional[float] = None
    monthly_emi:             Optional[float] = None
    due_date:                Optional[int]   = None
    notes:                   Optional[str]   = None
    credit_limit:            Optional[float] = None
    shared_limit_account_id: Optional[int]   = None


class CategoryIn(BaseModel):
    name:  str
    icon:  str = "💰"
    color: str = "#6366f1"


class TransactionIn(BaseModel):
    amount:        float
    type:          TransactionType = TransactionType.expense
    description:   str
    note:          Optional[str]  = None
    date:          date
    account_id:    int
    category_id:   Optional[int] = None
    to_account_id: Optional[int] = None


class BudgetIn(BaseModel):
    category_id: int
    month:       int
    year:        int
    amount:      float


class LendingIn(BaseModel):
    person_name:    str
    type:           LendingType
    amount:         float
    amount_settled: float = 0.0
    date:           date
    due_date:       Optional[date] = None
    note:           Optional[str]  = None
    is_settled:     bool           = False


# ── Accounts ───────────────────────────────────────────────────────────────

@app.get("/api/accounts", include_in_schema=False)
def list_accounts(db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    return services.get_accounts(db, user_id=web_user.id if web_user else None)


@app.post("/api/accounts", status_code=201, include_in_schema=False)
def create_account(data: AccountIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    return services.create_account(db, **data.model_dump(), user_id=web_user.id if web_user else None)


@app.put("/api/accounts/{aid}", include_in_schema=False)
def update_account(aid: int, data: AccountIn, db: Session = Depends(get_db)):
    try:
        return services.update_account(db, aid, **data.model_dump())
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/accounts/{aid}", include_in_schema=False)
def delete_account(aid: int, db: Session = Depends(get_db)):
    try:
        services.delete_account(db, aid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ── Categories ─────────────────────────────────────────────────────────────

@app.get("/api/categories", include_in_schema=False)
def list_categories(db: Session = Depends(get_db)):
    return services.get_categories(db)


@app.post("/api/categories", status_code=201, include_in_schema=False)
def create_category(data: CategoryIn, db: Session = Depends(get_db)):
    return services.create_category(db, **data.model_dump())


# ── Transactions ───────────────────────────────────────────────────────────

@app.get("/api/transactions", include_in_schema=False)
def list_transactions(
    db:          Session       = Depends(get_db),
    month:       Optional[int] = None,
    year:        Optional[int] = None,
    account_id:  Optional[int] = None,
    category_id: Optional[int] = None,
    limit:       int           = 100,
    offset:      int           = 0,
    web_user=Depends(get_web_user),
):
    uid = web_user.id if web_user else None
    return services.list_transactions(db, month, year, account_id, category_id, limit, offset, user_id=uid)


@app.post("/api/transactions", status_code=201, include_in_schema=False)
def create_transaction(data: TransactionIn, db: Session = Depends(get_db)):
    return services.create_transaction(
        db,
        amount        = data.amount,
        description   = data.description,
        date          = data.date,
        account_id    = data.account_id,
        type          = data.type.value,
        category_id   = data.category_id,
        to_account_id = data.to_account_id,
        note          = data.note,
        source_plugin = "web",
    )


@app.put("/api/transactions/{tid}", include_in_schema=False)
def update_transaction(tid: int, data: TransactionIn, db: Session = Depends(get_db)):
    try:
        return services.update_transaction(
            db, tid,
            amount        = data.amount,
            description   = data.description,
            date          = data.date,
            account_id    = data.account_id,
            type          = data.type.value,
            category_id   = data.category_id,
            to_account_id = data.to_account_id,
            note          = data.note,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/transactions/{tid}", include_in_schema=False)
def delete_transaction(tid: int, db: Session = Depends(get_db)):
    try:
        services.delete_transaction(db, tid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ── Budgets ────────────────────────────────────────────────────────────────

@app.get("/api/budgets", include_in_schema=False)
def list_budgets(month: int, year: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    uid = web_user.id if web_user else None
    return services.list_budgets(db, month, year, user_id=uid)


@app.post("/api/budgets", status_code=201, include_in_schema=False)
def upsert_budget(data: BudgetIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    uid = web_user.id if web_user else None
    return services.upsert_budget(db, data.category_id, data.month, data.year, data.amount, user_id=uid)


# ── Lending ────────────────────────────────────────────────────────────────

@app.get("/api/lending", include_in_schema=False)
def list_lending(db: Session = Depends(get_db), settled: Optional[bool] = None, web_user=Depends(get_web_user)):
    return services.list_lending(db, settled, user_id=web_user.id if web_user else None)


@app.post("/api/lending", status_code=201, include_in_schema=False)
def create_lending(data: LendingIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    uid = web_user.id if web_user else None
    return services.create_lending(db, user_id=uid, **data.model_dump())


@app.put("/api/lending/{lid}", include_in_schema=False)
def update_lending(lid: int, data: LendingIn, db: Session = Depends(get_db)):
    try:
        return services.update_lending(db, lid, **data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(404, str(e))

@app.put("/api/lending/{lid}/settle", include_in_schema=False)
def settle_lending(lid: int, amount: float = Query(...), db: Session = Depends(get_db)):
    try:
        return services.settle_lending(db, lid, amount)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/lending/{lid}", include_in_schema=False)
def delete_lending(lid: int, db: Session = Depends(get_db)):
    try:
        services.delete_lending(db, lid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


# ── Groups / Splitwise ─────────────────────────────────────────────────────

class GroupSettleIn(BaseModel):
    from_member_id: int
    to_member_id:   int
    amount:         float

class GroupCloseIn(BaseModel):
    use_simplified: bool = True

class GroupCreateIn(BaseModel):
    name: str

class GroupMemberAddIn(BaseModel):
    display_name: str
    email: Optional[str] = None

class GroupExpenseAddIn(BaseModel):
    paid_by_member_id: int
    amount: float
    description: str
    date: str
    shares: list[dict]   # [{"member_id": int, "ratio": float}]


@app.get("/api/groups", include_in_schema=False)
def list_groups(db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    return services.get_user_groups(web_user.id, db)


@app.post("/api/groups", include_in_schema=False)
def create_group(data: GroupCreateIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    if not data.name.strip():
        raise HTTPException(400, "Name required")
    return services.create_web_group(data.name.strip(), web_user.id, db)


@app.get("/api/groups/{gid}/members", include_in_schema=False)
def group_members(gid: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    # Verify user is a member of this group
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    return members


@app.get("/api/groups/{gid}/expenses", include_in_schema=False)
def group_expenses(gid: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    return services.list_group_expenses(gid, db)


@app.get("/api/groups/{gid}/balances", include_in_schema=False)
def group_balances(
    gid: int,
    simplified: bool = False,
    db: Session = Depends(get_db),
    web_user=Depends(get_web_user),
):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    if simplified:
        return services.get_simplified_balances(gid, db)
    return services.get_group_balances(gid, db)


@app.post("/api/groups/{gid}/close", include_in_schema=False)
def close_group(gid: int, data: GroupCloseIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    return services.close_group(gid, data.use_simplified, db)


@app.post("/api/groups/{gid}/members", include_in_schema=False)
def add_group_member(gid: int, data: GroupMemberAddIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    telegram_bot = os.getenv("TELEGRAM_BOT_USERNAME", "")
    return services.add_group_member_web(
        gid, data.display_name.strip(), data.email,
        inviter_name=web_user.name, telegram_bot=telegram_bot, db=db,
    )


@app.delete("/api/groups/{gid}/members/{mid}", include_in_schema=False)
def remove_group_member(gid: int, mid: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    result = services.delete_group_member(mid, gid, db)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return {"ok": True}


@app.post("/api/groups/{gid}/expenses", include_in_schema=False)
def add_group_expense(gid: int, data: GroupExpenseAddIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    from datetime import date as _date
    expense_date = _date.fromisoformat(data.date) if data.date else _date.today()
    return services.create_group_expense(
        group_chat_id=gid,
        paid_by_member_id=data.paid_by_member_id,
        amount=data.amount,
        description=data.description,
        expense_date=expense_date,
        shares=data.shares,
        db=db,
    )


@app.delete("/api/groups/{gid}", include_in_schema=False)
def delete_group(gid: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    ok = services.delete_group(gid, db)
    if not ok:
        raise HTTPException(404, "Group not found")
    return {"ok": True}


@app.delete("/api/groups/{gid}/expenses/{eid}", include_in_schema=False)
def delete_group_expense(gid: int, eid: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    ok = services.delete_group_expense(eid, gid, db)
    if not ok:
        raise HTTPException(404, "Expense not found")
    return {"ok": True}


@app.post("/api/groups/{gid}/settle", include_in_schema=False)
def settle_group(gid: int, data: GroupSettleIn, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    if not web_user:
        raise HTTPException(401, "Not authenticated")
    members = services.get_group_members(gid, db)
    if not any(m["user_id"] == web_user.id for m in members):
        raise HTTPException(403, "Not a member of this group")
    return services.settle_group(gid, data.from_member_id, data.to_member_id, data.amount, db)


# ── Analytics ──────────────────────────────────────────────────────────────

@app.get("/api/analytics/summary", include_in_schema=False)
def monthly_summary(month: int, year: int, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    return services.monthly_summary(db, month, year, user_id=web_user.id if web_user else None)


@app.get("/api/analytics/trend", include_in_schema=False)
def spending_trend(months: int = 6, db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    return services.spending_trend(db, months, user_id=web_user.id if web_user else None)


# ── CSV Export ─────────────────────────────────────────────────────────────

@app.get("/api/export/csv", include_in_schema=False)
def export_csv(db: Session = Depends(get_db), web_user=Depends(get_web_user)):
    csv_content = services.export_csv(db, user_id=web_user.id if web_user else None)
    filename = f"paisa_export_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
