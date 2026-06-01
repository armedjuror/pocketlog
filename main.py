"""
main.py — FastAPI application.

Responsibilities:
  - HTTP routing only (request parsing → service call → response serialisation)
  - Mount plugins (Telegram, future WhatsApp / email routers)
  - Serve the static dashboard
  - NO business logic here — that all lives in services.py
"""

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
    title="Hisaab API",
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
<title>Hisaab API</title>
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
    token = request.cookies.get("hisaab_session")
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
    token = request.cookies.get("hisaab_session")
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
    email: str

class WebOTPVerify(BaseModel):
    email: str
    otp:   str


@app.post("/api/auth/web/request-otp", include_in_schema=False)
async def web_request_otp(data: WebOTPRequest, db: Session = Depends(get_db)):
    user = auth_service.get_user_by_email(data.email, db)
    if not user:
        return {"ok": True, "sent_to": None}

    otp        = auth_service.generate_otp(user.id, db)
    identities = auth_service.get_user_bot_identities(user.id, db)
    primary    = next((i for i in identities if i.platform == user.primary_bot), None) \
                 or (identities[0] if identities else None)

    if primary:
        from plugins.registry import get as get_plugin
        plugin = get_plugin(primary.platform)
        if plugin:
            await plugin.send_message(
                primary.chat_id,
                f"Your Hisaab web login code:\n\n*{otp}*\n\nExpires in 10 minutes.",
            )

    return {"ok": True, "sent_to": primary.platform if primary else None}


@app.post("/api/auth/web/verify-otp", include_in_schema=False)
def web_verify_otp(data: WebOTPVerify, response: Response, db: Session = Depends(get_db)):
    user = auth_service.get_user_by_email(data.email, db)
    if not user:
        raise HTTPException(401, "Invalid credentials")

    session = auth_service.verify_otp(user.id, data.otp, db)
    if not session:
        raise HTTPException(401, "Invalid or expired code")

    response.set_cookie(
        key      = "hisaab_session",
        value    = session.token,
        max_age  = 30 * 24 * 3600,
        httponly = True,
        samesite = "lax",
        path     = "/",
    )
    return {"ok": True, "name": user.name}


@app.post("/api/auth/logout", include_in_schema=False)
def logout(response: Response):
    response.delete_cookie("hisaab_session", path="/")
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
def create_account(data: AccountIn, db: Session = Depends(get_db)):
    return services.create_account(db, **data.model_dump())


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
